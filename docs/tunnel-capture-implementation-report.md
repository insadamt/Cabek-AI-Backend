# Persistent draft and tunnel capture delivery report

This report records the architecture decisions, changed files, verification,
and manual acceptance checklist for durable local capture and real analysis.
It depends on [tunnel-capture-api.md](tunnel-capture-api.md), the existing scan
worker, trained models, dossier storage, and AutoEstimate. It is the consolidated
technical report for this feature; no separate development journal was created.

## Foundation and scope

- Branch: `codex/persistent-drafts-tunnel-capture`, created directly from `dev` at
  `5aff8fcd8043ec241a0236154a9bf6590dbaf84f`.
- Fetched `origin/dev` and verified that it matches this base. Reviewed the local
  `ScanAPI` history/diff and existing API, scan, core, catalog, docs, and test modules.
- Reviewed [draft PR #4](https://github.com/CDMA-Solutions/Cabek-AI/pull/4), head
  `c7be6140cac056e89786342cf062a2fb9925bf02`, based on older `ScanAPI`. It is closed
  and unmerged. Reused the durable draft/Unicode plate intent; integrated it into
  current workflow services rather than merging its duplicate draft router.
- Preserved external expertise ownership, existing upload/media/preview/result
  routes, scan queue/worker locks, financial semantics, and trained model execution.
  No frontend, Streamlit, core engine, model, or repository dossier data was changed.
- Delivery uses one consolidated Git commit; its hash is supplied in the task response.

## Architecture decisions

Creation now accepts only a local category and atomically publishes an empty
metadata/photo folder. Optional vehicle values preserve the existing full-input
creation workflow. A root file lock and a digest stored within the dossier provide
restart-safe creation idempotency without a second indexing transaction. Clients
must retain the dossier ID and reuse their creation key on transport retries.

The mock provider reads real bounded JPEG/PNG inputs from configured local storage.
Camera sides come from explicit folders/prefixes; capture phase remains a separate
field. The capture service persists its plan/status in metadata and publishes
exact copied bytes before their manifest entries. Stable IDs derived from capture
identity/source prevent duplicates across retries. Existing samples and saved
evidence are never silently overwritten. Multiple photos per side are supported;
verification validates side coverage instead of a hardcoded image count.

The single tunnel and per-dossier capture locks protect photo publication and scan
admission. A separate metadata lock serializes read/modify/write operations so
vehicle updates do not overwrite capture records. Vehicle fields remain editable
during capture for both local categories, and during quick inference. Photo edits
continue to use the existing scan/worker locks. Verification finalization stays
durable even if evidence later disappears.

Inspection of `core.expertise_engine.analyser` confirmed that its `marque`
argument is unused. Its initial damage costs use the existing vehicle-independent
barèmes. The runner can therefore retain the existing real detection, deduplication,
feedback, and replacement logic before vehicle metadata is available. After saving
annotations and a real detection checkpoint, managed quick workers wait for valid
vehicle values. Quick admission no longer contacts the catalog before detection.

The pricing gate revalidates selected IDs with AutoEstimate, updates canonical
labels, and atomically freezes a run-specific vehicle snapshot. Pricing, financial
output, JSON and PDF use it. API writes are then rejected. Quick input fingerprints
exclude only the permitted vehicle/workflow bookkeeping fields; immutable metadata,
photo manifest, image bytes, and feedback retain change detection. Supervision and
restart recovery also compare the frozen vehicle snapshot. Expertise keeps its
existing whole-metadata snapshot and admission-time catalog validation.

No frontend timing or synthetic detection progress was introduced. Camera counts
come from valid saved originals; AI counts come from generated annotation files.
Verification never invokes the engine or produces financial/report artifacts.

## APIs and storage

Added `POST/GET /api/dossiers/{id}/capture` and the compatible
`PATCH /api/dossiers/{id}/metadata` alias. Extended existing creation with optional
metadata and `Idempotency-Key`. Existing PATCH and vehicle selection now honor the
pricing gate. Dossier/list workflow fields add capturing, capture failure, and
metadata waiting; scan status remains its existing independent enum.

`metadata.json` stores draft identity/category/timestamps, vehicle fields, creation
idempotency information, capture records, and photo manifests. Originals remain
under `photos/`, or `photos/before/` and `photos/after/` for verification. Capture
records contain phase, private source plan, status, identity, and safe errors.
Photo records contain stable ID, original filename, side, phase, and capture ID.
Existing `analysis/status.json`, annotations, result and PDF remain authoritative.
New private detection/stage/vehicle files are never served by media endpoints.
See the contract for exact layout and response examples.

## Changed files

| Files | Purpose |
| --- | --- |
| `.env.example`, `README.md` | Camera configuration and entry point to the contract |
| `api/config.py`, `api/main.py` | Settings, lifecycle, capture service/router, idempotency CORS header |
| `api/capture_routes.py`, `api/capture_schemas.py` | Capture requests, typed status/photos, safe errors |
| `api/capture_service.py`, `api/tunnel_provider.py` | Persistent capture, real sample reads, retries and recovery |
| `api/creation.py` | Durable creation idempotency |
| `api/workflow_routes.py`, `api/workflow_schemas.py` | Draft requests, plate/date validation, metadata alias |
| `api/workflow_service.py`, `api/workflow_state.py` | Atomic drafts, metadata concurrency, independent lifecycle |
| `api/repository.py`, `api/schemas.py` | Capture metadata, multiple verification photos, waiting stage |
| `api/vehicle_routes.py`, `api/vehicle_service.py` | Existing selection contract follows pricing/finalization rules |
| `scans/contracts.py`, `scans/jobs.py` | Managed quick metadata deferral and stable-photo admission |
| `scans/runner.py`, `scans/metadata_gate.py` | Real detection checkpoint, vehicle wait/freeze, existing pricing reuse |
| `scans/artifacts.py`, `scans/supervisor.py` | Separate immutable-input and vehicle-snapshot validation |
| `scans/progress.py`, `scans/cleanup.py` | Real waiting stage and private-artifact retry cleanup |
| `tests/api/test_drafts.py`, `tests/api/test_capture.py` | Durable drafts, Unicode, ingestion, locking, failures/restarts, phases |
| `tests/scans/test_metadata_gate.py` | Pricing freeze and input fingerprint protections |
| `tests/api/conftest.py`, `tests/api/scan_worker_fixture.py` | Valid write fixtures and production metadata gate in test workers |
| `tests/api/test_config.py`, `tests/api/test_dossiers.py` | Environment and additive route contracts |
| `tests/api/test_workflows.py`, `tests/api/test_workflow_safety.py` | Changed metadata prerequisites, locks and offline detection admission |
| `tests/api/test_workflow_listing.py`, `tests/api/test_workflow_photos.py` | Valid new plate fixtures; legacy behavior remains covered separately |
| `docs/tunnel-capture-api.md`, this report | Final contract, complete curl flow, decisions and acceptance evidence |
| `docs/api.md`, `docs/metadata.md`, `docs/analysis-artifacts.md`, `docs/dossier-workflows-api.md` | Reconcile existing contracts with the final implementation |

## Verification evidence

- Baseline before implementation: **221 passed** in 24.89 seconds.
- Final suite: `python -m pytest tests -q -o faulthandler_timeout=30`;
  **252 passed**. One existing Starlette/httpx deprecation warning.
- `python -m compileall -q api scans tests/api tests/scans`: passed.
- `git diff --check`: passed.
- Tests use isolated temporary dossiers. They cover draft restart persistence,
  concurrent/idempotent creation, both plate alphabets, invalid writes, partial
  updates, source byte preservation, JPEG/PNG validation and limits, unsafe paths,
  duplicate capture, single-tunnel contention, failure/retry, interrupted capture
  restart, scan admission only after stable originals, metadata during inference,
  real artifact persistence, pricing freeze, and multiple before/after photos.
- Existing expertise, upload, media, previews, results, pricing/report, cleanup,
  recovery, vehicle catalog, and dashboard tests remain passing.
- The sandboxed baseline hung in TestClient startup; the approved test runs
  outside the sandbox passed. No Playwright or browser automation was used.

A separate smoke run used the actual three model files, one existing repository
vehicle photograph copied into a disposable sample directory, the production mock
provider, and the real subprocess backend. It created a draft, captured the image,
generated **3 detections** and **3 annotation images**, and reached
`awaiting_metadata`. Actual IDs from the live AutoEstimate catalog were then
saved with Arabic matricule `84654-ب-454`; the worker completed and its PDF URL
returned **200**. The served original SHA-256 matched its source. Financial output
was correctly marked **incomplete**, rather than presenting an invented full total.
The temporary dossier was deleted after verification. This is a pipeline smoke
test, not a detection-quality or vehicle-specific quote accuracy assessment.

## Configure and test the complete flow

Enable `CABEK_TUNNEL_MODE=mock`, set `CABEK_MOCK_PHOTOS_DIR` to actual side-labelled
JPEG/PNG files, optionally set `CABEK_MOCK_PHOTO_DELAY_SECONDS`, and install both
API/scan requirements. Run `python -m api`. Follow the curl walkthrough in
[tunnel-capture-api.md](tunnel-capture-api.md#complete-local-flow): create a draft
with an idempotency key, capture, poll camera/AI separately, select live vehicle
IDs, PATCH metadata, then retrieve the saved result, annotations and PDF. For
verification, capture before then after on the same saved ID and inspect every
returned original URL.

## Manual acceptance checklist

The backend smoke described above was executed. The following operator/browser
acceptance checks remain manual:

- [ ] Create a type-only quick draft with an idempotency key; refresh/reopen it,
  restart the API, and verify the same ID, category, timestamps and empty vehicle.
- [ ] Start delayed capture; watch counts increase only after an original can be
  opened. Repeat capture requests and confirm no duplicate photographs or runs.
- [ ] Enter lowercase Latin and Arabic plates; confirm normalization, persisted
  Arabic, and dashboard search. Reject malformed plates without losing old values.
- [ ] Let real YOLO finish before vehicle entry; observe saved annotations and
  `awaiting_metadata`, with no final PDF/result yet. Save live catalog IDs/date;
  verify pricing, result/PDF and frozen metadata behavior.
- [ ] Confirm unknown prices remain null/provisional with warnings; inspect the
  vehicle chosen for the actual car before accepting any estimate.
- [ ] Interrupt capture after one image; reopen and retry the same dossier/phase.
  Confirm saved IDs/bytes persist and missing samples are imported once.
- [ ] Use a corrupt, truncated, oversized, disguised, or unsafe sample; inspect
  the saved error, repair its source, and retry without creating another dossier.
- [ ] Capture verification before/after with several images on one side; confirm
  all photos, phases and representative comparison slots on the same dossier.
- [ ] Verify after-before ordering and final evidence locks; confirm verification
  never produces an analysis, estimate, or PDF.
- [ ] Open an external expertise dossier and confirm its existing scan, originals,
  annotations, report, vehicle selection and filtered listing still work.
- [ ] Stop/restart during inference; retry safely and confirm original hashes stay
  unchanged. Verify image and PDF URLs behind the intended proxy prefix/CORS origin.

## Remaining limitations

- The physical tunnel transport remains simulated. Camera side naming is explicit;
  no automatic camera-position inference was added.
- A metadata wait occupies the existing single model worker and counts toward its
  timeout. Other jobs remain queued. Failed/interrupted AI retries rerun inference;
  detection checkpoint resumption is not implemented.
- AutoEstimate reference availability is required to validate final IDs. Missing
  part quotes can leave finances incomplete, as observed in the real smoke run.
- A completed phase is immutable for capture replay. Source changes do not replace
  saved evidence. External loss/corruption of finalized files has no API repair flow.
- Atomic image and metadata publication remain separate filesystem operations;
  an abrupt crash between them can leave unreferenced bytes. They are not served
  by the allowlist, and retry uses stable paths. Local storage still assumes trusted
  filesystem writers and the existing unauthenticated local deployment boundary.

## Commit message suggestions

- `feat(dossiers): add persistent drafts and real-analysis tunnel capture`
- `feat(api): persist mock captures and defer vehicle pricing until metadata is ready`
