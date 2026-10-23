# import-relay

Presents a **USB thumb drive to a host computer** and fills it with files that
**many senders** drop at a central hub. The host only ever **reads** the drive.

This is the mirror image of [`export-relay`](../export-relay/) (host writes →
relay forwards) and uses the same **hub-and-spoke** topology as the rest of the
fleet — never edge↔edge.

```
  sender A ─┐
  sender B ─┤ POST /submit ─▶ [import-server on Pi3] ◀─ claim/fetch/ack ─ presenter (Pi Zero)
  sender C ─┘                     durable queue                                │
                                                                         inject into
                                                                         USB drive ─▶ host READS
```

## Why a hub, not peer-to-peer

The presenter is plugged into some host PC in the field, so **its** address is
the moving target. If senders had to push straight to it you'd be configuring
N sender↔presenter pairs by hand — fragile, and clients get it wrong. Instead:

- Senders POST to **one stable hub address**. They don't know or care which
  presenter exists.
- Presenters **pull** from the hub. Any number of senders, zero pairing.
- A sender can drop a file while the presenter is booting (~35s) or unplugged —
  it waits in the queue for the next poll.
- Claims are atomic and leased, so multiple presenters never double-deliver, and
  a presenter that dies mid-delivery has its items redelivered.

## Why the media-change cycle (the hard part)

You cannot modify the backing image while the host has the drive mounted — the
host caches the FAT and will show stale data or corrupt the filesystem. So every
write is wrapped in:

1. **eject**    — detach the backing file (`forced_eject`, else clear `lun.0/file`)
2. **inject**   — loop-mount the image read-write, copy the files, `sync`
3. **reattach** — repoint the LUN; the host sees a media-change and re-reads the FAT

A whole claimed batch goes in one cycle. `reattach` runs in a `finally`, so the
drive always comes back even if a copy fails.

## Files

| Path | Runs on | Role |
|------|---------|------|
| `pi3-server/import-server.py` | Pi3 (hub) | Durable queue: `/submit`, `/claim`, `/fetch`, `/ack` |
| `pi3-server/import-server.service` | Pi3 | systemd unit for the hub |
| `gadget/create-storage-image.sh` | Pi Zero | One-time: MBR + FAT32 backing image |
| `gadget/setup-gadget.sh` | Pi Zero | Bring up the mass-storage gadget |
| `daemon/import_daemon.py` | Pi Zero | Pull from hub + eject/inject/reattach cycle |
| `daemon/hub_client.py` | Pi Zero | Shared hub client (claim/fetch/ack); also used by kardisynch-receiver |
| `systemd/usb-import-gadget.service` | Pi Zero | Create image + gadget at boot |
| `systemd/import-daemon.service` | Pi Zero | Run the presenter daemon |
| `config/import.conf.example` | Pi Zero | Copy to `/etc/cied-import/import.conf` |

## Install — hub (Pi3)

```sh
install -m755 pi3-server/import-server.py      /usr/local/sbin/
install -m644 pi3-server/import-server.service /etc/systemd/system/
systemctl enable --now import-server
```

## Install — presenter (Pi Zero, mirrors export-relay)

```sh
install -m600 config/import.conf.example      /etc/cied-import/import.conf   # then edit
install -m755 gadget/create-storage-image.sh  /usr/local/sbin/
install -m755 gadget/setup-gadget.sh          /usr/local/sbin/setup-gadget-import.sh
install -m755 daemon/import_daemon.py          /usr/local/sbin/
install -m644 daemon/hub_client.py             /usr/local/sbin/   # shared hub client (imported by import_daemon.py)
install -m644 systemd/*.service                /etc/systemd/system/
systemctl enable --now usb-import-gadget import-daemon
```

Requires `dtoverlay=dwc2` in `config.txt` (already set for the printer/export relays).
No new Buildroot packages: uses `mount -o loop,rw` + `dosfstools`, already present.

## Sending a file (any sender → hub)

```sh
# to any presenter:
curl -sS -X POST "http://10.99.0.1:8090/submit" \
     -H "X-File-Name: report.pdf" --data-binary @report.pdf

# addressed to a specific presenter (optional):
curl -sS -X POST "http://10.99.0.1:8090/submit" \
     -H "X-File-Name: report.pdf" -H "X-Target-Node: import-01" --data-binary @report.pdf
```

Hub status: `curl http://10.99.0.1:8090/`  →  `pending=N inflight=M`

## Caveats to confirm with the client before deploying

- **USB storage lockdown.** Many clinical/hospital host PCs block USB
  mass-storage at the endpoint (BadUSB / DLP policy) even where USB *printers*
  are allowed. Verify on the *actual* target host before quoting.
- **Read-only host assumed.** The host must not write to this drive. If it also
  needs to write back, that's a two-master FAT and a different (messier) design.
- **Eject blip.** The media-change cycle briefly removes the drive. Fine for a
  "drop a file, host picks it up" workflow; disruptive if the host is mid-read.
- **Boot latency.** A real thumb drive is instant; a Pi Zero takes ~35s to boot
  before the drive appears. Keep it powered, or set expectations.
- **Platform.** Pi Zero (dwc2) only. ESP32-S3/Pico would need SD + FatFs and a
  hand-rolled media-change, which is significantly harder.
