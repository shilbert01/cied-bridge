#!/usr/bin/env python3
"""
Printer relay daemon — reads raw PostScript from the USB gadget printer device
and submits each job to the Pi3 CUPS print server via LPR (RFC 1179).

Each host open→write→close cycle is one job.  Jobs are written to a local queue
first so they survive a transient LPR failure.  The daemon retries with
exponential back-off until LPR_GIVEUP_SECS is exceeded, then drops the job.
"""

import fcntl
import os
import select
import socket
import struct
import sys
import time
import uuid
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH      = "/etc/cied-relay/relay.conf"
READ_CHUNK       = 4096
OPEN_RETRY_DELAY = 2     # seconds between retries when gadget device is not ready
JOB_IDLE_TIMEOUT = 5.0   # seconds of gadget silence before finalising a job

# ioctl to set the IEEE-1284 status byte returned for GET_PORT_STATUS.
# _IOW('g', 0x22, unsigned char) — see include/uapi/linux/usb/g_printer.h
_GADGET_SET_PRINTER_STATUS = 0x40016722
_PRINTER_STATUS_READY      = 0x18  # Selected (bit4) | Not-Error (bit3)


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


# ── LPR (RFC 1179) ────────────────────────────────────────────────────────────

def _lpr_recv_ack(sock: socket.socket) -> None:
    ack = sock.recv(1)
    if ack != b"\x00":
        raise OSError(f"LPR: non-zero ack {ack!r}")


def lpr_send(host: str, port: int, queue: str, data: bytes, job_name: str) -> None:
    """Submit one print job to a CUPS/lpd server via RFC 1179 LPR."""
    hostname = "relay"
    job_id   = "001"
    data_fn  = f"dfA{job_id}{hostname}"
    ctrl_fn  = f"cfA{job_id}{hostname}"

    ctrl = (
        f"H{hostname}\n"
        f"P root\n"
        f"N{job_name}\n"
        f"f{data_fn}\n"   # 'f' = formatted file (PostScript)
    ).encode()

    with socket.create_connection((host, port), timeout=30) as s:
        s.send(b"\x02" + queue.encode() + b"\n")
        _lpr_recv_ack(s)

        s.send(f"\x03{len(data)} {data_fn}\n".encode())
        _lpr_recv_ack(s)
        s.sendall(data)
        s.send(b"\x00")
        _lpr_recv_ack(s)

        s.send(f"\x02{len(ctrl)} {ctrl_fn}\n".encode())
        _lpr_recv_ack(s)
        s.sendall(ctrl)
        s.send(b"\x00")
        _lpr_recv_ack(s)


def _rewrite_pcl_paper_source(data: bytes, source: int = 7) -> bytes:
    """Rewrite PCL5 paper-source commands (ESC&l<N>H/h) to <source>.
    Source 7 = auto-select."""
    import re
    val = str(source).encode()
    return re.sub(rb'\x1b&l(\d+)([Hh])',
                  lambda m: b'\x1b&l' + val + m.group(2),
                  data)


def _rewrite_pjl_media_source(data: bytes, tray: str = "TRAY2") -> bytes:
    """Inject PJL MEDIASOURCE into PJL-wrapped jobs (PostScript, PCL XL).
    Inserts before @PJL ENTER LANGUAGE so it overrides the programmer's
    tray selection without touching the document body."""
    marker = b'@PJL ENTER LANGUAGE'
    if not data.lstrip().startswith(b'%-12345X') or marker not in data:
        return data
    inject = f'@PJL SET MEDIASOURCE={tray}\n'.encode()
    return data.replace(marker, inject + marker, 1)


def _rewrite_paper_source(data: bytes, mediasource: str) -> bytes:
    if not mediasource:
        return data
    data = _rewrite_pjl_media_source(data, mediasource)
    data = _rewrite_pcl_paper_source(data)
    return data


def deliver_with_retry(job_path: Path, conf: dict) -> bool:
    """Try to LPR-deliver job_path.  Returns True on success, False on give-up."""
    host       = conf["PRINT_SERVER_HOST"]
    port       = int(conf.get("PRINT_SERVER_PORT", "515"))
    queue      = conf["PRINT_SERVER_QUEUE"]
    giveup     = float(conf.get("LPR_GIVEUP_SECS", "300"))
    backoff    = 5.0
    deadline   = time.monotonic() + giveup
    data       = _rewrite_paper_source(job_path.read_bytes(), conf.get("MEDIASOURCE", ""))

    while True:
        try:
            lpr_send(host, port, queue, data, job_path.stem)
            return True
        except Exception as e:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logging.error("Giving up on %s after %.0fs: %s", job_path.name, giveup, e)
                return False
            wait = min(backoff, remaining)
            logging.warning("LPR delivery failed (%s) — retry in %.0fs", e, wait)
            time.sleep(wait)
            backoff = min(backoff * 2, 60)


