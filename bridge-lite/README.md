# CIED Bridge Lite

A serverless, plug-and-play file relay for small clinics. Two ESP32-S3 dongles
(T-Dongle-S3) move file exports from CIED programmers to the office PC with zero
infrastructure — no server, no network, no IT involvement.

## How it works

```
  Programmer (any vendor)              Office PC
       │                                   │
       │  USB — file export (PDF/XML)      │  USB — mass-storage drive
       ▼                                   ▼
  ┌──────────┐   WiFi (TCP)       ┌──────────┐
  │  Sender  │ ─────────────────► │ Receiver │
  │ T-Dongle │   direct link      │ T-Dongle │
  └──────────┘                    └──────────┘
     one per bay                   one per office
```

**Sender** — plugs into the programmer's USB port. The programmer sees a FAT32 USB
drive ("CIED Sender") and writes PDF/XML file exports to it. After copying, press the
BOOT button on the dongle — it scans the card, transmits files over WiFi to the
receiver, and moves them to the `done/` folder.

**Receiver** — plugs into the office PC's USB port. The PC sees a FAT32 USB drive
("CIED Receiver"). Received files appear in the `inbox/` folder, tagged with sender ID
(`bay1-report.pdf`). Staff open the drive and grab the PDF.

## Key properties

- **No server, no external WiFi** — receiver runs its own SoftAP; sender connects as station
- **One receiver, many senders** — one dongle on the office PC, one per interrogation bay
- **File export only** — PDF/XML from the programmer's native export; no print capture
- **SHA-256 verified** — every transfer is integrity-checked end to end
- **Under 30 EUR total** — T-Dongle-S3 is ~12 EUR/unit
- **Plug-and-forget** — preconfigure WiFi credentials and sender ID at flash time

## Hardware

**Target board:** LilyGo T-Dongle-S3

| Feature | Detail |
|---------|--------|
| MCU | ESP32-S3, 16 MB flash, no PSRAM |
| USB | USB-A male (plugs directly into host) |
| Storage | Built-in TF (microSD) card slot, SDMMC 4-bit |
| Display | ST7735S 160×80 LCD — role, status, file progress |
| LED | APA102 (SPI, GPIO 39/40) — status indicator |
| Button | BOOT (GPIO 0) — triggers file scan on sender |
| Form factor | USB stick with cap, 65 × 20 × 12 mm |

## Flashing

Requires [ESP-IDF v5.2+](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/get-started/).

### Quick (flash script)

Plug a dongle in with BOOT held, then:

```bash
cd firmware
./flash.sh --receiver --ssid "MyClinic" --password "s3cure!"
./flash.sh --sender   --ssid "MyClinic" --password "s3cure!" --sender-id bay1
```

The script patches sdkconfig, builds, and flashes in one step. SSID and password must
match between sender and receiver.

### Manual

```bash
cd firmware
idf.py set-target esp32s3
idf.py menuconfig        # CIED Bridge Lite → role, SSID, password, sender ID
idf.py build flash
```

The sender and receiver share one codebase; the role is selected at build time via
`CONFIG_BRIDGE_LITE_ROLE_SENDER` / `CONFIG_BRIDGE_LITE_ROLE_RECEIVER`.

## Usage

1. Plug the **receiver** into the office PC — it shows as "CIED Receiver" USB drive
2. Plug the **sender** into the programmer — it shows as "CIED Sender" USB drive
3. Export a PDF/XML from the programmer (it writes to the sender drive)
4. Press the **BOOT button** on the sender dongle
5. The file transfers over WiFi; it appears in the receiver's `inbox/` folder

## Protocol

Files are transferred over TCP (port 5000) on the receiver's SoftAP network
(default `192.168.4.1`).

Wire format per file:

| Field | Size | Description |
|-------|------|-------------|
| name_len | 2 bytes | Filename length (big-endian) |
| name | variable | Filename (prefixed with sender ID) |
| file_size | 4 bytes | File size (big-endian) |
| sha256 | 32 bytes | SHA-256 of the file |
| data | file_size | Raw file data (streamed in 4 KB chunks) |
| result | 1 byte | Response: 0x01 = OK, 0x00 = FAIL |

Throughput: limited by WiFi, typically 1–5 MB/s. A 5 MB Abbott export transfers
in a few seconds.

## Status

### LCD

Four text rows showing role, status, sender ID, and current operation:

```
CIED Bridge
SENDER
bay1
press BTN
```

### LED

| Color | Meaning |
|-------|---------|
| Green steady | Ready (SD mounted, WiFi connected/listening) |
| Blue steady | File transfer in progress |
| Green flash | Transfer complete |
| Red steady | Error (SD not mounted, WiFi disconnected) |

## Configuration

Build-time options in `menuconfig → CIED Bridge Lite`:

| Option | Default | Description |
|--------|---------|-------------|
| Role | Sender | Sender or Receiver |
| Sender bay ID | bay1 | Prefixed to filenames on the receiver |
| WiFi SSID | CIED-Bridge | AP name (must match on both) |
| WiFi password | cied-bridge-lite | WPA2 password (must match on both) |

## Relationship to CIED Bridge

CIED Bridge Lite is the zero-infrastructure variant of the [CIED Bridge](../README.md)
system. It covers the **file export** pathway only (PDF/XML). For print capture, structured
data extraction, or multi-site archiving, use the full CIED Bridge with a hub server.

## License

Same triple license as the parent project:

| Component | License |
|-----------|---------|
| Firmware | [MPL-2.0](https://www.mozilla.org/en-US/MPL/2.0/) |
| Hardware designs | [CERN-OHL-W-2.0](https://ohwr.org/cern_ohl_w_v2.txt) |
| Protocol spec | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) |
