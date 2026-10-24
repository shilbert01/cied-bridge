# Central Pi ↔ CIED Extract integration (Option C)

Status: **DRAFT** · Consumes: [CIED Extract API v1](../../cied-extract/docs/extract-api-v1.md)

## Role change
The central Pi stops parsing. It becomes **ingest + store + render + serve + orchestrate**:
- Accepts jobs from edge nodes (`/ingest`, `/ingest-export`) — unchanged.
- **Always produces a viewable PDF locally**, independent of any parser (see below).
- Delegates *structured extraction* to a configurable CIED Extract endpoint.
- Serves the dashboard: shows the raw PDF always, plus extracted fields when available.
- The four coordinate template parsers (`api/parsers/{medtronic,abbott,…}.py`) are **retired** — the fallback is raw display, not a weaker parser.

## Rule: the raw view never depends on CIED Extract
Browsers can't render PostScript, so degraded mode can't just "show the PS file."
On ingest the Pi runs `PS → PDF` via Ghostscript (already invoked in the old
`parsers/base.py`; runs fine on a 3B+). Export-route jobs are already PDF.
**Every job therefore has a viewable PDF before extraction is ever attempted.** The raw
viewer is a core dashboard feature (clinicians verify OCR against the original), not only a fallback.

## Job lifecycle
```
        ingest (/ingest = PS, /ingest-export = PDF)
                │  store raw file
                ▼
         [received] ──render PS→PDF (Ghostscript)──► [renderable]  ← raw PDF viewable from here on
                                                        │
                                        GET /health on CIED Extract endpoint
                                    ┌───────────────────┴───────────────────┐
                              reachable                                 unreachable
                                    │                                        │
                                [queued] ──POST /v1/extract──► [extracting]  └──► [raw_only]
                                                                   │                 (retry when health returns)
                    ┌──────────────────────────┬───────────────────┤
              200 ok/partial              200 no_text          503/timeout/conn
                    │                     4xx terminal              │
                    ▼                          ▼                    ▼
                [parsed]                   [failed]            [raw_only]  (re-queue w/ backoff)
```

### States (replace the binary `parsed`/`parse_error`)
| status | meaning | dashboard |
|--------|---------|-----------|
| `received` | ingested, raw stored | (transient) |
| `renderable` | viewable PDF ready, not yet extracted | raw PDF + "awaiting extraction" |
| `queued` | CIED Extract reachable, awaiting send | raw PDF + spinner |
| `extracting` | sent, awaiting response | raw PDF + spinner |
| `parsed` | structured fields stored | raw PDF + fields |
| `raw_only` | no parser reachable (transient) | raw PDF + "extraction offline — will retry" |
| `failed` | tried, terminal (`no_text`/4xx) | raw PDF + "could not extract" + manual reprocess |

### Transition rules
- **Health gate:** a background worker checks `GET /health`. If down, new jobs go straight to `raw_only`; no per-job blocking.
- **Retry/backoff:** `raw_only` jobs are re-queued with exponential backoff; on a health-down→up edge, **auto-requeue all `raw_only`**.
- **Reprocess:** the existing `POST /api/jobs/{id}/reprocess` re-queues any job (incl. `parsed`) — used when CIED Extract's `engine.version` improves. Store enough to re-run: raw file is always kept.
- **Idempotency:** re-extracting overwrites the interrogation for that job (keep prior `engine.version` in history if we want an audit of changes).

## Persistence deltas
Additive, backward-compatible migrations:

`jobs`
- add `status TEXT NOT NULL DEFAULT 'received'` (the enum above). Keep `parsed`/`parse_error` derivable during transition, or drop once callers use `status`.
- add `render_pdf_path TEXT` — the locally-rendered viewable PDF (distinct from `raw_ps_path`).

`interrogations`
- add `extract_json TEXT` — the **full** CIED Extract `report` verbatim (source of truth for re-projection; the flat columns are just an indexed projection of it).
- add audit stamp: `engine_name TEXT`, `engine_version TEXT`, `schema_version TEXT`, `request_id TEXT`, `extracted_at TEXT`.
- CIED Extract's richer nested report (battery_ra/rv, stimulation, diagnostics, …) → map the known fields into existing columns; everything else lives in `extract_json`. No lossy drop.

## Central config (new block, mirrors node.conf pattern)
```
CIED_EXTRACT_URL=http://bigboy.fritz.box:8000     # LAN now; https URL later
CIED_EXTRACT_TOKEN=                                # empty now; Bearer token when non-local/SaaS
CIED_EXTRACT_TIMEOUT=300                           # seconds per extract (measured ~68s/page on a 4090 → budget per-page)
CIED_EXTRACT_ALLOW_REMOTE_PHI=false               # gate: refuse to send PHI to a non-local endpoint
CIED_EXTRACT_HEALTH_INTERVAL=30
```
`ALLOW_REMOTE_PHI=false` + a non-local `CIED_EXTRACT_URL` ⇒ the Pi keeps jobs `raw_only` rather than sending patient data off-LAN. Flips to `true` (with auth + DPA) when non-local is approved. For now `CIED_EXTRACT_URL` is the LAN bigboy, so the flag is moot.

## Build order (contract-first)
1. **Freeze this contract + the v1 API doc.** ✅ (both sides build independently)
2. **CIED Extract:** FastAPI wrapper around `ocr_engine`/`extractor`, model loaded at startup, `GET /health` + `POST /v1/extract`. Deploy in its container on bigboy.
3. **Central Pi:** always-render PS→PDF on ingest; add the CIED Extract client (health gate + queue + retry); schema migration; map `report`→columns + store `extract_json`; retire template parsers.
4. **Dashboard:** raw-PDF viewer + status badges; render extracted fields from `extract_json`.
