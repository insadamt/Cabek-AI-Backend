# Saved analysis contract (version 1)

This is the boundary between the standalone scan runner and the API. The runner
copies orchestration and report code from Streamlit and calls the existing
engine. Original engine, pricing client, barèmes, and Streamlit files stay
untouched. HTTP handlers do not import YOLO or execute inference.

```text
data/dossiers/<dossier_id>/
  metadata.json
  photos/<original filename>
  analysis/
    status.json                 # owned by the scan API
    annotated/<photo_id>_pieces.png
    annotated/<photo_id>_dommages.png
    annotated/<photo_id>_critical.png
    report.pdf
    result.json                 # publish last
```

Metadata must contain a matching `dossier_id` and a `photos` array of objects
containing `path`. Originals must be directly under `photos/`. Photo IDs are
the original filename without its extension, unique within the dossier.
The existing repository's Windows-style photo paths are supported.

Vehicle identity uses optional positive integer `marque_id` and `model_id`,
provided together. IDs take precedence over names: the vehicle-selection API
validates their relationship against AutoEstimate and saves canonical `marque`
and `modele` labels. Scan admission repeats validation for ID-based dossiers
before taking the input snapshot. Saved labels keep reads available offline;
legacy name-only dossiers remain supported. Do not guess IDs from vehicle names.

The runner writes into a backend-supplied output directory and publishes each
artifact by writing a temporary sibling file, closing it, then renaming it.
The final filenames must never refer to partially written artifacts.
The runner writes directly into `analysis/`: annotations after each photo, then
`report.pdf`, then `result.json` last. The supervisor validates worker exit,
output files, and input fingerprints before setting `status: completed`.
Managed results remain unavailable while their worker is active or failed.

Retry admission first locks the dossier and confirms no worker is still writing.
Valid completed output is preserved and rejected with `409`. Incomplete generated
annotations, report, result, and scan temporary files are removed before a full
rerun. Original photos, metadata, feedback, manual files, and active locks remain.
There are no per-run output directories or final copying step.

Restart recovery validates unowned final output: valid files become completed;
missing/invalid output becomes failed. It waits for a surviving worker to release
its lock before reconciling or cleaning files.

Hidden `.scan.lock`, `.worker.lock`, `.request.json`, `.worker.log`, and `.runtime/`
provide locking, private worker input/logs, and dependency caches inside this same
directory. A run identifier prevents a delayed old worker from writing a newer
run. These files are never served as media. Generated analysis directories are
ignored by Git.

## Final JSON

```json
{
  "schema_version": 1,
  "dossier_id": "DOS-001",
  "vehicle": {
    "marque": "Dacia",
    "modele": "Logan",
    "matricule": "EXAMPLE",
    "annee": "2015",
    "date_sinistre": null
  },
  "financial": {
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
  },
  "parts": [],
  "detections": [],
  "photos": [
    {
      "photo_id": "avant_01",
      "original_path": "photos/avant_01.jpg",
      "parts_path": "analysis/annotated/avant_01_pieces.png",
      "damages_path": "analysis/annotated/avant_01_dommages.png",
      "critical_path": "analysis/annotated/avant_01_critical.png"
    }
  ]
}
```

Every metadata photo must appear exactly once with matching paths. All three
annotations are required even with zero detections. Critical-zone images are
served but need not be displayed by the frontend. Missing or unreadable required
artifacts make final output invalid. Zero input photos cannot form a completed
analysis.

Financial amounts must be finite and nonnegative. `known_subtotal` equals the sum
of known breakdown amounts (within one cent). `complete` requires every breakdown
amount and a matching numeric `estimated_total`. `incomplete` requires
`estimated_total: null` and at least one warning; do not replace unknown values
with zero. A partly known category can contain its known subtotal with a warning.
A completed scan may have incomplete finances and remain provisional.
The runner must reconcile PDF values with JSON before publishing; the API checks
PDF readability, not its monetary text.

`parts` contains `part_id`, `name`, `action`, `financial`, `detection_ids`, and
`source_images`. `detections` keeps engine fields, with duplicate replacement
billing suppressed in copies before pricing. The original engine is unchanged.
The envelope, financial fields, and photo URLs are typed in OpenAPI.
Serialize NumPy values as ordinary JSON types. Do not include image arrays,
absolute paths, secrets, or model objects. Media URLs and `report_url` are built
by the API, not stored by the runner.

## Standalone function

`scans.runner.analyze_dossier(request: ScanRequest) -> ScanSummary` accepts the
validated metadata, photo manifest, feedback, model directory, and the cleaned
`analysis/` directory. It writes artifacts there and raises on failure. The API
handles admission, locks, retry cleanup, process lifetime, input-change checks,
and completion validation. No progress callback is required.
