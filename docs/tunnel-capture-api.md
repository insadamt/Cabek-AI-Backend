# Persistent drafts, Tiandy cameras and demo capture

This is the frontend integration contract for durable drafts, camera simulation,
and real AI analysis. It depends on the existing dossier filesystem, FastAPI scan
queue, three trained YOLO models, AutoEstimate, and the separate Tiandy API.
Expertise capture requires selecting an externally created dossier before starting the tunnel.

## Tiandy and demonstration cameras

The sibling `../Tiandy_CABEK_API` service provides named channels using API v2.1.0.
Create its own virtual environment, install its `requirements.txt`, configure its
private `.env`, and run `.venv/bin/python camera_api.py`. It uses port 8001, one
worker and no reload. RGB remains on its separately configured port (9001 on this installation).
Never copy the archived virtual environment or place credentials in Vite.

Install `requirements-cameras.txt` into the existing Cabek Global API environment.
Set `CABEK_TUNNEL_MODE=tiandy`, `CABEK_TIANDY_API_URL` and
`CABEK_TIANDY_API_KEY` on Cabek Global. Restart the API after environment changes.
Tiandy's `CAMERA_API_KEY` and Cabek's `CABEK_TIANDY_API_KEY` must match.
The Tiandy process is only required when using physical cameras.

`GET/PATCH /api/settings/operation` includes persistent `demo_mode` and `slow_mode`.
PATCH accepts either or both strict booleans; empty/null updates are rejected.
Legacy settings files default demo mode from `CABEK_TUNNEL_MODE=mock`; `tiandy`
defaults to physical cameras unless a persisted preference overrides it.
`disabled` remains a hard stop regardless of the preference. This installation
starts with persisted demo mode enabled. Settings exit does not reset preferences.
Lighting remains controlled by its existing configuration in both source modes.

`GET /api/cameras` returns `source` (`demo`, `tiandy`, `disabled`) and thirteen
read-only camera identities, readiness and Cabek-owned preview URLs. Optional
`phase=before|after` selects demonstration evidence for that verification phase.
Cabek proxies `/api/cameras/{channel}/stream` and `/latest.jpg`; only the backend
supplies the upstream `X-API-Key`. Streams are not buffered and close on disconnect
or bounded upstream timeout. Existing trusted-local deployment restrictions remain.

Essential channels are 4/front, 13/rear, 14/left and 10/right. Optional zoom channels
are 1/rear, 2/front, 5/left, 6/front, 7/right, 8/right, 11/left and 12/rear.
Channel 3/Centre is diagnostic-only. Physical-camera display names come from
Tiandy; demo names use the same commissioned registry without contacting Tiandy.

Capture accepts `{"zoom_channels":[1,12]}` and verification additionally requires
`"phase":"before"` or `"after"`. Omitted zooms mean four essentials in Tiandy
mode; legacy `mock` calls without `zoom_channels` retain their previous file-based
semantics. The frontend always supplies the new selection, including an empty list.
Reject duplicate, unknown, essential and diagnostic channels in zoom selections.

Each modern capture plans exactly four essentials plus selected zooms and freezes
source, camera names and slow mode at admission. After lighting stabilizes, read
baseline frame IDs and require subsequent snapshots to advance. Validate age,
content type, full decoding and existing image limits. All planned photographs
must succeed before AI admission. Errors identify the unavailable camera; no
automatic fallback to demo or partial AI submission occurs.

Real-camera retries create a fresh complete current-phase attempt, preserving the
old partial evidence in `photo_history` and leaving the other verification phase
untouched. Source or zoom-selection changes on failed attempts also start fresh.
Active/completed requests remain idempotent; AI retries use saved originals.
Capture status includes `capture_source`, `zoom_channels` and `error_message`.
Camera photos include `camera_channel`, `camera_name`, `camera_role`,
`capture_source`, and `frame_id`; legacy photo responses omit absent camera fields.
Representative verification slots prefer essentials while retaining all zooms.

Demo reads the existing confined side samples, preferring phase directories when
present. One source per side supplies the essential image; the two side zooms use
the left and right image halves without enlargement. Preview and capture use the
same crop operation. Demo evidence is explicitly labeled and does not require Tiandy.

## Configure and start

```bash
pip install -r requirements-cameras.txt  # Additional transport for an installed API environment.
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
tunnel_unavailable`. Supported values are `disabled`, `mock`, and `tiandy`. Delivery
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
exists. Legacy mock expertise and quick capture need at least one photograph; modern camera captures require four essentials. Verification requires all
four distinct sides per phase and may save several photographs on each side.

## Endpoint contract

All paths below start with `/api/dossiers`. Existing media, preview, result,
scan, report, vehicle, manual upload, and dashboard APIs remain available.

| Method/path | Request | Response |
| --- | --- | --- |
| `POST /api/dossiers` | `{"type":"chiffrage_rapide"}` or `verification`; optional vehicle fields | `201` existing full dossier detail |
| `GET /{id}` or `GET /{id}/result` | Saved ID | Current vehicle, workflow, originals, AI progress/result |
| `PATCH /{id}/metadata` or existing `PATCH /{id}` | Nonempty partial vehicle fields | `200` full detail |
| `POST /{id}/capture` | Expertise/quick: omitted body or `{}`; verification: `{"phase":"before"}` / `after` | `202` capture status, including on duplicate requests |
| `GET /{id}/capture` | Expertise/quick: no query; verification: `?phase=before` / `after` | `200` capture status |
| `POST /{id}/scan` | No body | `202` existing real scan admission/retry |

