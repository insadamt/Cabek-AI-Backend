# Persistent drafts and simulated tunnel API

This is the frontend integration contract for durable drafts, camera simulation,
and real AI analysis. It depends on the existing dossier filesystem, FastAPI scan
queue, three trained YOLO models, and AutoEstimate. Only photograph delivery from
the physical tunnel is simulated. No frontend repository changes are required.

## Configure and start

```bash
pip install -r requirements-api.txt -r requirements-scan.txt
export CABEK_TUNNEL_MODE=mock
export CABEK_MOCK_PHOTOS_DIR=/absolute/path/to/sample-photos
export CABEK_MOCK_PHOTO_DELAY_SECONDS=0.3
python -m api
```

Use the local URL printed by the launcher as `CABEK_URL`. The dossier root must
exist. `CABEK_DOSSIERS_DIR` overrides `data/dossiers`; `CABEK_MODELS_DIR` overrides
`models`. Existing `.env` settings for `AUTOESTIMATE_BASE_URL` and
`AUTOESTIMATE_TOKEN` are inherited by workers. Draft creation without IDs and
image detection do not need AutoEstimate. Selecting IDs and beginning quick
pricing require the live catalog. Missing part quotes remain unknown amounts.

`CABEK_TUNNEL_MODE` defaults to `disabled`; capture then returns `503
tunnel_unavailable`. Only `disabled` and `mock` are currently implemented. Delivery
delay must be finite and nonnegative; it does not control inference or pricing.
Run one API process for this local prototype, with one tunnel and one model worker.

Provide real JPEG/PNG files using either side folders or filename prefixes:

```text
sample-photos/
  front_01.jpg
  front_02.png
  rear_01.jpg
  left_01.jpg
  right_01.jpg
```

Alternatively use `front/01.jpg`, `rear/01.jpg`, etc. French prefixes/folders
`avant`, `arriere`, `gauche`, `droite` are accepted. Prefix delimiters are `_`,
`-`, space, or dot. Unrecognized image sides, deeper layouts, unsafe names, and
symlinks are rejected. Non-image files and hidden entries are ignored.

For verification, optional `before/` and `after/` directories each contain this
same layout. If the requested phase directory exists, only its photographs are
read; otherwise a flat sample directory can be reused for both phases. Side
identifies camera position; phase identifies capture time. No four-photo limit
exists. Quick capture needs at least one photograph; verification requires all
four distinct sides per phase and may save several photographs on each side.

## Endpoint contract

All paths below start with `/api/dossiers`. Existing media, preview, result,
scan, report, vehicle, manual upload, and dashboard APIs remain available.

| Method/path | Request | Response |
| --- | --- | --- |
| `POST /api/dossiers` | `{"type":"chiffrage_rapide"}` or `verification`; optional vehicle fields | `201` existing full dossier detail |
| `GET /{id}` or `GET /{id}/result` | Saved ID | Current vehicle, workflow, originals, AI progress/result |
| `PATCH /{id}/metadata` or existing `PATCH /{id}` | Nonempty partial vehicle fields | `200` full detail |
| `POST /{id}/capture` | Quick: omitted body or `{}`; verification: `{"phase":"before"}` / `after` | `202` capture status, including on duplicate requests |
| `GET /{id}/capture` | Quick: no query; verification: `?phase=before` / `after` | `200` capture status |
| `POST /{id}/scan` | No body | `202` existing real scan admission/retry |

`POST /api/dossiers` creates the folder and metadata before returning. Type-only
creation returns `workflow_status: draft`, `status: not_analyzed`, empty vehicle
names/plate, null vehicle IDs/date, no photos, a unique ID, and UTC creation time.
`forfait_expertise` remains externally created; new creation/capture/PATCH reject it.

Send a stable `Idempotency-Key` header on creation, and retain the returned dossier
ID across refreshes. Repeating that key and the original request returns the same
dossier even after restart or later metadata edits. Reusing it with a different
creation payload returns `409 idempotency_conflict`. Without a key, each creation
request deliberately creates a different dossier. Keys have 1–200 characters;
only their SHA-256 digest is stored. CORS allows this header for configured origins.

