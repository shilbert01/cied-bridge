#!/usr/bin/env python3
"""Export Relay — file ingest endpoint and status web UI.

Serves http://10.99.0.1/ on the relayap WiFi.

POST /ingest-export   — receives a file from Pi Zero, saves to IMPORT_DIR
GET  /                — HTML status page listing received files
POST /clear-files     — delete all files in IMPORT_DIR
"""

import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

IMPORT_DIR = Path("/var/lib/pacemaker-export/import")
PORT       = 80


def _file_rows() -> str:
    rows = []
    try:
        files = sorted(IMPORT_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            if not f.is_file():
                continue
            stat = f.stat()
            size = stat.st_size
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
            size_str = f"{size:,}" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
            rows.append(
                f"<tr><td>{f.name}</td><td>{size_str}</td><td>{mtime}</td></tr>"
            )
    except Exception:
        pass
    if not rows:
        return '<tr><td colspan="3" style="color:#888">No files received yet</td></tr>'
    return "\n".join(rows)


def _samba_status() -> str:
    try:
        import subprocess
        r = subprocess.run(["systemctl", "is-active", "smbd"],
                           capture_output=True, text=True, timeout=3)
        return "running" if r.stdout.strip() == "active" else "stopped"
    except Exception:
        return "unknown"


def _html() -> bytes:
    try:
        count = sum(1 for f in IMPORT_DIR.iterdir() if f.is_file())
    except Exception:
        count = 0

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Export Relay</title>
<style>
  body {{font-family:sans-serif;margin:2em;background:#f5f5f5}}
  h1 {{color:#333}}
  table {{border-collapse:collapse;width:100%;background:#fff}}
  th,td {{border:1px solid #ddd;padding:8px 12px;text-align:left}}
  th {{background:#4a7;color:#fff}}
  .badge {{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.85em}}
  .ok  {{background:#4a7;color:#fff}}
  .err {{background:#c44;color:#fff}}
  .btn {{margin:4px;padding:6px 14px;cursor:pointer;border:none;border-radius:4px;
         background:#c44;color:#fff;font-size:.9em}}
</style>
</head><body>
<h1>Export Relay</h1>
<p>
  SAMBA: <span class="badge {'ok' if _samba_status()=='running' else 'err'}">{_samba_status()}</span>
  &nbsp;
  Files in share: <strong>{count}</strong>
  &nbsp;
  Share path: <code>\\\\PI3\\import</code>
</p>
<form method="post" action="/clear-files"
      onsubmit="return confirm('Delete all files from the import share?')">
  <button class="btn" type="submit">Clear all files</button>
</form>
<br>
<table>
  <tr><th>Filename</th><th>Size</th><th>Received</th></tr>
  {_file_rows()}
</table>
</body></html>"""
    return html.encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # suppress per-request noise in systemd journal

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(_html())
        else:
            self._send(b"Not found", 404, "text/plain")

    def do_POST(self):
        if self.path == "/ingest-export":
            fname = self.headers.get("X-File-Name", "")
            if not fname:
                fname = f"export_{int(time.time())}.bin"
            # Sanitise: strip path separators, then stamp with arrival time
            fname = Path(fname).name
            stem, suffix = Path(fname).stem, Path(fname).suffix
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"{stem}_{ts}{suffix}"
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            IMPORT_DIR.mkdir(parents=True, exist_ok=True)
            dest = IMPORT_DIR / fname
            dest.write_bytes(data)
            self._send(b"accepted", 202, "text/plain")
            self.log_error("Received %s (%d bytes) → %s", fname, len(data), dest)

        elif self.path == "/clear-files":
            deleted = 0
            try:
                for f in IMPORT_DIR.iterdir():
                    if f.is_file():
                        f.unlink()
                        deleted += 1
            except Exception:
                pass
            # Redirect back to root after clear
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        else:
            self._send(b"Not found", 404, "text/plain")


def main():
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    server = HTTPServer(("", PORT), Handler)
    print(f"Export Relay web UI on port {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