`POST /api/dossiers` creates the folder and metadata before returning. Type-only
creation returns `workflow_status: draft`, `status: not_analyzed`, empty vehicle
names/plate, null vehicle IDs/date, no photos, a unique ID, and UTC creation time.
`forfait_expertise` remains externally created. Creation, metadata PATCH, manual
uploads, and deletion remain unavailable. Select an existing imported dossier,
then call its capture endpoint without a phase. A fresh capture requires no usable
original photographs; stale manifest entries for missing files do not block it.
Existing photo sets return `409 capture_not_allowed`. After a failed or interrupted
expertise capture, the next explicit scan starts a new capture identity and takes
all photos again. Superseded partial photos leave the active manifest and remain
in `photo_history`. Completed captures with available photos stay idempotent. Imported
metadata, unknown fields, and omitted legacy type/source fields are preserved.

For the expertise picker, use `GET /api/dossiers?type=forfait_expertise` with
`workflow_status=awaiting_photos`, plus `search`, `limit`, and `offset`.
Failed expertise captures return to this same list; there is no separate resume tab. No dossier is created by selection, restoration, or capture.

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

1. Select the existing expertise dossier, or persist a quick/verification draft; request capture for that ID and its applicable phase.
2. The provider validates each actual file with the existing bounded decoder:
   extension/content agreement, full decode, single frame, 10 MiB, 20 megapixels.
   It atomically publishes copied bytes before adding their manifest entry.
3. Capture completes only after the declared images exist and required sides
   validate. Original sample files remain unchanged for repeated testing.
4. Completed expertise and quick captures submit the existing scan manager automatically.
   Expertise retains its existing vehicle validation, legacy metadata support, and
   immutable input snapshot. Capture bookkeeping finishes before that snapshot.
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

Expertise exposes `awaiting_photos`, `capturing`, or `ready_to_scan` before
analysis; failed captures return to `awaiting_photos`. Active/final analysis
retains `queued`, `processing`, `completed`, or `failed`. Empty unprocessed dossiers
show Scanner even when their metadata still lists unavailable original paths.

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

Concurrent/repeated requests reuse the active capture identity. Active capture
blocks manual image mutations and scans. Expertise restarts failed capture from
the beginning with a new identity, new photo IDs, and a fresh source-file plan;
previous partial photos are archived rather than reused. Quick-estimation and
verification retries retain their phase identity, skip registered images, and
import only missing/new samples. Restore or repair planned but unsaved source files before
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

## Interior white lighting (Shelly Plus 1PM)

The backend alone controls Shelly; its URL must never be placed in Vite settings.
Environment-loaded startup defaults to verification enabled and rejects a missing
`CABEK_INTERNAL_LIGHTS_URL`. Set `CABEK_INTERNAL_LIGHTS_ENABLED=false` explicitly
for a hardware-free setup. Direct `ApiSettings` construction defaults to disabled
for isolated tests. See `.env.example` for switch ID (0), HTTP timeout (3 seconds),
stabilization (500 ms), and optional `CABEK_INTERNAL_LIGHTS_MIN_POWER_W`.
Numeric settings must be finite; timeout is positive and at most 30 seconds,
stabilization is 0–30000 ms, and minimum power is nonnegative.

After acquiring capture and tunnel locks, the worker POSTs `Switch.Set`, GETs
`Switch.GetStatus`, requires `output=true`, and waits the stabilization interval
before reading photographs. If a calibrated minimum power is supplied, `apower`
must be a finite numeric measurement strictly above it. No threshold is guessed.
Relay-only verification cannot guarantee physical LED illumination. Commission
with an electrician/operator and measure the real healthy load before configuring
this threshold. Automated tests mock the device and never switch hardware.

Lights remain on for the entire photograph sequence. OFF is attempted before
releasing tunnel ownership; failures are logged without changing capture success.
There is no delayed interior cleanup. Exterior RGB lifecycle remains independent;
lighting failures request `warning`, repeated at brightness 100, without allowing
RGB errors to replace the original capture error.

Lighting failures persist `status=failed`, `error_code`, and a French
`error_message`: `internal_lighting_unavailable`,
`internal_lighting_not_confirmed`, or `internal_lighting_power_low`.
No photographs are taken, metadata is retained, and POST capture retries normally.
Admission remains asynchronous (202); poll capture status for worker failures.

The frontend should use `stage=lighting_preparing` for « Préparation de
l'éclairage… », `lighting_ready` for « Éclairage prêt », and `capturing` for
« Capture en cours… ». On lighting failure show « Éclairage indisponible » or
« Éclairage insuffisant » for low power, the existing warning icon and retry action,
and stop progress animation. Keep `error_message` in scan controls after the Island
dismisses. Stages may pass between polls; do not manufacture photo progress.
This repository contains no React/Dynamic Island or central settings frontend.

Local maintenance endpoints reuse loopback-client and local-Origin restrictions:
`GET /api/settings/internal-lights/status`, `POST /api/settings/internal-lights/on`,
and `POST /api/settings/internal-lights/off`. Responses expose only `output`,
`apower`, `voltage`, and `current`. Both commands acquire the tunnel lock and return
409 while capture owns it. Disabled devices receive no HTTP requests. Interior
maintenance does not require exterior RGB to be enabled.

Run backend tests in hardware-free mode:
`CABEK_INTERNAL_LIGHTS_ENABLED=false .venv/bin/python -m pytest tests -q`.
