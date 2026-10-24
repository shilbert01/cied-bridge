# Pacemaker Central Relay — Product Specification

> **Audience:** This document is the single source of truth for the relay hardware/software
> product line. It is intended to be handed to a separate team (design, marketing, web) as a
> self-contained briefing. Sections marked `[TBD]` are undecided or unknown at the time of
> writing; best-guess suggestions are included inline.

---

## 1. Product Overview

### Product name and branding

- **Product family:** Pacemaker Central Relay
- **Sub-products:** Printer Relay, Data Export Relay (see §2)
- **Parent platform:** Pacemaker Central — `[TBD: confirm relationship to Konsilado brand]`
- **Short product handle:** PCRelay (internal), "Relay" (user-facing)

### Elevator pitch

> The Pacemaker Central Relay is a plug-and-play hardware bridge that silently captures data
> from cardiac device programmers — the proprietary bedside computers used to interrogate
> pacemakers and defibrillators — and delivers it automatically into the Pacemaker Central
> platform. No manual export steps, no USB sticks, no paper stacks. The device looks like a
> printer or USB cable to the programmer; everything else happens invisibly.

### The core problem

Cardiac electrophysiology (EP) departments operate with programmers from multiple manufacturers
(Abbott, Medtronic, Boston Scientific, Biotronik). Each programmer stores patient device data in
a proprietary format and offers only limited, manufacturer-specific export paths — typically a
physical printout or a USB/SD card export that requires manual staff intervention.

The consequences:
- Reports are printed on paper and filed manually (or not at all)
- Digital archiving requires a staff member to scan, rename, and upload each document
- Cross-manufacturer data consolidation is essentially impossible without a middleware layer
- Follow-up scheduling, compliance tracking, and outcome analysis depend on data that never
  leaves the programmer room

The relay devices eliminate this gap by intercepting the data at the point of output — either
the printer port or the USB export — and routing it automatically into Pacemaker Central.

---

## 2. Device Variants

### 2a. Printer Relay

#### What it does

The Printer Relay presents itself to a cardiac device programmer as a USB laser printer. When
the clinician triggers a report printout on the programmer, the relay captures the raw print
data (PostScript or PCL), forwards it to a local CUPS print server on a companion Raspberry
Pi 3, and the physical report is printed on whatever USB laser printer is connected.

In parallel, the print data is preserved for ingestion into Pacemaker Central. `[TBD: the
pipeline from CUPS/LPR to the Pacemaker Central backend is not yet implemented — see §4]`

#### Platform

| Component | Choice | Reason |
|-----------|--------|--------|
| Relay node | Raspberry Pi Zero W | Smallest form factor with USB device-mode (OTG) and WiFi. The BCM2835 SoC supports USB gadget mode natively. |
| Print server | Raspberry Pi 3B/3B+ | Full USB host, CUPS, WiFi AP capability, enough CPU for CUPS filtering. |

#### Hardware (Pi Zero W relay node)

- Raspberry Pi Zero W (BCM2835, ARM1176JZF-S @ 1 GHz, 512 MB RAM)
- SSD1306 OLED display, 0.96", 128×64 px, I2C (status readout)
- USB micro-B → USB-A cable to programmer (gadget/device mode)
- 5V micro-USB power supply
- MicroSD card (≥4 GB) — Buildroot image, ~256 MB ext4 rootfs
- Enclosure: `[TBD]`

#### Hardware (Pi 3 print server)

- Raspberry Pi 3B or 3B+ (BCM2837, quad-core Cortex-A53 @ 1.2–1.4 GHz, 1 GB RAM)
- USB-A → USB-B cable to laser printer
- Ethernet (eth0) for management LAN access (optional — not required for operation)
- 5V USB-C / micro-USB power supply
- MicroSD card (≥8 GB)
- Enclosure: `[TBD]`
- Any USB laser printer (auto-detected and configured on plug-in via udev — see §3)

#### Software stack (Pi Zero W)

| Layer | Technology |
|-------|------------|
| OS | Buildroot Linux (custom minimal image, ~256 MB) |
| Init | BusyBox init, S-number init scripts |
| USB gadget | Linux `g_printer` / USB configfs, class 0x07 (printer) |
| Relay service | `relay_daemon.py` (Python 3, stdlib only) |
| Display service | `display_daemon.py` (Python 3, Pillow, embedded 5×7 bitmap font) |
| WiFi | wpa_supplicant, hidden SSID `relayap` |
| Protocol to Pi3 | LPR (RFC 1179), TCP port 515 |

