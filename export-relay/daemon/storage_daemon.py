#!/usr/bin/env python3
"""
Monitors the USB mass storage backing image for new export files written by
the connected cardiac device programmer, then forwards them to the Pi3 server.

Strategy:
  1. Poll the disk image's mtime every POLL_INTERVAL seconds.
  2. After IDLE_TIMEOUT seconds of no mtime change, the host is done writing.
  3. Loop-mount the image (read-only), extract new/changed files to EXPORT_DIR.
  4. HTTP POST each new file to Pi3's /ingest-export endpoint with retry.

The seen_exports.txt manifest prevents re-forwarding files across reboots.
"""

import hashlib
import os
import struct
import sys
import time
import logging
import signal
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_PATH   = "/etc/pacemaker-export/export.conf"
POLL_INTERVAL = 3      # seconds between mtime checks
IDLE_TIMEOUT  = 15     # seconds of stable mtime before assuming host is done
MOUNT_TIMEOUT = 10     # seconds to wait for loop mount

# Extensions to extract and forward; everything else is silently ignored
EXPORT_EXTENSIONS = {".hl7", ".log", ".pdf", ".xml", ".cfg", ".txt",
                     ".bnk", ".pdd", ".pkg"}


def load_config(path: str) -> dict:
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
    except OSError as e:
        logging.error("Cannot read config %s: %s", path, e)
    return conf


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_seen(db_path: str) -> set:
    try:
        return set(Path(db_path).read_text().splitlines())
    except FileNotFoundError:
        return set()


def _save_seen(db_path: str, seen: set):
    Path(db_path).write_text("\n".join(sorted(seen)))


def _get_partition_offset(img_path: str) -> int:
    """Return byte offset of first MBR partition, or 0 for raw/unpartitioned image."""
    try:
        with open(img_path, "rb") as f:
            f.seek(446)
            for _ in range(4):
                entry = f.read(16)
                start_lba = struct.unpack_from("<I", entry, 8)[0]
                if start_lba > 0:
                    return start_lba * 512
    except OSError:
        pass
    return 0


def _loop_mount(img_path: str, mount_point: str) -> bool:
    offset = _get_partition_offset(img_path)
    opts = f"loop,ro,offset={offset}" if offset else "loop,ro"
    result = subprocess.run(
        ["mount", "-o", opts, img_path, mount_point],
        capture_output=True, timeout=MOUNT_TIMEOUT,
    )
    if result.returncode != 0:
        logging.error("loop-mount failed: %s", result.stderr.decode().strip())
    return result.returncode == 0


def _loop_umount(mount_point: str):
    subprocess.run(["umount", mount_point],
                   capture_output=True, timeout=MOUNT_TIMEOUT)


def _extract_new_files(img_path: str, export_dir: Path, seen: set) -> list:
    """Mount image read-only, copy new/changed files to export_dir. Returns new paths."""
    new_files = []
    with tempfile.TemporaryDirectory(prefix="pmcexp_mnt_") as mnt:
        if not _loop_mount(img_path, mnt):
            return []
        try:
            for root, dirs, files in os.walk(mnt):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.startswith("."):
                        continue
                    ext = Path(fname).suffix.lower()
                    if ext not in EXPORT_EXTENSIONS:
                        continue
                    src = Path(root) / fname
                    rel = str(src.relative_to(mnt))
                    key = f"{rel}:{_md5(src)}"
                    if key in seen:
                        logging.debug("Already seen: %s", rel)
                        continue
                    dest = export_dir / rel.lstrip("/")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(str(src), str(dest))  # copy without mtime — display_daemon filters by boot time
                    seen.add(key)
                    new_files.append(dest)
                    logging.info("Extracted: %s", dest)
        finally:
            _loop_umount(mnt)
    return new_files


