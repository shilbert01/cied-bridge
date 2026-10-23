#!/usr/bin/env python3
"""
Import queue server — the hub between many senders and one-or-more presenters.

Topology (mirrors the rest of the fleet: hub-and-spoke, never edge↔edge):

    sender A ─┐
    sender B ─┤ POST /submit ─▶  [ this server, on the Pi3 ]  ◀─ claim/fetch/ack ─ presenter
    sender C ─┘                        (durable queue)               (Pi Zero gadget)

Senders never need to know a presenter's address (it's the moving target — it's
plugged into some host PC in the field). They drop files at one stable address.
Presenters pull. Nobody sets up pairs.

Queue is a directory of items. Each item is a dir:
    queue/<id>/data        raw file bytes
    queue/<id>/meta.json   {name, target, submitted}

Claiming is an atomic os.rename of the item dir into inflight/<node>/, so two
presenters racing can never both grab the same file. Unacked items whose lease
expires are swept back to queue/ (a presenter that died mid-delivery).

Endpoints:
    POST /submit           body=bytes, X-File-Name:, optional X-Target-Node:
    POST /claim?node=&max= -> JSON [{id,name}], atomically leased to <node>
    GET  /fetch/<node>/<id>  -> bytes
    POST /ack/<node>/<id>    -> delete (delivery confirmed)
    GET  /health
    GET  /                   -> queue status (human readable)
"""

import json
import logging
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

QUEUE_ROOT = Path(os.environ.get("IMPORT_QUEUE_ROOT", "/var/lib/cied-import-hub"))
LISTEN_HOST = os.environ.get("IMPORT_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("IMPORT_LISTEN_PORT", "8090"))
LEASE_SECS = int(os.environ.get("IMPORT_LEASE_SECS", "120"))  # requeue if unacked

QUEUE_DIR = QUEUE_ROOT / "queue"
INFLIGHT_DIR = QUEUE_ROOT / "inflight"


def _new_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _read_meta(item_dir: Path) -> dict:
    try:
        return json.loads((item_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ── sender side ─────────────────────────────────────────────────────────
    def _submit(self):
        name = os.path.basename(self.headers.get("X-File-Name", "").strip())
        if not name:
            return self._send(400, b"missing X-File-Name\n")
        target = self.headers.get("X-Target-Node", "").strip()  # "" = any presenter
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length else b""

        item_id = _new_id()
        # Build in a temp dir then atomically rename into queue/ so a claimer
        # never sees a half-written item.
        tmp = QUEUE_DIR / f".{item_id}.tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "data").write_bytes(data)
        (tmp / "meta.json").write_text(json.dumps(
            {"name": name, "target": target, "submitted": time.time(),
             "source": self.client_address[0]}))
        tmp.replace(QUEUE_DIR / item_id)

        logging.info("Queued %s (%s, %d bytes) target=%s",
                     item_id, name, len(data), target or "any")
        self._send(202, json.dumps({"id": item_id}).encode(), "application/json")

    # ── presenter side ──────────────────────────────────────────────────────
    def _claim(self, qs):
        node = (qs.get("node", [""])[0]).strip()
        if not node:
            return self._send(400, b"missing node\n")
        max_n = int(qs.get("max", ["10"])[0])

        node_inflight = INFLIGHT_DIR / node
        node_inflight.mkdir(parents=True, exist_ok=True)

        claimed = []
        for item_dir in sorted(QUEUE_DIR.iterdir()):  # FIFO by id (timestamped)
            if len(claimed) >= max_n:
                break
            if not item_dir.is_dir() or item_dir.name.startswith("."):
                continue
            meta = _read_meta(item_dir)
            target = meta.get("target", "")
            if target and target != node:
                continue  # addressed to a different presenter
            dest = node_inflight / item_dir.name
            try:
                os.rename(item_dir, dest)  # atomic claim; loser of a race gets FileNotFound/OSError
            except OSError:
                continue
            (dest / "claimed_at").write_text(str(time.time()))
            claimed.append({"id": item_dir.name, "name": meta.get("name", item_dir.name)})

        self._send(200, json.dumps(claimed).encode(), "application/json")

    def _fetch(self, node, item_id):
        data_file = INFLIGHT_DIR / node / item_id / "data"
        if not data_file.exists():
            return self._send(404, b"not found\n")
        self._send(200, data_file.read_bytes(), "application/octet-stream")

    def _ack(self, node, item_id):
        item_dir = INFLIGHT_DIR / node / item_id
        if not item_dir.exists():
            return self._send(404, b"not found\n")
        for p in item_dir.iterdir():
            p.unlink(missing_ok=True)
        item_dir.rmdir()
        logging.info("Acked %s by %s", item_id, node)
        self._send(200, b"ok\n")

    # ── routing ───────────────────────────────────────────────────────────────
    def do_POST(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if u.path == "/submit":
            return self._submit()
        if u.path == "/claim":
            return self._claim(parse_qs(u.query))
        if len(parts) == 3 and parts[0] == "ack":
            return self._ack(parts[1], parts[2])
        self._send(404, b"not found\n")

    def do_GET(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if u.path == "/health":
            return self._send(200, b"ok\n")
        if len(parts) == 3 and parts[0] == "fetch":
            return self._fetch(parts[1], parts[2])
        if u.path == "/":
            pend = sum(1 for p in QUEUE_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))
            infl = sum(1 for n in INFLIGHT_DIR.glob("*/*") if n.is_dir())
            return self._send(200, f"pending={pend} inflight={infl}\n".encode())
        self._send(404, b"not found\n")


def sweep_expired_leases():
    """Move inflight items whose lease expired back to the queue for redelivery."""
    now = time.time()
    for item_dir in INFLIGHT_DIR.glob("*/*"):
        if not item_dir.is_dir():
            continue
        try:
            claimed_at = float((item_dir / "claimed_at").read_text())
        except (OSError, ValueError):
            claimed_at = now
        if now - claimed_at > LEASE_SECS:
            (item_dir / "claimed_at").unlink(missing_ok=True)
            try:
                os.rename(item_dir, QUEUE_DIR / item_dir.name)
                logging.warning("Lease expired, requeued %s", item_dir.name)
            except OSError:
                pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    logging.info("Import hub on %s:%d  queue=%s  lease=%ds",
                 LISTEN_HOST, LISTEN_PORT, QUEUE_ROOT, LEASE_SECS)

    # Sweep expired leases in a background thread.
    import threading
    def reaper():
        while True:
            time.sleep(min(LEASE_SECS, 30))
            sweep_expired_leases()
    threading.Thread(target=reaper, daemon=True).start()

    httpd.serve_forever()


if __name__ == "__main__":
    main()
