#!/usr/bin/env python3
"""
Import-hub client — the presenter's side of the durable queue on the Pi3.

Shared by every presenter, regardless of how it delivers to the host:
  - import_daemon.py  -> injects into a mass-storage image (eject/inject cycle)
  - kardisynch-receiver/hid/hid_sender.py -> streams over a USB HID pipe

The hub (import-relay/pi3-server/import-server.py) is generic: senders POST
/submit, presenters claim/fetch/ack. Claims are atomically leased to a node id,
so multiple presenters never double-deliver and anything left unacked is
redelivered after the hub's lease expires. That lease is what lets a presenter
safely ack ONLY after the host has confirmed receipt — a delivery that dies
half-way is simply retried.
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path


class Hub:
    def __init__(self, base_url: str, node_id: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.node = node_id
        self.timeout = timeout

    def _req(self, method, path, data=None):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.status, r.read()

    def claim(self, max_n: int) -> list:
        """Lease up to max_n items to this node. Returns [{id,name}]."""
        try:
            status, body = self._req(
                "POST", f"/claim?node={self.node}&max={max_n}", data=b"")
            if status == 200:
                return json.loads(body)
        except (urllib.error.URLError, ValueError, OSError) as e:
            logging.warning("claim failed: %s", e)
        return []

    def fetch(self, item_id: str, dest: Path) -> bool:
        try:
            status, body = self._req("GET", f"/fetch/{self.node}/{item_id}")
            if status == 200:
                dest.write_bytes(body)
                return True
        except (urllib.error.URLError, OSError) as e:
            logging.warning("fetch %s failed: %s", item_id, e)
        return False

    def ack(self, item_id: str) -> bool:
        try:
            status, _ = self._req("POST", f"/ack/{self.node}/{item_id}", data=b"")
            return status == 200
        except (urllib.error.URLError, OSError) as e:
            logging.warning("ack %s failed: %s", item_id, e)
            return False