def _post_file(file_path: Path, server_host: str, timeout: int = 30) -> bool:
    url = f"http://{server_host}/ingest-export"
    try:
        data = file_path.read_bytes()
    except OSError as e:
        logging.error("Cannot read %s: %s", file_path, e)
        return False
    headers = {
        "X-File-Name": file_path.name,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status in (200, 202)
            if not ok:
                logging.warning("POST %s → HTTP %d", file_path.name, resp.status)
            return ok
    except Exception as e:
        logging.warning("POST %s failed: %s", file_path.name, e)
        return False


UPLOAD_SENTINEL    = "/var/run/uploading"
RECEIVE_SENTINEL   = "/var/run/receiving"
DISPLAY_PID_FILE   = "/var/run/display_daemon.pid"
FORWARD_COUNT_FILE = "/var/lib/pacemaker-export/forward_count.txt"


def _notify_display(sig):
    try:
        pid = int(Path(DISPLAY_PID_FILE).read_text().strip())
        os.kill(pid, sig)
    except Exception:
        pass


def _set_receiving(active: bool):
    try:
        if active:
            Path(RECEIVE_SENTINEL).write_text("1")
        else:
            Path(RECEIVE_SENTINEL).unlink(missing_ok=True)
        _notify_display(signal.SIGUSR1)   # wake display to show/hide receiving screen
    except Exception:
        pass


def _set_uploading(active: bool):
    try:
        if active:
            Path(UPLOAD_SENTINEL).write_text("1")
            _notify_display(signal.SIGUSR2)   # SIGUSR2 = upload starting → show screen
        else:
            Path(UPLOAD_SENTINEL).unlink(missing_ok=True)
            _notify_display(signal.SIGUSR1)   # SIGUSR1 = upload done → refresh counter
    except Exception:
        pass


def _increment_count():
    try:
        try:
            n = int(Path(FORWARD_COUNT_FILE).read_text().strip())
        except Exception:
            n = 0
        Path(FORWARD_COUNT_FILE).write_text(str(n + 1))
    except Exception:
        pass


def _forward_files(new_files: list, server_host: str, giveup_secs: int):
    """POST each new file to Pi3 with exponential backoff; give up after giveup_secs."""
    _set_uploading(True)
    try:
        for fpath in new_files:
            deadline = time.monotonic() + giveup_secs
            delay = 5
            attempt = 0
            while True:
                attempt += 1
                if _post_file(fpath, server_host):
                    logging.info("Forwarded: %s (attempt %d)", fpath.name, attempt)
                    _increment_count()
                    break
                if time.monotonic() >= deadline:
                    logging.error("Giving up on %s after %ds", fpath.name, giveup_secs)
                    break
                logging.info("Retry %s in %ds", fpath.name, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
    finally:
        _set_uploading(False)


def storage_loop(img_path: str, export_dir: Path, seen_db: str,
                 server_host: str, giveup_secs: int):
    logging.info("Export daemon started — image: %s  export: %s  server: %s",
                 img_path, export_dir, server_host)
    export_dir.mkdir(parents=True, exist_ok=True)
    seen = _load_seen(seen_db)

    # Seed last_mtime from disk so the first poll doesn't falsely trigger "receiving"
    try:
        last_mtime: float = os.path.getmtime(img_path)
    except OSError:
        last_mtime = 0.0
    last_change: float = time.monotonic()
    receiving: bool = False   # True while host is actively writing to the drive
    skip_next_mtime: bool = False  # suppress one spurious mtime change after loop-mount

    while True:
        time.sleep(POLL_INTERVAL)

        try:
            current_mtime = os.path.getmtime(img_path)
        except OSError:
            logging.warning("Cannot stat %s — gadget may not be active yet", img_path)
            last_change = time.monotonic()
            continue

        if current_mtime != last_mtime:
            last_mtime = current_mtime
            last_change = time.monotonic()
            if not receiving and not skip_next_mtime:
                receiving = True
                logging.info("Host is writing — showing receiving screen")
                _set_receiving(True)
            skip_next_mtime = False
            continue

        idle_secs = time.monotonic() - last_change
        if idle_secs < IDLE_TIMEOUT:
            continue

        # Host has stopped writing for IDLE_TIMEOUT seconds — extract and forward
        if receiving:
            receiving = False
            _set_receiving(False)

        logging.info("Image idle for %.0fs — extracting new files", idle_secs)
        new_files = _extract_new_files(img_path, export_dir, seen)

        # Loop-mount/umount can update disk.img mtime asynchronously; suppress the
        # one spurious change it causes so we don't flash "Receiving file" again.
        skip_next_mtime = True

        if new_files:
            _save_seen(seen_db, seen)
            logging.info("Extracted %d new file(s)", len(new_files))
            _forward_files(new_files, server_host, giveup_secs)
        else:
            logging.info("No new files found")

        # Reset idle timer to avoid re-checking immediately
        last_change = time.monotonic()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    conf        = load_config(CONFIG_PATH)
    img_path    = conf.get("DISK_IMAGE", "")
    export_dir  = Path(conf.get("EXPORT_DIR", "/var/lib/pacemaker-export/exports"))
    seen_db     = conf.get("SEEN_DB", "/var/lib/pacemaker-export/seen_exports.txt")
    server_host = conf.get("EXPORT_SERVER_HOST", "10.99.0.1")
    giveup_secs = int(conf.get("GIVEUP_SECS", "300"))

    if not img_path:
        logging.error("DISK_IMAGE not set in %s", CONFIG_PATH)
        sys.exit(1)

    if not Path(img_path).exists():
        logging.error("Disk image not found: %s — run create-storage-image.sh first",
                      img_path)
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        storage_loop(img_path, export_dir, seen_db, server_host, giveup_secs)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
