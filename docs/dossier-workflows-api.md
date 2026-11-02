# Dossier workflows API contract

This document defines the backend contract for the React dossier dashboard,
vehicle entry, original-photo viewer, quick estimation, and repair comparisons.
It depends on the existing FastAPI file repository, AutoEstimate vehicle catalog,
and asynchronous expertise pipeline. Read it alongside [api.md](api.md),
[metadata.md](metadata.md), and [analysis-artifacts.md](analysis-artifacts.md).

## Categories and ownership

| Stable `type` | Origin | Manual creation | AI scan |
| --- | --- | --- | --- |
| `forfait_expertise` | Another CABEK application | Rejected | Existing expertise pipeline |
| `chiffrage_rapide` | `cabek_ai` | Supported | Same expertise pipeline, minimum one photo |
| `verification` | `cabek_ai` | Supported | Disabled: `409 scan_disabled_for_type` |

An absent `type` is read as `forfait_expertise`; an absent `source` is read as
`external`. Reads do not migrate or rewrite historical dossiers. The new PATCH
and upload routes reject expertise inputs with `409 externally_owned_dossier`.
The established `PUT /api/dossiers/{id}/vehicle` remains available for expertise,
with its existing canonical selection and scan locks. For new categories that
PUT follows the same edit/finalization rules as PATCH.

Verification never creates AI results, financial estimates, reports, approvals,
or certifications. Uploading any category's photo does not start a scan.

## Vehicle reference and required inputs

Use `GET /api/vehicles/marques` and
`GET /api/vehicles/models?marque_id=12`. Their existing responses are arrays:

```json
[{"id": 12, "libelle": "Dacia"}]
```

```json
[{"id": 34, "marque_id": 12, "libelle": "Sandero"}]
```

IDs here are illustrative. Use actual catalog IDs. New dossiers require positive
JSON integer `marque_id` and `model_id`, nonblank `matricule` (maximum 100
characters), and `date_mise_en_circulation` as a complete valid `YYYY-MM-DD`
string. Years, timestamps, compact dates, nulls, and impossible dates are rejected.
The backend verifies brand existence and model membership and saves canonical
`marque`/`modele` names; callers cannot supply these names.

Missing configuration, network failure, non-200 reference responses, malformed
catalog data, and inconsistent model brands return `503 autoestimate_unavailable`.
No dossier is created or changed on failed reference validation. Reads use saved
labels and work offline. IDs are revalidated at scan admission, as before.
The AutoEstimate token remains on the backend.

Both existing pricing clients use `GET /estimation` with `marque_id`, `model_id`,
`part_id`, `type`, and optional integer `year`. The worker derives that supported
`year` from the full registration date. It does not send a speculative registration
date query parameter. The complete date remains in metadata and the saved result
vehicle snapshot. `annee` remains the fallback for historical dossiers without
a complete date; local creation/date edits also maintain that compatibility year.

## Storage

```text
data/dossiers/{id}/
  metadata.json
  photos/{uuid}.jpg                     # quick estimate / legacy originals
  photos/before/{uuid}.png              # verification originals
  photos/after/{uuid}.png
  analysis/                            # existing scans and shared edit locks
```

New metadata includes `dossier_id`, immutable `type` and `source`, canonical
vehicle IDs/names, `matricule`, full registration date, compatibility `annee`, UTC
`created_at`/`updated_at`, `workflow_status`, active `photos`, and `photo_history`.
Existing `analysis/status.json` remains the source for AI scan state. The old
metadata `status` is preserved and is not interpreted as AI completion.

Each active new photo record includes:

```json
{
  "photo_id": "8a2577a77c2d4b318e09683d952e2340",
  "path": "photos/before/8a2577a77c2d4b318e09683d952e2340.png",
  "side": "front",
  "phase": "before",
  "uploaded_at": "2026-10-28T10:00:00+00:00"
}
```

Quick-estimation phases are null. Side/phase identity is explicit in metadata.
Verification allows one current photograph per phase/side. Quick estimates allow
several photos on the same side. Generated IDs avoid original filename collisions.
Published metadata and individual photos use temporary sibling files, fsync, and
atomic rename. New dossiers become visible only after a hidden staging directory
is fully initialized. Images are published before the manifest points at them;
metadata publication failure removes the newly uploaded file and retains old inputs.
A process crash between publications may leave an unreferenced image or hidden
staging folder; neither becomes readable through API media/list responses.

Verification writes use the same admission/worker locks as other edits; an
`analysis/` directory containing locks alone is not an analysis. Before evidence
locks when the first after photo is saved (`before_finalized_at`); all evidence
and metadata edits lock when all eight valid photos exist (`evidence_finalized_at`).
These durable markers remain effective if a file is later lost or corrupted.
There is no API unlock/delete/finalization override in this version.

## Workflow state and scan state

