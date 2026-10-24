# CIED Bridge

An open-source system that captures data from cardiac implantable electronic device (CIED) programmers — pacemakers, ICDs, CRT-Ds, ICMs — at the moment of printing or export, without modifying the programmers or their workflow.

A small USB relay plugs between the programmer and its printer or USB port. It intercepts print jobs and file exports over WiFi to a local hub, which archives, renders, and optionally extracts structured clinical data. The result is a searchable digital record of every interrogation, available seconds after the clinician presses "Print."

## Why

Cardiac device clinics still rely on thermal printouts taped into paper charts or scanned into EMRs by hand. The data inside those reports — impedances, thresholds, battery status, arrhythmia logs — is rich and structured, but the capture process is manual, error-prone, and slow. CIED Bridge eliminates that gap by digitising the report at the point of origin, transparently.

## Architecture

```
 Programmer (Medtronic / Boston Sci / Abbott / Biotronik)
      │
      │  USB — printer or mass-storage
      ▼
 ┌──────────┐     WiFi     ┌──────────┐
 │  Relay   │ ──────────►  │   Hub    │  local web UI, archive, Samba share
 │ ESP32-S3 │              │  Pi 3/x86│  optional: extraction service on LAN
 │ or Pi 0W │              └──────────┘
 └──────────┘
```

**Relay** — a plug-sized device that impersonates a USB printer (or storage device) to the programmer. Available as ESP32-S3 (print capture only) or Raspberry Pi Zero W (print, storage, HID, or composite). One per interrogation bay.

**Hub** — a Raspberry Pi 3 or x86 server running the local network. Roles are composable: *printer* (CUPS queue, print-relay web UI), *data* (file ingest, Samba share, structured extraction). Small clinics run both on one box.

**CIED Extract** — an optional LAN service that parses captured PDFs into structured fields using OCR and VQA. Runs on a separate GPU-equipped machine; the hub falls back to raw-PDF display when it is unavailable.

**CIED Studio** — the clinician's desktop application (Electron). Consumes structured data files delivered via USB relay or Samba share. Its built-in parsers cover HL7, XML, and vendor-proprietary formats; CIED Extract augments them for raster-only PDFs.

### Supported vendors

All four major CIED vendors are supported and have been validated in clinical use:

| Vendor | Print capture | File export | Notes |
|--------|:---:|:---:|-------|
| Medtronic | ✓ | ✓ | PCL print jobs; requires LaserJet-class printer (P3015dn) |
| Boston Scientific (Latitude) | ✓ | ✓ | PostScript print jobs |
| Abbott (Merlin) | ✓ | ✓ | Raster-only PDFs; structured extraction via VQA |
| Biotronik | ✓ | ✓ | PostScript + IEEE 11073 XML and carddas XML |

## Repository layout

```
printer-relay/          Relay firmware — ESP32-S3 (C/ESP-IDF) + Pi Zero (Python)
printer-relay/pi3-server/   Hub web UI + CUPS integration (Python/Flask)
edge-node/              Pi Zero W USB gadget, capture daemon, forwarder
export-relay/           Ingest-storage relay (Pi Zero mass-storage gadget)
import-relay/           Delivery-storage relay + hub queue server
kardisynch-receiver/    Delivery-HID relay + Windows bridge apps (C#)
central-server/         CIED Central — data hub API + dashboard (Python/FastAPI)
tools/                  Deployment, provisioning, and diagnostic scripts
docs/                   Architecture docs, integration specs
```

## Getting started

### Hub (Raspberry Pi 3 or x86 Debian)

**Remote deploy** (from your workstation, over SSH):

```bash
PI=192.168.1.50 REMOTE_USER=john bash tools/clinic-hub-redeploy.sh
```

**Local deploy** (already SSH'd into the hub):

```bash
git clone https://github.com/konsilado/cied-bridge.git
cd cied-bridge
sudo bash printer-relay/pi3-server/setup-cups.sh
```

Both paths install dependencies, configure the WiFi access point, CUPS, Samba, and systemd services. The scripts are idempotent — safe to re-run on an existing deployment.

### Relay (ESP32-S3)

Flash the pre-built firmware via the hub's web UI (USB flash tab), or build from source:

```bash
cd printer-relay/esp32
idf.py set-target esp32s3
idf.py build
idf.py flash
```

### Relay (Pi Zero W)

**Option A — Buildroot image** (production, ~5 s boot, read-only root):

```bash
cd edge-node/buildroot
bash build.sh                     # ~90 min first build, ~5 min after
dd if=buildroot-*/output/images/sdcard.img of=/dev/sdX bs=4M status=progress
```

**Option B — Raspberry Pi OS** (development, easier to debug):

```bash
# On a Pi Zero W running Raspberry Pi OS Lite:
cd edge-node
sudo bash install.sh
# Edit /etc/cied-edge/node.conf with the hub's address and ingest token
sudo reboot
```

## Configuration

Each hub reads a feature manifest at `/etc/cied-bridge/clinic.conf`:

```ini
FEATURE_RELAY_AP=on
FEATURE_SAMBA_SHARE=on       # Samba share for file export
FEATURE_IMPORT_HUB=on        # Delivery queue server on :8090
FEATURE_PRINTER_WEBUI=on     # CUPS + print-relay web UI
FEATURE_CENTRAL_WEBUI=off    # CIED Central data UI (Docker)
```

Generate a manifest interactively:

```bash
tools/configure-clinic.sh
```

## Status

Current version: **v1.6.0** — see [CHANGELOG](CHANGELOG.md) for release history.

## Not a medical device

CIED Bridge captures and displays data from CIED programmers. **It is not a medical device.** It does not control, program, or modify any implanted device or programmer. It does not make clinical decisions or recommendations.

Any party that incorporates CIED Bridge into a product intended for clinical decision-making, diagnostic use, or any purpose regulated under MDR 2017/745, FDA 21 CFR 820, or equivalent regulation bears full responsibility for regulatory classification, conformity assessment, and certification.

CIED Bridge is provided as-is, without warranty. See [LICENSE](LICENSE) for details.

## License

| Component | License |
|-----------|---------|
| Firmware and software | [MPL-2.0](https://www.mozilla.org/en-US/MPL/2.0/) |
| Hardware designs | [CERN-OHL-W-2.0](https://ohwr.org/cern_ohl_w_v2.txt) |
| Protocol and data format specifications | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) |

**Why this split:** MPL-2.0 lets anyone use, sell, and certify products built with this code. Changes to *these* files must stay open; your own additions remain yours. CERN-OHL-W applies the same principle to hardware. The specs are under CC-BY so anyone can write a compatible implementation from scratch.

### Patent grant

MPL-2.0 and CERN-OHL-W-2.0 both include explicit patent grants covering the contributed code and designs.

### Contributions

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/) (DCO). Sign off your commits with `git commit -s` to certify that you have the right to submit the code and agree to the DCO.

### Trademark

"CIED Bridge" is a trademark of Konsilado GmbH. Compatible implementations may use the name "CIED Bridge compatible" only if they pass the published conformance tests. This keeps the standard from fragmenting while allowing anyone to build on the technology.

## Contributing

Contributions are welcome. Please open an issue first to discuss non-trivial changes.

1. Fork the repository
2. Create a feature branch
3. Sign off your commits (`git commit -s`)
4. Open a pull request

## Contact

Sebastian Hilbert — [Konsilado GmbH](https://konsilado.de)