#### Software stack (Pi 3)

| Layer | Technology |
|-------|------------|
| OS | Raspberry Pi OS (Debian Bookworm) |
| Print server | CUPS 2.4 with cups-lpd (LPR listener) |
| WiFi AP | hostapd (hidden SSID `relayap`, WPA2-PSK) |
| DHCP | dnsmasq on wlan0 |
| NTP | chrony — internet sync when available; `local stratum 10` serves Pi Zero regardless |
| IP management | systemd oneshot (`relay-ap-ip.service`) assigns 10.99.0.1/24 to wlan0 |
| USB auto-config | udev rule → `cups-autoconf-printer.service` → `cups-autoconf-printer.sh` |
| Config web UI | `printer-config-server.py` (Python 3 stdlib, port 80) |

#### Network topology

```
[Cardiac programmer]
      │  USB (gadget mode — printer class 0x07)
      ▼
[Pi Zero W — 10.99.0.2]
   relay_daemon.py
      │  LPR / TCP 515 over WiFi (relayap, hidden WPA2)
      ▼
[Pi 3 — 10.99.0.1 (wlan0)]
   CUPS → cups-lpd
      │  USB
      ▼
[Laser printer]
```

Management access (optional):
```
[Engineer laptop] ──── eth0 (192.168.x.x / DHCP) ──── [Pi 3]
                   or WiFi → relayap → http://10.99.0.1/
```

#### Data flow (detailed)

1. Programmer opens USB gadget connection → Pi Zero kernel exposes `/dev/g_printer0`
2. `relay_daemon.py` polls the gadget device; when data arrives, writes to
   `/var/lib/pacemaker-relay/queue/<job>.ps.partial`
3. After a 5-second idle timeout (job complete), renames to `<job>.ps`
4. Simultaneously touches `/var/run/relay_printing` and sends `SIGUSR1` to
   `display_daemon.py` → OLED shows "Print in progress" for 10 seconds
5. `relay_daemon.py` opens a LPR connection to `10.99.0.1:515`, sends the job
6. On success, moves job file to `/var/lib/pacemaker-relay/sent/`
7. On failure, retries with exponential back-off up to a configurable give-up timeout;
   then drops the job

#### OLED status display

The SSD1306 display refreshes every 60 seconds (or immediately on SIGUSR1). It shows:

```
┌─────────────────────────┐
│  Printer Relay          │  ← inverted header bar
│  WiFi:   connected      │
│  Relay:  active         │
│  Server: online         │
│  Jobs:   3  14:32  21:07│  ← session job count + last job time + clock
└─────────────────────────┘
```

"Print in progress" is displayed full-screen (inverted) for 10 seconds when a job is captured.

WiFi state is read from `/sys/class/net/wlan0/carrier` (1 = associated with AP, not just
interface up). Time is NTP-synced from Pi3 (chrony). Timezone: `CET-1CEST,M3.5.0,M10.5.0/3`
written to `/etc/TZ` at build time (musl POSIX TZ string, no tzdata package needed).

---

### 2b. Data Export Relay

> **Status:** Prototype / early development. Less detail is available here; more `[TBD]` flags
> apply.

#### What it does

The Data Export Relay captures structured device interrogation data directly from the
programmer's digital export interface (USB mass storage, proprietary serial/network export)
and routes it to Pacemaker Central without a print step. This preserves structured data fields
(device serial, lead parameters, battery status, arrhythmia episode counts, etc.) rather than
a print-formatted document.

`[TBD: exact capture mechanism varies per programmer vendor — enumerate Abbott, Medtronic,
Boston Scientific, Biotronik interfaces]`

#### OCR pipeline (fallback / PDF-based path)

Where structured export is not available, the programmer produces a PDF or PostScript report.
The relay (or a backend service) uses OCR to extract structured fields:

- Model: `[TBD — prototype used PaddleOCR-VL-1.6]`
- Input: image-based PDFs (scanned appearance, not selectable text)
- Language: German (primary), English
- Target device: Abbott Aveir DR LP (prototype)
- Deployment: Docker `[TBD: on Pi 3 locally, or cloud-side after relay uploads PDF]`

#### Platform `[TBD]`

Options under consideration:
- Pi 3 (local OCR — feasible but slow, ~10–30s per page)
- Pi 4 / Pi 5 (faster local OCR)
- Cloud-side processing (relay uploads raw PDF; OCR runs on backend)

#### Data flow `[TBD]`

