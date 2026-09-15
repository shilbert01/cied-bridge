#!/usr/bin/env python3
"""Relay Hub — printer, export, and node management web UI.

Serves http://10.99.0.1/ on the relayap WiFi.
"""

import json
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

QUEUE     = "LaserPrinter"
PORT      = 80
CUPS_CONF = "/etc/cups/cupsd.conf"

# Every CUPS call gets a timeout. Without one, a wedged cupsd or a stuck backend
# (e.g. a job spooled against a disconnected usb:// printer) blocks the request
# forever — which used to take the whole UI down with it.
CMD_TIMEOUT   = 10   # ordinary lpstat/lpadmin/cancel calls
PROBE_TIMEOUT = 25   # lpinfo -v walks every backend and is genuinely slow
SLOW_TIMEOUT  = 30   # cups restart / spooling a job file

PI0_HOST  = "10.99.0.2"
PI0_USER  = "root"
PI0_CONF  = "/etc/pacemaker-relay/relay.conf"
PI0_PROBE_TIMEOUT = 0.5   # seconds; TCP probe before paying the ssh ConnectTimeout
MEDIASOURCE_TTL   = 60    # seconds between background refreshes of the cached value
DEVICE_TTL        = 20    # seconds between CUPS device/queue re-probes
EDGE_TTL          = 15    # seconds between edge-node (relay) status refreshes

RELAY_IFACE = "wlan0"     # the AP interface the edge nodes associate to
# Raspberry Pi MAC OUIs — used to flag which associated stations are relay nodes.
PI_OUIS = ("b8:27:eb", "dc:a6:32", "d8:3a:dd", "e4:5f:01", "28:cd:c1", "2c:cf:67")


# ── CUPS helpers ──────────────────────────────────────────────────────────────

def _current_uri_probe() -> str | None:
    try:
        out = subprocess.check_output(["lpstat", "-v"], text=True, stderr=subprocess.DEVNULL,
                                      timeout=CMD_TIMEOUT)
        for line in out.splitlines():
            if QUEUE in line:
                return line.split()[-1]
    except Exception:
        pass
    return None


def _available_probe() -> list[str]:
    try:
        out = subprocess.check_output(["lpinfo", "-v"], text=True, stderr=subprocess.DEVNULL,
                                      timeout=PROBE_TIMEOUT)
        return [line.split()[1] for line in out.splitlines()
                if line.startswith("direct usb://")]
    except Exception:
        return []


# ── USB printer scanner ─────────────────────────────────────────────────────

_PRINTER_CLASS = 7
_PROTO_NAMES = {1: "Unidirectional", 2: "Bidirectional", 3: "IEEE 1284.4", 4: "IPP-over-USB"}

def _scan_usb_printers() -> list[dict]:
    """Scan USB bus for printer-class devices and their interface protocols."""
    try:
        out = subprocess.check_output(["lsusb"], text=True, timeout=5)
    except Exception:
        return []

    printers = []
    for line in out.splitlines():
        m = re.match(r"Bus \d+ Device \d+: ID ([0-9a-f]{4}):([0-9a-f]{4})\s+(.*)", line, re.I)
        if not m:
            continue
        vid, pid, desc = m.group(1), m.group(2), m.group(3).strip()
        # Check if this device has a printer-class interface
        try:
            detail = subprocess.check_output(
                ["lsusb", "-v", "-d", f"{vid}:{pid}"],
                text=True, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            continue
        protocols = []
        in_printer = False
        for dl in detail.splitlines():
            dl = dl.strip()
            if dl.startswith("bInterfaceClass") and str(_PRINTER_CLASS) in dl and "Printer" in dl:
                in_printer = True
            elif dl.startswith("bInterfaceClass") and in_printer:
                in_printer = False
            elif dl.startswith("bInterfaceProtocol") and in_printer:
                pm = re.search(r"(\d+)", dl)
                if pm:
                    protocols.append(int(pm.group(1)))
        if not protocols:
            continue

        has_ipp = 4 in protocols
        transport = "ipp-usb" if has_ipp else "usblp"
        printers.append({
            "vid": vid, "pid": pid, "name": desc,
            "protocols": sorted(set(protocols)),
            "proto_names": [_PROTO_NAMES.get(p, f"proto-{p}") for p in sorted(set(protocols))],
            "has_ipp": has_ipp,
            "transport": transport,
        })
    return printers


def _setup_printer(vid_pid: str, transport: str) -> str:
    """Configure the print queue for a specific printer and transport."""
    msgs = []

    if transport == "ipp-usb":
        # Stop usblp, start ipp-usb
        subprocess.run(["rmmod", "usblp"], capture_output=True, timeout=5)
        subprocess.run(["systemctl", "unmask", "ipp-usb", "ipp-usb.socket"],
                       capture_output=True, timeout=5)
        subprocess.run(["systemctl", "start", "ipp-usb"],
                       capture_output=True, timeout=SLOW_TIMEOUT)
        time.sleep(3)
        try:
            with socket.create_connection(("localhost", 60000), timeout=5):
                msgs.append("ipp-usb active on port 60000")
        except OSError:
            msgs.append("WARNING: ipp-usb not responding on port 60000")
        subprocess.run(["lpadmin", "-p", QUEUE, "-v", "ipp://localhost:60000/ipp/print",
                        "-m", "everywhere", "-E"], check=True, timeout=CMD_TIMEOUT)
        msgs.append("Queue: ipp://localhost:60000/ipp/print (everywhere)")

    elif transport == "usblp":
        # Stop ipp-usb, load usblp
        subprocess.run(["systemctl", "stop", "ipp-usb"],
                       capture_output=True, timeout=SLOW_TIMEOUT)
        subprocess.run(["modprobe", "usblp"], capture_output=True, timeout=5)
        time.sleep(1)
        if Path("/dev/usb/lp0").exists():
            msgs.append("/dev/usb/lp0 available")
        else:
            msgs.append("WARNING: /dev/usb/lp0 not found")
        subprocess.run(["lpadmin", "-p", QUEUE, "-v", "usblp:///dev/usb/lp0",
                        "-m", "raw", "-E"], capture_output=True, timeout=CMD_TIMEOUT)
        msgs.append("Queue: usblp:///dev/usb/lp0 (raw)")

    else:
        return f"Unknown transport: {transport}"

    subprocess.run(["cupsenable", QUEUE], capture_output=True, timeout=CMD_TIMEOUT)
    subprocess.run(["cupsaccept", QUEUE], capture_output=True, timeout=CMD_TIMEOUT)
    msgs.append("Queue enabled")
    _probe_refresh()
    return " → ".join(msgs)


# ── CUPS probe cache ─────────────────────────────────────────────────────────
# Measured inside the running service (as root, which is when the CUPS backends
# actually probe):
#     lpinfo -v (_available_probe)     2.24 s
#     lpstat -v (_current_uri_probe)   1.08 s
# That was the entire 3.4 s page render. Both describe hardware/queue state that
# changes only when someone plugs a printer in or reconfigures the queue, so they
# are refreshed on a timer instead of on every request. Renders read memory.

_probe_lock    = threading.Lock()
_probe_devices: list[str] = []
_probe_uri: str | None    = None
_scan_results: list[dict] = []
_scan_time: float = 0.0


def _current_uri() -> str | None:
    with _probe_lock:
        return _probe_uri


def _available() -> list[str]:
    with _probe_lock:
        return list(_probe_devices)


def _probe_refresh() -> None:
    """Re-probe CUPS. Runs on a background thread and after _configure()."""
    uri  = _current_uri_probe()
    devs = _available_probe()
    global _probe_uri, _probe_devices
    with _probe_lock:
        _probe_uri, _probe_devices = uri, devs


def _probe_refresher() -> None:
    while True:
        _probe_refresh()
        time.sleep(DEVICE_TTL)


def _configure(uri: str) -> None:
    subprocess.run(["lpadmin", "-p", QUEUE, "-E", "-v", uri, "-m", "raw"],
                   check=True, timeout=CMD_TIMEOUT)
    subprocess.run(["cupsenable",  QUEUE], check=True, timeout=CMD_TIMEOUT)
    subprocess.run(["cupsaccept", QUEUE], check=True, timeout=CMD_TIMEOUT)
    subprocess.run(["lpoptions",  "-d", QUEUE], check=True, timeout=CMD_TIMEOUT)
    _probe_refresh()   # show the new binding at once, not after DEVICE_TTL


# ── Edge node (relay) status ─────────────────────────────────────────────────
# The Pi3 is the AP + DHCP server, so it can see every connected edge node.
# Sources, in order of reliability:
#   - `ip neigh` on wlan0 — authoritative "who is present"; crucially it catches
#     STATIC-IP nodes like the Pi Zero (10.99.0.2), which never appear in DHCP.
#   - dnsmasq leases  — hostname for DHCP clients.
#   - hostapd_cli     — signal strength, IF the hostapd control interface is on.
#   - a reachability ping.
# Refreshed on a timer (off the request path), same as the CUPS/MEDIASOURCE caches.
# This also generalises the old single hardcoded PI0_HOST into a live fleet view.

_edge_lock = threading.Lock()
_edge_cache: list[dict] = []


def _edge_nodes() -> list[dict]:
    with _edge_lock:
        return list(_edge_cache)


def _ping(ip: str) -> bool:
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3).returncode == 0
    except Exception:
        return False


