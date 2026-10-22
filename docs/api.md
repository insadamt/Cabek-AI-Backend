# Dossier API

Read existing dossiers, start real analyses, and retrieve saved results, images,
and PDFs. Only vehicle selection requires a JSON request body.

## Run locally

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-api.txt
python -m api --reload
```

To enable real scans, also install `pip install -r requirements-scan.txt`.
The API can serve saved results without model dependencies; scan requests return
`503` when dependencies or model files are missing.

The OS selects a free local port. Use the URL printed by Uvicorn, then append
`/docs` for interactive documentation. Each launch may use a different port.
Omit `--reload` to disable automatic restarts.
No authentication is required in this local integration phase.

The API automatically loads the repository-root `.env` on startup. Existing
environment variables take precedence, and scan workers inherit these values.
For local pricing, set `AUTOESTIMATE_TOKEN` and `AUTOESTIMATE_BASE_URL` there;
see `.env.example`. The private `.env` is ignored by Git. Restart the API after
editing it. The URL in the example is the original application's local service;
change it if your AutoEstimate service runs elsewhere.
Pricing also requires real `marque_id` and `model_id` values in dossier metadata;
vehicle names alone do not trigger a pricing lookup. The token stays on the backend and
is not needed in Postman or frontend requests.

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `CABEK_DOSSIERS_DIR` | Absolute path to the dossier folders | Repository `data/dossiers` |
| `CABEK_CORS_ORIGINS` | Comma-separated frontend origins | No cross-origin access |
| `CABEK_MODELS_DIR` | Directory containing the three existing `.pt` models | Repository `models` |
| `CABEK_SCAN_TIMEOUT_SECONDS` | Maximum execution time per scan | `1800` |
| `CABEK_SCAN_MAX_PENDING` | Maximum accepted scans, including the running scan | `8` |
| `AUTOESTIMATE_BASE_URL` | Pricing API base URL, including `/api/v1` | Unset |
| `AUTOESTIMATE_TOKEN` | Pricing bearer token | Unset |

Example frontend origin: `http://localhost:5173`.
Missing pricing configuration produces an incomplete estimate, not an invented
price. Use one API server process: it runs one model worker at a time and queues
other accepted dossiers to limit model memory use.

## Vehicle selection

`GET /api/vehicles/marques` returns `[{"id": 12, "libelle": "Dacia"}]`.
`GET /api/vehicles/models?marque_id=12` returns
`[{"id": 34, "marque_id": 12, "libelle": "Sandero"}]`.
These IDs illustrate the format; use actual returned IDs. Both endpoints use
the backend token and return `503 autoestimate_unavailable` if reference data
cannot be loaded or is invalid. A model query requires a positive brand ID;
an empty list means no models were returned.

`PUT /api/dossiers/{dossier_id}/vehicle` selects a vehicle for an existing dossier.
Send JSON with positive integer IDs only:

```json
{"marque_id": 12, "model_id": 34}
```

The backend verifies the brand exists and the model belongs to it, then atomically
saves the IDs and canonical names, preserving photos and other metadata. Returns
`200` with `{"marque_id": 12, "model_id": 34, "marque": "Dacia", "modele": "Sandero"}`.
Names cannot be supplied in this request. Invalid IDs or relationships return
`422`; unknown dossier `404`; active or completed scans `409`; unavailable
AutoEstimate or storage `503`. Failed validation leaves metadata unchanged.

Saved vehicle metadata looks like:

```json
{
  "dossier_id": "DOS-001",
  "marque_id": 12,
  "model_id": 34,
  "marque": "Dacia",
  "modele": "Sandero",
  "annee": "2020",
  "matricule": "EXAMPLE",
  "photos": [{"path": "photos/avant_01.jpg"}]
}
```

