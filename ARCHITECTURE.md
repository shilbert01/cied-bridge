# CIED Bridge — Architecture & Pipeline

## Overview

A cardiac clinic has up to 8 pacemaker interrogation devices from 4 vendors (Medtronic, Boston Scientific, Abbott, Biotronik). Each device can print interrogation reports via USB to a connected printer. This system intercepts those print jobs, extracts structured clinical data, and presents it through a tablet UI — without modifying the interrogation devices or their workflow.

---

## Physical Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Clinic Network                              │
│                                                                     │
│  ┌──────────────┐     USB      ┌──────────────┐                    │
│  │  Medtronic   │◄────────────►│  Pi Zero 2W  │ bay-01             │
│  │  Programmer  │  (gadget)    │  edge node   │                    │
│  └──────────────┘              └──────┬───────┘                    │
│                                       │ WiFi / Ethernet            │
│  ┌──────────────┐     USB      ┌──────┴───────┐                    │
│  │  Boston Sci  │◄────────────►│  Pi Zero 2W  │ bay-02             │
│  │  Programmer  │  (gadget)    │  edge node   │                    │
│  └──────────────┘              └──────┬───────┘                    │
│                                       │                            │
│  ┌──────────────┐     USB      ┌──────┴───────┐                    │
│  │   Abbott     │◄────────────►│  Pi Zero 2W  │ bay-03             │
│  │  Programmer  │  (gadget)    │  edge node   │                    │
│  └──────────────┘              └──────┬───────┘                    │
│                                       │                            │
│  ┌──────────────┐     USB      ┌──────┴───────┐                    │
│  │  Biotronik   │◄────────────►│  Pi Zero 2W  │ bay-04             │
│  │  Programmer  │  (gadget)    │  edge node   │                    │
│  └──────────────┘              └──────┬───────┘                    │
│                                       │ HTTPS POST                 │
│                                       ▼                            │
│                              ┌─────────────────┐                   │
│                              │   Pi 4B          │                   │
│                              │  Central Server  │ 192.168.x.x      │
│                              │  (Docker)        │ :8443            │
│                              └────────┬────────┘                   │
│                                       │ Bluetooth PAN              │
│                                       ▼                            │
│                              ┌─────────────────┐                   │
│                              │     Tablet       │                   │
│                              │   (Web UI)       │ 10.44.0.x        │
│                              └─────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Components

### Edge Node — Pi Zero 2W (one per interrogation bay)

**Hardware:** Raspberry Pi Zero 2W with USB OTG port connected to the interrogation device.

**Role:** Impersonates a USB printer to the interrogation device, captures the raw PostScript print job, and forwards it to the central server.

**Key files:**
```
edge-node/
├── config/node.conf          # Per-node identity (NODE_ID, VENDOR, URLs, token)
├── gadget/setup-gadget.sh    # Configures USB printer gadget via configfs
├── daemon/capture_daemon.py  # Reads raw PS from gadget device node
├── daemon/forwarder.py       # POSTs captured jobs to central server
└── systemd/                  # Service units for gadget, capture, forwarder
```

**Services (systemd):**
| Service | Description |
|---------|-------------|
| `usb-printer-gadget.service` | Runs at boot, sets up USB gadget via configfs |
| `capture-daemon.service` | Reads from `/dev/g_printer0`, writes PS files atomically |
| `forwarder.service` | Polls queue, POSTs to central server with exponential backoff |

---

### Central Server — Pi 4B (Docker)

**Hardware:** Raspberry Pi 4B, always-on, connected to clinic network and Bluetooth.

**Role:** Receives print jobs from all edge nodes, parses PostScript into structured clinical data, stores it in SQLite, and serves a REST API + web UI to the tablet.

**Key files:**
```
central-server/
├── Dockerfile                # python:3.11-slim + ghostscript
├── compose.yaml              # Single service, volumes: data, certs
├── api/
│   ├── main.py               # FastAPI: /ingest, /api/*, /ui/
│   ├── db.py                 # SQLite WAL, jobs/interrogations/devices tables
│   └── parsers/
│       ├── base.py           # PS→PDF (ghostscript), word extraction (pdfplumber)
│       ├── medtronic.py      # Medtronic field templates
│       ├── boston_scientific.py
│       ├── abbott.py
│       └── biotronik.py
├── ui/                       # Alpine.js + Pico CSS tablet web UI
└── bluetooth/                # BlueZ NAP + dnsmasq for tablet BT PAN
```

