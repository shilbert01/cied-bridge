import sqlite3
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH: str = ""


def init(db_path: str):
    global _DB_PATH
    _DB_PATH = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        _create_schema(conn)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Additive, idempotent migrations for the CIEDOcr integration (Option C)."""
    def columns(table: str) -> set:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    jc = columns("jobs")
    if "status" not in jc:
        conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'received'")
    if "render_pdf_path" not in jc:
        conn.execute("ALTER TABLE jobs ADD COLUMN render_pdf_path TEXT")
    if "published_to_outbox" not in jc:
        conn.execute("ALTER TABLE jobs ADD COLUMN published_to_outbox INTEGER NOT NULL DEFAULT 0")
    if "published_to_outbox_at" not in jc:
        conn.execute("ALTER TABLE jobs ADD COLUMN published_to_outbox_at TEXT")

    ic = columns("interrogations")
    for col in ("extract_json", "engine_name", "engine_version",
                "schema_version", "request_id", "extracted_at"):
        if col not in ic:
            conn.execute(f"ALTER TABLE interrogations ADD COLUMN {col} TEXT")
    if "threshold_lv_pw" not in ic:
        conn.execute("ALTER TABLE interrogations ADD COLUMN threshold_lv_pw REAL")
    if "threshold_ra_v" not in ic:
        conn.execute("ALTER TABLE interrogations ADD COLUMN threshold_ra_v REAL")
    if "threshold_ra_pw" not in ic:
        conn.execute("ALTER TABLE interrogations ADD COLUMN threshold_ra_pw REAL")
    # Backfill pulse-width columns from extract_json for records written before this mapping existed.
    _backfill_pulse_widths(conn)


def _backfill_pulse_widths(conn: sqlite3.Connection):
    """Backfill: read pulse_width_*_ms from extract_json into flat columns."""
    import json as _json
    import re as _re
    rows = conn.execute(
        "SELECT id, extract_json FROM interrogations "
        "WHERE extract_json IS NOT NULL AND ("
        "threshold_rv_pw IS NULL OR threshold_lv_pw IS NULL "
        "OR threshold_ra_v IS NULL OR threshold_ra_pw IS NULL)"
    ).fetchall()
    for row in rows:
        try:
            report = _json.loads(row["extract_json"])
        except Exception:
            continue

        def _num(v):
            if v is None:
                return None
            m = _re.search(r"[-+]?\d+(?:[.,]\d+)?", str(v))
            return float(m.group().replace(",", ".")) if m else None

        def _pw(s):
            if not isinstance(s, str):
                return None
            m = _re.search(r"@\s*([-+]?\d+(?:[.,]\d+)?)", s)
            return float(m.group(1).replace(",", ".")) if m else None

        def _sec(name, key):
            block = report.get(name)
            return block.get(key) if isinstance(block, dict) else None

        def _first(*vals):
            for v in vals:
                n = _num(v)
                if n is not None:
                    return n
            return None

        rv_pw = _first(
            report.get("pulse_width_rv_ms"), report.get("threshold_rv_pw_ms"),
            report.get("threshold_rv_pulse_ms"), report.get("pulse_width_v_ms"),
            _pw(_sec("test_rv", "pacing_threshold")),
        )
        lv_pw = _first(
            report.get("pulse_width_lv_ms"), report.get("threshold_lv_pw_ms"),
            report.get("threshold_lv_pulse_ms"),
            _pw(_sec("test_lv", "pacing_threshold")),
        )
        ra_v = _first(
            report.get("threshold_a_v"), report.get("threshold_ra_v"),
            _num(_sec("test_ra", "pacing_threshold")),
        )
        ra_pw = _first(
            report.get("threshold_a_pulse_ms"), report.get("threshold_ra_pw_ms"),
            report.get("pulse_width_a_ms"),
            _pw(_sec("test_ra", "pacing_threshold")),
        )
        if rv_pw is not None or lv_pw is not None or ra_v is not None or ra_pw is not None:
            conn.execute(
                "UPDATE interrogations SET "
                "threshold_rv_pw=COALESCE(threshold_rv_pw,?), "
                "threshold_lv_pw=COALESCE(threshold_lv_pw,?), "
                "threshold_ra_v=COALESCE(threshold_ra_v,?), "
                "threshold_ra_pw=COALESCE(threshold_ra_pw,?) WHERE id=?",
                (rv_pw, lv_pw, ra_v, ra_pw, row["id"]),
            )


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id     TEXT NOT NULL,
            vendor      TEXT NOT NULL,
            job_name    TEXT NOT NULL UNIQUE,
            received_at TEXT NOT NULL,
            raw_ps_path TEXT NOT NULL,
            pdf_path    TEXT,
            parsed      INTEGER NOT NULL DEFAULT 0,
            parse_error TEXT
        );

        CREATE TABLE IF NOT EXISTS interrogations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id              INTEGER NOT NULL REFERENCES jobs(id),
            patient_id          TEXT,
            device_serial       TEXT,
            device_model        TEXT,
            interrogation_date  TEXT,
            battery_voltage     REAL,
            battery_longevity   TEXT,
            impedance_rv        REAL,
            impedance_ra        REAL,
            impedance_lv        REAL,
            sensing_rv          REAL,
            sensing_ra          REAL,
            threshold_rv_v      REAL,
            threshold_rv_pw     REAL,
            threshold_lv_v      REAL,
            extra               TEXT
        );

        CREATE TABLE IF NOT EXISTS devices (
            device_serial   TEXT PRIMARY KEY,
            device_model    TEXT,
            vendor          TEXT,
            patient_id      TEXT,
            patient_name    TEXT,
            implant_date    TEXT,
            updated_at      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_vendor    ON jobs(vendor);
        CREATE INDEX IF NOT EXISTS idx_jobs_node      ON jobs(node_id);
        CREATE INDEX IF NOT EXISTS idx_interr_device  ON interrogations(device_serial);
        CREATE INDEX IF NOT EXISTS idx_interr_patient ON interrogations(patient_id);
    """)


