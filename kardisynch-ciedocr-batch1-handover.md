# KardiSynch ← CIEDOcr Integration — Batch 1 Handover

## What this patch does

Two features, shipped together because they share the `LeadData` type change:

1. **CIEDOcr as optional PDF parser backend** (Parts 1a–1c)
2. **Lead-device association data model** (Parts 2a–2c)

## New files

### `src/main/services/ciedocrClient.ts`

HTTP client for CIEDOcr's async extraction API.
Uses Electron's `net` module (not Node `http`) so it respects proxy/cert settings.

- **Config** — reads four keys from the Settings table via `getSettings()`:
  - `ciedocrUrl` — e.g. `http://bigboy.fritz.box:8000`
  - `ciedocrToken` — optional Bearer token
  - `ciedocrEnabled` — `"true"` / `"false"`
  - `ciedocrStrategy` — `"fallback"` (default) or `"always"`
- **`extract(pdfPath, vendorHint?)`** — POST PDF → poll job → return envelope
- **`health()`** — pings `/health`, returns boolean
- **`isConfigured()`** / **`getStrategy()`** — read from settings

The settings keys don't exist in the Settings table yet — they return empty/undefined
until the Settings UI writes them. This means CIEDOcr is inert by default; the
existing parser pipeline is completely unchanged until someone enables it.

### `src/main/parsers/ciedocr-adapter.ts`

Maps a CIEDOcr extraction envelope to KardiSynch's `UnifiedReport`.

Handles both schema families CIEDOcr produces:
- **Flat** (Biotronik, Medtronic, BSc, ELA/Sorin, Abbott Merlin pdfplumber):
  `impedance_a_ohm`, `sensing_rv_mv`, `battery_voltage_v`, `threshold_a_v`, etc.
- **Nested** (Abbott VQA / SummaryReport):
  `test_rv.impedance_ohm`, `battery_rv.voltage`, `parameters.mode`, etc.

Key mapping decisions documented in the code:
- **Patient name**: splits `"Mustermann, Max"` and `"MUSTERMANN MAX"` formats
- **Dates**: all piped through `normalizeDate()` with `hint: 'eu'` (CIEDOcr output
  is German-locale `DD.MM.YYYY`)
- **Leads**: constructed from flat `impedance_rv_ohm` / nested `test_rv.impedance_ohm`
  fields; each lead gets `device_serial` stamped from the report's device
- **Battery status**: inferred from voltage (≤2.5V → EOL, ≤2.8V → ERI, else OK)
- **Manufacturer**: detected from CIEDOcr's `engine.name` (maps to vendor), with
  fallback heuristics on device model keywords

## Modified files

### `src/main/reports.ts` — LeadData extended

```typescript
export interface LeadData {
  // ... existing fields ...
  device_serial?: string;                              // NEW
  status?: 'active' | 'abandoned' | 'capped' | string; // NEW
}
```

Both fields are optional — all existing code continues to work without them.

### `src/main/parser.ts` — CIEDOcr fallback wired in

The PDF branch of `parseFile()` now:
1. Runs local parsing as before (BSc markers → Biotronik filename → generic)
2. Checks CIEDOcr config: `isConfigured()` → `getStrategy()`
3. If strategy is `"always"`, or `"fallback"` and local result has `parseStatus: 'failed'`:
   calls `ciedocr.extract()` → `mapCiedocrToUnifiedReport()` → returns that
4. On CIEDOcr failure, falls through to the local result (even if partial)

Non-PDF formats (.xml, .bnk, .pdd, .pkg, .log) are untouched — CIEDOcr is PDF-only.

The visit.xml reader also now reads back `device_serial` and `status` on leads.

### `src/main/storage.ts` — XML generation extended

- `generatePatientXML()`: writes `<device_serial>` and `<status>` elements in each `<lead>`
- `generateVisitXML()`: same
- `writeMergedPatientXML()`: propagates `device_serial` and `status` through lead merge
- `refreshVisitMetadata()` lead merge: same propagation

New elements are empty strings when absent — XML stays valid, existing patient.xml
files read back fine (missing elements → `undefined`).

### All 5 vendor parsers — device serial propagated to leads

Each parser now stamps the device's serial number onto every lead it constructs:

| Parser | Method |
|--------|--------|
| `biotronik-parser.ts` | `for (const lead of leads) lead.device_serial = deviceSerial` after leads are built |
| `medtronic-parser.ts` (PDD) | Same loop, inside `if (leads.length > 0)` |
| `medtronic-parser.ts` (PKG/XML) | Same loop, before return |
| `boston-scientific-parser.ts` (BNK) | `leads.map(l => ({ ...l, device_serial }))` in return |
| `boston-scientific-parser.ts` (PDF) | Loop after `deriveParseStatus()` |
| `abbott-parser.ts` (coded-log) | `leads.map(l => ({ ...l, device_serial }))` in return |
| `abbott-parser.ts` (freeform) | Same, using `serialMatch[1].trim()` |

## What's NOT in this patch (Batch 2)

- **Settings UI** (Part 1d) — CIEDOcr URL/token/strategy fields in Settings.tsx
- **Lead-device UI** (Part 2d) — grouping leads by device in DeviceLeadEditor.tsx and FormattedReport.tsx
- **Migration** (Part 2e) — backfill `device_serial` on existing patient.xml leads

## How to test

1. **Without CIEDOcr configured**: import any supported file format → verify parsing
   works exactly as before (CIEDOcr path is never entered when `ciedocrEnabled` is
   not `"true"` in Settings)

2. **With CIEDOcr configured** (manually set settings in SQLite):
   ```sql
   INSERT OR REPLACE INTO Settings (key, value) VALUES ('ciedocrUrl', 'http://bigboy.fritz.box:8000');
   INSERT OR REPLACE INTO Settings (key, value) VALUES ('ciedocrEnabled', 'true');
   INSERT OR REPLACE INTO Settings (key, value) VALUES ('ciedocrStrategy', 'fallback');
   ```
   Then drop a raster-only PDF (e.g. Abbott Merlin) into the import dir → local
   parsing should fail → CIEDOcr should be called → report should appear with
   `formatVariant: 'ciedocr-fallback:ciedocr:abbott_merlin'`

3. **Lead-device association**: import any file → check visit.xml and patient.xml →
   leads should have `<device_serial>` populated with the device's serial number

## Applying the patch

```bash
cd /path/to/KardiSynch
git apply kardisynch-ciedocr-batch1.patch
```

The two new files will be created, seven existing files modified.
