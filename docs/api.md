# Dossier API

Read existing dossiers, saved results, images, and PDFs. All endpoints use `GET`
and have no request body. Scan execution and progress tracking come later.

## Run locally

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-api.txt
python -m api --reload
```

The OS selects a free local port. Use the URL printed by Uvicorn, then append
`/docs` for interactive documentation. Each launch may use a different port.
Omit `--reload` to disable automatic restarts.
No authentication is required in this local integration phase.

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `CABEK_DOSSIERS_DIR` | Absolute path to the dossier folders | Repository `data/dossiers` |
| `CABEK_CORS_ORIGINS` | Comma-separated frontend origins | No cross-origin access |

Example frontend origin: `http://localhost:5173`.

## GET `/api/dossiers`

Use for dashboard cards, search, filters, and counters.

**Needs:** an accessible dossier directory. Query parameters are optional:

| Parameter | Meaning | Default / allowed values |
| --- | --- | --- |
| `search` | Case-insensitive match against ID, marque, modèle, or matricule | Empty; maximum 200 characters |
| `status` | Filter by saved analysis state | Any; `not_analyzed`, `queued`, `processing`, `completed`, `failed` |
| `limit` | Maximum items returned | `20`; range `1–100` |
| `offset` | Items to skip after filtering | `0`; zero or greater |

Example: `/api/dossiers?search=dacia&status=not_analyzed&limit=20&offset=0`.

**Returns `200`**, sorted by dossier ID descending:

```json
{
  "items": [{
    "dossier_id": "DOS-001",
    "vehicle": {
      "marque": "Dacia", "modele": "Logan", "matricule": "EXAMPLE",
      "annee": "2015", "date_sinistre": null
    },
    "created_at": "2026-10-17T09:00:00",
    "status": "not_analyzed",
    "photo_count": 5
  }],
  "total": 1,
  "counts": {
    "all": 1, "not_analyzed": 1, "queued": 0,
    "processing": 0, "completed": 0, "failed": 0
  },
  "limit": 20,
  "offset": 0,
  "warnings": []
}
```

`total` is the number matching search and status, before pagination. `counts`
apply search but ignore the status filter and pagination. `photo_count` counts
existing declared originals. Invalid folders are skipped and reported in
`warnings` as `{dossier_id, code, message}`. No matches returns an empty list.

**Errors:** `422` invalid query; `503` storage unavailable.

## GET `/api/dossiers/{dossier_id}/result`

Use for the saved expertise screen. This endpoint does not start a scan.

**Needs:** an existing dossier ID, for example `DOS-001`. A completed result
requires valid `analysis/result.json`, all declared originals and annotations,
and `analysis/report.pdf`.

**Returns `200`** when no saved analysis exists:

```json
{
  "dossier_id": "DOS-001",
  "status": "not_analyzed",
  "progress": null,
  "error": null,
  "result": null
}
```

For a completed analysis, `status` is `completed` and `result` contains:

| Field | Returned value |
| --- | --- |
| `schema_version` | `1` |
| `vehicle` | Same vehicle fields as the listing; missing year/date stays `null` |
| `financial` | Completeness, currency, totals, breakdown, and warnings; example below |
| `parts` | Array of part objects; engine-specific fields still to be agreed |
| `detections` | Array of detection objects; engine-specific fields still to be agreed |
| `photos` | Array of photo IDs and URLs; example below |
| `report_url` | `/api/dossiers/DOS-001/report` |

An incomplete financial result can belong to a **completed scan**:

```json
{
  "status": "incomplete",
  "currency": "MAD",
  "estimated_total": null,
  "known_subtotal": 300,
  "breakdown": {
    "repair_labor": 0,
    "paint_labor": 0,
    "paint_materials": 0,
    "replacement_labor": 300,
    "parts_supply": null
  },
  "warnings": ["Replacement part price unavailable."]
}
```

`known_subtotal` adds known breakdown amounts. A complete estimate has every
amount and a numeric `estimated_total`; unknown costs remain `null`.

Each entry in `photos` has this shape:

```json
{
  "photo_id": "avant_01",
  "original_url": "/api/dossiers/DOS-001/media/photos/avant_01.jpg",
  "parts_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_pieces.png",
  "damages_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_dommages.png",
  "critical_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_critical.png"
}
```

Use the returned URLs directly. Critical-zone images need not appear in the UI.

`progress` is always `null` in this phase. Without final output, existing saved
states can be `not_analyzed`, `queued`, `processing`, or `failed`. A failed state
returns `error: {"code": "scan_failed", "message": "The scan failed."}` and
`result: null`. Legacy labels such as `À valider` do not establish completion.

**Errors:** `404` unknown dossier; `422` invalid ID, saved data, or required
artifacts; `503` storage unavailable.

Runner output format: [analysis-artifacts.md](analysis-artifacts.md).

## GET `/api/dossiers/{dossier_id}/media/{path}`

Use returned photo URLs as image sources for the viewer and comparison slider.

**Needs:** an existing dossier ID and a declared original or expected annotation
path. Use forward slashes, for example `photos/avant_01.jpg` or
`analysis/annotated/avant_01_pieces.png`.

**Returns `200`:** image bytes, with `Content-Type: image/jpeg` or `image/png`.
This is an image response, not JSON.

Metadata, feedback, arbitrary files, and paths escaping the dossier are blocked.
Windows-style paths in existing metadata are supported internally.

**Errors:** `404` unknown dossier or missing/disallowed image; `422` invalid ID,
metadata, or unreadable image; `503` storage unavailable.

## GET `/api/dossiers/{dossier_id}/report`

Use `report_url` for the PDF download action.

**Needs:** an existing dossier ID with a valid completed saved analysis.

**Returns `200`:** PDF bytes with `Content-Type: application/pdf` and
`Content-Disposition: attachment; filename="rapport_DOS-001.pdf"`.
The API serves the saved report; it does not generate one.

**Errors:** `404` unknown dossier or no final analysis; `422` invalid ID, saved
analysis, or required artifacts; `503` storage unavailable. A published result
whose PDF is missing or unreadable returns `422`.

## Error format

All error responses use:

```json
{"error": {"code": "dossier_not_found", "message": "Dossier not found."}}
```

Use the HTTP status and `error.code` for frontend handling. Internal paths and
raw exception details are not exposed.

## Verification

```bash
pip install -r requirements-api-dev.txt
python -m pytest tests/api -q
```

Tests use temporary fixtures, not repository data.

Manual checklist:

- Launch twice and confirm both instances receive different free ports.
- Open `/docs` using each printed URL.
- List, search, filter, and paginate dossiers in `/docs`.
- Confirm counts remain useful when filtering by status.
- Read an unscanned dossier and open an existing original image.
- Once real outputs exist, inspect the result and follow its image/PDF URLs.
- Restart the API and reopen a saved result.
- Check unknown IDs, invalid parameters, and disallowed file paths.
