"""
Central server: job ingestion from edge nodes + REST API for tablet.

Ingest:  POST /ingest          (Bearer token, edge node auth)
Tablet:  GET  /api/...         (no auth — tablet is on trusted BT PAN network)
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

import subprocess

from fastapi import FastAPI, Request, Header, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from . import db
from datetime import datetime, timezone

from .parsers import get_export_parser, detect_export_vendor
from .parsers.base import ParseError
from . import ciedocr, render, kardisynch_outbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Pacemaker Central", docs_url=None, redoc_url=None)

# Loaded at startup from environment (set by systemd unit from server.conf)
_INGEST_TOKEN: str = ""
_DATA_DIR: Path = Path("/data")


@app.on_event("startup")
def startup():
    global _INGEST_TOKEN, _DATA_DIR
    _INGEST_TOKEN = os.environ["INGEST_TOKEN"]
    _DATA_DIR     = Path(os.environ.get("DATA_DIR", "/data"))
    (_DATA_DIR / "ps").mkdir(parents=True, exist_ok=True)
    (_DATA_DIR / "pdf").mkdir(parents=True, exist_ok=True)
    (_DATA_DIR / "exports").mkdir(parents=True, exist_ok=True)
    db.init(os.environ.get("DB_PATH", str(_DATA_DIR / "db.sqlite")))
    threading.Thread(target=_retry_worker, name="ciedocr-retry", daemon=True).start()
    threading.Thread(target=_kardisynch_outbox_worker, name="kardisynch-outbox", daemon=True).start()
    log.info("Central server ready — data: %s  ·  CIEDOcr: %s",
             _DATA_DIR, ciedocr.url() or "(not configured)")


def _kardisynch_outbox_worker():
    interval = int(os.environ.get("KARDISYNCH_OUTBOX_INTERVAL", "15"))
    kardisynch_outbox.run_forever(_DATA_DIR, _DATA_DIR / "kardisynch-outbox", interval)


def _retry_worker():
    """Re-queue raw_only jobs when CIEDOcr is back; re-render pcl_only jobs if gpdl appears."""
    interval = int(os.environ.get("CIEDOCR_HEALTH_INTERVAL", "30"))
    while True:
        time.sleep(interval)
        try:
            # Re-queue raw_only → CIEDOcr extraction once it's reachable.
            if ciedocr.configured() and ciedocr.health():
                for row in db.get_jobs_for_retry():
                    pdf = row.get("render_pdf_path")
                    if pdf and Path(pdf).exists():
                        db.set_job_status(row["id"], "queued")
                        threading.Thread(target=_extract_job,
                                         args=(row["id"], pdf, row.get("vendor", "")),
                                         daemon=True).start()
            # Re-render pcl_only jobs if gpdl has since become available.
            if _pcl_retry_possible():
                for row in db.get_pcl_jobs_for_retry():
                    raw = row.get("raw_ps_path", "")
                    if not raw or not Path(raw).exists():
                        continue
                    pdf = raw.replace("/ps/", "/pdf/").replace(".ps", ".pdf")
                    if render.ps_to_pdf(raw, pdf):
                        db.set_job_status(row["id"], "renderable", render_pdf_path=pdf)
                        threading.Thread(target=_extract_job,
                                         args=(row["id"], pdf, row.get("vendor", "")),
                                         daemon=True).start()
        except Exception as e:
            log.warning("retry worker error: %s", e)


def _pcl_retry_possible() -> bool:
    return shutil.which("gpdl") is not None or shutil.which("gpcl6") is not None


_UI_DIR = Path("/app/ui")

def _asset_version() -> str:
    """Mtime-based version token for cache-busting. Recomputed on each container start."""
    try:
        return str(int(_UI_DIR.joinpath("app.js").stat().st_mtime))
    except OSError:
        import time as _time
        return str(int(_time.time()))

_ASSET_VER = _asset_version()


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


@app.get("/ui/", include_in_schema=False)
@app.get("/ui/index.html", include_in_schema=False)
def serve_ui_index():
    """Serve index.html with version-stamped asset URLs to bust browser cache on deploy."""
    try:
        html = (_UI_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(status_code=404)
    for asset in ("app.js", "style.css", "alpine.min.js"):
        html = html.replace(f'"/ui/{asset}"', f'"/ui/{asset}?v={_ASSET_VER}"')
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Mounted after explicit routes so /api/* and /ui/index.html routes take priority
app.mount("/ui", StaticFiles(directory="/app/ui", html=True), name="ui")


# ---------------------------------------------------------------------------
# Ingest endpoint (called by edge node forwarder.py)

@app.post("/ingest", status_code=202)
async def ingest(
    request: Request,
    authorization: str = Header(...),
    x_node_id: str  = Header(...),
    x_vendor: str   = Header(...),
    x_job_name: str = Header(...),
):
    if authorization != f"Bearer {_INGEST_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    ps_path  = _DATA_DIR / "ps"  / x_job_name
    pdf_path = _DATA_DIR / "pdf" / x_job_name.replace(".ps", ".pdf")

    ps_path.write_bytes(body)
    log.info("Received job %s from %s (%s)", x_job_name, x_node_id, x_vendor)

    job_id, created = db.insert_job(x_node_id, x_vendor, x_job_name, str(ps_path))
    if not created:
        log.info("Duplicate ingest for %s — already in DB as job %d", x_job_name, job_id)
        return {"job_id": job_id, "status": "accepted"}

    # Always render a viewable PDF locally so the dashboard works even if CIEDOcr is down.
    rendered = render.ps_to_pdf(str(ps_path), str(pdf_path))
    if not rendered:
        if render.is_pcl(str(ps_path)):
            # PCL file (Abbott Merlin / HP-based device) — gpdl not installed.
            # Store the raw PCL so the job is still accessible as a download.
            log.info("Job %d is PCL — marking pcl_only (install gpdl to enable rendering)", job_id)
            db.set_job_status(job_id, "pcl_only", render_pdf_path=str(ps_path))
        else:
            db.set_job_status(job_id, "failed", error="PS→PDF render failed")
        return {"job_id": job_id, "status": "accepted"}
    db.set_job_status(job_id, "renderable", render_pdf_path=str(pdf_path))

    # Delegate structured extraction to CIEDOcr in the background (202 returned now).
    threading.Thread(target=_extract_job, args=(job_id, str(pdf_path), x_vendor), daemon=True).start()
    return {"job_id": job_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# Export ingest endpoint (called by edge node storage_daemon via forwarder.py)

@app.post("/ingest-export", status_code=202)
async def ingest_export(
    request: Request,
    authorization: str = Header(...),
    x_node_id:    str  = Header(...),
    x_vendor:     str  = Header(...),
    x_session_id: str  = Header(...),
    x_file_name:  str  = Header(...),
):
    if authorization != f"Bearer {_INGEST_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    vendor = detect_export_vendor(x_file_name, x_vendor)
    if vendor != x_vendor:
        log.info("Vendor auto-detected as %s from '%s' (declared: %s)",
                 vendor, x_file_name, x_vendor)

    export_dir = _DATA_DIR / "exports" / x_session_id
    export_dir.mkdir(parents=True, exist_ok=True)
    file_path = export_dir / x_file_name
    file_path.write_bytes(body)
    log.info("Export %s/%s from %s (%s)", x_session_id, x_file_name, x_node_id, vendor)

    if file_path.suffix.lower() == ".pdf":
        job_name = f"export_{x_session_id}_{x_file_name}"
        job_id, created = db.insert_job(x_node_id, vendor, job_name, str(file_path))
        if created:
            db.set_job_status(job_id, "renderable", render_pdf_path=str(file_path))
            native_pdf_parser = get_export_parser(vendor, ".pdf")
            if native_pdf_parser is not None:
                # Vendor has a structured PDF export parser (e.g. Biotronik BIOSTD PDF).
                threading.Thread(target=_parse_export,
                                 args=(vendor, x_node_id, x_session_id, str(file_path)),
                                 daemon=True).start()
            else:
                # All other vendors (incl. abbott): attempt CIEDOcr extraction.
                # Abbott Merlin PDFs are raster-only but CIEDOcr handles them via Qwen3-VL VQA.
                threading.Thread(target=_extract_job, args=(job_id, str(file_path), vendor),
                                 daemon=True).start()
    else:
        # HL7 / logs / structured exports are not OCR targets — keep vendor export parsers.
        threading.Thread(
            target=_parse_export,
            args=(vendor, x_node_id, x_session_id, str(file_path)),
            daemon=True,
        ).start()

    return {"session_id": x_session_id, "file": x_file_name, "status": "accepted"}


def _parse_export(vendor: str, node_id: str, session_id: str, file_path: str):
    from pathlib import Path
    ext = Path(file_path).suffix.lower()
    parser_fn = get_export_parser(vendor, ext)
    if parser_fn is None and ext == ".log":
        # Content-sniff: 0x1C-delimited lines → SJM/Abbott Merlin log regardless of declared vendor.
        try:
            with open(file_path, "r", encoding="latin-1") as _f:
                _sniff = _f.read(512)
            if "\x1c" in _sniff:
                vendor = "abbott"
                parser_fn = get_export_parser("abbott", ".log")
                log.info("Auto-detected vendor=abbott for %s (0x1C delimiter)", Path(file_path).name)
        except OSError:
            pass
    if parser_fn is None:
        log.info("No export parser for vendor=%s ext=%s — stored only", vendor, ext)
        job_name = f"export_{session_id}_{Path(file_path).name}"
        db.insert_job(node_id, vendor, job_name, file_path)
        return
    try:
        raw = parser_fn(file_path)
    except (ParseError, Exception) as e:
        log.error("Export parse failed %s: %s", file_path, e)
        return

    records = raw if isinstance(raw, list) else [raw]
    for fields in records:
        if not fields:
            continue
        job_name = f"export_{session_id}_{Path(file_path).name}"
        job_id, created = db.insert_job(node_id, vendor, job_name, file_path)
        if created or True:  # always store parsed data even on retry
            try:
                interr_id = db.insert_interrogation(job_id, fields)
                db.set_job_status(job_id, "parsed")
                if fields.get("device_serial"):
                    db.upsert_device(fields["device_serial"], {
                        "device_model": fields.get("device_model"),
                        "vendor":       vendor,
                        "patient_id":   fields.get("patient_id"),
                        "patient_name": fields.get("patient_name"),
                    })
                log.info("Export parsed job %d → interrogation %d  fields: %s",
                         job_id, interr_id, list(fields))
            except Exception as e:
                log.error("Export DB insert failed: %s", e)


def _extract_job(job_id: int, pdf_path: str, vendor: str = ""):
    """Delegate extraction to CIEDOcr and drive the job state machine.

    States: renderable/queued → extracting → parsed | raw_only (transient) | failed (terminal)."""
    if not ciedocr.configured() or not ciedocr.health():
        db.set_job_status(job_id, "raw_only", error="CIEDOcr unreachable")
        return

    db.set_job_status(job_id, "extracting")
    try:
        envelope = ciedocr.extract(pdf_path, vendor)
    except ciedocr.CiedocrUnavailable as e:
        log.warning("CIEDOcr unavailable for job %d: %s — leaving raw_only", job_id, e)
        db.set_job_status(job_id, "raw_only", error=str(e))
        return
    except ciedocr.CiedocrError as e:
        log.error("CIEDOcr could not extract job %d: %s", job_id, e)
        db.set_job_status(job_id, "failed", error=str(e))
        return

    fields = ciedocr.map_report(envelope)
    fields["extracted_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db.clear_interrogations(job_id)          # idempotent (reprocess-safe)
        interr_id = db.insert_interrogation(job_id, fields)
        db.set_job_status(job_id, "parsed")
        if fields.get("device_serial"):
            db.upsert_device(fields["device_serial"], {
                "device_model": fields.get("device_model"),
                "vendor":       vendor,
                "patient_id":   fields.get("patient_id"),
                "patient_name": fields.get("patient_name"),
            })
        log.info("CIEDOcr extracted job %d → interrogation %d (engine %s %s)",
                 job_id, interr_id, fields.get("engine_name"), fields.get("engine_version"))
    except Exception as e:
        log.error("DB insert failed for job %d: %s", job_id, e)
        db.set_job_status(job_id, "failed", error=f"db: {e}")


# ---------------------------------------------------------------------------
# Tablet-facing REST API

@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    return db.get_stats()


@app.get("/api/jobs")
def jobs(
    limit:   int          = Query(50, le=200),
    offset:  int          = Query(0),
    vendor:  str | None   = Query(None),
    node_id: str | None   = Query(None),
):
    return db.get_jobs(limit=limit, offset=offset, vendor=vendor, node_id=node_id)


@app.get("/api/jobs/{job_id}")
def job(job_id: int):
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404)
    return row


_KNOWN_VENDORS = {"medtronic", "boston_scientific", "abbott", "biotronik", "ela_sorin"}


class JobVendorPatch(BaseModel):
    vendor: str


@app.patch("/api/jobs/{job_id}/vendor", status_code=200)
def patch_job_vendor(job_id: int, body: JobVendorPatch):
    vendor = body.vendor.strip().lower()
    if vendor not in _KNOWN_VENDORS:
        raise HTTPException(status_code=422, detail=f"Unknown vendor '{vendor}'")
    if not db.get_job(job_id):
        raise HTTPException(status_code=404)
    db.update_job_vendor(job_id, vendor)
    return {"job_id": job_id, "vendor": vendor}


@app.get("/api/jobs/{job_id}/pdf")
def job_pdf(job_id: int):
    """The locally-rendered (or export) PDF — always available once a job is renderable.
    For pcl_only jobs the raw PCL file is served as an octet-stream download."""
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404)
    pdf = row.get("render_pdf_path") or row.get("pdf_path")
    if not pdf or not Path(pdf).exists():
        raw = row.get("raw_ps_path", "")           # export-route PDFs are the raw file
        if raw.lower().endswith(".pdf") and Path(raw).exists():
            pdf = raw
    if not pdf or not Path(pdf).exists():
        raise HTTPException(status_code=404, detail="No rendered PDF for this job")
    # PCL-only jobs: render_pdf_path points to the raw .ps (PCL) file; serve as download.
    if pdf.lower().endswith(".ps") and row.get("status") == "pcl_only":
        return FileResponse(pdf, media_type="application/octet-stream",
                            headers={"Content-Disposition": f"attachment; filename=job-{job_id}.pcl"})
    return FileResponse(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=job-{job_id}.pdf"})


@app.post("/api/jobs/{job_id}/reprocess", status_code=202)
def reprocess_job(job_id: int):
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404)
    # Prefer the already-rendered PDF; re-render from raw PS/PCL if needed.
    pdf = row.get("render_pdf_path") or row.get("pdf_path")
    raw = row.get("raw_ps_path", "")
    if (not pdf or not Path(pdf).exists() or row.get("status") == "pcl_only") \
            and raw and raw.endswith(".ps") and Path(raw).exists():
        pdf_candidate = raw.replace("/ps/", "/pdf/").replace(".ps", ".pdf")
        if render.ps_to_pdf(raw, pdf_candidate):
            pdf = pdf_candidate
    if not pdf or not Path(pdf).exists() or pdf.endswith(".ps"):
        hint = " — PCL file requires gpdl on host; bind-mount /usr/bin/gpdl:/usr/local/bin/gpdl" \
               if (raw and render.is_pcl(raw)) else ""
        raise HTTPException(status_code=422,
                            detail=f"No renderable PDF for this job{hint}")
    db.clear_interrogations(job_id)
    db.set_job_status(job_id, "queued", render_pdf_path=pdf)
    threading.Thread(target=_extract_job, args=(job_id, pdf, row["vendor"]), daemon=True).start()
    return {"job_id": job_id, "status": "reprocessing"}


@app.post("/api/upload", status_code=202)
async def upload_file(
    vendor: str = Form(...),
    file: UploadFile = File(...),
):
    vendor = vendor.lower().strip()
    if vendor not in ("medtronic", "boston_scientific", "abbott", "biotronik", "ela_sorin"):
        raise HTTPException(status_code=422, detail=f"Unknown vendor '{vendor}'")

    ext = Path(file.filename).suffix.lower()
    safe_name = re.sub(r"[^\w.\-]", "_", file.filename)
    job_name = f"upload_{int(time.time())}_{safe_name}"

    body = await file.read()
    if not body:
        raise HTTPException(status_code=400, detail="Empty file")

    # Option C: documents (PDF/PS) go to CIEDOcr; only non-document structured
    # files (HL7/logs/etc.) use the legacy export parsers.
    is_export = ext not in (".pdf", ".ps")

    if is_export:
        save_path = _DATA_DIR / "exports" / job_name
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(body)
        job_id, _ = db.insert_job("manual-upload", vendor, job_name, str(save_path))
        threading.Thread(
            target=_parse_export,
            args=(vendor, "manual-upload", job_name, str(save_path)),
            daemon=True,
        ).start()
    else:
        save_path = _DATA_DIR / "ps" / job_name
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(body)
        job_id, _ = db.insert_job("manual-upload", vendor, job_name, str(save_path))
        # Already a PDF → use directly; PostScript/PCL → render locally first.
        if ext == ".pdf":
            pdf_path = str(save_path)
        else:
            pdf_path = str(_DATA_DIR / "pdf" / (Path(job_name).stem + ".pdf"))
            if not render.ps_to_pdf(str(save_path), pdf_path):
                if render.is_pcl(str(save_path)):
                    db.set_job_status(job_id, "pcl_only", render_pdf_path=str(save_path))
                else:
                    db.set_job_status(job_id, "failed", error="PS→PDF render failed")
                return {"job_id": job_id, "status": "accepted", "job_name": job_name}
        db.set_job_status(job_id, "renderable", render_pdf_path=pdf_path)
        threading.Thread(target=_extract_job, args=(job_id, pdf_path, vendor), daemon=True).start()

    return {"job_id": job_id, "status": "accepted", "job_name": job_name}


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: int):
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404)
    with db._conn() as conn:
        conn.execute("DELETE FROM interrogations WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return Response(status_code=204)


@app.get("/api/interrogations")
def interrogations(
    limit:         int        = Query(50, le=200),
    offset:        int        = Query(0),
    patient_id:    str | None = Query(None),
    device_serial: str | None = Query(None),
):
    return db.get_interrogations(
        limit=limit, offset=offset,
        patient_id=patient_id, device_serial=device_serial,
    )


@app.get("/api/interrogations/{interr_id}")
def interrogation(interr_id: int):
    row = db.get_interrogation(interr_id)
    if not row:
        raise HTTPException(status_code=404)
    return row


class InterrogationPatch(BaseModel):
    interrogation_date: Optional[str] = None


@app.patch("/api/interrogations/{interr_id}")
def patch_interrogation(interr_id: int, body: InterrogationPatch):
    if not db.get_interrogation(interr_id):
        raise HTTPException(status_code=404)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    with db._conn() as conn:
        for k, v in updates.items():
            conn.execute(f"UPDATE interrogations SET {k}=? WHERE id=?", (v, interr_id))
    return db.get_interrogation(interr_id)


@app.get("/api/interrogations/{interr_id}/qr")
def interrogation_qr(interr_id: int):
    row = db.get_interrogation(interr_id)
    if not row:
        raise HTTPException(status_code=404)
    result: dict = {"followup": _build_followup_qr(row)}
    params_qr = _build_params_qr(row)
    if params_qr:
        result["params"] = params_qr
    return result


def _resolve_interr_pdf(interr_id: int) -> str:
    """Return the best available PDF path for an interrogation, or raise 404."""
    interr = db.get_interrogation(interr_id)
    if not interr:
        raise HTTPException(status_code=404)
    job = db.get_job(interr["job_id"])
    if not job:
        raise HTTPException(status_code=404)
    for key in ("render_pdf_path", "pdf_path"):
        p = job.get(key)
        if p and Path(p).exists():
            return p
    raw = Path(job.get("raw_ps_path", ""))
    if raw.suffix.lower() == ".pdf" and raw.exists():
        return str(raw)
    raise HTTPException(status_code=404, detail="No PDF available for this interrogation")


@app.get("/api/interrogations/{interr_id}/pages")
def interrogation_pages(interr_id: int):
    """Return the page count of the source PDF."""
    pdf_path = _resolve_interr_pdf(interr_id)
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            count = len(pdf.pages)
    except Exception:
        count = 1
    return {"count": count}


@app.get("/api/interrogations/{interr_id}/page/{page_num}")
def interrogation_page(interr_id: int, page_num: int):
    """Render and return a single PDF page as a PNG image (page_num is 1-indexed).
    Rendered PNGs are cached in DATA_DIR/render/ so subsequent requests are instant."""
    if page_num < 1:
        raise HTTPException(status_code=400, detail="page_num must be >= 1")
    pdf_path = _resolve_interr_pdf(interr_id)
    render_dir = _DATA_DIR / "render"
    render_dir.mkdir(exist_ok=True)
    cache_path = render_dir / f"{Path(pdf_path).stem}_p{page_num}.png"
    if not cache_path.exists():
        result = subprocess.run(
            ["gs", "-dBATCH", "-dNOPAUSE", "-dSAFER",
             "-sDEVICE=png16m", "-r150",
             f"-dFirstPage={page_num}", f"-dLastPage={page_num}",
             f"-sOutputFile={cache_path}", pdf_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or not cache_path.exists():
            raise HTTPException(status_code=500, detail="Page render failed")
    return FileResponse(str(cache_path), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/interrogations/{interr_id}/document")
def interrogation_document(interr_id: int):
    interr = db.get_interrogation(interr_id)
    if not interr:
        raise HTTPException(status_code=404)
    job = db.get_job(interr["job_id"])
    if not job:
        raise HTTPException(status_code=404)

    # Prefer the GS-rendered PDF (print jobs)
    if job.get("pdf_path"):
        p = Path(job["pdf_path"])
        if p.exists():
            return FileResponse(str(p), media_type="application/pdf",
                                headers={"Content-Disposition": "inline"})

    # Native PDF export (e.g. Biotronik BIOSTD)
    raw = Path(job["raw_ps_path"])
    if raw.suffix.lower() == ".pdf" and raw.exists():
        return FileResponse(str(raw), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})

    # PS file — convert on the fly with Ghostscript
    if raw.suffix.lower() == ".ps" and raw.exists():
        result = subprocess.run(
            ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
             "-sOutputFile=-", str(raw)],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            return Response(content=result.stdout, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})

    raise HTTPException(status_code=404, detail="No document available for this interrogation")


@app.get("/api/interrogations/{interr_id}/qr/followup")
def interrogation_qr_followup(interr_id: int):
    row = db.get_interrogation(interr_id)
    if not row:
        raise HTTPException(status_code=404)
    return Response(content=_make_qr_png(_build_followup_qr(row)), media_type="image/png")


@app.get("/api/interrogations/{interr_id}/qr/params")
def interrogation_qr_params(interr_id: int):
    row = db.get_interrogation(interr_id)
    if not row:
        raise HTTPException(status_code=404)
    payload = _build_params_qr(row)
    if payload is None:
        raise HTTPException(status_code=404, detail="No parameter data")
    return Response(content=_make_qr_png(payload), media_type="image/png")


@app.get("/api/devices")
def devices():
    return db.get_devices()


@app.get("/ca.crt", include_in_schema=False)
def ca_cert():
    """Download the local CA cert — install on tablet to trust HTTPS without warnings."""
    cert = Path("/certs/ca.crt")
    if not cert.exists():
        raise HTTPException(status_code=404, detail="CA cert not found")
    return FileResponse(str(cert), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": "attachment; filename=PacemakerCentral-CA.crt"})


# ---------------------------------------------------------------------------
# CardioPal QR payload builders

def _to_iso_date(s: str) -> str | None:
    if not s:
        return None
    m = re.match(r'^(\d{2})\.(\d{2})\.(\d{4})', s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r'^(\d{4}-\d{2}-\d{2})', s)
    if m:
        return m.group(1)
    return None


def _extra(interr: dict) -> dict:
    raw = interr.get("extra")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _report_json(interr: dict) -> dict:
    raw = interr.get("extract_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _num(value) -> float | None:
    """First numeric token from a value like '2,5 V' or '50 min⁻¹'."""
    if value is None:
        return None
    import re as _re
    m = _re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    return float(m.group().replace(",", ".")) if m else None


def _build_followup_qr(interr: dict) -> str:
    extra = _extra(interr)
    date_str = interr.get("interrogation_date") or ""
    iso_date = _to_iso_date(date_str) or date_str

    d: dict = {"date": iso_date}

    if interr.get("battery_voltage") is not None:
        d["bv"] = interr["battery_voltage"]

    a_ch: dict = {}
    rv_ch: dict = {}
    lv_ch: dict = {}

    if interr.get("sensing_ra") is not None:
        a_ch["se"] = interr["sensing_ra"]
    if interr.get("impedance_ra") is not None:
        a_ch["im"] = interr["impedance_ra"]

    if interr.get("sensing_rv") is not None:
        rv_ch["se"] = interr["sensing_rv"]
    if interr.get("impedance_rv") is not None:
        rv_ch["im"] = interr["impedance_rv"]
    if interr.get("threshold_rv_v") is not None:
        rv_ch["ta"] = interr["threshold_rv_v"]
    if interr.get("threshold_rv_pw") is not None:
        rv_ch["tp"] = interr["threshold_rv_pw"]

    if interr.get("impedance_lv") is not None:
        lv_ch["im"] = interr["impedance_lv"]
    if interr.get("threshold_lv_v") is not None:
        lv_ch["ta"] = interr["threshold_lv_v"]

    if a_ch:
        d["a"] = a_ch
    if rv_ch:
        d["rv"] = rv_ch
    if lv_ch:
        d["lv"] = lv_ch

    env = {"v": 1, "t": "fu", "ts": int(time.time()), "d": d}
    return json.dumps(env, separators=(",", ":"))


def _make_qr_png(payload: str) -> bytes:
    import io, qrcode
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_params_qr(interr: dict) -> str | None:
    extra = _extra(interr)
    report = _report_json(interr)
    date_str = interr.get("interrogation_date") or ""
    iso_date = _to_iso_date(date_str) or date_str

    def _rsec(name, key):
        block = report.get(name)
        return block.get(key) if isinstance(block, dict) else None

    def _nf(*vals):
        for v in vals:
            n = _num(v)
            if n is not None:
                return n
        return None

    d: dict = {"date": iso_date}

    mode = (extra.get("pacing_mode") or report.get("mode") or _rsec("parameters", "mode"))
    if mode:
        d["mode"] = mode

    br = _nf(extra.get("base_rate_bpm"), report.get("base_rate_bpm"),
             _rsec("parameters", "base_rate"))
    if br is not None:
        d["br"] = br

    ra_ch: dict = {}
    rv_ch: dict = {}
    lv_ch: dict = {}

    ra_amp = _nf(extra.get("amplitude_a_v"), report.get("amplitude_a_v"),
                 _rsec("stimulation_ra", "amplitude_v"))
    if ra_amp is not None:
        ra_ch["am"] = ra_amp

    rv_amp = _nf(extra.get("amplitude_rv_v"), report.get("amplitude_rv_v"),
                 report.get("amplitude_v_v"), _rsec("stimulation_rv", "amplitude_v"))
    if rv_amp is not None:
        rv_ch["am"] = rv_amp

    lv_amp = _nf(extra.get("amplitude_lv_v"), report.get("amplitude_lv_v"),
                 _rsec("stimulation_lv", "amplitude_v"))
    if lv_amp is not None:
        lv_ch["am"] = lv_amp

    if ra_ch:
        d["ra"] = ra_ch
    if rv_ch:
        d["rv"] = rv_ch
    if lv_ch:
        d["lv"] = lv_ch

    if len(d) <= 1:
        return None

    env = {"v": 1, "t": "params", "ts": int(time.time()), "d": d}
    return json.dumps(env, separators=(",", ":"))
