# kardisynch-receiver

The **outbound** counterpart to the printer relay. Where the printer relay pulls
print jobs *in* from a programmer, this node pushes finished files *out* of the
server to a **Windows PC running KardiSynch**, over USB, for PCs that can't reach
the server on the network.

```
 central server            Pi3 (import hub)           receiver Pi Zero W          Windows PC
 /data/kardisynch-outbox ─▶ import-server.py  ◀─claim/fetch/ack─ presenter ─USB─▶ KardiSynch
 (outbox-to-hub.py POSTs)   durable queue                 │                        (folder watch)
                                                    HID *or* mass-storage
```

Two delivery modes, chosen by the target PC's USB policy:

| Mode | When | How | Code |
|------|------|-----|------|
| **HID** | mass-storage + USB-NIC blocked, HID allowed (e.g. **Herzzentrum Leipzig**) | 64-byte HID report stream + Windows companion app, ACK-gated | `hid/` + `windows/` (this subproject) |
| **mass-storage** | USB unrestricted (colleague's hospital) | fake USB drive, media-change cycle | **reuses [`import-relay/`](../import-relay/)** — see `mass-storage/` |

RNDIS/USB-Ethernet (doc Scenario 1a) is **not built** — no site needs it. Full
background + all three scenarios: [`../kardisynch-receiver-implementation.md`](../kardisynch-receiver-implementation.md).

## Why it reuses import-relay

The hub (`import-relay/pi3-server/import-server.py`) is a generic durable queue:
senders `POST /submit`, presenters `claim/fetch/ack`, claims are atomically
leased per node and **redelivered if left unacked**. Both presenters share the
hub client `import-relay/daemon/hub_client.py`. Only the *delivery step* differs
— HID framing vs. the mass-storage eject/inject cycle. That lease is what lets
the HID presenter safely ack **only after** the host confirms the file: a stalled
transfer is simply retried.

## Platform

**Raspberry Pi OS Lite** (Bookworm/Bullseye). `usb_f_hid` and Python are already
present, so this is the fastest path to a testable device. Porting the gadget
scripts to the Buildroot fleet is deferred until HID graduates to deployment.

## Layout

```
common/    setup-otg.sh (dwc2+libcomposite) · receiver.conf.example · set-mode.sh (hid|mass-storage)
hid/       gadget-hid.sh · hid_sender.py · two systemd units
windows/   KardiSynchHidBridge/  (C#/HidSharp companion app)
mass-storage/  README.md  (deploy notes — reuses import-relay presenter unchanged)
server-feed/   outbox-to-hub.py · outbox-to-hub.service  (feeds the hub from the outbox)
```

## Install — HID mode (on a freshly-imaged Pi Zero W)

Flash **Raspberry Pi OS Lite** with Raspberry Pi Imager and set WiFi (`relayap`)
+ SSH in its customisation. Then copy the repo to the Pi and run the installer —
it enables the dwc2 gadget, installs the payload + services, and writes the
config:

```sh
# from your workstation — copy this subproject AND import-relay (shared hub_client):
rsync -a pacemaker-bridge/{kardisynch-receiver,import-relay} pi@<pi-host>:~/

# on the Pi:
sudo bash kardisynch-receiver/install-receiver.sh hid
sudo reboot                                    # first time only (dwc2)
```

Override node/hub if needed:

```sh
sudo NODE_ID=hid-leipzig-01 HUB_URL=http://10.99.0.1:8090 \
     bash kardisynch-receiver/install-receiver.sh hid
```

If Imager did **not** set the WiFi, add `CONFIGURE_WIFI=1` (writes the `relayap`
connection — but that can drop an SSH-over-WiFi session).

Then on the Windows host build + run `windows/KardiSynchHidBridge` (see its
README) and point KardiSynch's watcher at `C:\KardiSynch\incoming`.

## Install — server feed (where /data/kardisynch-outbox lives)

```sh
sudo install -m755 server-feed/outbox-to-hub.py       /usr/local/sbin/
sudo install -m644 server-feed/outbox-to-hub.service  /etc/systemd/system/
# edit HUB_URL / TARGET_NODE in the unit if the hub isn't 10.99.0.1 or you want
# to pin this receiver (TARGET_NODE must equal the presenter's NODE_ID)
sudo systemctl enable --now outbox-to-hub
```

## Test end-to-end (bench)

```sh
# hub status
curl http://10.99.0.1:8090/                     # pending=N inflight=M

# inject a test file for a specific receiver
curl -sS -X POST http://10.99.0.1:8090/submit \
     -H "X-File-Name: test.pdf" -H "X-Target-Node: hid-leipzig-01" \
     --data-binary @test.pdf

# expect: file appears in C:\KardiSynch\incoming (CRC OK), hub goes pending->0
```

**Reliability check:** close the Windows app mid-transfer → the item stays
`inflight` → after the hub lease (`IMPORT_LEASE_SECS`, default 120 s) it requeues
and redelivers on the next poll.

**The go/no-go (Leipzig):** run the companion app on the *actual* locked-down PC;
confirm the HID device enumerates under their policy and a real interrogation file
reaches KardiSynch's folder unchanged.