def insert_job(node_id: str, vendor: str, job_name: str, raw_ps_path: str) -> tuple[int, bool]:
    """Returns (job_id, created). created=False if job_name already existed (idempotent retry)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs (node_id, vendor, job_name, received_at, raw_ps_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, vendor, job_name, now, raw_ps_path),
        )
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid, True
        row = conn.execute("SELECT id FROM jobs WHERE job_name=?", (job_name,)).fetchone()
        return row[0], False


def update_job_vendor(job_id: int, vendor: str):
    with _conn() as conn:
        conn.execute("UPDATE jobs SET vendor=? WHERE id=?", (vendor, job_id))


def mark_job_parsed(job_id: int, pdf_path: str | None = None, error: str | None = None):
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET parsed=?, pdf_path=?, parse_error=? WHERE id=?",
            (0 if error else 1, pdf_path, error, job_id),
        )


# ── CIEDOcr job state machine (Option C) ──────────────────────────────────────
# status ∈ received | renderable | queued | extracting | parsed | raw_only | failed
def set_job_status(job_id: int, status: str,
                   render_pdf_path: str | None = None, error: str | None = None):
    with _conn() as conn:
        if render_pdf_path is not None:
            conn.execute(
                "UPDATE jobs SET status=?, render_pdf_path=?, parse_error=?, parsed=? WHERE id=?",
                (status, render_pdf_path, error, 1 if status == "parsed" else 0, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status=?, parse_error=?, parsed=? WHERE id=?",
                (status, error, 1 if status == "parsed" else 0, job_id),
            )


def get_jobs_for_retry() -> list[dict]:
    """Jobs stuck without a reachable parser — re-queued when CIEDOcr health returns."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, vendor, render_pdf_path, raw_ps_path FROM jobs WHERE status='raw_only'"
        ).fetchall()
    return [dict(r) for r in rows]