`type`, `workflow_status`, and `status` are independent response fields.
Workflow state is derived on reads from vehicle completeness, available valid
photos, and saved scan files. The metadata `workflow_status` is a write-time
snapshot; clients must use API state during/after asynchronous scanning.

| Category | `workflow_status` | Meaning |
| --- | --- | --- |
| Expertise | Existing scan state | `not_analyzed`, `queued`, `processing`, `completed`, `failed` |
| Quick | `draft` | Required vehicle inputs incomplete in saved metadata |
| Quick | `awaiting_photos` | Vehicle complete; no valid original |
| Quick | `ready_to_scan` | Vehicle complete and at least one valid original |
| Quick | `analyzing` | Existing scan state queued or processing |
| Quick | `estimated` | Completed validated analysis, even if finances are incomplete |
| Quick | `failed` | Scan failed; retry uses the existing queue/cleanup behavior |
| Verification | `before_incomplete` | One or more before sides missing/invalid |
| Verification | `awaiting_after` | Four before sides present; zero after sides |
| Verification | `after_incomplete` | Four before sides present; some after sides missing |
| Verification | `comparison_ready` | All eight valid photographs present |

New requests require complete vehicle inputs, so normally start in
`awaiting_photos` or `before_incomplete`. `draft` supports incomplete externally
edited/imported quick metadata. Verification always returns AI `status:
not_analyzed`, `progress: null`, `result: null`. Missing/corrupt evidence updates
the comparison's missing-side arrays but cannot unlock finalized inputs.

## Endpoint reference

Paths use `/api/dossiers`; substitute the saved ID for `{id}`. All existing
result, scan, media, WebP preview, PDF, and vehicle routes remain available.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /api/dossiers` | JSON required vehicle fields plus new `type` | `201` full dossier detail |
| `GET /api/dossiers` | Optional filters below | `200` paginated summaries/counts |
| `GET /api/dossiers/{id}` | None | `200` full dossier detail |
| `PATCH /api/dossiers/{id}` | JSON permitted vehicle edits | `200` full dossier detail |
| `GET /api/dossiers/{id}/result` | None | `200` same detail and saved result |
| `GET /api/dossiers/{id}/photos` | Optional `phase=before\|after` | `200` `{dossier_id, photos, comparison}` |
| `POST /api/dossiers/{id}/photos` | Multipart `file`, `side`, optional `phase` | `201` full dossier detail |
| `PUT /api/dossiers/{id}/photos/{photo_id}` | Same multipart fields | `200` full dossier detail |
| `POST /api/dossiers/{id}/scan` | No body | `202` queued scan; verification rejects |
| `GET /api/dossiers/{id}/media/{path}` | Returned URL | Original/annotation JPEG or PNG |
| `GET /api/dossiers/{id}/preview/{path}.webp` | Returned URL | Existing cached WebP preview |
| `GET /api/dossiers/{id}/report` | Returned URL | Existing saved PDF; verification returns `404` |

Creation example:

```http
POST /api/dossiers
Content-Type: application/json

{
  "type": "chiffrage_rapide",
  "marque_id": 12,
  "model_id": 34,
  "matricule": "EXAMPLE-123",
  "date_mise_en_circulation": "2020-05-17"
}
```

Use `"type": "verification"` for before/after collection. Unknown JSON fields,
manual expertise creation, and supplied workflow/scan states are rejected (`422`).

PATCH accepts any nonempty subset of `matricule`, `date_mise_en_circulation`, and
the pair `marque_id`/`model_id`. Both IDs must be supplied together. Null values
and unknown fields are rejected. Photos, source, type, timestamps, expert fields,
and arbitrary metadata cannot be patched. Active/completed scans and finalized
verification comparisons reject edits (`409`). Example:

```json
{"matricule": "CORRECTED-123", "date_mise_en_circulation": "2021-06-01"}
```

### Original upload and explicit replacement

Send multipart fields `file` (one image), `side` (`front`, `rear`, `left`, `right`),
and, for verification, required `phase` (`before`, `after`). Quick estimates reject
phases. Upload several photos with successive requests; four quick-estimation
sides are not required. Verification after uploads require all four valid before
photos. A duplicate verification slot returns `409 photo_slot_occupied`.

```bash
curl -X POST "$CABEK_URL/api/dossiers/$DOSSIER_ID/photos" \
  -F 'side=front' -F 'phase=before' -F 'file=@front.png;type=image/png'