# ── USB gadget capture ────────────────────────────────────────────────────────

def open_gadget(device_path: str) -> int:
    while True:
        try:
            return os.open(device_path, os.O_RDWR)
        except OSError as e:
            logging.warning("Cannot open %s (%s), retrying in %ds",
                            device_path, e, OPEN_RETRY_DELAY)
            time.sleep(OPEN_RETRY_DELAY)


def _effective_node_id(conf: dict) -> str:
    """Unique per-unit node id for job attribution in a fleet.

    Prefer a site-specific NODE_ID from relay.conf, but fall back to the
    hostname when it's still the shipped default — S40wifi now sets the hostname
    to a unique 'relay-pizero-XXXXXX' (from the MAC), so every node self-tags
    without per-unit config. Kept as the LPR job-name prefix so the Pi3 can tell
    which relay a job came from.
    """
    configured = conf.get("NODE_ID", "").strip()
    if configured and configured not in ("relay-01", "relay-pizero", "relay"):
        return configured
    import socket
    return socket.gethostname()


def capture_and_relay(conf: dict) -> None:
    device_path = conf["GADGET_DEVICE"]
    queue_dir   = Path(conf["QUEUE_DIR"])
    sent_dir    = Path(conf["SENT_DIR"])
    node_id     = _effective_node_id(conf)
    logging.info("Node id: %s", node_id)

    queue_dir.mkdir(parents=True, exist_ok=True)
    sent_dir.mkdir(parents=True, exist_ok=True)

    logging.info("Relay daemon started — gadget: %s  print server: %s:%s/%s",
                 device_path,
                 conf["PRINT_SERVER_HOST"],
                 conf.get("PRINT_SERVER_PORT", "515"),
                 conf["PRINT_SERVER_QUEUE"])

    while True:
        conf = load_config(CONFIG_PATH)
        fd = open_gadget(device_path)

        # Set IEEE-1284 status to Selected+Not-Error via ioctl.  The kernel
        # initialises printer_status with only PRINTER_SELECTED (0x10), leaving
        # the Not-Error bit (0x08) clear.  Windows (usbprint.sys) queries
        # GET_PORT_STATUS before sending data; bit3=0 means "error" and causes
        # it to abort the job without sending anything.
        try:
            fcntl.ioctl(fd, _GADGET_SET_PRINTER_STATUS,
                        struct.pack('B', _PRINTER_STATUS_READY))
        except OSError:
            pass

        ts      = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        job     = f"{node_id}_{ts}_{uuid.uuid4().hex}"
        partial = queue_dir / f"{job}.ps.partial"
        final   = queue_dir / f"{job}.ps"
        bytes_rx = 0

        logging.info("Job started: %s", job)

        try:
            with open(partial, "wb") as out:
                while True:
                    ready, _, _ = select.select([fd], [], [], JOB_IDLE_TIMEOUT)
                    if not ready:
                        out.flush()
                        os.fsync(out.fileno())
                        logging.info("Job idle timeout — finalising (%d bytes)", bytes_rx)
                        break

                    try:
                        chunk = os.read(fd, READ_CHUNK)
                    except OSError as e:
                        logging.warning("Read error on %s: %s — discarding", job, e)
                        bytes_rx = 0
                        break

                    if not chunk:
                        out.flush()
                        os.fsync(out.fileno())
                        break

                    out.write(chunk)
                    bytes_rx += len(chunk)
        finally:
            os.close(fd)

        if bytes_rx == 0:
            logging.info("Empty job — skipping")
            partial.unlink(missing_ok=True)
            continue

        partial.rename(final)
        logging.info("Job captured: %s (%d bytes) — relaying to print server", final.name, bytes_rx)

        # Wake the display daemon immediately so it can show "Print in progress"
        try:
            Path("/var/run/relay_printing").touch()
            _display_pid = Path("/var/run/display_daemon.pid").read_text().strip()
            os.kill(int(_display_pid), signal.SIGUSR1)
        except Exception:
            pass

        if deliver_with_retry(final, conf):
            final.rename(sent_dir / final.name)
            logging.info("Job delivered: %s", final.name)
        else:
            logging.error("Job dropped (delivery failed): %s", final.name)
            final.unlink(missing_ok=True)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    conf = load_config(CONFIG_PATH)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        capture_and_relay(conf)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