def _hostapd_signal() -> dict:
    """mac -> signal (dBm). Empty if the hostapd control interface isn't enabled."""
    sig: dict[str, int] = {}
    try:
        out = subprocess.check_output(["hostapd_cli", "-i", RELAY_IFACE, "all_sta"],
                                      text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return sig
    mac = None
    for line in out.splitlines():
        line = line.strip()
        if re.fullmatch(r"[0-9a-fA-F:]{17}", line):
            mac = line.lower()
        elif line.startswith("signal=") and mac:
            try:
                sig[mac] = int(line.split("=", 1)[1].split()[0])
            except ValueError:
                pass
    return sig


def _dhcp_leases() -> dict:
    """mac -> (ip, hostname) from dnsmasq leases (hostname '' if unnamed)."""
    out: dict[str, tuple] = {}
    try:
        for line in Path("/var/lib/misc/dnsmasq.leases").read_text().splitlines():
            p = line.split()
            if len(p) >= 4:
                out[p[1].lower()] = (p[2], "" if p[3] == "*" else p[3])
    except Exception:
        pass
    return out


def _edge_refresh() -> None:
    nodes: dict[str, dict] = {}   # mac -> node

    # 1. Live neighbours on the AP — authoritative "present now" (catches static-IP
    #    nodes like a Pi Zero that never DHCPs).
    try:
        out = subprocess.check_output(["ip", "neigh", "show", "dev", RELAY_IFACE],
                                      text=True, timeout=5)
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 3 and p[1] == "lladdr" and p[-1] not in ("FAILED", "INCOMPLETE"):
                nodes[p[2].lower()] = {"ip": p[0], "mac": p[2].lower()}
    except Exception:
        pass

    # 2. Merge in DHCP-leased and user-NAMED nodes so a KNOWN node stays listed
    #    (as "unreachable") when it's currently offline — otherwise a node you just
    #    named would disappear from the panel the moment it drops off WiFi.
    leases  = _dhcp_leases()                       # mac -> (ip, host)
    hosts   = {m: h for m, (_i, h) in leases.items()}
    for mac, (ip, _host) in leases.items():
        nodes.setdefault(mac, {"ip": ip, "mac": mac})
    for mac in _node_names():
        nodes.setdefault(mac, {"ip": "", "mac": mac})

    sig    = _hostapd_signal()
    result = []
    for mac, n in nodes.items():
        n["host"]   = hosts.get(mac, "")
        n["signal"] = sig.get(mac)
        n["is_pi"]  = any(mac.startswith(o) for o in PI_OUIS)
        n["online"] = _ping(n["ip"]) if n["ip"] else False
        result.append(n)
    result.sort(key=lambda x: (not x["is_pi"], x["ip"] or "z"))

    global _edge_cache
    with _edge_lock:
        _edge_cache = result


def _edge_refresher() -> None:
    while True:
        _edge_refresh()
        time.sleep(EDGE_TTL)


# ── Pi0 relay config (SSH) ───────────────────────────────────────────────────

def _pi0_reachable() -> bool:
    """Cheap TCP probe before attempting SSH.

    The relay node is often absent (powered down, or replaced by the Pico). Going
    straight to ssh costs ConnectTimeout (3 s) on *every* page render; this probe
    settles it in ~50 ms.
    """
    try:
        with socket.create_connection((PI0_HOST, 22), timeout=PI0_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _pi0_ssh(cmd: str, timeout: int = 5) -> str:
    if not _pi0_reachable():
        # Callers treat an exception as "relay unreachable" (see _mediasource_get).
        raise ConnectionError(f"relay node {PI0_HOST} unreachable")
    result = subprocess.run(
        ["ssh",
         "-i", "/root/.ssh/id_ed25519_relay",
         "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=3",
         "-o", "BatchMode=yes",
         f"{PI0_USER}@{PI0_HOST}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout


def _mediasource_get() -> str | None:
    """Return current MEDIASOURCE value from Pi0 relay.conf, or None if unreachable."""
    try:
        out = _pi0_ssh(f"grep '^MEDIASOURCE=' {PI0_CONF} | cut -d= -f2-")
        return out.strip()
    except Exception:
        return None


# ── MEDIASOURCE cache ────────────────────────────────────────────────────────
# Reading this from the relay node costs a full SSH round-trip: ~2.6 s when the
# node answers (ARM11 handshake) and ~3 s of dead timeout when it does not. Doing
# that during a page render made the UI feel broken, and it would scale with the
# number of relay nodes — an unreachable node must be cheap, since nodes are
# routinely asleep or unplugged.
#
# So the value is cached and refreshed off the request path. Renders never block:
# they get the last known value plus its freshness.
#
# NOTE: PI0_HOST is a single hardcoded node. Supporting a fleet means turning it
# into a list and refreshing the nodes in parallel — the cache below is per-node
# state and would need to become a dict keyed by node.

_ms_lock  = threading.Lock()
_ms_value: str | None = None    # last known value ("" = override disabled)
_ms_fresh = False               # did the most recent refresh succeed?
_ms_at    = 0.0                 # monotonic time of last successful refresh


def _mediasource_cached() -> tuple[str | None, bool]:
    """Return (last known value, is_fresh) without ever doing I/O."""
    with _ms_lock:
        return _ms_value, _ms_fresh


def _mediasource_refresh() -> None:
    """Fetch once and update the cache. Safe to call from a background thread."""
    global _ms_value, _ms_fresh, _ms_at
    try:
        value = _mediasource_get()
    except Exception:
        value = None
    with _ms_lock:
        if value is None:
            _ms_fresh = False          # keep the last known value, flag it stale
        else:
            _ms_value, _ms_fresh, _ms_at = value, True, time.monotonic()


def _mediasource_refresher() -> None:
    while True:
        _mediasource_refresh()
        time.sleep(MEDIASOURCE_TTL)


def _mediasource_set(value: str) -> None:
    safe = value.upper().strip()
    cmd = (
        f"grep -q '^MEDIASOURCE=' {PI0_CONF} && "
        f"sed -i 's/^MEDIASOURCE=.*/MEDIASOURCE={safe}/' {PI0_CONF} || "
        f"echo 'MEDIASOURCE={safe}' >> {PI0_CONF}"
    )
    _pi0_ssh(cmd, timeout=10)
    # The write succeeded, so reflect it immediately rather than showing a stale
    # value until the next background refresh.
    global _ms_value, _ms_fresh, _ms_at
    with _ms_lock:
        _ms_value, _ms_fresh, _ms_at = safe, True, time.monotonic()


# ── Job preservation toggle ───────────────────────────────────────────────────

def _preserve_enabled() -> bool:
    try:
        for line in Path(CUPS_CONF).read_text().splitlines():
            if re.match(r"(?i)^\s*PreserveJobFiles\s+", line):
                return "yes" in line.lower()
    except Exception:
        pass
    return False


def _set_preserve(enabled: bool) -> None:
    value   = "Yes" if enabled else "No"
    content = Path(CUPS_CONF).read_text()
    if re.search(r"(?im)^PreserveJobFiles\s+\S+", content):
        content = re.sub(r"(?im)^(PreserveJobFiles\s+)\S+", rf"\g<1>{value}", content)
    else:
        content += f"\nPreserveJobFiles {value}\n"
    Path(CUPS_CONF).write_text(content)
    subprocess.run(["systemctl", "reload-or-restart", "cups"],
                   check=True, timeout=SLOW_TIMEOUT)


# ── Job listing ───────────────────────────────────────────────────────────────

def _active_ids() -> set[str]:
    try:
        out = subprocess.check_output(["lpstat", "-o", QUEUE], text=True,
                                      stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT)
        return {line.split()[0] for line in out.splitlines() if line.strip()}
    except Exception:
        return set()


def _all_jobs() -> list[dict]:
    try:
        out = subprocess.check_output(
            ["lpstat", "-W", "all", "-o", QUEUE], text=True, stderr=subprocess.DEVNULL,
            timeout=CMD_TIMEOUT)
    except Exception:
        return []
    active = _active_ids()
    jobs   = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts  = line.split()
        job_id = parts[0]                          # e.g. LaserPrinter-14
        num    = job_id.rsplit("-", 1)[-1]
        size   = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        ts     = " ".join(parts[3:]) if len(parts) > 3 else ""
        spool  = Path(f"/var/spool/cups/d{int(num):05d}-001")
        jobs.append({
            "id":       job_id,
            "num":      num,
            "size_kb":  size // 1024,
            "time":     ts,
            "active":   job_id in active,
            "has_file": spool.exists(),
            "spool":    str(spool),
        })
    return sorted(jobs, key=lambda j: int(j["num"]), reverse=True)


def _cancel(job_id: str, purge: bool = False) -> None:
    cmd = ["cancel"] + (["-x"] if purge else []) + [job_id]
    # Tolerate a non-zero exit: a job that has already completed or been purged
    # can't be cancelled, but for the Cancel/Delete buttons the outcome is the
    # same (the job is gone), so don't turn it into an error page.
    subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT)


def _reprint(spool: str) -> None:
    subprocess.run(["lpr", "-P", QUEUE, spool], check=True, timeout=SLOW_TIMEOUT)


# ── Custom node names ─────────────────────────────────────────────────────────
# User-assigned labels for relay nodes, keyed by MAC (stable per unit), so a tech
# can call a node "Cathlab-2" instead of reading relay-pizero-XXXXXX. Persisted on
# the Pi3; no firmware/node change needed.
NODE_NAMES_FILE = Path("/var/lib/relay-ui/node-names.json")
_names_lock = threading.Lock()


def _node_names() -> dict:
    with _names_lock:
        try:
            return json.loads(NODE_NAMES_FILE.read_text())
        except Exception:
            return {}


def _set_node_name(mac: str, name: str) -> None:
    mac  = (mac or "").strip().lower()
    name = (name or "").strip()
    if not mac:
        return
    with _names_lock:
        try:
            data = json.loads(NODE_NAMES_FILE.read_text())
        except Exception:
            data = {}
        if name:
            data[mac] = name
        else:
            data.pop(mac, None)                     # blank clears the custom label
        NODE_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NODE_NAMES_FILE.write_text(json.dumps(data))


def _job_sources() -> dict:
    """Map CUPS job number -> source IP, from the cups-lpd journal.

    A relay delivers via LPR, so cups-lpd logs 'Connection from <ip>' then
    'Print file - job ID = N' from the same PID; we pair them by PID. (We run as
    root, so no sudo needed.) Falls back to {} silently — the Source column just
    shows '—' for jobs whose log line has rotated away.
    """
    try:
        out = subprocess.check_output(
            ["journalctl", "-t", "cups-lpd", "--no-pager", "-n", "500"],
            text=True, stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT)
    except Exception:
        return {}
    conn: dict[str, str] = {}   # pid -> ip
    jobs: dict[str, str] = {}   # jobnum -> ip
    for line in out.splitlines():
        m = re.search(r"cups-lpd\[(\d+)\].*Connection from.*?(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            conn[m.group(1)] = m.group(2)
            continue
        m = re.search(r"cups-lpd\[(\d+)\].*job ID = (\d+)", line)
        if m and m.group(1) in conn:
            jobs[m.group(2)] = conn[m.group(1)]
    return jobs


# ── Export share (Samba import directory) ─────────────────────────────────────

IMPORT_DIR = Path("/var/lib/pacemaker-export/import")


def _export_files() -> list[dict]:
    try:
        files = sorted(IMPORT_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        return [{"name": f.name, "size": f.stat().st_size,
                 "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))}
                for f in files if f.is_file()]
    except Exception:
        return []


def _samba_status() -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", "smbd"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _clear_export_files() -> int:
    deleted = 0
    try:
        for f in IMPORT_DIR.iterdir():
            if f.is_file():
                f.unlink()
                deleted += 1
    except Exception:
        pass
    return deleted


# ── USB printer diagnostics ──────────────────────────────────────────────────

def _usb_diag() -> dict:
    """Gather USB printer diagnostic info."""
    diag: dict = {}

    # USB driver in use (dwc2 preferred, dwc_otg has NAK bugs)
    try:
        out = subprocess.check_output(["dmesg"], text=True, timeout=5)
        if "dwc2" in out:
            diag["usb_driver"] = "dwc2"
        elif "dwc_otg" in out:
            diag["usb_driver"] = "dwc_otg"
        else:
            diag["usb_driver"] = "unknown"
    except Exception:
        diag["usb_driver"] = "unknown"

    # ipp-usb service status
    try:
        r = subprocess.run(["systemctl", "is-active", "ipp-usb"],
                           capture_output=True, text=True, timeout=3)
        diag["ipp_usb_active"] = r.stdout.strip() == "active"
    except Exception:
        diag["ipp_usb_active"] = False

    # ipp-usb port reachable
    try:
        with socket.create_connection(("localhost", 60000), timeout=2):
            diag["ipp_usb_reachable"] = True
    except OSError:
        diag["ipp_usb_reachable"] = False

    # lsusb — find the printer
    try:
        out = subprocess.check_output(["lsusb"], text=True, timeout=5)
        printers = [l.strip() for l in out.splitlines()
                    if any(kw in l.lower() for kw in ("printer", "hp", "laser", "canon", "epson", "brother"))]
        diag["usb_devices"] = printers or []
    except Exception:
        diag["usb_devices"] = []

    # CUPS queue state
    try:
        out = subprocess.check_output(["lpstat", "-p", QUEUE], text=True,
                                       stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT)
        diag["queue_enabled"] = "enabled" in out.lower()
        diag["queue_idle"] = "idle" in out.lower()
    except Exception:
        diag["queue_enabled"] = False
        diag["queue_idle"] = False

    # Active/held jobs
    try:
        out = subprocess.check_output(["lpstat", "-o", QUEUE], text=True,
                                       stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT)
        diag["pending_jobs"] = len([l for l in out.splitlines() if l.strip()])
    except Exception:
        diag["pending_jobs"] = 0

    # dmesg USB errors
    try:
        out = subprocess.check_output(["dmesg"], text=True, timeout=5)
        lines = [l for l in out.splitlines()
                 if any(kw in l.lower() for kw in ("usblp", "eshutdown", "epipe",
                        "bulk status", "urb status", "usb_reset", "disconnect"))]
        diag["dmesg_usb"] = lines[-5:] if lines else []
    except Exception:
        diag["dmesg_usb"] = []

    # CUPS error log — recent job errors only
    try:
        lines = Path("/var/log/cups/error_log").read_text().splitlines()
        errors = [l for l in lines[-50:]
                  if re.search(r"\[Job \d+\]", l) and
                  any(kw in l.lower() for kw in ("fail", "block", "kill", "stopped", "held", "error"))]
        diag["cups_errors"] = errors[-5:] if errors else []
    except Exception:
        diag["cups_errors"] = []

    # Overall health assessment
    problems = []
    if not diag["usb_devices"]:
        problems.append(("No USB printer detected",
                         "Check the USB cable and make sure the printer is powered on."))
    if not diag["ipp_usb_active"]:
        problems.append(("ipp-usb service not running",
                         "Use 'Reset printer connection' below to restart it."))
    elif not diag["ipp_usb_reachable"]:
        problems.append(("ipp-usb not reachable on port 60000",
                         "The service is running but not accepting connections. "
                         "Try 'Reset printer connection' below."))
    if diag["usb_driver"] == "dwc_otg":
        problems.append(("Old USB driver (dwc_otg)",
                         "This driver has known bugs with consecutive prints. "
                         "Add dtoverlay=dwc2,dr_mode=host to config.txt and reboot."))
    if not diag["queue_enabled"]:
        problems.append(("Print queue disabled",
                         "Use 'Reset printer connection' below to re-enable it."))
    if diag["pending_jobs"] > 3:
        problems.append((f"{diag['pending_jobs']} jobs queued",
                         "Jobs may be backed up. If nothing is printing, "
                         "cancel all and power cycle the printer."))
    diag["problems"] = problems
    diag["healthy"] = (len(problems) == 0 and bool(diag["usb_devices"])
                       and diag["ipp_usb_reachable"])

    return diag


def _usb_test_print() -> str:
    """Send a minimal PCL test page. Returns status message."""
    pcl = (b"\x1b%-12345X@PJL\r\n@PJL ENTER LANGUAGE = PCL\r\n"
           b"\x1bE\x1b&l0O\x1b(s0P\x1b(s12H"
           + f"\r\n\r\n     USB test page  {time.strftime('%Y-%m-%d %H:%M:%S')}\r\n".encode()
           + b"\x0c\x1bE\x1b%-12345X")
    try:
        r = subprocess.run(["lp", "-d", QUEUE, "-o", "raw", "-"],
                           input=pcl, capture_output=True, timeout=SLOW_TIMEOUT)
        if r.returncode == 0:
            return "Test page sent — check printer output."
        return f"lp failed: {r.stderr.decode().strip()}"
    except Exception as exc:
        return f"Error: {exc}"


def _usb_reset() -> str:
    """Full printer connection reset: cancel jobs, restart ipp-usb, re-enable queue.

    This is the field-ready recovery procedure when the printer stops responding.
    ipp-usb is udev-activated and manages the USB device; restarting it forces a
    fresh connection to the printer's IPP-over-USB interface (protocol 4).
    """
    msgs = []

    # 1. Cancel all pending jobs
    subprocess.run(["cancel", "-a"], capture_output=True, timeout=CMD_TIMEOUT)
    msgs.append("All jobs cancelled")

    # 2. Restart ipp-usb (forces fresh USB connection)
    subprocess.run(["systemctl", "restart", "ipp-usb"],
                   capture_output=True, timeout=SLOW_TIMEOUT)
    time.sleep(3)

    # 3. Verify ipp-usb is serving
    try:
        with socket.create_connection(("localhost", 60000), timeout=5):
            msgs.append("ipp-usb restarted — port 60000 OK")
    except OSError:
        msgs.append("WARNING: ipp-usb not responding on port 60000 — power cycle the printer")

    # 4. Re-enable CUPS queue
    subprocess.run(["cupsenable", QUEUE], capture_output=True, timeout=CMD_TIMEOUT)
    subprocess.run(["cupsaccept", QUEUE], capture_output=True, timeout=CMD_TIMEOUT)
    msgs.append("Print queue re-enabled")

    _probe_refresh()
    return " → ".join(msgs) + "."


# ── HTML rendering ────────────────────────────────────────────────────────────

CSS = """
body  { font-family: sans-serif; max-width: 800px; margin: 2em auto; padding: 0 1em; color: #222; }
h1   { font-size: 1.2em; margin-bottom: 0.2em; }
h2   { font-size: 1em; margin: 1.4em 0 0.4em; color: #555; border-bottom: 1px solid #eee; padding-bottom: 0.2em; }
.cur { font-family: monospace; font-size: 0.85em; background: #f4f4f4;
       padding: 0.5em 0.7em; border: 1px solid #ddd; border-radius: 3px; }
table { border-collapse: collapse; width: 100%; margin-top: 0.3em; }
td, th { padding: 0.4em 0.6em; border: 1px solid #ddd; vertical-align: middle; }
th   { background: #f8f8f8; font-weight: normal; color: #555; text-align: left; }
.uri { font-family: monospace; font-size: 0.82em; }
.empty { color: #888; font-style: italic; }
button { padding: 0.3em 0.7em; cursor: pointer; }
button[disabled] { opacity: 0.45; cursor: default; }
button.danger { color: #c33; }
.ok   { color: #2a7; font-weight: bold; }
.error { color: #c33; font-weight: bold; }
.hint { font-size: 0.78em; color: #888; margin-top: 1.2em; }
.toggle { display: flex; align-items: center; gap: 0.6em; margin: 0.4em 0; }
.badge-active { background:#d4edda; color:#155724; padding:0.1em 0.5em;
                border-radius:3px; font-size:0.8em; }
.badge-done   { background:#e2e3e5; color:#383d41; padding:0.1em 0.5em;
                border-radius:3px; font-size:0.8em; }
.tabs { display:flex; gap:0.3em; border-bottom:2px solid #ddd; margin:1em 0 0.8em; }
.tabs button { border:1px solid #ddd; border-bottom:none; background:#f4f4f4;
               padding:0.5em 1.1em; border-radius:4px 4px 0 0; margin-bottom:-2px;
               color:#555; font-size:0.95em; }
.tabs button.active { background:#fff; color:#222; font-weight:bold;
                      border-bottom:2px solid #fff; }
.tab { display:none; }
.tab.active { display:block; }
.rename { display:inline-flex; gap:0.3em; align-items:center; margin:0; }
.rename input { width:9em; padding:0.2em 0.35em; font-size:0.85em; }
.sub { color:#999; font-size:0.72em; margin-top:0.2em; font-family:monospace; }
.src { font-size:0.82em; }
"""

def _btn(action: str, **fields) -> str:
    inputs = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
    labels = {"cancel": "Cancel", "delete": "Delete", "reprint": "Reprint", "configure": "Use",
              "set_preserve": ""}
    return (f'<form method="post" action="/{action}" style="display:inline">'
            f'{inputs}<button type="submit"'
            f'{"  class=danger" if action in ("cancel","delete") else ""}>'
            f'{labels.get(action, action)}</button></form>')


def _page(message: str = "", error: bool = False) -> bytes:
    cur        = _current_uri()
    preserve   = _preserve_enabled()
    jobs       = _all_jobs()
    names      = _node_names()
    nodes      = _edge_nodes()
    job_src    = _job_sources()            # job number -> source IP
    # source IP -> friendly label: custom name > auto hostname > generic
    ip2label = {}
    for n in nodes:
        lbl = names.get(n["mac"]) or n["host"] or ("relay" if n["is_pi"] else "client")
        if n["ip"]:
            ip2label[n["ip"]] = lbl
    # Cached: never blocks the render, even when the relay node is unreachable.
    # mediasrc None = never successfully read; ms_fresh False = last refresh failed
    # (value shown is the last known one).
    mediasrc, ms_fresh = _mediasource_cached()
    export_files = _export_files()
    samba_st     = _samba_status()
    diag         = _usb_diag()

    # ── scanned printer rows
    with _probe_lock:
        scanned = list(_scan_results)
        scan_age = time.monotonic() - _scan_time if _scan_time else None
    scan_rows = ""
    for sp in scanned:
        protos = ", ".join(sp["proto_names"])
        rec = sp["transport"]
        rec_label = "ipp-usb (recommended)" if sp["has_ipp"] else "usblp (raw)"
        vid_pid = f'{sp["vid"]}:{sp["pid"]}'
        scan_rows += (
            f'<tr><td><strong>{sp["name"]}</strong><br>'
            f'<span class="sub">{vid_pid}</span></td>'
            f'<td style="font-size:0.85em">{protos}</td>'
            f'<td>{"<span class=ok>IPP-USB</span>" if sp["has_ipp"] else "<span style=color:#888>No</span>"}</td>'
            f'<td><form method="post" action="/setup_printer" style="display:inline">'
            f'<input type="hidden" name="vid_pid" value="{vid_pid}">'
            f'<input type="hidden" name="transport" value="{rec}">'
            f'<button type="submit">Use ({rec_label})</button></form></td></tr>'
        )
    if not scan_rows and scan_age is not None:
        scan_rows = '<tr><td colspan="4" class="empty">No USB printers found. Check cable and power.</td></tr>'

    # ── job rows
    job_rows = ""
    for j in jobs:
        badge  = '<span class="badge-active">printing</span>' if j["active"] else '<span class="badge-done">done</span>'
        reprint_btn = _btn("reprint", spool=j["spool"]) if (j["has_file"] and not j["active"]) else \
                      ('<button disabled title="Enable \'Preserve jobs\' to reprint">Reprint</button>' if not j["active"] else "")
        cancel_btn  = _btn("cancel", job_id=j["id"]) if j["active"] else _btn("delete", job_id=j["id"])
        src_ip = job_src.get(j["num"])
        src    = ip2label.get(src_ip) or (src_ip or "—")
        job_rows += (
            f'<tr>'
            f'<td>#{j["num"]}</td>'
            f'<td>{badge}</td>'
            f'<td class="src">{src}</td>'
            f'<td>{j["size_kb"]} KB</td>'
            f'<td style="font-size:0.85em">{j["time"]}</td>'
            f'<td>{reprint_btn} {cancel_btn}</td>'
            f'</tr>'
        )
    if not job_rows:
        job_rows = '<tr><td colspan="6" class="empty">No jobs.</td></tr>'

    # ── preserve toggle
    toggle = (
        f'<div class="toggle">'
        f'<form method="post" action="/set_preserve">'
        f'<input type="hidden" name="enabled" value="{"0" if preserve else "1"}">'
        f'<button type="submit">{"Disable" if preserve else "Enable"} job preservation</button>'
        f'</form>'
        f'<span style="color:#888;font-size:0.85em">'
        f'{"Jobs are kept in spool — reprint available." if preserve else "Jobs are discarded after printing."}'
        f'</span></div>'
    )

    # ── tray override toggle
    if mediasrc is None:
        tray_widget = ('<span style="color:#888;font-size:0.85em">'
                       'Relay node not read yet — unreachable since start-up.</span>')
    else:
        tray_on = bool(mediasrc)
        stale = ('' if ms_fresh else
                 ' <b>(last known — relay node currently unreachable)</b>')
        tray_widget = (
            f'<div class="toggle">'
            f'<form method="post" action="/set_mediasource">'
            f'<input type="hidden" name="value" value="{"" if tray_on else "TRAY2"}">'
            f'<button type="submit">{"Disable" if tray_on else "Enable"} tray 2 override</button>'
            f'</form>'
            f'<span style="color:#888;font-size:0.85em">'
            f'{"PJL MEDIASOURCE=TRAY2 injected into every job." if tray_on else "Printer uses its own tray default (recommended)."}'
            f'{stale}'
            f'</span></div>'
        )

    # ── edge node rows (with an inline custom-name field, keyed by MAC)
    edge_rows = ""
    for n in nodes:
        auto   = n["host"] or ("relay node" if n["is_pi"] else "client")
        custom = names.get(n["mac"], "")
        kind   = "Pi relay" if n["is_pi"] else "other"
        # brcmfmac AP mode reports signal=0 (unknown); show it as "—", not "0 dBm".
        sig    = f'{n["signal"]} dBm' if n["signal"] else "—"
        status = ('<span class="ok">online</span>' if n["online"]
                  else '<span class="error">unreachable</span>')
        name_cell = (
            f'<form method="post" action="/set_node_name" class="rename">'
            f'<input type="hidden" name="mac" value="{n["mac"]}">'
            f'<input name="name" value="{custom}" placeholder="{auto}">'
            f'<button type="submit">Save</button></form>'
            f'<div class="sub">{auto}</div>'
        )
        edge_rows += (
            f'<tr><td>{name_cell}</td><td class="uri">{n["ip"]}</td>'
            f'<td class="uri">{n["mac"]}</td><td>{kind}</td>'
            f'<td>{sig}</td><td>{status}</td></tr>'
        )
    if not edge_rows:
        edge_rows = '<tr><td colspan="6" class="empty">No edge nodes connected.</td></tr>'

    # ── export file rows
    export_rows = ""
    for ef in export_files:
        sz = f"{ef['size']:,}" if ef["size"] < 1024 * 1024 else f"{ef['size'] / 1024 / 1024:.1f} MB"
        export_rows += f'<tr><td>{ef["name"]}</td><td>{sz}</td><td>{ef["mtime"]}</td></tr>'
    if not export_rows:
        export_rows = '<tr><td colspan="3" class="empty">No files in share.</td></tr>'
    samba_badge = "ok" if samba_st == "active" else "error"

    msg = f'<p class="{"error" if error else "ok"}">{message}</p>' if message else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>Relay Hub</title>
  <style>{CSS}</style>
</head>
<body>
  <h1>Pacemaker Relay Hub</h1>
  {msg}

  <div class="tabs">
    <button type="button" data-tab="jobs">Jobs</button>
    <button type="button" data-tab="printers">Printers</button>
    <button type="button" data-tab="export">Export</button>
    <button type="button" data-tab="nodes">Nodes</button>
    <button type="button" data-tab="diag">Diag</button>
  </div>

  <section id="tab-jobs" class="tab">
    {toggle}
    <table>
      <tr><th>#</th><th>Status</th><th>Source</th><th>Size</th><th>Time</th><th>Actions</th></tr>
      {job_rows}
    </table>
  </section>

  <section id="tab-printers" class="tab">
    <h2>Active printer</h2>
    <div class="cur">{cur or "Not configured"}</div>

    <h2>Search for printers</h2>
    <form method="post" action="/scan_printers" style="margin-bottom:0.6em">
      <button type="submit">Scan USB</button>
      <span style="font-size:0.85em;color:#888;margin-left:0.5em">
        {f'Last scan: {int(scan_age)}s ago' if scan_age is not None else 'Not scanned yet'}
      </span>
    </form>
    {'<table><tr><th>Printer</th><th>Protocols</th><th>IPP-USB</th><th>Action</th></tr>' + scan_rows + '</table>' if scan_rows else ''}
    <p style="font-size:0.82em;color:#888;margin-top:0.5em">
      Printers with IPP-over-USB (protocol 4) use <b>ipp-usb</b> for reliable consecutive printing.<br>
      Older printers without protocol 4 use <b>usblp</b> with raw mode.
    </p>

    <h2>Print settings</h2>
    {tray_widget}
  </section>

  <section id="tab-export" class="tab">
    <p>
      Samba: <span class="{samba_badge}" style="padding:0.1em 0.5em;border-radius:3px;font-size:0.85em">{samba_st}</span>
      &nbsp; Files: <strong>{len(export_files)}</strong>
      &nbsp; Share: <code>\\\\PI3\\import</code>
    </p>
    <form method="post" action="/clear_export"
          onsubmit="return confirm('Delete all files from the import share?')">
      <button type="submit" class="danger">Clear all files</button>
    </form>
    <table style="margin-top:0.6em">
      <tr><th>Filename</th><th>Size</th><th>Received</th></tr>
      {export_rows}
    </table>
  </section>

  <section id="tab-nodes" class="tab">
    <table>
      <tr><th>Name</th><th>IP</th><th>MAC</th><th>Type</th><th>Signal</th><th>Status</th></tr>
      {edge_rows}
    </table>
  </section>

  <section id="tab-diag" class="tab">
    <h2>Printer Status</h2>
    <div style="padding:0.7em;border-radius:5px;margin-bottom:0.8em;background:{'#d4edda;border:1px solid #c3e6cb' if diag.get('healthy') else '#f8d7da;border:1px solid #f5c6cb'}">
      <strong>{'Printer ready' if diag.get('healthy') else 'Printer has issues — see below'}</strong>
    </div>

    {''.join(
      f'<div style="padding:0.5em 0.7em;margin-bottom:0.4em;background:#fff3cd;border:1px solid #ffc107;border-radius:4px">'
      f'<strong>{p[0]}</strong><br><span style="font-size:0.9em">{p[1]}</span></div>'
      for p in diag.get('problems', [])
    )}

    <table>
      <tr><td>USB driver</td><td>{'<span class="ok">dwc2</span>' if diag.get('usb_driver') == 'dwc2' else '<span class="error">' + str(diag.get('usb_driver', '?')) + '</span>'}</td></tr>
      <tr><td>Printer</td><td>{diag['usb_devices'][0] if diag.get('usb_devices') else '<span class="error">not detected</span>'}</td></tr>
      <tr><td>ipp-usb</td><td>{'<span class="ok">active (port 60000)</span>' if diag.get('ipp_usb_reachable') else ('<span class="error">not reachable</span>' if diag.get('ipp_usb_active') else '<span class="error">not running</span>')}</td></tr>
      <tr><td>Print queue</td><td>{'<span class="ok">enabled</span>' if diag.get('queue_enabled') else '<span class="error">disabled</span>'}
        {', idle' if diag.get('queue_idle') else ''}</td></tr>
      <tr><td>Pending jobs</td><td>{diag.get('pending_jobs', 0)}</td></tr>
    </table>

    <h2>Actions</h2>
    <div style="display:flex;gap:0.5em;flex-wrap:wrap;margin:0.5em 0">
      <form method="post" action="/diag_test_print"><button type="submit">Test print</button></form>
      <form method="post" action="/diag_usb_reset"
            onsubmit="return confirm('This will cancel all jobs and reset the USB printer connection. Continue?')">
        <button type="submit" class="danger">Reset printer connection</button></form>
    </div>
    <p style="font-size:0.85em;color:#555;margin:0.6em 0">
      <b>Test print</b> — sends a one-line test page to verify the printer works.<br>
      <b>Reset printer connection</b> — cancels all jobs, restarts ipp-usb,
      and re-enables the queue.
      Use this when jobs are stuck or the printer stops responding.<br>
      If reset does not help, <b>power cycle the printer</b> (turn off, wait 10 seconds, turn on).
    </p>

    {'<h2>Recent errors</h2><pre style="font-size:0.75em;background:#f4f4f4;padding:0.5em;overflow-x:auto;max-height:8em">' + '&#10;'.join(diag.get('cups_errors', [])) + '</pre>' if diag.get('cups_errors') else ''}
    {'<h2>USB log</h2><pre style="font-size:0.75em;background:#f4f4f4;padding:0.5em;overflow-x:auto;max-height:8em">' + '&#10;'.join(diag.get('dmesg_usb', [])) + '</pre>' if diag.get('dmesg_usb') else ''}
  </section>

  <p class="hint">Refresh after plugging in a new printer or sending a print job.</p>

  <script>
  (function() {{
    var tabs = document.querySelectorAll('.tabs button');
    function show(name) {{
      document.querySelectorAll('.tab').forEach(function(s) {{
        s.classList.toggle('active', s.id === 'tab-' + name);
      }});
      tabs.forEach(function(b) {{
        b.classList.toggle('active', b.dataset.tab === name);
      }});
      try {{ localStorage.setItem('relayTab', name); }} catch (e) {{}}
    }}
    tabs.forEach(function(b) {{
      b.addEventListener('click', function() {{ show(b.dataset.tab); }});
    }});
    var saved = 'jobs';
    try {{ saved = localStorage.getItem('relayTab') || 'jobs'; }} catch (e) {{}}
    show(document.getElementById('tab-' + saved) ? saved : 'jobs');
  }})();
  </script>
</body>
</html>"""
    return html.encode()


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return parse_qs(self.rfile.read(length).decode())

    def do_GET(self):
        self._send(_page())

    def do_POST(self):
        # /ingest-export is raw binary, not form-encoded — handle before _params().
        if self.path == "/ingest-export":
            try:
                fname = self.headers.get("X-File-Name", "")
                if not fname:
                    fname = f"export_{int(time.time())}.bin"
                fname = Path(fname).name
                stem, suffix = Path(fname).stem, Path(fname).suffix
                ts = time.strftime("%Y%m%d_%H%M%S")
                fname = f"{stem}_{ts}{suffix}"
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                IMPORT_DIR.mkdir(parents=True, exist_ok=True)
                (IMPORT_DIR / fname).write_bytes(data)
                self._send(b"accepted", 202)
            except Exception as exc:
                self._send(str(exc).encode(), 500)
            return

        p = self._params()

        try:
            if self.path == "/configure":
                uri = p.get("uri", [""])[0]
                if not uri:
                    raise ValueError("No URI")
                _configure(uri)
                self._send(_page(f"Configured: {uri}"))

            elif self.path == "/cancel":
                job_id = p.get("job_id", [""])[0]
                _cancel(job_id)
                self._send(_page(f"Cancelled job {job_id}"))

            elif self.path == "/delete":
                job_id = p.get("job_id", [""])[0]
                _cancel(job_id, purge=True)
                self._send(_page(f"Deleted job {job_id}"))

            elif self.path == "/reprint":
                spool = p.get("spool", [""])[0]
                if not Path(spool).exists():
                    raise FileNotFoundError(f"Spool file missing: {spool}")
                _reprint(spool)
                self._send(_page("Reprint submitted."))

            elif self.path == "/set_preserve":
                enabled = p.get("enabled", ["0"])[0] == "1"
                _set_preserve(enabled)
                state = "enabled" if enabled else "disabled"
                self._send(_page(f"Job preservation {state}. CUPS restarted."))

            elif self.path == "/set_mediasource":
                value = p.get("value", [""])[0]
                _mediasource_set(value)
                state = f"MEDIASOURCE={value}" if value else "tray override disabled"
                self._send(_page(f"Relay updated: {state}."))

            elif self.path == "/set_node_name":
                mac  = p.get("mac", [""])[0]
                name = p.get("name", [""])[0]
                _set_node_name(mac, name)
                msg = f'Node {mac} named "{name}"' if name.strip() else f"Cleared name for {mac}"
                self._send(_page(msg))

            elif self.path == "/clear_export":
                n = _clear_export_files()
                self._send(_page(f"Cleared {n} file(s) from export share."))

            elif self.path == "/scan_printers":
                global _scan_results, _scan_time
                results = _scan_usb_printers()
                with _probe_lock:
                    _scan_results = results
                    _scan_time = time.monotonic()
                n = len(results)
                self._send(_page(f"Found {n} printer{'s' if n != 1 else ''}."))

            elif self.path == "/setup_printer":
                vid_pid = p.get("vid_pid", [""])[0]
                transport = p.get("transport", [""])[0]
                if not vid_pid or not transport:
                    raise ValueError("Missing printer or transport")
                msg = _setup_printer(vid_pid, transport)
                self._send(_page(f"Configured: {msg}"))

            elif self.path == "/diag_test_print":
                msg = _usb_test_print()
                self._send(_page(msg))

            elif self.path == "/diag_usb_reset":
                msg = _usb_reset()
                self._send(_page(msg))

            else:
                self._send(b"Not found", 404)

        except Exception as exc:
            self._send(_page(str(exc), error=True))


if __name__ == "__main__":
    # Keep the relay-node SSH off the request path entirely.
    _probe_refresh()   # prime the cache so the first page is correct
    _edge_refresh()
    threading.Thread(target=_probe_refresher, daemon=True).start()
    threading.Thread(target=_mediasource_refresher, daemon=True).start()
    threading.Thread(target=_edge_refresher, daemon=True).start()

    # Threaded: a single slow/blocked CUPS call must not freeze the whole UI.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    server.daemon_threads = True
    print(f"Relay Hub UI on port {PORT}")
    server.serve_forever()