PATCH supports `matricule`, the pair `marque_id`/`model_id`,
`date_mise_en_circulation`, `annee` (four digits), and `date_sinistre` (ISO date).
Omitted fields and unknown legacy metadata are preserved; null/empty updates and
unknown fields are rejected. Both IDs must be supplied together and validated
through AutoEstimate. A complete registration date also updates `annee`.

New plate writes require 1–6 ASCII digits, a single Latin or Arabic letter, then
1–6 ASCII digits, separated by hyphens. `14554-B-4578`, `14554-b-4578`, and
`84654-ب-454` are valid. Latin is uppercased; Arabic stays unchanged through
storage, search, and responses. Historical plate formats remain readable and
can remain untouched when updating other fields.

Representative capture response; actual IDs and URLs come from the server:

```json
{
  "dossier_id": "DOS-EXAMPLE",
  "phase": "before",
  "capture_id": "CAPTURE_ID",
  "status": "capturing",
  "expected_count": 5,
  "saved_count": 1,
  "photos": [{
    "photo_id": "PHOTO_ID",
    "side": "front",
    "phase": "before",
    "status": "saved",
    "original_filename": "front_01.jpg",
    "uploaded_at": "2026-11-02T15:00:00+00:00",
    "original_url": "/api/dossiers/DOS-EXAMPLE/media/photos/before/PHOTO_ID.jpg",
    "preview_url": "/api/dossiers/DOS-EXAMPLE/preview/photos/before/PHOTO_ID.jpg.webp"
  }],
  "error_code": null,
  "analysis_error": null
}
```

Capture `status` is `not_started`, `capturing`, `completed`, or `failed`.
Absent captures have null identity, zero counts, and an empty collection.
`saved_count` counts registered, valid dossier images; it is independent of AI
progress. URLs include the configured proxy root prefix and return actual bytes.
No image data is embedded in JSON. Public responses omit source filesystem paths.

## Capture, inference, and completion

1. Persist the draft, then request capture for that ID and phase.
2. The provider validates each actual file with the existing bounded decoder:
   extension/content agreement, full decode, single frame, 10 MiB, 20 megapixels.
   It atomically publishes copied bytes before adding their manifest entry.
3. Capture completes only after the declared images exist and required sides
   validate. Original sample files remain unchanged for repeated testing.
4. Completed quick capture submits the existing scan manager automatically.
   Manual uploads retain explicit `POST /scan`. Verification never submits AI.
5. YOLO writes real annotation files, then a private detection checkpoint. Quick
   jobs with missing vehicle values expose `progress.stage: awaiting_metadata`
   and retain the generated annotation counts. No final result or report exists yet.
6. The operator can PATCH vehicle fields during capture, queued scans, inference,
   and metadata waiting. Required quick pricing inputs are validated IDs, a
   nonblank matricule, and a full registration date. The worker validates IDs
   again against AutoEstimate and freezes the vehicle under the metadata lock.
7. After the freeze, vehicle writes return `409 pricing_in_progress`. The worker
   runs existing AutoEstimate pricing, financial calculations, JSON and PDF
   publication. The supervisor validates outputs and inputs before completion.

Capture errors use `error_code`; an automatic scan admission failure (missing
models, unavailable catalog, full queue) uses `analysis_error`. A saved capture
stays `completed` even if AI admission or inference fails. Repeating completed
capture admission can retry an unstarted scan; retry a failed AI job with
`POST /scan`. Never repeat dossier creation to recover a capture or AI failure.

`status` in dossier detail remains the existing analysis enum. New workflow
states are `draft`, `capturing`, `capture_failed`, and `awaiting_metadata`; quick
completion is `estimated`. Existing ready/analyzing/failure states remain valid.
No finalize request is needed: quick completion is server-managed by validated
worker output. Verification becomes `awaiting_after` after valid before evidence,
then `comparison_ready` when both phases have all required sides. Before evidence
locks when after collection starts; complete evidence locks after capture completes.
`comparison.slots` holds one representative photo per side; use `photos` or phase
photo listing for every photograph when a side has multiple images.

## Persistence and retry

