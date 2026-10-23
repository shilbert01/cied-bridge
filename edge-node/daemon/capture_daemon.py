#!/usr/bin/env python3
"""
Reads raw print jobs from the USB printer gadget device node.
Each host open→write→close cycle is one job. Writes atomically:
  <queue>/<node>_<timestamp>_<uuid>.ps.partial  →  rename to .ps on job complete.
"""

import os
import select
import sys
import time
import uuid
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH      = "/etc/cied-edge/node.conf"
READ_CHUNK       = 4096
OPEN_RETRY_DELAY = 2    # seconds between retries when gadget device is not yet ready
JOB_IDLE_TIMEOUT = 5.0  # seconds of silence after last byte before finalising job

# IEEE-1284 printer status: Selected | No-Error (printer online and idle)
PRINTER_STATUS_IDLE = bytes([0x18])


def load_config(path: str) -> dict:
    conf = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip()
    return conf


def open_gadget(device_path: str) -> int:
    """Blocking open with retry — gadget device may appear briefly after systemd starts us.
    O_RDWR required: CUPS holds the connection open waiting for a printer status byte;
    without write access we can never unblock it."""
    while True:
        try:
            return os.open(device_path, os.O_RDWR)
        except OSError as e:
            logging.warning("Cannot open %s (%s), retrying in %ds", device_path, e, OPEN_RETRY_DELAY)
            time.sleep(OPEN_RETRY_DELAY)


def capture_loop(device_path: str, queue_dir: Path, node_id: str):
    logging.info("Capture daemon started — device: %s  queue: %s", device_path, queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)

    while True:
        fd = open_gadget(device_path)
        ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        job = f"{node_id}_{ts}_{uuid.uuid4().hex}"
        partial = queue_dir / f"{job}.ps.partial"
        final   = queue_dir / f"{job}.ps"
        bytes_received = 0

        logging.info("Job started: %s", job)

        try:
            with open(partial, "wb") as out:
                while True:
                    ready, _, _ = select.select([fd], [], [], JOB_IDLE_TIMEOUT)
                    if not ready:
                        # Timeout — CUPS is holding the connection open waiting for status.
                        # Treat inactivity as end-of-job and signal printer idle.
                        out.flush()
                        os.fsync(out.fileno())
                        try:
                            os.write(fd, PRINTER_STATUS_IDLE)
                        except OSError:
                            pass
                        logging.info("Job idle timeout — finalising (%d bytes)", bytes_received)
                        break

                    try:
                        chunk = os.read(fd, READ_CHUNK)
                    except OSError as e:
                        logging.warning("Read error on job %s: %s — discarding partial", job, e)
                        bytes_received = 0
                        break

                    if not chunk:
                        # Clean EOF from host
                        out.flush()
                        os.fsync(out.fileno())
                        break

                    out.write(chunk)
                    bytes_received += len(chunk)

        finally:
            os.close(fd)

        if bytes_received == 0:
            logging.info("Empty job (host connected then disconnected with no data) — skipping")
            partial.unlink(missing_ok=True)
            continue

        # Atomic publish: rename is atomic at the filesystem level
        partial.rename(final)
        logging.info("Job complete: %s  (%d bytes)", final.name, bytes_received)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    conf = load_config(CONFIG_PATH)
    device_path = conf["GADGET_DEVICE"]
    queue_dir   = Path(conf["CAPTURE_DIR"])
    node_id     = conf["NODE_ID"]

    # Let systemd kill us cleanly on SIGTERM
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        capture_loop(device_path, queue_dir, node_id)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
