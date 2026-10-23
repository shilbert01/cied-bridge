#!/usr/bin/env python3
"""
Monitors the USB mass storage backing image for new export files written by
the connected interrogation device programmer.

Strategy:
  1. Poll the image file's mtime every POLL_INTERVAL seconds.
  2. After IDLE_TIMEOUT seconds of no mtime change, the host is done writing.
  3. Loop-mount the image, copy new/changed files to the export queue directory.
  4. Unmount, let the forwarder pick them up.

The export queue directory is watched by forwarder.py, which POSTs each file
to /ingest-export on the central server.
"""

import os
import struct
import sys
import time
import logging
import signal
import subprocess
import shutil
import tempfile
from pathlib import Path

CONFIG_PATH   = "/etc/cied-edge/node.conf"
POLL_INTERVAL = 3      # seconds between mtime checks
IDLE_TIMEOUT  = 15     # seconds of stable mtime before assuming host is done
MOUNT_TIMEOUT = 10     # seconds to wait for loop mount
SEEN_DB       = "/var/lib/cied-edge/seen_exports.txt"

# File extensions we forward; everything else is silently ignored
EXPORT_EXTENSIONS = {".hl7", ".log", ".pdf", ".xml", ".cfg", ".txt"}


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


def _extract_new_files(img_path: str, export_dir: Path, seen: set) -> list[str]:
    """Mount image, copy new/changed files to export_dir, return list of new paths."""
    new_files = []
    with tempfile.TemporaryDirectory(prefix="pmcedge_mnt_") as mnt:
        if not _loop_mount(img_path, mnt):
            logging.error("Failed to loop-mount %s", img_path)
            return []
        try:
            for root, dirs, files in os.walk(mnt):
                # Skip hidden dirs (MacOS .Spotlight, etc.)
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.startswith("."):
                        continue
                    ext = Path(fname).suffix.lower()
                    if ext not in EXPORT_EXTENSIONS:
                        continue
                    src = Path(root) / fname
                    rel = str(src.relative_to(mnt))
                    stat = src.stat()
                    key  = f"{rel}:{stat.st_size}:{int(stat.st_mtime)}"
                    if key in seen:
                        continue
                    # Preserve relative path under export_dir for context
                    dest = export_dir / rel.lstrip("/")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dest))
                    seen.add(key)
                    new_files.append(str(dest))
                    logging.info("Extracted export file: %s", dest)
        finally:
            _loop_umount(mnt)
    return new_files


def storage_loop(img_path: str, export_dir: Path):
    logging.info("Storage daemon started — image: %s  export: %s", img_path, export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    seen = _load_seen(SEEN_DB)

    last_mtime: float = 0.0
    last_change: float = time.monotonic()

    while True:
        time.sleep(POLL_INTERVAL)

        try:
            current_mtime = os.path.getmtime(img_path)
        except OSError:
            logging.warning("Cannot stat %s — gadget may not be active yet", img_path)
            last_change = time.monotonic()  # reset idle timer
            continue

        if current_mtime != last_mtime:
            last_mtime = current_mtime
            last_change = time.monotonic()
            logging.debug("Image mtime changed: host is still writing")
            continue

        idle_secs = time.monotonic() - last_change
        if idle_secs < IDLE_TIMEOUT:
            continue

        # Host has stopped writing — extract new files
        logging.info("Image idle for %.0fs — extracting new export files", idle_secs)
        new_files = _extract_new_files(img_path, export_dir, seen)
        if new_files:
            _save_seen(SEEN_DB, seen)
            logging.info("Extracted %d new export file(s)", len(new_files))

        # Reset idle timer so we don't re-check immediately
        last_change = time.monotonic()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    conf       = load_config(CONFIG_PATH)
    img_path   = conf.get("STORAGE_IMG", "")
    export_dir = Path(conf.get("EXPORT_DIR", "/var/lib/cied-edge/exports"))

    if not img_path:
        logging.error("STORAGE_IMG not set in %s — storage daemon exiting", CONFIG_PATH)
        sys.exit(1)

    if not Path(img_path).exists():
        logging.error("Storage image not found: %s — run create-storage-image.sh first",
                      img_path)
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        storage_loop(img_path, export_dir)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
