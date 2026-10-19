# Saved analysis contract (version 1)

This is the proposed boundary between the scan runner and the read-only API.
Agree it with the model developer before integrating her runner. The API does
not import the model code or translate raw Streamlit session state.

```text
data/dossiers/<dossier_id>/
  metadata.json
  photos/<original filename>
  analysis/
    status.json                 # owned by the future scan API
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

The runner writes into a backend-supplied output directory and publishes each
artifact by writing a temporary sibling file, closing it, then renaming it.
The final filenames must never refer to partially written artifacts.
Publish annotations after each photo and `result.json` after the PDF.
Future scan attempts must be isolated; this phase creates no attempts.

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
with zero. A completed scan may have incomplete finances and remain provisional.
The runner must reconcile PDF values with JSON before publishing; the API checks
PDF readability, not its monetary text.

`parts` and `detections` are arrays of objects passed through unchanged; their
engine-specific fields still need agreement with the model/frontend developers.
The envelope, financial fields, and photo URLs are typed in OpenAPI now.
Serialize NumPy values as ordinary JSON types. Do not include image arrays,
absolute paths, secrets, or model objects. Media URLs and `report_url` are built
by the API, not stored by the runner.
