# Changelog

All notable changes to CIED Bridge are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed
- Rebrand pacemaker-* → cied-* across all source code, config paths, service names, and UI strings (48 files)
- Rename CiedOCR references → CIED Extract (module, env vars, classes, docs)

## [1.8.0] — 2026-10-27

### Added
- No-eject USB: patched esp_tinyusb managed component so host never loses the drive during file transfer (BECOMING_READY SCSI sense while VFS is locally mounted, UNIT_ATTENTION after release)
- Soft USB reconnect (`tud_disconnect`/`tud_connect`) forces host to re-mount with fresh FAT — only reliable way to surface new files through a mounted FAT cache
- Synchronous `tud_msc_write10_cb` replaces deferred single-buffer writes — fixes data loss when host sends rapid consecutive WRITE10 commands (e.g. Windows Explorer file deletion)
- FAT32 volume label writer (`sd_set_volume_label`) — cards identify as "CIED Sender" / "CIED Receiver" in OS file manager
- Managed component patch script (`patches/apply-msc-no-eject.sh`) — re-applies all esp_tinyusb changes after `idf.py fullclean`
- Windows test tools (`test_send.go`, `test_receive.go`) — auto-detect dongles by volume label, write dummy files and monitor inbox

### Changed
- WiFi upgraded from WPA2 to WPA3-SAE on both AP (receiver) and STA (sender)
- Sender preserves original filenames instead of prefixing with bay ID
- Sender creates `done/` directory before each scan to prevent rename failures on fresh cards

### Fixed
- Transfer hang: `_msc_storage_read_sector` was missing `is_fat_mounted` guard — USB host READ10 hit SDMMC concurrently with FatFS
- Files deleted via Windows Explorer reappeared with 0 bytes due to deferred write buffer being overwritten by a second WRITE10 before the first completed
- Sent files re-transferred on next BOOT press because `done/` directory did not exist on card

## [1.7.0] — 2026-10-24

### Added
- CIED Bridge Lite: serverless WiFi file relay for small clinics using two T-Dongle-S3 USB dongles
- ESP-NOW replaced by TCP/WiFi transport with SHA-256 integrity verification
- Sender: BOOT button triggers scan-and-send; files moved to `done/` after transfer
- Receiver: SoftAP + TCP server; received files written to `inbox/` folder
- APA102 LED status (green=ready, blue=transferring, red=error) and ST7735S LCD display
- SDMMC 4-bit SD card interface with automatic FAT32 formatting

## [1.6.0] — 2026-10-17

### Added
- Board-specific firmware upload: select DevKitC or T-Dongle-S3 in the hub web UI before flashing
- Node removal button (✕) to clean up stale relay entries from the node table

### Fixed
- Variable shadowing bug where loop variable overwrote version dict in firmware UI
- hostapd-station-handler crash loop and overly aggressive health monitor deauth

## [1.5.0] — 2026-10-15

### Added
- Disk space display in diagnostics tab with low-space warning (< 500 MB)
- Retry PDF conversion for PCL jobs archived before gpcl6 was available

### Changed
- Pin RT5370 USB WiFi adapter to `wlan_ap` via udev MAC rule for stable interface naming
- Ship gpcl6 binary instead of apt-get ghostpcl (not packaged in Debian 13)
- `cups-lpd -o raw` replaces `-o document-format=application/octet-stream` — fixes PJL header rendering as text page on Abbott print jobs

### Fixed
- Deploy: `which` fails in non-interactive SSH where `/usr/sbin` isn't in PATH; now uses `command -v` fallback
- Deploy: always write `cups-lpd@.service` (not only on fresh installs)
- Stop purging CUPS jobs after archiving

## [1.4.0] — 2026-10-04

### Added
- Print job archive: completed jobs stored as PDF with view/download/delete in web UI
- CDC-ACM serial delivery mode for relay → Windows bridge (KardiSynch receiver)
- KardiSynchSerialBridge: `--output` and `--port` CLI flags
- Export hub: forward ingested export files to import hub for serial bridge delivery
- Export tab in web UI: view/download/send links for data files
- ESP32 firmware detection, version display, and USB flashing from web UI
- Station health monitor: deauth unresponsive WiFi clients
- Software versioning for hub deployments
- Firmware-announced nodes shown in web UI even when offline

### Changed
- Auto-detect AP interface from hostapd.conf (wlan1 support)
- Sync system clock to DS3231 RTC during deploy
- Graceful fallback when ghostpcl is unavailable

### Fixed
- Diagnostics tab: correctly detect usblp printers
- Autoconf: load usblp kernel module for non-ipp-usb printers

## [1.3.0] — 2026-09-21

### Added
- Idempotent hub provisioner: detects fresh Raspberry Pi OS and provisions AP, CUPS, Samba, and all services; on existing hubs, updates code and configs only
- USB printer scanner and one-click transport setup in web UI
- WiFi client flush button in web UI
- Protocol-aware printer autoconf: ipp-usb for protocol 4, usblp for protocol 2

### Fixed
- Detect Kyocera/UTAX printers in diagnostics tab
- Queue status detection on German-locale systems
- Stop watchdog from deauthing live WiFi stations
- Export web UI moved to port 8080 to avoid conflict with printer UI

### Changed
- Deploy: add `country_code=DE` and disable AP power save
- Web UI auto-refresh interval: 5 s → 10 s

## [1.0.0] — 2026-09-14

### Added
- Initial release
- Pi Zero W edge node: USB printer gadget, capture daemon, forwarder with exponential backoff
- Pi 3 hub: CUPS raw queue, LPR passthrough, print-relay web UI
- Pi 4 central server: FastAPI ingest API, vendor parsers (Medtronic, Boston Scientific, Abbott, Biotronik), SQLite storage, tablet web UI
- Self-signed TLS with CA distribution to edge nodes
- Bluetooth PAN bridge for tablet access
- ipp-usb transport, fast ARP expiry, hardened watchdog