```
[Programmer] → USB/network export
      ↓
[Relay device] → raw file capture
      ↓
[Upload to Pacemaker Central backend]  ← Cloudflare Tunnel [TBD]
      ↓
[OCR / parser] → structured record
      ↓
[Pacemaker Central patient record]
```

---

## 3. Technical Architecture

### Network connectivity

**On-site (relay subnet):**
- Pi 3 creates a WPA2-PSK WiFi AP (`relayap`, default passphrase `CiedRelay#1` — change before production deploy)
- Subnet: `10.99.0.0/24`, Pi 3 at `.1`, Pi Zero at `.2` (static)
- Pi Zero has no route to the internet — all traffic stays on the relay subnet
- Pi 3 eth0 connects to the clinic LAN or internet for management and (future) cloud upload

**Remote management / cloud upload `[TBD]`:**
- Proposed: Cloudflare Tunnel on Pi 3 eth0 for zero-configuration inbound access
- This would allow remote SSH, log retrieval, and job upload without firewall changes
- Alternative: VPN (WireGuard) `[TBD — decision pending deployment scale]`

### Security model

**Physical:** The relay devices are small and intended to live in the programmer room. Physical
security is the clinic's responsibility.

**Network:**
- Relay subnet is air-gapped from clinic LAN by default (Pi Zero has no external route)
- relayap is hidden SSID + WPA2-PSK — not visible to casual WiFi scanning
- CUPS web UI (port 631) and relay config UI (port 80) are accessible only from relayap

**Data at rest:**
- Print jobs are stored temporarily in `/var/lib/pacemaker-relay/queue/` and `sent/`
- No encryption at rest currently `[TBD — LUKS on rootfs partition is an option for
  sensitive deployments]`
- CUPS spool files: preserved or discarded per `PreserveJobFiles` toggle in config UI

**Data in transit:**
- LPR (port 515) between Pi Zero and Pi 3 is unencrypted — acceptable within the
  isolated relay subnet (no external exposure)
- Upload to Pacemaker Central backend `[TBD — HTTPS/TLS assumed]`

**Patient data:** All samples except the Abbott Aveir demo contain real patient data
(names, DOBs, device serials). The relay must remain local or data must be anonymised
before sharing or logging.

### Deployment and provisioning workflow

**Printer Relay (Pi Zero):**
1. Flash `sdcard.img` to microSD (built with Buildroot `build.sh`, ~5 min incremental)
2. Boot — no further configuration needed for basic relay function
3. Connect to relayap (`http://10.99.0.1/`) to verify printer is configured
4. Edit `/etc/pacemaker-relay/relay.conf` if non-default print server IP

**Pi 3 print server:**
1. Flash Raspberry Pi OS
2. Run `sudo bash pi3-server/setup-cups.sh` (installs CUPS, hostapd, dnsmasq, chrony,
   udev printer auto-config, config web UI in one shot)
3. Plug in USB laser printer — auto-detected and configured within ~5 seconds

**Printer auto-configuration (key feature for customer shipping):**
A udev rule fires on USB printer plug-in (class 0x07), starts
`cups-autoconf-printer.service`, which polls `lpinfo -v` until the printer appears, then
updates the `LaserPrinter` CUPS queue. No per-site `lpadmin` command needed. Different
printer models are handled automatically — serial-number lock-in is avoided.

### NTP / time synchronisation

- Pi Zero has no RTC; time is lost on every power cycle
- On each 60-second display refresh, `display_daemon.py` queries Pi 3 (`10.99.0.1:123`)
  via a raw NTP UDP packet (pure Python, no external library)
- Pi 3 runs chrony: syncs from internet NTP pools when eth0 has connectivity;
  `local stratum 10` ensures it serves its own clock to the Pi Zero even in air-gapped
  deployments
- Timezone: Central European (`CET-1CEST,M3.5.0,M10.5.0/3`) written to `/etc/TZ` in
  Buildroot `post-build.sh`
- `[TBD: DS3231 hardware RTC on Pi Zero is an option for deployments where the Pi 3
  is not always present at power-on. Not currently implemented.]`

### Monitoring, health checks, remote management

**On-device (Pi Zero OLED):**
- WiFi: connected / disconnected (carrier state, not just interface up)
- Relay: active / stopped (PID file check)
- Server: online / offline (TCP probe to Pi 3 port 515)
- Jobs: session count (resets at boot) + timestamp of last job
- "Print in progress" full-screen alert (10-second hold)