```

Use the same fields with PUT and the current returned `photo_id` for an explicit
replacement. Replacement preserves the existing phase/side, saves a new UUID
file, and appends the old record to `photo_history` with `replaced_at` and
`replaced_by`. Previous bytes are retained on disk and are not publicly allowlisted.
The new response returns the new ID/URLs. A stale ID returns `404 photo_not_found`.
Finalized evidence returns `409 evidence_finalized`.

Only JPEG (`.jpg`/`.jpeg`, `image/jpeg`) and PNG (`.png`, `image/png`) are supported.
Extension, MIME, signature, complete decoding, and single-frame content must agree.
Maximum image: 10 MiB and 20 million pixels. Entire multipart request: 11 MiB,
including chunked requests, bounded before parsing/spooling. Original filenames
never determine paths; separators, drive syntax, and nulls reaching validation
are rejected. The multipart parser may first strip legacy Windows path prefixes.
Invalid images/fields return `422`; excessive bytes return `413 upload_too_large`.

### Detail and comparison response

Both GET detail routes preserve `vehicle`, `status`, `photos`, `progress`, `error`,
and `result`, and add category/workflow fields. Saved financial/annotation/PDF
fields retain their existing structure. Newly uploaded originals are immediately
available through the same confined media/preview handlers before any scan.

Representative verification detail with one original (IDs/URLs shortened):

```json
{
  "dossier_id": "DOS-EXAMPLE",
  "type": "verification",
  "source": "cabek_ai",
  "created_at": "2026-10-28T10:00:00+00:00",
  "updated_at": "2026-10-28T10:01:00+00:00",
  "workflow_status": "before_incomplete",
  "status": "not_analyzed",
  "vehicle": {
    "marque_id": 12, "model_id": 34, "marque": "Dacia", "modele": "Sandero",
    "matricule": "EXAMPLE-123", "annee": "2020",
    "date_mise_en_circulation": "2020-05-17", "date_sinistre": null
  },
  "photos": [{
    "photo_id": "PHOTO_ID", "side": "front", "phase": "before",
    "uploaded_at": "2026-10-28T10:01:00+00:00",
    "original_url": "/api/dossiers/DOS-EXAMPLE/media/photos/before/PHOTO_ID.png",
    "preview_url": "/api/dossiers/DOS-EXAMPLE/preview/photos/before/PHOTO_ID.png.webp"
  }],
  "progress": null, "error": null, "result": null,
  "comparison": {
    "slots": [
      {"side": "front", "before": {"photo_id": "PHOTO_ID", "side": "front", "phase": "before", "original_url": "/api/dossiers/DOS-EXAMPLE/media/photos/before/PHOTO_ID.png", "preview_url": "/api/dossiers/DOS-EXAMPLE/preview/photos/before/PHOTO_ID.png.webp", "uploaded_at": "2026-10-28T10:01:00+00:00"}, "after": null},
      {"side": "rear", "before": null, "after": null},
      {"side": "left", "before": null, "after": null},
      {"side": "right", "before": null, "after": null}
    ],
    "missing_before": ["rear", "left", "right"],
    "missing_after": ["front", "rear", "left", "right"],
    "before_locked": false, "finalized": false
  }
}
```

Comparison slots always appear in `front`, `rear`, `left`, `right` order, and
contain the same photo objects as `photos`. `comparison` is null for other
categories. Photo listing's `phase` filter affects its `photos`, while its
`comparison` always describes the whole dossier. Missing originals are omitted
from `photos`; invalid originals cannot be served and are missing in comparisons.
Legacy photo objects keep their existing three URL/ID fields. New photo objects
also expose `side`, `phase` (null for quick estimates), and `uploaded_at`.
Use returned URLs directly: they include a reverse-proxy deployment prefix.

### Dashboard filtering, sorting, and counts

| Parameter | Allowed value/default |
| --- | --- |
| `type` | Any stable category ID; omitted means all |
| `workflow_status` | Any documented workflow state; mismatched category yields no items |
| `status` | Existing `not_analyzed`, `queued`, `processing`, `completed`, `failed` |
| `search` | Case-insensitive ID, saved marque/modele, matricule; maximum 200 characters |
| `sort` | `id_desc` (default), `id_asc`, `created_at_desc`, `created_at_asc`, `updated_at_desc`, `updated_at_asc` |
| `created_from`, `created_to` | Inclusive UTC creation dates, `YYYY-MM-DD` |
| `limit`, `offset` | Existing defaults `20`, `0`; limit 1–100; offset nonnegative |

All filters and sorting run before pagination. Date sorts use chronological UTC
timestamps with dossier ID as the deterministic tiebreaker. Missing/invalid
timestamps sort before dated records ascending, after them descending, and do
not match a creation-date filter. An inverted date range returns `422`.

List responses preserve `items`, `total`, `counts`, `limit`, `offset`, `warnings`.
Items add `type`, `source`, `workflow_status`, `updated_at`, and full registration
date under `vehicle`. `total` applies every selected filter before pagination.
Counters have these exact scopes:

- `type_counts`: search and creation range, ignoring type/workflow/scan selectors.
- `workflow_counts`: search, creation range, selected type; ignores workflow/scan selectors.
- `counts`: search, creation range, selected type/workflow; ignores scan status and pagination.

`counts.all` is the sum of the existing scan-state counters. Verification counts
as `not_analyzed`. `type_counts` always has the three keys, including zeros;
`workflow_counts` contains states present in its scope. Invalid folders remain
skipped with safe warnings.

### Scan admission and saved results

Expertise keeps its existing behavior. Quick scans require complete saved vehicle
inputs and at least one valid declared photo; every declared original must be
valid. Only one photographed side is sufficient. Admission uses the existing
AutoEstimate validation, queue, locks, subprocess worker, progress, input
fingerprints, YOLO detections, pricing, finance calculations, and saved artifacts.
Poll `/result` until completed/failed. Completed analysis cannot be silently
replaced; failed scans retry through the same endpoint and cleanup rules.

Verification scan initiation returns `409 scan_disabled_for_type` before creating
scan directories/jobs or checking model availability. The standalone runner also
rejects verification requests as a second guard. PDF and financial generation
are not part of verification.

## Errors

Existing safe envelope: `{"error":{"code":"...","message":"..."}}`.
Upstream credentials, raw errors, and internal absolute paths are never returned.

| HTTP | Code | Meaning |
| --- | --- | --- |
| 404 | `dossier_not_found`, `photo_not_found`, `artifact_not_found`, `report_not_found` | Unknown/unavailable allowlisted resource |
| 409 | `externally_owned_dossier` | New write route cannot edit expertise inputs |
| 409 | `scan_disabled_for_type` | Verification AI disabled |
| 409 | `scan_in_progress`, `already_analyzed` | Input snapshot or completed result protected |
| 409 | `before_photos_incomplete` | After collection requires four valid before sides |
| 409 | `photo_slot_occupied` | Explicit replacement required |
| 409 | `evidence_finalized` | Before evidence or complete comparison locked |
| 413 | `upload_too_large` | Image/request byte limit exceeded |
| 422 | `invalid_request` | Invalid JSON, fields, types, side, phase, query, or category |
| 422 | `invalid_vehicle_selection` | Unknown brand/model or incomplete ID pair |
| 422 | `invalid_metadata_update` | Empty PATCH or null edit values |
| 422 | `invalid_filename`, `unsupported_image`, `invalid_image`, `invalid_photo_phase`, `invalid_photo_slot` | Invalid photo request/content |
| 422 | `no_valid_photos`, `incomplete_vehicle_metadata` | Quick scan prerequisites missing |
| 422 | `invalid_date_range`, `invalid_metadata`, `invalid_result`, `invalid_status` | Invalid filter/saved input/artifacts |
| 503 | `autoestimate_unavailable`, `storage_unavailable`, `worker_unavailable` | Upstream/storage/worker or queue unavailable |

## Security boundary and production controls

This integration intentionally has **no authentication or per-dossier
authorization**. The supplied launcher binds to `127.0.0.1`. Use it in a trusted
local environment; configuring CORS does not provide authentication. These write
endpoints are not production-secure merely because paths/uploads are validated.
Before public exposure, enforce authenticated access and per-dossier ownership,
TLS, upload/request-rate/concurrency quotas, storage limits, and backup/retention
controls at the trusted gateway/service boundary. Protect the dossier directory
from untrusted local writers. Existing external filesystem writers do not take
API locks; scan fingerprints detect changed inputs, but cannot prevent their edits.

## Verification and manual checklist

Run `python -m pytest tests -q` after installing `requirements-api-dev.txt`.
Optional scan/report dependency tests run when `requirements-scan.txt` is present.
API tests generate temporary images and use a subprocess fixture instead of YOLO;
they do not validate detection quality or availability of a live AutoEstimate.

- [ ] Open a historical expertise dossier; confirm vehicle, originals, saved analysis, annotations, and PDF still load.
- [ ] Select actual AutoEstimate IDs, create each supported category, and reopen before scanning.
- [ ] Confirm incomplete dates, mismatched models, unavailable AutoEstimate, and manual expertise creation fail cleanly.
- [ ] Upload one quick photo on one side; start a real scan, observe progress, and open finances, annotations, and PDF.
- [ ] Confirm a second active/completed scan and changes to protected inputs return `409`.
- [ ] Create verification, add four before sides, reopen, add four after sides, and check each paired URL and missing-side list.
- [ ] Confirm after-before ordering, duplicate slot rejection, explicit replacement/history, and finalized-evidence locks.
- [ ] Confirm verification scans always fail and no estimate/report is produced.
- [ ] Compare category/workflow/scan counters across search, dates, sorting, and pagination.
- [ ] Try corrupt images, unsupported formats, excessive bytes, invalid sides/phases, traversal, and unlisted media paths.
- [ ] Reopen behind a deployment prefix and check original/preview URLs; verify PATCH/upload CORS preflights from the configured frontend.
