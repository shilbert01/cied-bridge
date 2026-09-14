# KardiSynch ← CIEDOcr Integration — Batch 2 Handover

**Depends on**: Batch 1 must be applied first. This patch modifies files that Batch 1
does not touch, so the two patches don't conflict, but the IPC handler and Settings
keys reference the `ciedocrClient.ts` module created in Batch 1.

## What this patch does

Three features:

1. **CIEDOcr Settings UI** (Part 1d) — configuration panel in the Automation tab
2. **Lead-device association UI** (Part 2d) — device selector + status on leads
3. **Migration strategy** (Part 2e) — no code migration needed; "Reparse Everything" backfills

## Modified files

### `src/renderer/Settings.tsx` — CIEDOcr configuration panel

Added a "CIEDOcr Extraction Service" card to the Automation tab with:

- **Enable/disable** toggle — controls `ciedocrEnabled` setting (`"true"`/`"false"`)
- **Server URL** text field — e.g. `http://bigboy.fritz.box:8000`
- **Bearer Token** password field — optional, for authenticated deployments
- **Strategy** radio cards:
  - "Fallback only" (default) — CIEDOcr is called only when local parsing fails
  - "Always" — CIEDOcr handles all PDF extraction
- **Test Connection** button — saves current settings, calls `/health`, shows green/red result

All four settings keys (`ciedocrUrl`, `ciedocrToken`, `ciedocrEnabled`, `ciedocrStrategy`)
are stored in the existing `Settings` table via the existing `setSettings()` mechanism.
No schema changes needed.

### `src/preload/preload.ts` — CIEDOcr IPC bridge

Added one IPC channel:

```typescript
ciedocrHealth: () => ipcRenderer.invoke('ciedocr-health'),
```

This is the only new IPC channel. The CIEDOcr extraction itself is called from the
main process during `parseFile()` (Batch 1), not from the renderer.

### `src/main/main.ts` — IPC handler

Added handler:

```typescript
ipcMain.handle('ciedocr-health', async () => {
  const { health } = await import('./services/ciedocrClient');
  return health();
});
```

Dynamic import so the module isn't loaded at startup when CIEDOcr is unconfigured.

### `src/components/DeviceLeadEditor.tsx` — Lead-device association editor

Changes to the patient record editor:

- **Lead interface** extended with `device_serial?: string` and `status?: string`
- **Layout** changed from 6-column single row to two 4-column rows per lead for readability
- **New "Connected Device" dropdown** — lists all devices from the current patient record
  (filtered to those with a serial number). Selecting a device stamps its serial onto the
  lead. "Unassociated" clears the field.
- **New "Lead Status" dropdown** — `active` / `abandoned` / `capped`. Defaults to `active`.

The device dropdown is dynamic — it reads from `formData.devices`, so adding/removing
devices in the same edit session immediately updates the available options.

### `src/renderer/components/FormattedReport.tsx` — Leads grouped by device

The clinical report view now groups leads by `device_serial` when present:

- If any lead has a `device_serial`, leads are grouped under headers like
  "Device ABC123" / "Unassociated"
- If no leads have a `device_serial` (legacy data), the flat list renders exactly as before
- Leads with a non-`active` status show a badge (e.g. "abandoned", "capped")

## Migration (Part 2e)

No code-level migration is needed:

- **Database**: The `leads` column in `Patients` is a JSON text blob. New fields
  (`device_serial`, `status`) are simply additional keys in the JSON objects — SQLite
  doesn't care.
- **XML files**: New `<device_serial>` and `<status>` elements in `patient.xml` and
  `visit.xml` (from Batch 1) are empty strings when absent. Existing XML files without
  these elements read back `undefined`, which is the correct default.
- **Backfill**: Running "Reparse Everything" from Settings → Database re-runs all vendor
  parsers (now with `device_serial` propagation from Batch 1) and rebuilds all XML files.
  For patients with exactly one device, all leads automatically get associated.

## How to test

### CIEDOcr Settings (Part 1d)

1. Open Settings → Automation tab
2. Scroll to "CIEDOcr Extraction Service" card
3. Enter URL (e.g. `http://bigboy.fritz.box:8000`), click "Test Connection" → should show green checkmark
4. Toggle Enable, select Strategy, click "Save Changes"
5. Drop a raster-only PDF into the import dir → verify CIEDOcr extraction triggers

### Lead-device association (Part 2d)

1. Open a patient with at least one device → click "Edit" to open DeviceLeadEditor
2. Each lead now has "Connected Device" and "Lead Status" dropdowns
3. Select a device from the dropdown → save → re-open → verify the association persists
4. View a visit report → if leads have `device_serial`, they're grouped under device headers
5. Set a lead's status to "abandoned" → the FormattedReport shows a badge

### Migration (Part 2e)

1. Go to Settings → Database → click "Reparse Everything"
2. After reparse, open any patient → leads should now have `device_serial` populated
   (matches the device serial from the same interrogation)

## Applying the patch

```bash
cd /path/to/KardiSynch
# Apply Batch 1 first if not already applied
git apply kardisynch-ciedocr-batch1.patch
# Then Batch 2
git apply kardisynch-ciedocr-batch2.patch
```

Five files modified, no new files.
