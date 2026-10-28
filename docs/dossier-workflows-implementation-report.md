# Dossier workflows implementation report

This report records the delivered backend changes, validation evidence, and manual
acceptance checks for the three dossier categories. It depends on the contract in
[dossier-workflows-api.md](dossier-workflows-api.md) and the existing expertise
engine; it gives the frontend task a concrete handoff without changing React.

## Delivery and branch review

- Feature branch: `feat/dossier-workflows-api`.
- Foundation: local `ScanAPI` at `df3a515`, including its newer metadata, original-photo, and WebP work.
- Remote references were fetched and checked. `origin/dev` at `e518094` has the same API/scan/core/docs tree as `origin/ScanAPI` at `3334940`; local `ScanAPI` has newer suitable API changes.
- `main` precedes the FastAPI implementation; `Engine` has no API/scan/docs tree to incorporate.
- Pre-existing dossier data edits remain preserved in their checkpoint. The implementation commit changes no dossier data, models, core pricing calculations, or Streamlit files.
- The implementation is delivered in one consolidated commit; its final hash is reported in the task response.

## Behavior delivered

Legacy dossiers default to `forfait_expertise` without migration and retain
expertise scans, annotations, saved finances, and PDF access. Manual expertise
creation is rejected; new generic metadata/photo writes respect external ownership.

`chiffrage_rapide` supports local creation, canonical vehicle selection, complete
registration dates, side-associated photos, and analysis with one photographed
side. It uses the existing queue, YOLO orchestration, pricing, financial result,
and report pipeline. The full registration date maps to AutoEstimate's supported
integer `year` query parameter; historical `annee` remains supported.

`verification` supports four before and four after slots, paired URLs, missing-side
lists, explicit image replacement/history, and durable evidence locks. Before
photos lock when after collection starts; all edits lock at complete comparison.
AI admission and the standalone runner both reject this category. No financial,
approval, certification, or report workflow was added.

## Endpoints

Added `POST /api/dossiers`, `GET/PATCH /api/dossiers/{id}`,
`GET/POST /api/dossiers/{id}/photos`, and
`PUT /api/dossiers/{id}/photos/{photo_id}`. Photo writes use multipart
`file`, `side`, and optional/required category-specific `phase` fields.

Extended `GET /api/dossiers` with category, workflow, date, and sort filters and
useful counters before pagination. Detail/result responses preserve existing
fields and add type, workflow state, timestamps, and optional paired comparisons.
Existing scan, result, media, preview, PDF, vehicle catalog, and vehicle selection
routes remain available. Vehicle selection also honors verification finalization.

## Workflow definitions

| Category | Workflow states |
| --- | --- |
| Expertise | Existing `not_analyzed`, `queued`, `processing`, `completed`, `failed` |
| Quick estimate | `draft`, `awaiting_photos`, `ready_to_scan`, `analyzing`, `estimated`, `failed` |
| Verification | `before_incomplete`, `awaiting_after`, `after_incomplete`, `comparison_ready` |

Workflow progress is derived from saved inputs and scan state; clients cannot
manually patch it. AI `status` remains separate. Verification AI status stays
`not_analyzed`; its `progress` and `result` stay null.

## Changed files

