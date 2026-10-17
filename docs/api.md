# Dossier API

This phase exposes existing folders through read-only routes. It does not start
scans, track progress, run models, or generate demo results. The Streamlit app
and engine remain independent.

## Setup

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-api.txt
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for interactive OpenAPI documentation.
The default data root is `data/dossiers`, resolved from the repository location.
Set `CABEK_DOSSIERS_DIR` to an absolute directory to use another source.
The API never creates or edits that directory.

Set `CABEK_CORS_ORIGINS` to comma-separated frontend origins, for example
`http://localhost:5173,http://127.0.0.1:5173`. No origins are allowed by default.
This phase has no authentication and is intended for local integration.

## Routes

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/api/dossiers` | Search, filter, counts, pagination |
| GET | `/api/dossiers/{id}/result` | Saved analysis or an empty result |
| GET | `/api/dossiers/{id}/media/{path}` | Allowlisted original/annotation |
| GET | `/api/dossiers/{id}/report` | Saved PDF attachment |

`POST /api/dossiers/{id}/scan` is deliberately deferred. No artificial progress
or sample dossier folders are created.

### Folder listing

Query parameters: `search` (case-insensitive ID/marque/modèle/matricule,
maximum 200 characters), `status`, `limit` (1–100, default 20), `offset`
(zero or greater). Results sort by dossier ID descending.

The response contains `items`, `total` after search and status filtering,
`counts` after search but before status filtering, `limit`, `offset`, and
`warnings`. Counts contain `all`, `not_analyzed`, `queued`, `processing`,
`completed`, and `failed`. Invalid dossier folders are skipped with a warning;
direct access to such a folder returns an error. Dot folders are ignored.

Each item has `dossier_id`, `vehicle`, `created_at`, `status`, and `photo_count`.
Photo count includes existing allowlisted originals, rather than missing files.
`vehicle` includes `marque`, `modele`, `matricule`, nullable `annee`, and nullable
`date_sinistre`. Missing dates and years are never invented.

### Result

```json
{
  "dossier_id": "DOS-001",
  "status": "not_analyzed",
  "progress": null,
  "error": null,
  "result": null
}
```

A valid final `analysis/result.json` plus its required artifacts establishes
`completed`. Legacy metadata labels such as `À valider` do not imply a saved
analysis. Without a final result, the API can read `status` from an existing
`analysis/status.json` (`not_analyzed`, `queued`, `processing`, `failed`). A
stored `completed` state without final output is invalid. `progress` stays null
in this phase; future scan work will implement the agreed file-based tracking.
Failed states expose a fixed safe error message, never stored exception text.

Completed responses replace `result: null` with vehicle, financial, parts,
detections, photos, schema version, and a backend-generated `report_url`.
Photo entries contain `photo_id`, `original_url`, `parts_url`, `damages_url`,
and `critical_url`. See [the artifact contract](analysis-artifacts.md).

### Media and reports

Originals must be explicitly declared in metadata. Annotation names are derived
from those photos, never arbitrary requested filenames. Windows backslashes in
legacy metadata are normalized; request paths must use forward slashes.
Traversal, absolute paths, and symlinks leaving the dossier are rejected.
Metadata, feedback, status files, and arbitrary filesystem files are not media.
Images are verified before serving. Reports require a valid completed result and
a readable PDF. File responses have `X-Content-Type-Options: nosniff`.

### Errors

```json
{"error": {"code": "dossier_not_found", "message": "Dossier not found."}}
```

`404`: unknown dossier or unavailable/disallowed artifact.
`422`: invalid parameters, malformed metadata/status/result, or invalid saved
artifacts. `503`: configured data root missing or unreadable. Paths and raw
exception details are not returned to clients.

## Verification

```bash
pip install -r requirements-api-dev.txt
python -m pytest tests/api -q
```

Tests create isolated temporary fixtures, never demo progress or repository data.
The endpoint tests use FastAPI's documented
[TestClient workflow](https://fastapi.tiangolo.com/tutorial/testing/).

Manual checklist:

- List existing folders in `/docs`; search by ID and vehicle information.
- Filter by `not_analyzed` and confirm counts and pagination.
- Open an existing original image, including Windows-style metadata paths.
- Confirm an unscanned dossier returns `result: null`.
- Once real outputs exist, open results, all annotations, and the PDF download.
- Restart the API and confirm the same saved result is available.
- Request an unknown dossier, undeclared image, and traversal path; confirm errors.
- Confirm no scan-start endpoint or fabricated progress exists.