IDs must be supplied together. Existing dossiers with names only remain supported
without automatic migration. When IDs are present, scan admission revalidates
them and refreshes saved names before capturing inputs; AutoEstimate must be
reachable. Dashboard/result reads use saved names without network calls. New
analysis vehicle objects include both IDs; legacy results may return null IDs.

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

## POST `/api/dossiers/{dossier_id}/scan`

Start a real analysis using the existing models. No body or model settings are
accepted from the frontend.

**Needs:** an existing dossier with at least one photo; all declared originals
must be valid JPEG/PNG files whose content matches the extension. The three model
files and scan dependencies must be available.

**Returns `202`:** an accepted job, without waiting for inference:

```json
{
  "dossier_id": "DOS-001",
  "status": "queued",
  "photos": [],
  "progress": {
    "stage": "queued",
    "annotations_ready": 0,
    "annotations_expected": 15,
    "photos_completed": 0,
    "photos_total": 5,
    "elapsed_seconds": 0,
    "eta_seconds": null,
    "report_ready": false,
    "result_ready": false
  },
  "error": null,
  "result": null
}
```

Poll `GET /api/dossiers/{dossier_id}/result` every 1–2 seconds until `completed`
or `failed`. Counts come from valid generated files, not a timer. Each photo
requires parts, damage, and critical-zone annotations: five photos mean fifteen
files. Stages are `queued`, `preparing`, `analyzing_photos`, `finalizing`, then
`completed` or `failed`. ETA stays `null` while no reliable estimate is available.

Failed scans can retry with the same POST. All outputs use the same `analysis/`
directory. Under the dossier lock, retry cleanup removes incomplete generated
files before analysis starts again from scratch. It preserves original inputs,
manual files, active locks, and completed results. There are no per-run folders.

Completed dossiers reject rescans permanently. Closing the browser does not stop
processing. API shutdown stops its worker. After restart, valid final output is
preserved as completed; incomplete interrupted work becomes failed and can retry.
Cleanup waits if an old worker is still running. Computation is not resumed
mid-photo.

**Errors:** `404` unknown dossier; `409 already_analyzed` or `scan_in_progress`;
`422` invalid inputs; `503 worker_unavailable` for missing dependencies/models,
a full queue, or a stopped worker. Stored failure codes include `scan_failed`,
`scan_timeout`, `scan_interrupted`, and `scan_inputs_changed`.

## GET `/api/dossiers/{dossier_id}/result`

Use for dossier details and the saved expertise screen. This endpoint does not start a scan.
The top-level `photos` array contains existing declared originals in every scan
state, including before analysis. Missing originals are omitted; URLs include
the deployment prefix. The separate `/photos` endpoint has been removed; use
this response instead. The paginated dossier list continues to return `photo_count`.

**Needs:** an existing dossier ID, for example `DOS-001`. A completed result
requires valid `analysis/result.json`, all declared originals and annotations,
and `analysis/report.pdf`.

**Returns `200`** when no saved analysis exists:

