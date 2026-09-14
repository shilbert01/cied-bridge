"""
Client for the CIEDOcr extraction service — see docs/ciedocr-integration.md and
CiedOCR/docs/extract-api-v1.md.

The central Pi submits a PDF, polls the async job, and maps the returned
`PacemakerReport` onto the `interrogations` table (storing the full report verbatim
in `extract_json`). Two failure classes are distinguished so the job state machine
can react:
  * CiedocrUnavailable — transient (service down/loading/timeout, or blocked by the
    PHI policy) → caller keeps the job re-queueable (`raw_only`).
  * CiedocrError       — terminal (service rejected the PDF, or returned no_text) →
    caller marks the job `failed`.

Config (env, mirrors node.conf pattern):
  CIEDOCR_URL, CIEDOCR_TOKEN, CIEDOCR_TIMEOUT, CIEDOCR_ALLOW_REMOTE_PHI
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

POLL_INTERVAL = 5   # seconds


class CiedocrUnavailable(Exception):
    """Transient — retry later (service down/loading/timeout, or PHI-blocked)."""


class CiedocrError(Exception):
    """Terminal — the request/PDF cannot be extracted (rejected or no_text)."""


# ── config ────────────────────────────────────────────────────────────────────
def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def url() -> str:
    return _cfg("CIEDOCR_URL").rstrip("/")


def _token() -> str:
    return _cfg("CIEDOCR_TOKEN")


def _timeout() -> int:
    return int(_cfg("CIEDOCR_TIMEOUT", "300"))


def _allow_remote_phi() -> bool:
    return _cfg("CIEDOCR_ALLOW_REMOTE_PHI", "false").lower() in ("1", "true", "yes")


def configured() -> bool:
    return bool(url())


def is_remote() -> bool:
    """Conservative: an endpoint is 'local' only if it is loopback / RFC-1918 / .fritz.box."""
    u = url()
    local_markers = ("localhost", "127.0.0.1", "::1", ".fritz.box",
                     "192.168.", "10.", "172.16.", "172.17.", "172.18.",
                     "172.19.", "172.2", "172.30.", "172.31.")
    return not any(m in u for m in local_markers)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"} if _token() else {}


# ── service calls ─────────────────────────────────────────────────────────────
def health() -> bool:
    if not configured():
        return False
    try:
        req = urllib.request.Request(url() + "/health", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def extract(pdf_path: str, vendor_hint: str = "") -> dict:
    """Submit a PDF async, poll to completion, return the result envelope.

    vendor_hint selects the per-vendor extractor on the service side.
    Raises CiedocrUnavailable (transient) or CiedocrError (terminal)."""
    if not configured():
        raise CiedocrUnavailable("CIEDOCR_URL not set")
    if is_remote() and not _allow_remote_phi():
        raise CiedocrUnavailable("remote CIEDOcr blocked by CIEDOCR_ALLOW_REMOTE_PHI=false")

    data = Path(pdf_path).read_bytes()

    # submit (async)
    try:
        headers = {"Content-Type": "application/pdf", **_auth_headers()}
        if vendor_hint:
            headers["X-Vendor-Hint"] = vendor_hint
        req = urllib.request.Request(
            url() + "/v1/extract?mode=async",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            submit = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise CiedocrUnavailable(f"service loading (HTTP {e.code})")
        raise CiedocrError(f"submit rejected (HTTP {e.code})")
    except Exception as e:
        raise CiedocrUnavailable(f"submit failed: {e}")

    poll_url = url() + submit.get("poll", "/v1/jobs/" + str(submit.get("job_id")))

    deadline = time.monotonic() + _timeout()
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            req = urllib.request.Request(poll_url, headers=_auth_headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise CiedocrError("job expired or unknown")
            raise CiedocrUnavailable(f"poll failed (HTTP {e.code})")
        except Exception as e:
            raise CiedocrUnavailable(f"poll failed: {e}")

        status = res.get("status")
        if status in ("queued", "running"):
            continue
        if status == "ok":
            return res
        if status == "no_text":
            raise CiedocrError("no_text — nothing extractable from PDF")
        if status == "error":
            raise CiedocrError(res.get("detail", "extraction error"))
        raise CiedocrError(f"unexpected status: {status}")

    raise CiedocrUnavailable("extraction poll timed out")


# ── mapping: CIEDOcr PacemakerReport → interrogations fields ──────────────────
def _num(value) -> float | None:
    """First numeric token from strings like '330 Ω', '6,9 mV' (comma decimals)."""
    if value is None:
        return None
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    return float(m.group().replace(",", ".")) if m else None


def _pw(threshold_str) -> float | None:
    """Pulse width after '@' in combined strings like '1,25 V @ 0,4 ms'."""
    if not isinstance(threshold_str, str):
        return None
    m = re.search(r"@\s*([-+]?\d+(?:[.,]\d+)?)", threshold_str)
    return float(m.group(1).replace(",", ".")) if m else None


def map_report(envelope: dict) -> dict:
    """Envelope → `fields` dict for db.insert_interrogation (+ full report in extract_json).

    Projects the per-vendor extractor schema (extractors/*) onto the interrogations
    columns. ELA/Sorin names the ventricle `_v_` where others use `_rv_`. The full
    verbatim report is always kept in extract_json regardless."""
    report = envelope.get("report", {}) or {}
    engine = envelope.get("engine", {}) or {}

    def first(*keys):
        for k in keys:
            v = report.get(k)
            if v not in (None, "", [], {}):
                return v
        return None

    def sec(name, key):
        """Nested Abbott/PacemakerReport lookup, e.g. sec('test_rv','impedance_ohm')."""
        block = report.get(name)
        return block.get(key) if isinstance(block, dict) else None

    def numf(*vals):
        for v in vals:
            n = _num(v)
            if n is not None:
                return n
        return None

    # Two extractor families: flat (pdfplumber vendors) and nested
    # (Abbott VQA → PacemakerReport). Try both so either populates the columns.
    fields = {
        "patient_id":         first("patient_id", "patient_name", "patient"),
        "device_serial":      report.get("device_serial"),
        "device_model":       first("device_model", "device"),
        "interrogation_date": first("interrogation_date", "followup_date", "implant_date"),
        # leads — RA=atrial 'a', RV=ventricle 'rv'/'v', LV
        "impedance_ra":   numf(report.get("impedance_a_ohm"), sec("test_ra", "impedance_ohm")),
        "impedance_rv":   numf(report.get("impedance_rv_ohm"), report.get("impedance_v_ohm"), sec("test_rv", "impedance_ohm")),
        "impedance_lv":   numf(report.get("impedance_lv_ohm")),
        "sensing_ra":     numf(report.get("sensing_a_mv"), sec("test_ra", "sensing_mv")),
        "sensing_rv":     numf(report.get("sensing_rv_mv"), report.get("sensing_v_mv"), sec("test_rv", "sensing_mv")),
        "threshold_ra_v":  numf(report.get("threshold_a_v"), report.get("threshold_ra_v"),
                                sec("test_ra", "pacing_threshold")),
        "threshold_ra_pw": numf(report.get("threshold_a_pulse_ms"), report.get("threshold_ra_pw_ms"),
                                report.get("threshold_a_pw_ms"),
                                _pw(sec("test_ra", "pacing_threshold"))),
        "threshold_rv_v":  numf(report.get("threshold_rv_v"), report.get("threshold_v_v"), sec("test_rv", "pacing_threshold")),
        "threshold_rv_pw": numf(report.get("pulse_width_rv_ms"), report.get("threshold_rv_pw_ms"),
                                report.get("threshold_rv_pulse_ms"),
                                _pw(sec("test_rv", "pacing_threshold"))),
        "threshold_lv_v":  numf(report.get("threshold_lv_v"), sec("test_lv", "pacing_threshold")),
        "threshold_lv_pw": numf(report.get("pulse_width_lv_ms"), report.get("threshold_lv_pw_ms"),
                                report.get("threshold_lv_pulse_ms"),
                                _pw(sec("test_lv", "pacing_threshold"))),
        # battery
        "battery_voltage":   numf(report.get("battery_voltage_v"), sec("battery_rv", "voltage"), sec("battery_ra", "voltage")),
        "battery_longevity": first("battery_life", "battery_status", "battery_longevity") or sec("battery_rv", "estimated_longevity") or sec("battery_ra", "estimated_longevity"),
        # verbatim payload + audit stamp (source of truth for re-projection/reprocess)
        "extract_json":   json.dumps(report, ensure_ascii=False),
        "engine_name":    engine.get("name"),
        "engine_version": engine.get("version"),
        "schema_version": envelope.get("schema_version"),
        "request_id":     envelope.get("request_id"),
    }
    return {k: v for k, v in fields.items() if v is not None}
