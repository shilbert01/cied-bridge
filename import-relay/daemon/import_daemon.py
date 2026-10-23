#!/usr/bin/env python3
"""
Import relay presenter — pulls files from the hub and injects them into the
USB drive the host computer reads.

Topology (hub-and-spoke, like the rest of the fleet — never edge↔edge):

    many senders ─POST /submit─▶ [import-server on Pi3] ◀─claim/fetch/ack─ THIS node
                                                                                │
                                                                          inject into
                                                                          USB drive ─▶ host READS

Why pull, not push: the presenter is plugged into some host PC in the field, so
ITS address is the moving target. Senders drop files at one stable hub address;
this node polls the hub. No pairs to configure, any number of senders, and a
sender can drop a file while this node is booting/unplugged — it's waiting on
the next poll.

Each poll cycle:
    1. POST /claim   — atomically lease up to CLAIM_MAX items to this node
    2. GET  /fetch   — download each into the spool
    3. inject        — ONE eject/inject/reattach media-change cycle for the batch
    4. POST /ack     — confirm each delivered item so the hub drops it
       (anything not acked is redelivered after the hub's lease expires)

The media-change cycle is the crux: you cannot modify the backing image while
the host has the drive mounted (stale FAT cache / corruption), so every write
is wrapped in eject -> write -> reattach so the host re-reads the filesystem.
"""

import logging
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

from hub_client import Hub

CONFIG_PATH = "/etc/cied-import/import.conf"
MOUNT_TIMEOUT = 15  # seconds


# ─────────────────────────────── config ────────────────────────────────────
def load_config(path: str) -> dict:
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip().strip('"')
    except OSError as e:
        logging.error("Cannot read config %s: %s", path, e)
    return conf


# ──────────────────────────── gadget LUN control ───────────────────────────
class Lun:
    """Wraps the configfs mass_storage LUN so we can eject/reattach the media."""

    def __init__(self, gadget_name: str, image: str):
        base = f"/sys/kernel/config/usb_gadget/{gadget_name}/functions/mass_storage.0/lun.0"
        self.file = Path(base) / "file"
        self.forced_eject = Path(base) / "forced_eject"
        self.image = image

    def eject(self):
        if self.forced_eject.exists():
            try:
                self.forced_eject.write_text("1")
                return
            except OSError as e:
                logging.warning("forced_eject failed (%s), clearing file instead", e)
        self.file.write_text("")

    def reattach(self):
        self.file.write_text(self.image)

    def is_attached(self) -> bool:
        try:
            return self.file.read_text().strip() == self.image
        except OSError:
            return False


# ─────────────────────────── image write helpers ───────────────────────────
def partition_offset(img_path: str) -> int:
    try:
        with open(img_path, "rb") as f:
            f.seek(446)
            for _ in range(4):
                start_lba = struct.unpack_from("<I", f.read(16), 8)[0]
                if start_lba > 0:
                    return start_lba * 512
    except OSError:
        pass
    return 0


def inject_files(img_path: str, files: list, subdir: str) -> list:
    """Loop-mount the image READ-WRITE and copy files in. LUN must be ejected first."""
    offset = partition_offset(img_path)
    opts = f"loop,rw,offset={offset}" if offset else "loop,rw"
    mnt = Path("/tmp") / f"pmcimp_mnt_{os.getpid()}"
    mnt.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(["mount", "-o", opts, img_path, str(mnt)],
                       capture_output=True, timeout=MOUNT_TIMEOUT)
    if r.returncode != 0:
        logging.error("rw loop-mount failed: %s", r.stderr.decode().strip())
        return []

    written = []
    try:
        dest_dir = mnt / subdir if subdir else mnt
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in files:
            src = Path(src)
            clean_name = src.name.split("__", 1)[-1] if "__" in src.name else src.name
            try:
                shutil.copy(str(src), str(dest_dir / clean_name))
                written.append(src)
                logging.info("Injected: %s -> drive:/%s%s",
                             clean_name, (subdir + "/") if subdir else "", clean_name)
            except OSError as e:
                logging.error("Failed to inject %s: %s", src.name, e)
        subprocess.run(["sync"], timeout=MOUNT_TIMEOUT)
    finally:
        subprocess.run(["umount", str(mnt)], capture_output=True, timeout=MOUNT_TIMEOUT)
        try:
            mnt.rmdir()
        except OSError:
            pass
    return written


# ─────────────────────────────── poll loop ─────────────────────────────────
def poll_loop(cfg: dict, lun: Lun, hub: Hub, stop):
    spool_dir = Path(cfg["SPOOL_DIR"])
    spool_dir.mkdir(parents=True, exist_ok=True)
    subdir = cfg.get("DRIVE_SUBDIR", "")
    poll_interval = int(cfg.get("POLL_INTERVAL", "5"))
    claim_max = int(cfg.get("CLAIM_MAX", "10"))

    logging.info("Presenter %s polling hub %s every %ds",
                 hub.node, hub.base, poll_interval)

    while not stop():
        time.sleep(poll_interval)

        items = hub.claim(claim_max)
        if not items:
            continue
        logging.info("Claimed %d item(s)", len(items))

        # 1. Fetch every claimed item into the spool. Map spool path -> item id
        #    so we only ack the ones that actually make it onto the drive.
        fetched = {}  # spool_path -> item_id
        for it in items:
            spool_path = spool_dir / f"{it['id']}__{it['name']}"
            if hub.fetch(it["id"], spool_path):
                fetched[spool_path] = it["id"]

        if not fetched:
            continue

        # 2. ONE media-change cycle for the whole batch.
        try:
            lun.eject()
            time.sleep(1)  # let the host register medium removal
            written = inject_files(cfg["DISK_IMAGE"], list(fetched.keys()), subdir)
        finally:
            lun.reattach()  # always put the media back, even on failure

        # 3. Ack only what landed on the drive; clean up spool. Unacked items
        #    are redelivered after the hub's lease expires.
        for spool_path in written:
            item_id = fetched[spool_path]
            if hub.ack(item_id):
                spool_path.unlink(missing_ok=True)
        logging.info("Delivered %d/%d item(s); host will see media change",
                     len(written), len(items))


# ─────────────────────────────────── main ──────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    cfg = load_config(CONFIG_PATH)

    img = cfg.get("DISK_IMAGE", "")
    if not img or not Path(img).exists():
        logging.error("DISK_IMAGE missing (%s) — run create-storage-image.sh first", img)
        sys.exit(1)

    node_id = cfg.get("NODE_ID", "import-01")
    hub_url = cfg.get("IMPORT_HUB_URL", "http://10.99.0.1:8090")

    lun = Lun(cfg.get("GADGET_NAME", "import_gadget"), img)
    if not lun.is_attached():
        logging.warning("Gadget LUN not attached to %s — is setup-gadget.sh run?", img)

    hub = Hub(hub_url, node_id)

    stopped = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__("v", True))

    logging.info("Import daemon started — image: %s  hub: %s  node: %s",
                 img, hub_url, node_id)
    try:
        poll_loop(cfg, lun, hub, lambda: stopped["v"])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
