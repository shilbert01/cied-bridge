# Mass-storage mode — reuse `import-relay/` unchanged

For sites where the host PC's USB ports are **unrestricted** (e.g. a colleague's
hospital), the receiver presents a **fake USB drive** and the host reads files
off it. That is exactly what the [`import-relay/`](../../import-relay/) subproject
already does — a durable hub queue + a presenter that runs one
eject → loop-mount → copy → `sync` → reattach media-change cycle per batch.

**There is no separate code here.** This mode is the import-relay presenter,
driven by the same hub and the same server feed as HID mode. `common/set-mode.sh
mass-storage` enables the import-relay units (`usb-import-gadget`,
`import-daemon`) instead of the HID ones.

## Deploy

1. **Hub (Pi3)** — once, shared with HID mode:
   ```sh
   cd ../../import-relay
   install -m755 pi3-server/import-server.py      /usr/local/sbin/
   install -m644 pi3-server/import-server.service /etc/systemd/system/
   systemctl enable --now import-server
   ```

2. **Presenter (Pi Zero)** — the import-relay presenter (note `hub_client.py`
   ships alongside `import_daemon.py`):
   ```sh
   cd ../../import-relay
   install -m600 config/import.conf.example      /etc/cied-import/import.conf  # then edit
   install -m755 gadget/create-storage-image.sh  /usr/local/sbin/
   install -m755 gadget/setup-gadget.sh          /usr/local/sbin/setup-gadget-import.sh
   install -m755 daemon/import_daemon.py          /usr/local/sbin/
   install -m644 daemon/hub_client.py             /usr/local/sbin/
   install -m644 systemd/*.service                /etc/systemd/system/
   ```
   Then select the mode:
   ```sh
   ../common/set-mode.sh mass-storage
   ```

3. **Server feed** — same forwarder as HID mode; it doesn't care which presenter
   drains the hub:
   ```sh
   install -m755 ../server-feed/outbox-to-hub.py /usr/local/sbin/
   install -m644 ../server-feed/outbox-to-hub.service /etc/systemd/system/
   systemctl enable --now outbox-to-hub
   ```

## Not yet hardware-tested

The import-relay README flags that its **gadget half was never validated on real
hardware**. Before quoting this at a site, do one real pass: submit a file to the
hub, confirm the media-change cycle makes the host re-read the drive, and that
KardiSynch on the host picks the file up. The eject blip briefly removes the
drive — fine for "drop a file, host picks it up," disruptive mid-read.

See [`../../import-relay/README.md`](../../import-relay/README.md) for the full
design (why hub-not-P2P, why the media-change cycle, caveats to confirm with the
client re: USB lockdown).