**On-device (Pi 3 web UI — `http://10.99.0.1/`):**
- Active printer URI
- Available USB printers (plug-and-switch)
- Job queue: list, cancel, delete, reprint
- Job preservation toggle (`PreserveJobFiles` in cupsd.conf)

**Remote `[TBD]`:**
- Cloudflare Tunnel / WireGuard for SSH access to Pi 3
- Log forwarding `[TBD]`
- Centralised fleet monitoring `[TBD]`

---

## 4. Integration with Pacemaker Central

> **Status: TBD — this section describes the intended architecture, not yet implemented.**

### Intended data flow

```
Pi 3 CUPS (job spooled)
      │
      ▼  [TBD: upload agent on Pi 3, triggered after successful print]
Pacemaker Central backend API
      │
      ▼  [TBD: OCR / parser for PDF/PS → structured record]
Patient record in Pacemaker Central
      │
      ▼
EP department staff view: device report attached to scheduled follow-up
```

### Data formats

- Raw print data: PostScript (most programmers) or PCL5
- Paper size: embedded in PCL/PS; varies by programmer vendor and region
  - Abbott Aveir: narrow thermal roll (~110 mm width), formatted for thermal printer
  - Medtronic CareLink: A4 or US Letter depending on regional configuration
  - `[TBD: Boston Scientific, Biotronik]`
- Structured output post-OCR: `[TBD — JSON schema to be defined with Pacemaker Central
  backend team]`

### What EP staff see `[TBD]`

- PDF rendition of the captured report attached to the patient's device follow-up record
- Extracted structured fields (device serial, last interrogation date, battery status,
  episode counts) displayed inline `[TBD]`
- Notification that a new report was captured (email / in-app) `[TBD]`

---

## 5. Target Audience & Use Cases

### Primary users

| Role | Context |
|------|---------|
| EP nurses / device clinic coordinators | Run the programmer room day-to-day; initiate interrogations; currently manage paper reports |
| Electrophysiologists / cardiologists | Review device data; need timely access to interrogation findings |
| Hospital IT / biomedical engineering | Responsible for network security; need simple, auditable deployment |

### Typical deployment scenario

A device clinic sees 20–60 pacemaker/ICD patients per week across multiple programmer
brands. Reports are currently printed on the programmer's built-in thermal printer or on a
shared floor printer, then scanned or filed manually. A relay device is installed once per
programmer (or shared between programmers in the same room). From that point, every
interrogation printout is captured automatically with zero workflow change for clinical staff.

### Pain points addressed

- **Manual archiving burden:** Staff currently scan, rename, and upload each printed report.
  The relay eliminates this entirely.
- **Lost reports:** Paper reports are misfiled or lost. Relay creates a permanent digital copy
  at the moment of printing.
- **Cross-programmer fragmentation:** Abbott data lives in Abbott's software; Medtronic in
  Medtronic's. The relay is vendor-agnostic — it sees a printer, not a programmer.
- **Follow-up scheduling:** EP teams currently cross-reference a paper binder with their
  scheduling system. With structured data in Pacemaker Central, scheduling can be
  semi-automated against device longevity and clinical criteria.
- **Audit / compliance:** Regulators increasingly expect structured archiving of device
  follow-up. Paper is not auditable at scale.

---

## 6. Value Propositions

### Key benefits

- **Zero workflow change for clinical staff** — the programmer still thinks it's talking to
  a printer; staff still press "print" as they always have
- **Vendor-agnostic** — works with any programmer that can print (Abbott, Medtronic, Boston
  Scientific, Biotronik, Sorin/MicroPort)
- **Plug-and-play at new sites** — udev auto-detects and configures any USB laser printer;
  no per-site IT configuration
- **Air-gapped by default** — relay subnet is isolated from clinic LAN; no patient data
  leaves the device until explicitly configured for upload
- **Self-contained, low maintenance** — Buildroot image, no package manager, no background
  updates, no user accounts to manage
- **Near-instant visibility** — from printout to digital archive in seconds, not days

### ROI arguments `[TBD — align with existing Pacemaker Central ROI framing]`

- Staff time saved: `[TBD — estimate based on current manual archiving workflow]`
- Reduction in lost/misfiled reports: `[TBD]`
- Faster follow-up scheduling through structured data availability: `[TBD]`
- Compliance risk reduction: `[TBD]`

### Competitive differentiation `[TBD]`

- `[TBD: are there competing products in this space? HL7 integration engines, proprietary
  programmer network interfaces (e.g. Medtronic CareLink Network, Abbott Merlin.net)?]`
