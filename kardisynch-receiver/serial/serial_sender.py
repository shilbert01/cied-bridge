#!/usr/bin/env python3
"""
KardiSynch serial presenter — pulls files from the import hub and sends them
to a Windows host over a USB CDC-ACM serial link (/dev/ttyGS0 ↔ COMx).

Sibling of hid_sender.py: same hub integration, different transport.

Wire format — length-prefixed frames over the serial line:
    [SYNC 2B][TYPE 1B][LENGTH 4B LE][PAYLOAD …][CRC32 4B LE]

  SYNC     : 0x4B 0x53 ("KS")
  Types:
    0x01 FILE_START  payload: size(u32 LE) + crc32(u32 LE) + filename(UTF-8)
    0x02 FILE_DATA   payload: chunk bytes (up to 4096)
    0x03 FILE_END    payload: crc32(u32 LE)
    0x05 ACK         payload: status(1B, 1=OK 0=fail) + crc32(u32 LE)

ACK is sent by the host companion; the Pi waits for it before acking the hub.
"""

import logging
import os
import signal
import struct
import sys
import termios
import time
import tty
import zlib
from pathlib import Path

from hub_client import Hub

CONFIG_PATH = "/etc/kardisynch-receiver/receiver.conf"

SYNC = b"KS"
PKT_FILE_START = 0x01
PKT_FILE_DATA = 0x02
PKT_FILE_END = 0x03
PKT_ACK = 0x05

CHUNK_SIZE = 4096
ACK_SIZE = 5  # 1 status + 4 crc


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


def frame_crc(pkt_type: int, payload: bytes) -> int:
    return zlib.crc32(struct.pack("<BBI", SYNC[0], SYNC[1], pkt_type) + payload) & 0xFFFFFFFF


def write_all(fd, data: bytes):
    mv = memoryview(data)
    off = 0
    while off < len(data):
        try:
            n = os.write(fd, mv[off:])
        except BlockingIOError:
            import select
            select.select([], [fd], [], 0.1)
            continue
        off += n


def send_frame(fd, pkt_type: int, payload: bytes):
    length = len(payload)
    header = SYNC + struct.pack("<BI", pkt_type, length)
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    write_all(fd, header + payload + struct.pack("<I", crc))


def read_exact(fd, n: int, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"read_exact: got {len(buf)}/{n} bytes")
        try:
            chunk = os.read(fd, n - len(buf))
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if not chunk:
            time.sleep(0.01)
            continue
        buf += chunk
    return buf


def wait_for_ack(fd, expected_crc: int, timeout: float) -> bool:
    """Read frames looking for a matching ACK."""
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if not chunk:
            time.sleep(0.01)
            continue
        buf += chunk

        while len(buf) >= 2:
            idx = buf.find(SYNC)
            if idx < 0:
                buf = buf[-1:]
                break
            if idx > 0:
                buf = buf[idx:]
            # need header: SYNC(2) + TYPE(1) + LENGTH(4) = 7
            if len(buf) < 7:
                break
            pkt_type = buf[2]
            length = struct.unpack_from("<I", buf, 3)[0]
            frame_end = 7 + length + 4
            if len(buf) < frame_end:
                break
            payload = buf[7:7 + length]
            crc_recv = struct.unpack_from("<I", buf, 7 + length)[0]
            buf = buf[frame_end:]

            crc_calc = zlib.crc32(payload) & 0xFFFFFFFF
            if pkt_type != PKT_ACK or crc_recv != crc_calc:
                continue
            if len(payload) < 5:
                continue
            status = payload[0]
            ack_crc = struct.unpack_from("<I", payload, 1)[0]
            if status == 1 and ack_crc == expected_crc:
                return True
    return False


def send_file(fd, path: Path, ack_timeout: float) -> bool:
    data = path.read_bytes()
    size = len(data)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    name = path.name.split("__", 1)[-1]

    logging.info("Serial send: %s (%d bytes, crc=%08x)", name, size, crc)

    # FILE_START
    fname_bytes = name.encode("utf-8")
    send_frame(fd, PKT_FILE_START,
               struct.pack("<II", size, crc) + fname_bytes)

    # FILE_DATA chunks
    off = 0
    while off < size:
        chunk = data[off:off + CHUNK_SIZE]
        send_frame(fd, PKT_FILE_DATA, chunk)
        off += CHUNK_SIZE

    # FILE_END
    send_frame(fd, PKT_FILE_END, struct.pack("<I", crc))

    scaled_timeout = max(ack_timeout, 10 + size / 200_000)
    ok = wait_for_ack(fd, crc, scaled_timeout)
    logging.info("Serial send %s: host ACK=%s (timeout=%.0fs)", name, "OK" if ok else "NONE", scaled_timeout)
    return ok


def poll_loop(cfg: dict, hub: Hub, stop):
    spool_dir = Path(cfg.get("SPOOL_DIR", "/var/lib/kardisynch-receiver/spool"))
    spool_dir.mkdir(parents=True, exist_ok=True)
    serial_device = cfg.get("SERIAL_DEVICE", "/dev/ttyGS0")
    poll_interval = int(cfg.get("POLL_INTERVAL", "5"))
    claim_max = int(cfg.get("CLAIM_MAX", "10"))
    ack_timeout = float(cfg.get("ACK_TIMEOUT", "10"))

    logging.info("Serial presenter %s polling hub %s every %ds -> %s",
                 hub.node, hub.base, poll_interval, serial_device)

    while not stop():
        time.sleep(poll_interval)

        items = hub.claim(claim_max)
        if not items:
            continue
        logging.info("Claimed %d item(s)", len(items))

        fetched = {}
        for it in items:
            spool_path = spool_dir / f"{it['id']}__{it['name']}"
            if hub.fetch(it["id"], spool_path):
                fetched[spool_path] = it["id"]
        if not fetched:
            continue

        try:
            fd = os.open(serial_device, os.O_RDWR | os.O_NONBLOCK)
            tty.setraw(fd)
        except OSError as e:
            logging.error("cannot open %s (%s) — is gadget-serial.sh run?", serial_device, e)
            continue

        try:
            for spool_path, item_id in fetched.items():
                try:
                    delivered = send_file(fd, spool_path, ack_timeout)
                except (TimeoutError, OSError) as e:
                    logging.warning("delivery of %s failed: %s (will retry)", spool_path.name, e)
                    delivered = False
                if delivered and hub.ack(item_id):
                    spool_path.unlink(missing_ok=True)
                else:
                    logging.warning("kept %s pending", spool_path.name)
        finally:
            os.close(fd)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    cfg = load_config(CONFIG_PATH)

    node_id = cfg.get("NODE_ID", "serial-01")
    hub_url = cfg.get("IMPORT_HUB_URL", "http://10.99.0.1:8090")
    hub = Hub(hub_url, node_id)

    serial_device = cfg.get("SERIAL_DEVICE", "/dev/ttyGS0")
    if not Path(serial_device).exists():
        logging.warning("%s missing — run gadget-serial.sh first", serial_device)

    stopped = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__("v", True))

    logging.info("Serial sender started — hub: %s  node: %s  serial: %s",
                 hub_url, node_id, serial_device)
    try:
        poll_loop(cfg, hub, lambda: stopped["v"])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
