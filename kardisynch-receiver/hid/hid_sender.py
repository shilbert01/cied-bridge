#!/usr/bin/env python3
"""
KardiSynch HID presenter — pulls files from the import hub and streams them to a
locked-down Windows host over a USB HID pipe, where a companion app reassembles
them into KardiSynch's watched folder.

This is a sibling of import-relay's import_daemon.py: it reuses the SAME hub
client (claim/fetch/ack) and only swaps the delivery step — HID framing instead
of the mass-storage eject/inject cycle.

    many senders ─POST /submit─▶ [import hub, Pi3] ◀─claim/fetch/ack─ THIS node
                                                                           │
                                                                    /dev/hidg0
                                                                           ▼
                                                            Windows companion app ─▶ KardiSynch

Reliability: the hub leases claims and redelivers anything left unacked. We
therefore ack the hub ONLY after the host companion app confirms the file with a
CRC-matching ACK output report. A transfer that stalls (app not running, cable
pulled) is never acked, so the hub redelivers it after its lease expires.

Wire format — 64-byte reports, no HID Report ID (see gadget-hid.sh):
    byte 0    : 0x01 constant marker
    byte 1    : packet type
    byte 2-3  : sequence number (uint16 LE)
    byte 4-63 : payload (60 bytes)

  FILE_START payload: size(u32 LE), crc32(u32 LE), filename(null-term UTF-8 ≤52)
  FILE_DATA  payload: up to 60 bytes of file content
  FILE_END   payload: crc32(u32 LE)
  ACK (host->device OUTPUT report): status(1 byte, 1=OK 0=fail), crc32(u32 LE)
"""

import logging
import os
import select
import signal
import struct
import sys
import time
import zlib
from pathlib import Path

# Shared with import-relay's presenter; both are installed side by side in
# /usr/local/sbin/ (see kardisynch-receiver/README.md → install).
from hub_client import Hub

CONFIG_PATH = "/etc/kardisynch-receiver/receiver.conf"

REPORT_SIZE = 64
PAYLOAD_SIZE = 60  # 64 - 4 header bytes
MARKER = 0x01

PKT_FILE_START = 0x01
PKT_FILE_DATA = 0x02
PKT_FILE_END = 0x03
PKT_HEARTBEAT = 0x04
PKT_ACK = 0x05  # host -> device, in an OUTPUT report

WRITE_TIMEOUT = 5.0  # seconds to wait for the IN endpoint to drain one report


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


# ─────────────────────────── HID report framing ────────────────────────────
def make_report(pkt_type: int, seq: int, payload: bytes) -> bytes:
    header = struct.pack("<BBH", MARKER, pkt_type, seq & 0xFFFF)
    data = payload[:PAYLOAD_SIZE].ljust(PAYLOAD_SIZE, b"\x00")
    return header + data


def write_report(fd: int, report: bytes, send_delay: float):
    """Send one 64-byte INPUT report, waiting (bounded) for the EP to be writable."""
    r, w, _ = select.select([], [fd], [], WRITE_TIMEOUT)
    if not w:
        raise TimeoutError("HID IN endpoint not draining (host not polling?)")
    os.write(fd, report)
    if send_delay:
        time.sleep(send_delay)