**Exposed ports:** `8443/tcp` (HTTPS, self-signed TLS)

---

### Tablet (Web UI consumer)

Connects to the central Pi via Bluetooth PAN (NAP profile). The Pi bridges the tablet onto network `10.44.0.0/24` via `dnsmasq`. The tablet accesses `https://10.44.0.1:8443/` in the browser — no app install required.

---

## Data Pipeline

```
Interrogation Device
        │
        │  USB (IEEE-1284 printer protocol)
        ▼
┌───────────────────────────────────────────────────────┐
│  USB Gadget Layer  (Linux configfs + usb_f_printer)   │
│                                                       │
│  /dev/g_printer0  — character device                  │
│                                                       │
│  Appears to the host as a USB printer with the        │
│  PnP string:  MFG:Generic;CMD:PS;MDL:Generic...       │
└───────────────────────┬───────────────────────────────┘
                        │  open(O_RDWR) + select() read loop
                        │  5-second idle timeout detects end-of-job
                        │  (host CUPS never sends EOF)
                        ▼
┌───────────────────────────────────────────────────────┐
│  capture_daemon.py                                    │
│                                                       │
│  • Writes chunks to  <queue>/<job>.ps.partial         │
│  • On idle timeout: fsync → rename to <job>.ps        │
│  • Writes 0x18 status byte back to unblock CUPS       │
│  • Empty jobs (0 bytes) silently discarded            │
└───────────────────────┬───────────────────────────────┘
                        │  atomic rename (partial → .ps)
                        ▼
              /var/lib/cied-edge/queue/
              <node>_<timestamp>_<uuid>.ps
                        │
                        │  poll every 5s
                        ▼
┌───────────────────────────────────────────────────────┐
│  forwarder.py                                         │
│                                                       │
│  HTTPS POST https://<central>:8443/ingest             │
│  Headers:  Authorization: Bearer <token>             │
│            X-Node-Id, X-Vendor, X-Job-Name            │
│  Body:     raw PostScript bytes                       │
│  Retry:    exponential backoff (5s → 300s cap)        │
│  TLS:      verifies against central CA cert           │
│            (falls back to unverified if cert missing) │
└───────────────────────┬───────────────────────────────┘
                        │  HTTP 202 Accepted → move to sent/
                        ▼
              /var/lib/cied-edge/sent/
              (kept for audit; not deleted automatically)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ network boundary ━━━━━━━━━━━━━━━━━━━━━━━

                        │  HTTPS POST arrives at central server
                        ▼
┌───────────────────────────────────────────────────────┐
│  FastAPI  /ingest  (api/main.py)                      │
│                                                       │
│  1. Validate Bearer token                             │
│  2. Write body to  /data/ps/<job_name>                │
│  3. INSERT OR IGNORE into jobs table (idempotent)     │
│  4. Spawn background parse thread → return 202        │
└───────────────────────┬───────────────────────────────┘
                        │  background thread
                        ▼
┌───────────────────────────────────────────────────────┐
│  Parser  (api/parsers/)                               │
│                                                       │
│  1. Ghostscript:  .ps  →  .pdf  (at 150 DPI)         │
│  2. pdfplumber:   .pdf → Word list  (x, y, text)      │
│  3. Vendor parser: spatial label matching             │
│     find_label(text) → value_right_of() / below()    │
│  4. INSERT interrogation record into SQLite           │
│  5. UPSERT device record (by serial number)           │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│  SQLite  /data/db.sqlite  (WAL mode)                  │
│                                                       │
│  jobs            — one row per print job              │
│  interrogations  — parsed clinical fields             │
│  devices         — known device inventory             │
└───────────────────────┬───────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────┐
│  REST API  /api/*  (no auth — trusted BT PAN)         │
│                                                       │
│  GET /api/stats                                       │
│  GET /api/jobs                                        │
│  GET /api/interrogations                              │
│  GET /api/devices                                     │
└───────────────────────┬───────────────────────────────┘
                        │  Bluetooth PAN  10.44.0.0/24
                        ▼
                  Tablet Web UI
                  (Alpine.js + Pico CSS, no build step)
```