```json
{
  "dossier_id": "DOS-001",
  "status": "not_analyzed",
  "photos": [{
    "photo_id": "avant_01",
    "original_url": "/api/dossiers/DOS-001/media/photos/avant_01.jpg",
    "preview_url": "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp"
  }],
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
| `parts` | Part ID, name, action (`repair`/`replace`), financial details, detection IDs, source images |
| `detections` | Existing engine detections, including severity, confidence, feedback, and costs |
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

Each entry in `result.photos` has this shape:

```json
{
  "photo_id": "avant_01",
  "original_url": "/api/dossiers/DOS-001/media/photos/avant_01.jpg",
  "parts_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_pieces.png",
  "damages_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_dommages.png",
  "critical_url": "/api/dossiers/DOS-001/media/analysis/annotated/avant_01_critical.png",
  "original_preview_url": "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp",
  "parts_preview_url": "/api/dossiers/DOS-001/preview/analysis/annotated/avant_01_pieces.png.webp",
  "damages_preview_url": "/api/dossiers/DOS-001/preview/analysis/annotated/avant_01_dommages.png.webp",
  "critical_preview_url": "/api/dossiers/DOS-001/preview/analysis/annotated/avant_01_critical.png.webp"
}
```

Use the returned URLs directly. For display, use top-level `photos[].preview_url`
and `result.photos[].*_preview_url`. Existing `*_url` media fields still serve
the full saved files for downloads or detailed inspection. Critical-zone images
need not appear in the UI. Load annotations only when their view is opened.

`progress` is `null` before a managed scan exists. During a scan, it contains
observed output counts as shown above. The last snapshot remains available on
failure or completion. Legacy external status files have no managed progress.
A failed state returns a safe error code/message and `result: null`.
Legacy labels such as `À valider` do not establish completion.

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

## GET `/api/dossiers/{dossier_id}/preview/{path}.webp`

Use the returned preview URLs for the viewer and comparison slider. The source
extension is preserved before `.webp`, for example `photos/avant_01.jpg.webp`
or `analysis/annotated/avant_01_pieces.png.webp`.

**Returns `200`:** `image/webp` bytes, encoded at quality 82, with the longest
edge limited to 1600 pixels. Aspect ratio is preserved and small images are
not enlarged. Pixel orientation is preserved to align originals and annotations.
Original files, saved PNG annotations, inference inputs, and PDFs are unchanged.
WebP is a display format; scan inputs still require JPEG/PNG.

Previews are generated on demand and kept in a bounded in-memory cache of
16 entries per API process. No preview files are written to dossier storage.
Source identity, size, and modification/change times invalidate cache entries;
restart or eviction requires encoding again. Pillow must have WebP support.

Responses include `ETag` and `Cache-Control: private, no-cache`, allowing browser
storage with revalidation. Send `If-None-Match` to receive `304` with no body
when unchanged. The original media allowlist and image validation run even on
cache hits and conditional requests. Cached images cannot bypass deleted,
invalid, or disallowed sources.

**Errors:** `404` missing/disallowed source or missing `.webp` suffix; `422`
invalid ID, metadata, or unreadable image; `503` storage unavailable.

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
python -m pytest tests -q
```

Tests use temporary fixtures, not repository data. Model/report tests require
the optional scan dependencies.

Manual checklist:

- Launch twice and confirm both instances receive different free ports.
- Open `/docs` using each printed URL.
- List, search, filter, and paginate dossiers in `/docs`.
- Confirm counts remain useful when filtering by status.
- Read an unscanned dossier and open an image from the top-level `photos` array.
- Confirm completed details include originals in `photos` and annotations in `result.photos`.
- Confirm the removed `/api/dossiers/{dossier_id}/photos` endpoint returns `404`.
- POST a scan, poll real file counts, and confirm the final JSON/PDF open.
- Confirm concurrent scans and successful rescans return `409`.
- Retry a failed scan; verify incomplete outputs are cleared and progress starts from zero.
- Confirm photos, metadata, feedback, manual files, and completed results are preserved.
- Once real outputs exist, inspect the result and follow its image/PDF URLs.
- Restart the API and reopen a saved result.
- Check unknown IDs, invalid parameters, and disallowed file paths.

WebP preview manual checklist:

- Open an unscanned dossier's `photos[].preview_url`; confirm `image/webp`.
- Open all four completed `*_preview_url` values, including behind a proxy prefix.
- Compare fine scratches, labels, and damage boundaries against the full saved image.
- Confirm original and annotation previews align in the comparison slider.
- Check the longest edge is at most 1600 pixels and smaller images are not enlarged.
- Compare transferred bytes and loading time in browser DevTools.
- Revalidate with `If-None-Match`; expect `304`, then change the source and expect `200` with a new ETag.
- Confirm original downloads, scan inputs, and saved analysis files are unchanged.
- Confirm missing, corrupt, undeclared, and escaping source paths remain blocked.
