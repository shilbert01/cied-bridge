#!/usr/bin/env python3
"""Relay Hub — printer, export, and node management web UI.

Serves http://10.99.0.1/ on the relayap WiFi.
"""

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

QUEUE     = "LaserPrinter"
PORT      = 80
CUPS_CONF = "/etc/cups/cupsd.conf"
VERSION_FILE = Path("/etc/pacemaker/version")

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

def _detect_ap_iface() -> str:
    try:
        for line in Path("/etc/hostapd/hostapd.conf").read_text().splitlines():
            if line.startswith("interface="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "wlan0"

RELAY_IFACE = _detect_ap_iface()
# Raspberry Pi MAC OUIs — used to flag which associated stations are relay nodes.
PI_OUIS = ("b8:27:eb", "dc:a6:32", "d8:3a:dd", "e4:5f:01", "28:cd:c1", "2c:cf:67")


# ── Version ──────────────────────────────────────────────────────────────────

def _read_version() -> dict:
    info = {"version": "unknown", "commit": "unknown", "deployed": "unknown"}
    try:
        for line in VERSION_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                info[k.strip()] = v.strip()
    except OSError:
        pass
    return info


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
#   - hostapd_cli     — station management (deauth, flush).
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
    for mac in _node_firmware():
        nodes.setdefault(mac, {"ip": "", "mac": mac})

    result = []
    for mac, n in nodes.items():
        n["host"]   = hosts.get(mac, "")
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
NODE_FW_FILE    = Path("/var/lib/relay-ui/node-firmware.json")
FIRMWARE_DIR    = Path("/usr/local/share/relay-firmware")
JOB_ARCHIVE_DIR = Path("/var/lib/relay-ui/job-archive")
ARCHIVE_RETENTION_DAYS = 30
ARCHIVE_MAX_JOBS = 200
ARCHIVE_SCAN_INTERVAL = 15
_names_lock = threading.Lock()
_fw_lock    = threading.Lock()
_archive_lock = threading.Lock()


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


def _node_firmware() -> dict:
    with _fw_lock:
        try:
            return json.loads(NODE_FW_FILE.read_text())
        except Exception:
            return {}


def _set_node_firmware(mac: str, node_id: str, firmware: str) -> None:
    mac = (mac or "").strip().lower()
    if not mac:
        return
    with _fw_lock:
        try:
            data = json.loads(NODE_FW_FILE.read_text())
        except Exception:
            data = {}
        data[mac] = {
            "node_id": node_id,
            "firmware": firmware,
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        NODE_FW_FILE.parent.mkdir(parents=True, exist_ok=True)
        NODE_FW_FILE.write_text(json.dumps(data))


def _usb_esp_devices() -> list[dict]:
    """Detect ESP32-S3 boards connected via USB (shows as /dev/ttyACM*).

    The UART port uses a QinHeng CH340 (1a86:55d3), not an Espressif VID,
    so we match both.
    """
    ESP_VIDS = {"303a:", "1a86:"}
    devices = []
    try:
        out = subprocess.check_output(["lsusb"], text=True, timeout=5)
        for line in out.splitlines():
            if any(vid in line.lower() for vid in ESP_VIDS):
                m = re.search(r"Bus (\d+) Device (\d+): ID (\S+)\s+(.*)", line)
                if m:
                    devices.append({
                        "bus": m.group(1), "dev": m.group(2),
                        "id": m.group(3), "name": m.group(4).strip(),
                    })
    except Exception:
        pass
    import glob
    ports = sorted(glob.glob("/dev/ttyACM*"))
    for i, d in enumerate(devices):
        d["port"] = ports[i] if i < len(ports) else ""
    return devices


def _latest_firmware_bin() -> Path | None:
    """Return the newest .bin file in the firmware directory."""
    if not FIRMWARE_DIR.is_dir():
        return None
    bins = sorted(FIRMWARE_DIR.glob("*.bin"), key=lambda f: f.stat().st_mtime, reverse=True)
    return bins[0] if bins else None


def _firmware_bin_version(fw: Path | None) -> str:
    """Extract the semver string baked into the firmware binary.

    The version is a null-terminated C string from FIRMWARE_VERSION.
    Match only short segments (0-99) to avoid IP addresses like 10.99.0.1.
    """
    if not fw:
        return ""
    try:
        raw = fw.read_bytes()
        for m in re.finditer(rb"\x00(\d{1,2}\.\d{1,2}\.\d{1,2})\x00", raw):
            return m.group(1).decode("ascii")
        return ""
    except OSError:
        return ""


def _flash_esp(port: str) -> str:
    """Flash the latest firmware binary to an ESP32-S3 on the given serial port."""
    fw = _latest_firmware_bin()
    if not fw:
        return f"No firmware binary found in {FIRMWARE_DIR}"
    if not Path(port).exists():
        return f"Serial port {port} not found"
    try:
        r = subprocess.run(
            ["esptool.py", "--chip", "esp32s3", "-p", port, "-b", "460800",
             "--before=default_reset", "--after=hard_reset",
             "write_flash", "--flash_mode", "dio", "--flash_freq", "80m",
             "--flash_size", "16MB", "0x10000", str(fw)],
            capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return f"Flashed {fw.name} to {port} successfully."
        return f"Flash failed: {r.stderr[-500:]}"
    except subprocess.TimeoutExpired:
        return "Flash timed out after 120 seconds."
    except FileNotFoundError:
        return "esptool.py not found — install with: pip3 install esptool"
    except Exception as exc:
        return f"Flash error: {exc}"


def _ensure_preserve_jobs() -> None:
    """Enable PreserveJobFiles so the archiver can capture completed job data."""
    if not _preserve_enabled():
        _set_preserve(True)


def _archive_convert(pcl_path: Path, pdf_path: Path) -> bool:
    for cmd in ("gpcl6", "pcl6", "gs"):
        try:
            r = subprocess.run(
                [cmd, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dQUIET",
                 "-dSAFER", f"-sOutputFile={pdf_path}", str(pcl_path)],
                capture_output=True, timeout=120)
            if r.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
                return True
            pdf_path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except Exception:
            pdf_path.unlink(missing_ok=True)
    return False


def _archived_jobs() -> list[dict]:
    if not JOB_ARCHIVE_DIR.is_dir():
        return []
    # List by PCL (always present), join with PDF if it exists.
    pcls = sorted(JOB_ARCHIVE_DIR.glob("*.pcl"),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for pcl in pcls:
        pdf = pcl.with_suffix(".pdf")
        st = pcl.stat()
        result.append({
            "num": pcl.stem,
            "has_pdf": pdf.exists(),
            "pdf": pdf.name,
            "has_pcl": True,
            "size_kb": pdf.stat().st_size // 1024 if pdf.exists() else 0,
            "pcl_kb": st.st_size // 1024,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    return result


def _archive_scan() -> None:
    with _archive_lock:
        JOB_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        existing = {f.stem for f in JOB_ARCHIVE_DIR.glob("*.pdf")}
        existing |= {f.stem for f in JOB_ARCHIVE_DIR.glob("*.pcl")}

        jobs = _all_jobs()
        for job in jobs:
            if job["active"] or not job["has_file"]:
                continue
            num = job["num"].zfill(5)
            if num in existing:
                continue

            pcl_path = JOB_ARCHIVE_DIR / f"{num}.pcl"
            pdf_path = JOB_ARCHIVE_DIR / f"{num}.pdf"
            try:
                shutil.copy2(job["spool"], pcl_path)
            except Exception:
                continue

            _archive_convert(pcl_path, pdf_path)


def _archive_retention() -> None:
    if not JOB_ARCHIVE_DIR.is_dir():
        return
    cutoff = time.time() - ARCHIVE_RETENTION_DAYS * 86400
    for f in list(JOB_ARCHIVE_DIR.iterdir()):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    pdfs = sorted(JOB_ARCHIVE_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
    while len(pdfs) > ARCHIVE_MAX_JOBS:
        oldest = pdfs.pop(0)
        oldest.unlink(missing_ok=True)
        oldest.with_suffix(".pcl").unlink(missing_ok=True)


def _archive_delete(num: str) -> None:
    with _archive_lock:
        for ext in (".pdf", ".pcl"):
            (JOB_ARCHIVE_DIR / f"{num}{ext}").unlink(missing_ok=True)


def _archive_refresher() -> None:
    time.sleep(10)
    while True:
        try:
            _archive_scan()
            _archive_retention()
        except Exception:
            pass
        time.sleep(ARCHIVE_SCAN_INTERVAL)


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
IMPORT_HUB_URL = "http://localhost:8090"


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


def _submit_to_hub(filename: str, data: bytes) -> None:
    """Forward an ingested file to the import hub so CDC receivers can claim it."""
    try:
        req = Request(f"{IMPORT_HUB_URL}/submit", data=data, method="POST",
                      headers={"X-File-Name": filename,
                               "Content-Type": "application/octet-stream"})
        urlopen(req, timeout=10)
    except Exception:
        pass


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
                    if any(kw in l.lower() for kw in ("printer", "hp", "laser", "canon", "epson", "brother", "kyocera", "utax"))]
        diag["usb_devices"] = printers or []
    except Exception:
        diag["usb_devices"] = []

    # CUPS queue state
    try:
        out = subprocess.check_output(["lpstat", "-p", QUEUE], text=True,
                                       stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT,
                                       env={**os.environ, "LANG": "C"})
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

    # Current queue transport: usblp:// means a non-ipp printer — ipp-usb is N/A.
    cur_uri = _current_uri() or ""
    diag["uses_usblp"] = cur_uri.startswith("usblp:")

    # Overall health assessment
    problems = []
    if not diag["usb_devices"]:
        problems.append(("No USB printer detected",
                         "Check the USB cable and make sure the printer is powered on."))
    if not diag["uses_usblp"]:
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
    if diag["uses_usblp"]:
        diag["healthy"] = (len(problems) == 0 and bool(diag["usb_devices"])
                           and diag["queue_enabled"])
    else:
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


def _wifi_flush_arp() -> str:
    """Flush stale ARP entries and deauth orphaned hostapd stations."""
    msgs = []
    for state in ("failed", "stale", "incomplete"):
        subprocess.run(["ip", "neigh", "flush", "dev", "wlan0", "nud", state],
                       capture_output=True, timeout=CMD_TIMEOUT)
    msgs.append("ARP cache flushed")

    try:
        r = subprocess.run(["/usr/sbin/hostapd_cli", "-p", "/var/run/hostapd", "all_sta"], capture_output=True,
                           text=True, timeout=CMD_TIMEOUT)
        neigh = subprocess.run(["ip", "neigh", "show", "dev", "wlan0"],
                               capture_output=True, text=True, timeout=CMD_TIMEOUT)
        neigh_out = neigh.stdout
        deauthed = 0
        for line in r.stdout.splitlines():
            mac = line.strip()
            if len(mac) == 17 and mac.count(":") == 5 and mac not in neigh_out:
                subprocess.run(["/usr/sbin/hostapd_cli", "-p", "/var/run/hostapd", "deauthenticate", mac],
                               capture_output=True, timeout=CMD_TIMEOUT)
                deauthed += 1
        if deauthed:
            msgs.append(f"deauthenticated {deauthed} stale station(s)")
        else:
            msgs.append("no stale stations")
    except Exception as exc:
        msgs.append(f"hostapd check failed: {exc}")

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
.banner { padding: 0.7em 1em; border-radius: 6px; margin-bottom: 1em; }
.banner.ok { background: #d4edda; color: #155724; }
.banner.error { background: #f8d7da; color: #721c24; }
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
    fw_info      = _node_firmware()
    usb_esps     = _usb_esp_devices()
    latest_fw    = _latest_firmware_bin()
    avail_fw_ver = _firmware_bin_version(latest_fw)
    export_files = _export_files()
    samba_st     = _samba_status()
    diag         = _usb_diag()
    archived     = _archived_jobs()
    ver          = _read_version()

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
        fw_node_id = fw_info.get(n["mac"], {}).get("node_id", "")
        auto   = n["host"] or fw_node_id or ("relay node" if n["is_pi"] else "client")
        custom = names.get(n["mac"], "")
        kind   = "Pi relay" if n["is_pi"] else ("ESP relay" if fw_node_id else "other")
        status = ('<span class="ok">online</span>' if n["online"]
                  else '<span class="error">unreachable</span>')
        fw_entry = fw_info.get(n["mac"], {})
        fw_ver   = fw_entry.get("firmware", "")
        if fw_ver and avail_fw_ver and fw_ver != avail_fw_ver:
            fw_cell = f'<span class="uri">{fw_ver}</span> <span class="error" title="available: {avail_fw_ver}">↑{avail_fw_ver}</span>'
        elif fw_ver:
            fw_cell = f'<span class="uri">{fw_ver}</span>'
        else:
            fw_cell = '<span class="empty">—</span>'
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
            f'<td>{fw_cell}</td><td>{status}</td></tr>'
        )
    if not edge_rows:
        edge_rows = '<tr><td colspan="6" class="empty">No edge nodes connected.</td></tr>'

    # USB-connected ESP32 devices (for flashing)
    esp_rows = ""
    for esp in usb_esps:
        port = esp["port"]
        if port:
            fw_avail_cell = f'<span class="uri">{avail_fw_ver}</span>' if avail_fw_ver else '<span class="empty">—</span>'
            esp_rows += (
                f'<tr><td>{esp["name"]}</td><td class="uri">{port}</td>'
                f'<td class="uri">{esp["id"]}</td>'
                f'<td>{fw_avail_cell}</td>'
                f'<td><form method="post" action="/flash_esp" style="display:inline"'
                f' onsubmit="return confirm(\'Flash firmware to {port}?\')">'
                f'<input type="hidden" name="port" value="{port}">'
                f'<button type="submit">Flash</button></form></td></tr>'
            )
    fw_name = latest_fw.name if latest_fw else "none"
    fw_label = f"{fw_name} (v{avail_fw_ver})" if avail_fw_ver else fw_name

    # ── export file rows
    export_rows = ""
    for ef in export_files:
        sz = f"{ef['size']:,}" if ef["size"] < 1024 * 1024 else f"{ef['size'] / 1024 / 1024:.1f} MB"
        export_rows += (f'<tr><td>{ef["name"]}</td><td>{sz}</td><td>{ef["mtime"]}</td>'
                        f'<td><a href="/export/{ef["name"]}" target="_blank">View</a> '
                        f'<a href="/export/{ef["name"]}?dl=1">Download</a> '
                        f'<a href="#" onclick="sendToHub(\'{ef["name"]}\');return false">Send</a>'
                        f'</td></tr>')
    if not export_rows:
        export_rows = '<tr><td colspan="4" class="empty">No files in share.</td></tr>'
    samba_badge = "ok" if samba_st == "active" else "error"

    msg = f'<p class="banner {"error" if error else "ok"}">{message}</p>' if message else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {'<!-- auto-refresh suppressed: showing result message -->' if message else '<meta http-equiv="refresh" content="10">'}
  <title>Relay Hub</title>
  <style>{CSS}</style>
</head>
<body>
  <h1>Pacemaker Relay Hub</h1>
  {msg}

  <div class="tabs">
    <button type="button" data-tab="jobs">Jobs</button>
    <button type="button" data-tab="archive">Archive</button>
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

  <section id="tab-archive" class="tab">
    <p style="font-size:0.85em;color:#888">
      Completed print jobs archived as PDF.
      Retention: {ARCHIVE_RETENTION_DAYS} days / {ARCHIVE_MAX_JOBS} jobs.
    </p>
    <table>
      <tr><th>#</th><th>Date</th><th>PDF</th><th>PCL</th><th>Actions</th></tr>
      {''.join(
          '<tr>'
          '<td>' + a["num"].lstrip("0") + '</td>'
          '<td style="font-size:0.85em">' + a["time"] + '</td>'
          '<td>' + (str(a["size_kb"]) + ' KB' if a["has_pdf"] else '<span class="empty">failed</span>') + '</td>'
          '<td>' + str(a["pcl_kb"]) + ' KB</td>'
          '<td>'
          + ('<a href="/archive/' + a["pdf"] + '" target="_blank">View</a> '
             '<a href="/archive/' + a["pdf"] + '?dl=1">Download</a> ' if a["has_pdf"] else '')
          + '<form method="post" action="/archive_delete" style="display:inline"'
          ' onsubmit="return confirm(\'Delete archive #' + a["num"] + '?\')">'
          '<input type="hidden" name="num" value="' + a["num"] + '">'
          '<button type="submit" class="danger" style="padding:0.15em 0.4em;font-size:0.85em">Delete</button>'
          '</form></td></tr>'
          for a in archived
      ) or '<tr><td colspan="5" class="empty">No archived jobs yet. Completed print jobs will appear here as PDFs.</td></tr>'}
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
      <tr><th>Filename</th><th>Size</th><th>Received</th><th>Actions</th></tr>
      {export_rows}
    </table>
  </section>

  <section id="tab-nodes" class="tab">
    <table>
      <tr><th>Name</th><th>IP</th><th>MAC</th><th>Type</th><th>Firmware</th><th>Status</th></tr>
      {edge_rows}
    </table>
    <h2>ESP32 Firmware</h2>
    <p style="font-size:0.85em;color:#888">Current: <code>{fw_label}</code>
      &nbsp;
      <form method="post" action="/upload_firmware" enctype="multipart/form-data"
            style="display:inline">
        <input type="file" name="firmware" accept=".bin" required style="font-size:0.8em">
        <button type="submit" style="font-size:0.8em">Upload .bin</button>
      </form>
    </p>
    {'<h3 style="font-size:0.95em;margin-top:0.8em">USB-connected devices</h3>'
     '<table><tr><th>Device</th><th>Port</th><th>ID</th><th>Firmware</th><th>Action</th></tr>'
     + esp_rows + '</table>' if esp_rows else
     '<p style="font-size:0.85em;color:#888">'
     'No ESP32 connected via USB. Plug one in to flash firmware.</p>'}
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
      <tr><td>Transport</td><td>{'<span class="ok">usblp (raw)</span>' if diag.get('uses_usblp') else ('<span class="ok">ipp-usb (port 60000)</span>' if diag.get('ipp_usb_reachable') else ('<span class="error">ipp-usb not reachable</span>' if diag.get('ipp_usb_active') else '<span class="error">ipp-usb not running</span>'))}</td></tr>
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
      <form method="post" action="/diag_flush_arp"><button type="submit">Flush WiFi clients</button></form>
    </div>
    <p style="font-size:0.85em;color:#555;margin:0.6em 0">
      <b>Test print</b> — sends a one-line test page to verify the printer works.<br>
      <b>Reset printer connection</b> — cancels all jobs, restarts ipp-usb,
      and re-enables the queue.
      Use this when jobs are stuck or the printer stops responding.<br>
      <b>Flush WiFi clients</b> — clears stale ARP entries and deauthenticates
      orphaned stations. Use when an ESP32 is stuck blinking red after reconnect.<br>
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
  function sendToHub(name) {{
    var f = document.createElement('form');
    f.method = 'POST'; f.action = '/send_to_hub';
    var i = document.createElement('input');
    i.type = 'hidden'; i.name = 'file'; i.value = name;
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }}
  </script>
<footer style="margin-top:2em;padding:0.8em 0;border-top:1px solid #ddd;color:#999;font-size:0.8em;text-align:center">
  Relay Hub v{ver["version"]} ({ver["commit"]})
</footer>
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
        if self.path == "/api/version":
            info = _read_version()
            info["hostname"] = socket.gethostname()
            self._send(json.dumps(info).encode(), content_type="application/json")
            return
        if self.path.startswith("/archive/"):
            self._serve_archive()
            return
        if self.path.startswith("/export/"):
            self._serve_export()
            return
        self._send(_page())

    def _serve_archive(self):
        name = self.path.split("/archive/", 1)[1].split("?")[0]
        if not re.match(r"^\d{5}\.(pdf|pcl)$", name):
            self._send(b"Not found", 404)
            return
        fpath = JOB_ARCHIVE_DIR / name
        if not fpath.is_file():
            self._send(b"Not found", 404)
            return
        ct = "application/pdf" if name.endswith(".pdf") else "application/octet-stream"
        dl = "?dl=1" in self.path
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        if dl:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_export(self):
        name = self.path.split("/export/", 1)[1].split("?")[0]
        fpath = IMPORT_DIR / name
        if not fpath.is_file() or fpath.resolve().parent != IMPORT_DIR.resolve():
            self._send(b"Not found", 404)
            return
        ct = mimetypes.guess_type(name)[0] or "application/octet-stream"
        dl = "?dl=1" in self.path
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        if dl:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # /node-announce is JSON, not form-encoded.
        if self.path == "/node-announce":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode())
                _set_node_firmware(body.get("mac", ""), body.get("node_id", ""),
                                   body.get("firmware", ""))
                self._send(b"ok", 200)
            except Exception as exc:
                self._send(str(exc).encode(), 400)
            return

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
                _submit_to_hub(fname, data)
                self._send(b"accepted", 202)
            except Exception as exc:
                self._send(str(exc).encode(), 500)
            return

        if self.path == "/upload_firmware":
            try:
                ct = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ct:
                    raise ValueError("Expected multipart form data")
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                bm = re.search(r"boundary=([^\s;]+)", ct)
                if not bm:
                    raise ValueError("No multipart boundary")
                boundary = b"--" + bm.group(1).encode()
                parts = raw.split(boundary)
                data = b""
                filename = ""
                for part in parts:
                    if b'name="firmware"' not in part:
                        continue
                    fm = re.search(rb'filename="([^"]+)"', part)
                    if not fm:
                        continue
                    filename = fm.group(1).decode()
                    hdr_end = part.find(b"\r\n\r\n")
                    if hdr_end < 0:
                        continue
                    data = part[hdr_end + 4:]
                    if data.endswith(b"\r\n"):
                        data = data[:-2]
                    break
                if not filename:
                    raise ValueError("No file selected")
                if not filename.endswith(".bin"):
                    raise ValueError("Only .bin firmware files accepted")
                if len(data) < 1024:
                    raise ValueError("File too small to be firmware")
                FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
                dest = FIRMWARE_DIR / "usb_dongle.bin"
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.rename(dest)
                ver = _firmware_bin_version(dest)
                label = f" (v{ver})" if ver else ""
                self._send(_page(f"Firmware uploaded: {filename}{label} ({len(data):,} bytes)"))
            except Exception as exc:
                self._send(_page(str(exc), error=True))
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

            elif self.path == "/send_to_hub":
                fname = p.get("file", [""])[0]
                fpath = IMPORT_DIR / fname
                if not fpath.is_file() or fpath.resolve().parent != IMPORT_DIR.resolve():
                    raise FileNotFoundError(f"File not found: {fname}")
                _submit_to_hub(fname, fpath.read_bytes())
                self._send(_page(f"Sent {fname} to import hub."))

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

            elif self.path == "/diag_flush_arp":
                msg = _wifi_flush_arp()
                self._send(_page(msg))

            elif self.path == "/archive_delete":
                num = p.get("num", [""])[0]
                if not re.match(r"^\d{5}$", num):
                    raise ValueError("Invalid archive number")
                _archive_delete(num)
                self._send(_page(f"Deleted archive #{num}"))

            elif self.path == "/flash_esp":
                port = p.get("port", [""])[0]
                if not port:
                    raise ValueError("No serial port specified")
                msg = _flash_esp(port)
                is_err = "failed" in msg.lower() or "error" in msg.lower() or "not found" in msg.lower()
                self._send(_page(msg, error=is_err))

            else:
                self._send(b"Not found", 404)

        except Exception as exc:
            self._send(_page(str(exc), error=True))


if __name__ == "__main__":
    # Keep the relay-node SSH off the request path entirely.
    _probe_refresh()   # prime the cache so the first page is correct
    _edge_refresh()
    _ensure_preserve_jobs()   # archive needs spool files kept
    threading.Thread(target=_probe_refresher, daemon=True).start()
    threading.Thread(target=_mediasource_refresher, daemon=True).start()
    threading.Thread(target=_edge_refresher, daemon=True).start()
    threading.Thread(target=_archive_refresher, daemon=True).start()

    # Threaded: a single slow/blocked CUPS call must not freeze the whole UI.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    server.daemon_threads = True
    print(f"Relay Hub UI on port {PORT}")
    server.serve_forever()
