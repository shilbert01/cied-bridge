"""
Mirrors finished PDFs and KardiSynch-parseable export files into a dedicated
outbox (/data/kardisynch-outbox), copied (not moved) from the authoritative
/data/pdf and /data/exports trees. A sibling Samba container shares ONLY this
outbox directory with the KardiSynch desktop workstation.

Why a separate outbox instead of sharing /data/pdf directly: KardiSynch's
watcher deletes/moves files out of whatever directory it watches as soon as it
starts processing them (moveFile() in its watcher.ts does fs.rename, falling
back to copy+unlink) — pointing it at /data/pdf would let it delete files from
our system of record. /data/db.sqlite must never be shared (SMB/CIFS write
locking risks SQLite corruption).

Idempotency: jobs.published_to_outbox (db.py) persists across publisher
restarts, so re-running never re-copies a job already mirrored.
Cleanup: none needed — KardiSynch itself drains the outbox by deleting what it
imports.
"""
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

# Extensions KardiSynch's watcher recognizes (src/main/storage.ts
# PARSEABLE_EXTENSIONS). NOTE: .hl7 (Boston Scientific's export format) is NOT
# in this list — known gap, intentionally skipped here, not solved by this
# module. PCL-only jobs (status='pcl_only') are also naturally excluded: their
# render_pdf_path/raw_ps_path point at the raw captured file, which keeps a
# .ps extension regardless of actual PCL content, so it never matches here.
KARDISYNCH_EXTS = {".pdf", ".xml", ".txt", ".log", ".pkg", ".bnk", ".pdd"}


def _select_source(row: dict) -> Path | None:
    """Pick the file to mirror for a job row, or None if nothing publishable."""
    for key in ("render_pdf_path", "pdf_path"):
        p = row.get(key)
        if p and Path(p).exists() and Path(p).suffix.lower() in KARDISYNCH_EXTS:
            return Path(p)
    raw = Path(row.get("raw_ps_path") or "")
    if raw.exists() and raw.suffix.lower() in KARDISYNCH_EXTS:
        return raw
    return None


def _publish_one(row: dict, outbox_dir: Path) -> bool:
    src = _select_source(row)
    if src is None:
        # Nothing publishable (e.g. .hl7 export, or a pcl_only job with no
        # rendered PDF). Mark published anyway so we stop re-checking it.
        db.mark_published_to_outbox(row["id"])
        return False

    dest_name = f"{row['id']}_{src.name}"
    dest = outbox_dir / dest_name
    tmp = outbox_dir / f".{dest_name}.partial-{uuid.uuid4().hex[:8]}"

    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
            fdst.flush()
            os.fsync(fdst.fileno())
        os.replace(tmp, dest)  # atomic rename — mirrors capture_daemon.py's .partial idiom
    except OSError as e:
        log.error("Outbox publish failed for job %d (%s): %s", row["id"], src, e)
        tmp.unlink(missing_ok=True)
        return False

    db.mark_published_to_outbox(row["id"])
    log.info("Published job %d -> outbox/%s", row["id"], dest_name)
    return True


def publish_pending(data_dir: Path, outbox_dir: Path) -> int:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    exports_dir = str(data_dir / "exports")
    rows = db.get_jobs_pending_outbox(exports_dir)
    return sum(1 for row in rows if _publish_one(row, outbox_dir))


def run_forever(data_dir: Path, outbox_dir: Path, interval: int = 15):
    outbox_dir.mkdir(parents=True, exist_ok=True)
    # Group-writable so the Samba container's mapped SMB user (a different
    # UID/GID) can delete files it imports — see compose.yaml KARDISYNCH_SMB_GID.
    # This container runs as root so the chown is unrestricted.
    try:
        os.chmod(outbox_dir, 0o775)
        os.chown(outbox_dir, 0, int(os.environ.get("KARDISYNCH_SMB_GID", "1000")))
    except OSError as e:
        log.warning("Could not set outbox ownership/perms: %s", e)

    while True:
        try:
            n = publish_pending(data_dir, outbox_dir)
            if n:
                log.info("kardisynch-outbox: published %d file(s)", n)
        except Exception as e:
            log.warning("kardisynch-outbox worker error: %s", e)
        time.sleep(interval)