---

## Database Schema

```sql
jobs (
    id           INTEGER PRIMARY KEY,
    node_id      TEXT,          -- e.g. "bay-01"
    vendor       TEXT,          -- medtronic | boston_scientific | abbott | biotronik
    job_name     TEXT UNIQUE,   -- <node>_<timestamp>_<uuid>.ps
    received_at  TEXT,          -- ISO-8601 UTC
    raw_ps_path  TEXT,          -- /data/ps/<job_name>
    pdf_path     TEXT,          -- /data/pdf/<job_name>.pdf (null if parse failed)
    parsed       INTEGER,       -- 0 = pending/failed, 1 = success
    parse_error  TEXT           -- null on success
)

interrogations (
    id                 INTEGER PRIMARY KEY,
    job_id             INTEGER REFERENCES jobs(id),
    patient_id         TEXT,
    device_serial      TEXT,
    device_model       TEXT,
    interrogation_date TEXT,
    battery_voltage    REAL,
    battery_longevity  TEXT,
    impedance_rv       REAL,
    impedance_ra       REAL,
    impedance_lv       REAL,
    sensing_rv         REAL,
    sensing_ra         REAL,
    threshold_rv_v     REAL,
    threshold_rv_pw    REAL,
    threshold_lv_v     REAL,
    extra              TEXT      -- JSON blob for vendor-specific fields
)

devices (
    device_serial  TEXT PRIMARY KEY,
    device_model   TEXT,
    vendor         TEXT,
    patient_id     TEXT,
    patient_name   TEXT,
    implant_date   TEXT,
    updated_at     TEXT
)
```

---

## Security Model

| Boundary | Mechanism |
|----------|-----------|
| Edge → Central (ingest) | HTTPS with self-signed TLS; Bearer token in `Authorization` header |
| Central → Tablet (API) | No auth — tablet connects over Bluetooth PAN, a trusted private link |
| TLS certificates | Self-signed CA; CA cert distributed to edge nodes for verification |
| Secrets | `INGEST_TOKEN` in Docker environment, `CENTRAL_TOKEN` in `/etc/cied-edge/node.conf` (root-readable only) |

---

## Resilience

| Failure | Behaviour |
|---------|-----------|
| USB cable unplugged mid-job | `.ps.partial` stays in queue; discarded on next daemon start if empty |
| Central server unreachable | Forwarder retries indefinitely with exponential backoff (cap 300s) |
| Duplicate delivery (retry after network blip) | Ingest endpoint is idempotent — `INSERT OR IGNORE` prevents duplicate DB rows |
| Pi power loss mid-write | Atomic rename (partial → .ps) ensures queue only contains complete files |
| Central DB corruption | SQLite WAL mode; journal survives unclean shutdown |

---

## Vendor Parser Calibration

Each vendor parser (`api/parsers/<vendor>.py`) inherits from `BaseParser` and defines a `FIELD_LABELS` dict mapping clinical field names to the text labels that appear on the printed report. The base class uses spatial matching:

- **`value_right_of(label)`** — finds the word to the right of a label on the same line
- **`value_below(label)`** — finds the word directly below a label

Parsers are currently stubs with empty `FIELD_LABELS`. They will be calibrated once real interrogation device printouts are available. If device output is raster-only (image-based PostScript), an OCR step (Tesseract) will be inserted between the Ghostscript and pdfplumber stages.

---

## Deployment Summary

| Component | Hardware | OS | Runtime |
|-----------|----------|----|---------|
| Edge node | Pi Zero 2W | Raspberry Pi OS Lite (64-bit) | Python 3, systemd |
| Central server | Pi 4B | Raspberry Pi OS Lite (64-bit) | Docker, `python:3.11-slim-bookworm` |
| Tablet UI | Any tablet | Browser | Static HTML/JS (no install) |
</content>
</invoke>