```text
data/dossiers/{id}/
  metadata.json                  # vehicle, lifecycle, captures, photo manifests
  .capture.lock                  # per-dossier camera admission
  .metadata.lock                 # atomic read/modify/write and pricing freeze
  photos/{stable_photo_id}.jpg    # quick originals, exact source bytes
  photos/before/{id}.png          # verification originals
  photos/after/{id}.png
  analysis/status.json            # independent existing AI state/progress
  analysis/annotated/*            # existing real model artifacts
  analysis/.detections.json       # private real engine checkpoint
  analysis/.pipeline.json         # private waiting/finalizing marker
  analysis/.vehicle.json          # private pricing snapshot and run identity
  analysis/result.json
  analysis/report.pdf
```

The existing scan locks, request snapshots, supervision, cleanup, and restart
recovery remain. The root `.tunnel.lock` protects the one tunnel; `.creation.lock`
serializes idempotent creation. Hidden files are never served by media endpoints.

Each dossier phase has one durable capture identity. Concurrent/repeated requests
reuse it. Active capture blocks manual image mutations and scans. Failed capture
retry skips already registered images, preserves their IDs/bytes, and imports only
missing/new samples. Restore or repair planned but unsaved source files before
retrying; newly added filenames extend the plan on an explicit failed retry.
Source edits never silently replace already saved evidence. Completed capture
does not import additional files; use a new dossier for a new quick inspection.

Shutdown or restart marks interrupted captures failed after confirming no capture
owner holds the lock. Partial images and metadata remain inspectable. Retry the
same capture request. A crash after completed capture but before scan submission
is reconciled on restart; failed AI jobs require explicit scan retry. AI failures
and timeouts preserve originals. If storage cannot save the failure marker, the
next restart can recover the lingering active capture.

The metadata wait uses the existing single worker and counts toward
`CABEK_SCAN_TIMEOUT_SECONDS` (default 1800). Other model jobs remain queued while
it waits. Timeout/restart retries rerun inference; checkpoint-based resumption is
not implemented. Missing AutoEstimate quotes may produce a completed analysis
with incomplete finances; consult financial warnings instead of assuming zero.

## Complete local flow

```bash
# Set CABEK_URL to the URL printed by python -m api.
curl -s -X POST "$CABEK_URL/api/dossiers" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: local-inspection-001' \
  -d '{"type":"chiffrage_rapide"}'
# Retain dossier_id from this response as DOSSIER_ID.
curl -s -X POST "$CABEK_URL/api/dossiers/$DOSSIER_ID/capture"
curl -s "$CABEK_URL/api/dossiers/$DOSSIER_ID/capture"
curl -s "$CABEK_URL/api/dossiers/$DOSSIER_ID/result"
curl -s "$CABEK_URL/api/vehicles/marques"
curl -s "$CABEK_URL/api/vehicles/models?marque_id=$MARQUE_ID"
# Set both IDs to actual values returned by these reference endpoints.
curl -s -X PATCH "$CABEK_URL/api/dossiers/$DOSSIER_ID/metadata" \
  -H 'Content-Type: application/json' \
  -d "{\"marque_id\":$MARQUE_ID,\"model_id\":$MODEL_ID,\"matricule\":\"84654-ب-454\",\"date_mise_en_circulation\":\"2020-05-17\"}"
curl -s "$CABEK_URL/api/dossiers/$DOSSIER_ID/result"
```

Poll capture and result separately every 1–2 seconds. Open returned originals,
annotations, and `report_url`. For verification, create `type: verification`, POST
capture with `{"phase":"before"}`, poll with `?phase=before`, then POST `after`
on the same ID and poll with `?phase=after`. AI state stays `not_analyzed`.

Additional errors include `409 tunnel_busy`, `capture_in_progress`,
`capture_incomplete`, `before_photos_incomplete`, `evidence_finalized`; `422
invalid_photo_phase`, `invalid_sample_side`, `invalid_sample_layout`,
`unsafe_sample_path`, `no_sample_photos`; and `503 mock_photos_unavailable`.
Asynchronous decoder, storage, interruption, and side errors appear in the saved
capture status. Existing safe error envelopes and unauthenticated local deployment
boundaries remain as documented in [dossier-workflows-api.md](dossier-workflows-api.md).

Tests and manual acceptance checks are in
[tunnel-capture-implementation-report.md](tunnel-capture-implementation-report.md).