def get_pcl_jobs_for_retry() -> list[dict]:
    """PCL print jobs that couldn't be rendered — re-tried when gpdl becomes available."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, vendor, raw_ps_path FROM jobs WHERE status='pcl_only'"
        ).fetchall()
    return [dict(r) for r in rows]


# ── KardiSynch outbox mirror ──────────────────────────────────────────────
def get_jobs_pending_outbox(exports_prefix: str) -> list[dict]:
    """Jobs whose file is fully written and not yet mirrored to the KardiSynch outbox.

    Print/upload jobs: gated on status having reached 'renderable' or later —
    render_pdf_path is only set after ps_to_pdf()'s blocking subprocess.run()
    returns, so the file is guaranteed complete by then.

    Export-route files (under exports_prefix): raw_ps_path is written
    synchronously by /ingest-export before the jobs row is created, so row
    existence alone proves the file is complete — needed because non-PDF
    exports never advance past status='received'.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, job_name, vendor, raw_ps_path, render_pdf_path, pdf_path, status "
            "FROM jobs WHERE published_to_outbox = 0 AND ("
            "  status NOT IN ('received', 'failed')"
            "  OR raw_ps_path LIKE ?"
            ")",
            (exports_prefix + "/%",),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_published_to_outbox(job_id: int):
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET published_to_outbox=1, published_to_outbox_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )


def clear_interrogations(job_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM interrogations WHERE job_id=?", (job_id,))


def insert_interrogation(job_id: int, fields: dict) -> int:
    extra = {k: v for k, v in fields.items() if k not in _KNOWN_COLUMNS}
    known = {k: fields.get(k) for k in _KNOWN_COLUMNS}
    known["job_id"] = job_id
    known["extra"] = json.dumps(extra) if extra else None
    cols = ", ".join(known.keys())
    placeholders = ", ".join("?" * len(known))
    with _conn() as conn:
        cur = conn.execute(
            f"INSERT INTO interrogations ({cols}) VALUES ({placeholders})",
            list(known.values()),
        )
        return cur.lastrowid


def upsert_device(serial: str, fields: dict):
    # Only include columns that exist in the devices table
    allowed = {"device_model", "vendor", "patient_id", "patient_name", "implant_date"}
    clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
    clean["device_serial"] = serial
    clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(clean.keys())
    placeholders = ", ".join("?" * len(clean))
    updates = ", ".join(f"{k}=excluded.{k}" for k in clean if k != "device_serial")
    with _conn() as conn:
        conn.execute(
            f"INSERT INTO devices ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(device_serial) DO UPDATE SET {updates}",
            list(clean.values()),
        )


def get_jobs(limit: int = 50, offset: int = 0, vendor: str | None = None,
             node_id: str | None = None) -> list[dict]:
    where, params = _build_where(vendor=vendor, node_id=node_id)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY received_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_interrogations(limit: int = 50, offset: int = 0,
                       patient_id: str | None = None,
                       device_serial: str | None = None) -> list[dict]:
    where, params = _build_where(patient_id=patient_id, device_serial=device_serial)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM interrogations {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows]


def get_interrogation(interr_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM interrogations WHERE id=?", (interr_id,)
        ).fetchone()
    return dict(row) if row else None


def get_devices() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    with _conn() as conn:
        jobs_total   = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        jobs_pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE parsed=0 AND parse_error IS NULL").fetchone()[0]
        interr_total = conn.execute("SELECT COUNT(*) FROM interrogations").fetchone()[0]
        devices      = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        by_status    = {r[0]: r[1] for r in
                        conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
    return {
        "jobs_total": jobs_total,
        "jobs_pending": jobs_pending,
        "jobs_by_status": by_status,
        "interrogations_total": interr_total,
        "devices_known": devices,
    }


def _build_where(**filters) -> tuple[str, list]:
    clauses = [f"{k}=?" for k, v in filters.items() if v is not None]
    params  = [v for v in filters.values() if v is not None]
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


_KNOWN_COLUMNS = {
    "patient_id", "device_serial", "device_model", "interrogation_date",
    "battery_voltage", "battery_longevity",
    "impedance_rv", "impedance_ra", "impedance_lv",
    "sensing_rv", "sensing_ra",
    "threshold_ra_v", "threshold_ra_pw",
    "threshold_rv_v", "threshold_rv_pw", "threshold_lv_v", "threshold_lv_pw",
    # CIEDOcr extraction: verbatim report + audit stamp
    "extract_json", "engine_name", "engine_version",
    "schema_version", "request_id", "extracted_at",
}
