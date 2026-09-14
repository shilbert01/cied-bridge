#!/usr/bin/env python3
"""
Feed the import hub from the central server's KardiSynch outbox.

    /data/kardisynch-outbox  ──(this)──▶  import hub /submit  ──▶  receiver presenter ──▶ Windows/KardiSynch

The central server (central-server/api/kardisynch_outbox.py) copies finished,
KardiSynch-parseable files into /data/kardisynch-outbox and marks each job
published so it never re-copies. In the *networked* deployment KardiSynch mounts
that outbox over Samba and drains it itself. In the *receiver* deployment
KardiSynch is on an isolated PC reachable only over USB, so it can't reach the
outbox — this forwarder bridges the gap by POSTing each new outbox file to the
import hub, from where the HID (or mass-storage) presenter delivers it.

Idempotent: forwarded (name, size, mtime) keys are persisted, so restarts and
re-scans never double-submit. Files are left in place (the outbox is the central
server's record; another cleanup owns deletion) — we only ever read them.

Config via environment (all optional):
    OUTBOX_DIR    default /data/kardisynch-outbox
    HUB_URL       default http://10.99.0.1:8090   (Pi3 import hub; localhost if co-located)
    TARGET_NODE   default ""  (empty = any presenter; set to pin a specific receiver)
    STATE_FILE    default /var/lib/kardisynch-receiver/forwarded.json
    POLL_INTERVAL default 10 (seconds)
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUTBOX_DIR = Path(os.environ.get("OUTBOX_DIR", "/data/kardisynch-outbox"))
HUB_URL = os.environ.get("HUB_URL", "http://10.99.0.1:8090").rstrip("/")
TARGET_NODE = os.environ.get("TARGET_NODE", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE",
                                 "/var/lib/kardisynch-receiver/forwarded.json"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))

# Match the outbox publisher's set (kardisynch_outbox.KARDISYNCH_EXTS).
FORWARD_EXTS = {".pdf", ".xml", ".txt", ".log", ".pkg", ".bnk", ".pdd"}


def _key(p: Path) -> str:
    st = p.stat()
    return f"{p.name}:{st.st_size}:{int(st.st_mtime)}"


def load_state() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def save_state(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(STATE_FILE)


def submit(path: Path) -> bool:
    """POST one file to the hub. Returns True on 2xx."""
    headers = {"X-File-Name": path.name, "Content-Type": "application/octet-stream"}
    if TARGET_NODE:
        headers["X-Target-Node"] = TARGET_NODE
    try:
        data = path.read_bytes()
    except OSError as e:
        logging.warning("cannot read %s: %s", path, e)
        return False
    req = urllib.request.Request(HUB_URL + "/submit", data=data,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except urllib.error.URLError as e:
        logging.warning("submit %s failed: %s", path.name, e)
        return False


def scan_once(seen: set) -> int:
    if not OUTBOX_DIR.is_dir():
        logging.warning("outbox %s not present yet", OUTBOX_DIR)
        return 0
    forwarded = 0
    for p in sorted(OUTBOX_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() not in FORWARD_EXTS:
            continue
        try:
            k = _key(p)
        except OSError:
            continue
        if k in seen:
            continue
        if submit(p):
            seen.add(k)
            forwarded += 1
            logging.info("forwarded %s -> hub%s", p.name,
                         f" (target={TARGET_NODE})" if TARGET_NODE else "")
    if forwarded:
        save_state(seen)
    return forwarded


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    logging.info("outbox->hub: %s -> %s  target=%s  every %ds",
                 OUTBOX_DIR, HUB_URL, TARGET_NODE or "any", POLL_INTERVAL)
    seen = load_state()
    # One-shot mode for testing: `outbox-to-hub.py --once`
    once = "--once" in sys.argv[1:]
    while True:
        try:
            scan_once(seen)
        except Exception as e:  # never let the loop die
            logging.error("scan error: %s", e)
        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