| Files | Purpose |
| --- | --- |
| `api/workflow_schemas.py` | Typed creation/edit requests and stable workflow identifiers |
| `api/workflow_state.py` | Derived progress, available evidence, and comparison slots |
| `api/workflow_service.py` | Atomic local creation, metadata edits, ownership and locking |
| `api/workflow_routes.py` | Creation, editing, listing/uploading/replacing photos |
| `api/photo_service.py` | Content validation, side/phase rules, explicit revision history |
| `api/upload_limits.py` | Bound multipart bytes before parsing, including chunked bodies |
| `api/repository.py` | Typed/legacy metadata and confined verification paths |
| `api/schemas.py` | Additive response fields and typed photo comparisons |
| `api/service.py` | Filtering, counters, sorting, detail and comparison assembly |
| `api/routes.py` | Extended dashboard queries and metadata detail route |
| `api/main.py` | Shared workflow services, upload middleware, PATCH CORS |
| `api/vehicle_service.py` | Apply workflow locks through the existing vehicle PUT |
| `scans/jobs.py` | Category scan guards and quick vehicle prerequisites |
| `scans/runner.py` | Reuse pipeline and guard verification at the runner boundary |
| `scans/vehicle.py` | Full-date to supported pricing-year mapping |
| `requirements-api.txt` | Multipart parser dependency |
| `tests/api/conftest.py` | Isolated catalog, creation, and image-upload fixtures |
| `tests/api/test_dossiers.py`, `tests/api/test_vehicles.py` | Extend existing response/route expectations |
| `tests/api/test_workflows.py` | Creation, vehicle validation, ownership, one-side scan regression |
| `tests/api/test_workflow_photos.py` | Before/after pairing, replacement, content security, URLs |
| `tests/api/test_workflow_listing.py` | Category/workflow/date filters, totals and sort/pagination |
| `tests/api/test_workflow_safety.py` | Atomic failures, locks, input races, chunked limits and CORS |
| `tests/scans/test_runner_workflows.py` | Shared runner, pricing request contract, verification rejection |
| `docs/dossier-workflows-api.md` | Complete frontend API contract with examples and errors |
| `docs/api.md`, `docs/metadata.md`, `docs/analysis-artifacts.md` | Update existing contracts and compatibility notes |
| `README.md` | Workflow overview and contract link |
| `docs/dossier-workflows-implementation-report.md` | This delivery report and acceptance checklist |

## Validation and limits

- `.venv/bin/python -m pytest tests -q -o faulthandler_timeout=30`: **221 passed** in 25.74 seconds.
- One existing Starlette/httpx TestClient deprecation warning; no test failures or skips.
- `.venv/bin/python -m compileall -q api scans tests/api tests/scans`: passed.
- `git diff --check`: passed.
- No Playwright or browser automation used.
- Tests use temporary dossiers, mock upstream reference/pricing responses, subprocess scan fixtures, and mocked model detections. Live AutoEstimate availability and real YOLO detection quality were not acceptance-tested.
- No authentication/authorization exists in this local integration. The launcher binds to loopback. Public deployment requires authenticated per-dossier access, TLS, quotas, and storage/backup controls as documented in the API contract.
- Atomic publication prevents partial visible files; a process crash between image and metadata publication can leave unreferenced bytes. These bytes are not served by the allowlist.
- No implementation blockers remain; live integration acceptance and production access controls are deployment follow-up work.

## Manual acceptance checklist (not executed)

- [ ] Open a historical expertise dossier and verify vehicle details, originals, annotations, saved finances, and PDF.
- [ ] Load actual AutoEstimate brands/models; create quick and verification dossiers with a complete registration date.
- [ ] Try an incomplete date, mismatched model, and unavailable reference service; verify clear errors and no partial data.
- [ ] Upload one quick-estimation photo on one side; run real inference and check progress, financial output, annotations, and PDF.
- [ ] Attempt editing queued/completed inputs and starting duplicate scans; expect `409`.
- [ ] Upload four verification before photos, reopen the dossier, then upload four after photos and inspect all pairs.
- [ ] Test duplicate slots and explicit replacement; verify old bytes/history remain and finalized evidence cannot change.
- [ ] Attempt verification scans before/after completion; confirm rejection and absence of analysis/financial/report output.
- [ ] Check category/workflow/scan filters, search, inclusive dates, sort orders, counts, totals, and page boundaries.
- [ ] Try malformed, disguised, excessive-size, and invalid-side/phase uploads and disallowed/traversal media URLs.
- [ ] Run behind the intended proxy prefix and frontend origin; check returned image/preview URLs and write preflights.

## Commit message suggestions

- `feat(api): implement dossier workflows and safe photo evidence`
- `feat(dossiers): add quick estimates and before-after verification APIs`
