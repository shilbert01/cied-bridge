# KardiSynch HID Bridge — WebHID (no-install companion)

A single self-contained web page (`index.html`) that does what the native
`../windows/KardiSynchHidBridge` app does, but with **zero install** — it runs in
Edge/Chrome using the browser's **WebHID** + **File System Access** APIs. This is
the companion for sites where nothing can be installed on the PC (e.g. Herzzentrum
Leipzig).

It reassembles the file stream from the Pi Zero HID receiver
(`../hid/hid_sender.py`) and writes each file into a folder KardiSynch watches,
sending the ACK the Pi waits for (unacked files are redelivered by the hub).

**Validated end-to-end** (Aug 2026, Chromium on Linux): hub → Pi Zero HID gadget →
41 HID reports → this page → KardiSynch import folder, SHA-256 byte-identical.

## Use it (Windows target — Edge)

1. Copy `index.html` to the PC (a file, not an install) and open it in **Edge**.
   Opening via `file://` works (Edge treats it as a secure context, which WebHID
   requires). If a policy blocks `file://`, serve it from `http://localhost` (any
   static server) — localhost is also a secure context. **Plain `http://` on a LAN
   IP will NOT work** (not a secure context → no WebHID).
2. Click **Connect HID device** → pick **KardiSynch** (`1D6B:0104`).
3. Click **Choose output folder** → select KardiSynch's watch/import folder, allow
   read/write. (Chrome/Edge can remember this grant across sessions.)
4. Leave the tab open. Files arrive automatically; the log shows `saved … (CRC OK)`.

## Requirements & the real go/no-go

- **Chromium-family browser** (Edge/Chrome). Not Firefox/Safari. Edge ships with
  Windows, so usually fine.
- **The unknown to test on the actual PC:** a locked-down machine can disable
  **WebHID** (`DefaultWebHidGuardSetting`) and/or **File System Access**
  (`DefaultFileSystemWriteGuardSetting`) by enterprise policy. If installs are
  blocked, these might be too — verify early. If WebHID is policy-blocked, no web
  page can work around it.
- A couple of permission clicks per session (device + folder); folder permission
  can persist.

## Critical: HID usage page must be vendor-defined (not FIDO)

The gadget's report descriptor (`../hid/gadget-hid.sh`) uses **usage page 0xFF00**
(vendor-defined). Do **not** use `0xF1D0` — that's the FIDO/security-key page, which
WebHID **blocks**, making the device invisible to the browser (a native HID app
wouldn't notice, which is how the original descriptor slipped through).

## Linux note (bench testing only)

On Linux, Chrome reaches HID via `/dev/hidraw*`, which is root-only by default, so a
udev rule is needed to grant your user access:

```
KERNEL=="hidraw*", ATTRS{idVendor}=="1d6b", ATTRS{idProduct}=="0104", MODE="0660", GROUP="<your-group>"
```

then `sudo udevadm control --reload-rules && sudo udevadm trigger --action=add --subsystem-match=hidraw`.
**Windows needs none of this** — HID devices are user-accessible there.

## Protocol

Identical to the native app / `hid_sender.py`: 64-byte reports
`FILE_START → FILE_DATA* → FILE_END`, then a `PKT_ACK` (0x05) output report with
`status` + CRC32 (IEEE/reflected, == Python `zlib.crc32`). Files are written via
`createWritable()`, which swaps in atomically on close so KardiSynch never sees a
partial file.