- Key differentiator vs. manufacturer networks: the relay is manufacturer-agnostic and
  integrates into a unified platform rather than siloed vendor portals

---

## 7. Branding & Visual Identity

> **[TBD — the following is a placeholder pending input from the Pacemaker Central / Konsilado
> brand guidelines.]**

- `[TBD: Pacemaker Central color palette, typography, logo]`
- `[TBD: Konsilado relationship — is Konsilado the company, Pacemaker Central the product?]`
- **Suggested tone of voice:** Clinical confidence without coldness. The audience is medical
  professionals who are sceptical of technology promises; copy should be precise, evidence-
  grounded, and respectful of their time. Avoid startup hyperbole.
- **Visual metaphor suggestions:** Signal path / waveform (ECG-adjacent without being cliché),
  bridge/relay iconography, clean hardware photography

---

## 8. Website Requirements

### Suggested page structure

1. **Hero** — one-sentence value prop, product photo/render, CTA ("Request a demo" / "Learn more")
2. **Problem** — brief framing of the manual archiving pain point (recognisable to any EP coordinator)
3. **How it works** — three-step diagram: plug in → print as usual → data in Pacemaker Central
4. **Device overview** — Printer Relay and Data Export Relay cards, specs, photos
5. **Technical specs** — collapsible section for IT/biomedical engineering audience
6. **Integration** — how it connects to Pacemaker Central; what staff see after capture
7. **FAQ** — network security, patient data handling, supported programmers, printer compatibility
8. **Contact / demo CTA** — form or calendar link

### Content blocks / assets needed

- Product photography or 3D render of the relay hardware `[TBD — no enclosure designed yet]`
- Network diagram (clean, designed version of the ASCII diagram in §2)
- "Before / after" workflow comparison
- Supported programmer logos `[TBD — licensing/trademark considerations]`
- `[TBD: case study or testimonial from pilot clinic]`

### Animations `[TBD]`

- Data flow animation (programmer → relay → cloud → screen) could be effective for the
  "how it works" section

### Target languages

- German (primary — target market is DACH EP departments)
- English (secondary — for international reach and hospital IT documentation)
- `[TBD: French, Dutch for Benelux expansion]`

---

## Appendix A — File / Repository Layout

```
printer-relay/
├── buildroot/
│   ├── build.sh                        # incremental Buildroot build (~5 min)
│   ├── configs/pacemaker_relay_defconfig
│   └── board/pacemaker-relay/
│       ├── post-build.sh               # installs daemons, writes /etc/TZ
│       ├── rootfs-overlay/             # init scripts, WiFi config, firmware
│       └── kernel.config               # USB gadget + I2C fragment
├── daemon/
│   ├── relay_daemon.py                 # USB gadget → LPR relay
│   └── display_daemon.py               # SSD1306 OLED status display
├── pi3-server/
│   ├── setup-cups.sh                   # one-shot Pi 3 setup script
│   ├── cups-autoconf-printer.sh        # udev-triggered printer auto-config
│   ├── cups-autoconf-printer.service
│   ├── 99-usb-printer.rules
│   ├── printer-config-server.py        # web UI (port 80)
│   ├── printer-config.service
│   └── README.md
├── config/
│   └── relay.conf.example
└── gadget/
    └── setup-gadget.sh
```

## Appendix B — Known Limitations / Open Issues

| Issue | Status | Notes |
|-------|--------|-------|
| Paper size mismatch (Abbott narrow vs A4) | Open | Abbott thermal roll ~110 mm; prints in top-left of A4 sheet. PCL stream rewriting needed for clean output. |
| Upload to Pacemaker Central backend | Not implemented | Data stays on Pi 3 after print. |
| Enclosure design | TBD | No physical enclosure designed; bare boards currently. |
| Pi Zero boot time | ~35 seconds | Acceptable for current use; Pico 2W considered for near-instant boot in future. |
| Job persistence across Pi Zero reboot | By design — resets | Sent/ directory survives; job count resets to 0 on boot (uses /proc/uptime filter). |
| DS3231 RTC | Not implemented | chrony + NTP from Pi3 is the current time source. |
| LUKS / encryption at rest | Not implemented | Consider for sensitive deployments. |
| Cloudflare Tunnel / remote access | Not implemented | Proposed for fleet management. |
| Data Export Relay | Prototype only | OCR pipeline exists (PaddleOCR-VL-1.6) but not integrated with relay hardware. |
| Supported programmer list | Partial | Verified: Abbott Aveir DR LP. Others untested. |
