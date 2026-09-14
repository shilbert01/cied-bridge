#!/usr/bin/env python3
"""
Watches two queues and forwards their contents to the central server:

  queue_dir/   — *.ps print jobs  → POST /ingest
  export_dir/  — export files     → POST /ingest-export

Files are only moved to sent/ after the server acknowledges receipt
(HTTP 200 or 202).  When the server is unreachable the forwarder enters
offline mode: new files accumulate locally and the full queue is drained
once connectivity is restored.
"""

import os
import sys
import time
import logging
import signal
import ssl
import threading
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_PATH    = "/etc/pacemaker-edge/node.conf"
CA_CERT        = "/etc/pacemaker-edge/central-ca.crt"
POLL_INTERVAL  = 5    # seconds between queue scans when online
OFFLINE_POLL   = 30   # seconds between connectivity checks when offline

# Extensions forwarded from the export directory
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


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if Path(CA_CERT).exists():
        ctx.load_verify_locations(CA_CERT)
    else:
        logging.warning("CA cert not found at %s — TLS verification disabled", CA_CERT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _server_root(url: str) -> str:
    for suffix in ("/ingest-export", "/ingest"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url.rsplit("/", 1)[0]


def _is_reachable(url: str) -> bool:
    try:
        req = urllib.request.Request(_server_root(url) + "/health")
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=5) as r:
            return r.status < 500
    except Exception:
        return False


def _wait_for_online(url: str, label: str = ""):
    """Block until the central server responds, polling every OFFLINE_POLL seconds."""
    if not _is_reachable(url):
        logging.warning("%scentral server unreachable — storing locally, "
                        "retrying every %ds", label, OFFLINE_POLL)
        while not _is_reachable(url):
            time.sleep(OFFLINE_POLL)
        logging.info("%scentral server reachable again — draining queue", label)


def post_print_job(job_path: Path, node_id: str, vendor: str,
                   url: str, token: str) -> bool:
    data = job_path.read_bytes()
    headers = {
        "Content-Type":  "application/postscript",
        "Authorization": f"Bearer {token}",
        "X-Node-Id":     node_id,
        "X-Vendor":      vendor,
        "X-Job-Name":    job_path.name,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:
            return resp.status in (200, 202)
    except urllib.error.HTTPError as e:
        logging.warning("Server rejected print job %s: HTTP %d", job_path.name, e.code)
        return e.code < 500   # 4xx = terminal, don't retry
    except Exception as e:
        logging.warning("Network error forwarding print job %s: %s", job_path.name, e)
        return False


def post_export_file(file_path: Path, node_id: str, vendor: str,
                     url: str, token: str, session_id: str) -> bool:
    data = file_path.read_bytes()
    ext  = file_path.suffix.lower().lstrip(".")
    content_type_map = {
        "hl7": "application/hl7-v2",
        "log": "text/plain",
        "pdf": "application/pdf",
        "xml": "application/xml",
        "cfg": "text/plain",
        "txt": "text/plain",
    }
    content_type = content_type_map.get(ext, "application/octet-stream")
    headers = {
        "Content-Type":   content_type,
        "Authorization":  f"Bearer {token}",
        "X-Node-Id":      node_id,
        "X-Vendor":       vendor,
        "X-Session-Id":   session_id,
        "X-File-Name":    file_path.name,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as resp:
            return resp.status in (200, 202)
    except urllib.error.HTTPError as e:
        logging.warning("Server rejected export %s: HTTP %d", file_path.name, e.code)
        return e.code < 500
    except Exception as e:
        logging.warning("Network error forwarding export %s: %s", file_path.name, e)
        return False


def print_forward_loop(queue_dir: Path, sent_dir: Path, node_id: str, vendor: str,
                       url: str, token: str):
    logging.info("Print forwarder started — queue: %s", queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    sent_dir.mkdir(parents=True, exist_ok=True)

    while True:
        jobs = sorted(queue_dir.glob("*.ps"))
        if not jobs:
            time.sleep(POLL_INTERVAL)
            continue

        for job_path in jobs:
            if not job_path.exists():
                continue  # already sent by a previous iteration
            logging.info("Forwarding print job %s", job_path.name)
            if post_print_job(job_path, node_id, vendor, url, token):
                job_path.rename(sent_dir / job_path.name)
                logging.info("Delivered print job: %s", job_path.name)
            else:
                # Transient failure — wait for connectivity then restart the drain
                _wait_for_online(url, label="[print] ")
                break  # re-scan queue from the top after reconnect


def export_forward_loop(export_dir: Path, sent_dir: Path, node_id: str, vendor: str,
                        url: str, token: str):
    logging.info("Export forwarder started — export_dir: %s", export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    (sent_dir / "exports").mkdir(parents=True, exist_ok=True)

    while True:
        time.sleep(POLL_INTERVAL)

        pending = []
        for root, dirs, files in os.walk(str(export_dir)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in sorted(files):
                if Path(fname).suffix.lower() in EXPORT_EXTENSIONS:
                    pending.append((Path(root), fname))

        if not pending:
            continue

        offline_triggered = False
        for root, fname in pending:
            file_path = root / fname
            if not file_path.exists():
                continue
            rel = str(root.relative_to(export_dir)).replace(os.sep, "_")
            session_id = rel if rel and rel != "." else "default"
            logging.info("Forwarding export %s/%s", session_id, fname)
            if post_export_file(file_path, node_id, vendor, url, token, session_id):
                dest = sent_dir / "exports" / session_id
                dest.mkdir(parents=True, exist_ok=True)
                file_path.rename(dest / fname)
                logging.info("Delivered export: %s/%s", session_id, fname)
            else:
                _wait_for_online(url, label="[export] ")
                offline_triggered = True
                break  # re-scan after reconnect

        if offline_triggered:
            continue  # restart the outer while immediately to re-scan


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    conf       = load_config(CONFIG_PATH)
    queue      = Path(conf["CAPTURE_DIR"])
    sent       = Path(conf["SENT_DIR"])
    node_id    = conf["NODE_ID"]
    vendor     = conf["VENDOR"]
    url        = conf["CENTRAL_URL"]
    token      = conf["CENTRAL_TOKEN"]
    export_dir = Path(conf.get("EXPORT_DIR", str(queue.parent / "exports")))
    export_url = conf.get("CENTRAL_EXPORT_URL", url.replace("/ingest", "/ingest-export"))

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    export_thread = threading.Thread(
        target=export_forward_loop,
        args=(export_dir, sent, node_id, vendor, export_url, token),
        daemon=True,
    )
    export_thread.start()

    try:
        print_forward_loop(queue, sent, node_id, vendor, url, token)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