def wait_for_ack(fd: int, expected_crc: int, timeout: float) -> bool:
    """Wait for the companion app's ACK OUTPUT report; True iff status OK + CRC matches."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            return False
        try:
            rep = os.read(fd, REPORT_SIZE)
        except OSError as e:
            logging.warning("ACK read failed: %s", e)
            return False
        if len(rep) < 9 or rep[1] != PKT_ACK:
            continue  # stray/heartbeat report — keep waiting within the deadline
        status = rep[4]
        ack_crc = struct.unpack_from("<I", rep, 5)[0]
        if status == 1 and ack_crc == expected_crc:
            return True
        # A negative or stale ACK (e.g. a late one for a prior file) must not end
        # this wait — keep listening until the matching ACK or the deadline, so we
        # never falsely mark this file failed and trigger a spurious redelivery.
        continue


def send_file(fd: int, path: Path, send_delay: float, ack_timeout: float) -> bool:
    """Stream one file over HID and return True only if the host ACKs it CRC-OK."""
    data = path.read_bytes()
    size = len(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    # Strip the hub's "<id>__" spool prefix so the host sees the real name.
    name = path.name.split("__", 1)[-1]
    fname = name.encode("utf-8")[:52]

    logging.info("HID send: %s (%d bytes, crc=%08x)", name, size, crc)

    seq = 0
    write_report(fd, make_report(PKT_FILE_START, seq,
                                 struct.pack("<II", size, crc) + fname + b"\x00"),
                 send_delay)
    seq += 1

    off = 0
    while off < size:
        chunk = data[off:off + PAYLOAD_SIZE]
        write_report(fd, make_report(PKT_FILE_DATA, seq, chunk), send_delay)
        seq += 1
        off += PAYLOAD_SIZE

    write_report(fd, make_report(PKT_FILE_END, seq, struct.pack("<I", crc)), send_delay)

    ok = wait_for_ack(fd, crc, ack_timeout)
    logging.info("HID send %s: %d reports, host ACK=%s", name, seq + 1, "OK" if ok else "NONE")
    return ok


# ─────────────────────────────── poll loop ─────────────────────────────────
def poll_loop(cfg: dict, hub: Hub, stop):
    spool_dir = Path(cfg.get("SPOOL_DIR", "/var/lib/kardisynch-receiver/spool"))
    spool_dir.mkdir(parents=True, exist_ok=True)
    hid_device = cfg.get("HID_DEVICE", "/dev/hidg0")
    poll_interval = int(cfg.get("POLL_INTERVAL", "5"))
    claim_max = int(cfg.get("CLAIM_MAX", "10"))
    send_delay = float(cfg.get("SEND_DELAY", "0.001"))
    ack_timeout = float(cfg.get("ACK_TIMEOUT", "10"))

    logging.info("HID presenter %s polling hub %s every %ds -> %s",
                 hub.node, hub.base, poll_interval, hid_device)

    while not stop():
        time.sleep(poll_interval)

        items = hub.claim(claim_max)
        if not items:
            continue
        logging.info("Claimed %d item(s)", len(items))

        # Fetch into the spool first, mapping spool path -> item id.
        fetched = {}
        for it in items:
            spool_path = spool_dir / f"{it['id']}__{it['name']}"
            if hub.fetch(it["id"], spool_path):
                fetched[spool_path] = it["id"]
        if not fetched:
            continue

        # Open the HID pipe once for the batch. O_RDWR: write IN reports, read the
        # host's OUT (ACK) reports. Non-blocking so select() governs all waits.
        try:
            fd = os.open(hid_device, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            logging.error("cannot open %s (%s) — is gadget-hid.sh run?", hid_device, e)
            continue  # items stay unacked; hub redelivers

        try:
            for spool_path, item_id in fetched.items():
                try:
                    delivered = send_file(fd, spool_path, send_delay, ack_timeout)
                except (TimeoutError, OSError) as e:
                    logging.warning("delivery of %s failed: %s (will retry)", spool_path.name, e)
                    delivered = False
                if delivered and hub.ack(item_id):
                    spool_path.unlink(missing_ok=True)
                else:
                    # leave the spool file; hub redelivers after lease expiry
                    logging.warning("kept %s pending", spool_path.name)
        finally:
            os.close(fd)


# ─────────────────────────────────── main ──────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    cfg = load_config(CONFIG_PATH)

    node_id = cfg.get("NODE_ID", "hid-01")
    hub_url = cfg.get("IMPORT_HUB_URL", "http://10.99.0.1:8090")
    hub = Hub(hub_url, node_id)

    hid_device = cfg.get("HID_DEVICE", "/dev/hidg0")
    if not Path(hid_device).exists():
        logging.warning("%s missing — run gadget-hid.sh (gadget not up yet?)", hid_device)

    stopped = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__("v", True))

    logging.info("HID sender started — hub: %s  node: %s  hid: %s",
                 hub_url, node_id, hid_device)
    try:
        poll_loop(cfg, hub, lambda: stopped["v"])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
