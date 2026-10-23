# Dossier metadata format

Each dossier stores its inputs in `data/dossiers/<dossier_id>/metadata.json`.
The API validates this file through `DossierMetadata` in `api/repository.py`;
vehicle fields use `Vehicle` in `api/schemas.py`.

## Current format

```json
{
  "dossier_id": "DOS-001",
  "marque_id": 12,
  "model_id": 34,
  "marque": "Dacia",
  "modele": "Sandero",
  "matricule": "EXAMPLE",
  "annee": "2020",
  "date_sinistre": null,
  "created_at": "2026-10-17T09:00:00",
  "photos": [{"path": "photos/avant_01.jpg"}]
}
```

The IDs above illustrate the format. Select real IDs from the vehicle reference
endpoints described in [api.md](api.md#vehicle-selection).

| Field | Contract |
| --- | --- |
| `dossier_id` | Required string matching the containing folder name |
| `marque_id`, `model_id` | Positive JSON integers supplied together; both may be absent or `null` for legacy records |
| `marque`, `modele` | Saved string labels; omitted names default to `""`, and legacy `null` names are read as `""` |
| `matricule` | String; defaults to `""` when omitted |
| `annee`, `date_sinistre` | Strings or `null`; default to `null` when omitted |
| `created_at` | String or `null`; defaults to `null` when omitted |
| `photos` | Array of objects containing a string `path`; defaults to an empty array when omitted |

Original photo paths must point directly under `photos/` and use JPEG or PNG
extensions. Filename stems must be unique within the dossier. Write new paths
with forward slashes; existing Windows-style separators remain supported.
Every declared original must exist and pass image validation before a scan.
An empty photo array can be read but cannot be scanned.

Existing collection fields such as `expert`, `status`, `updated_at`, and photo
fields such as `view`, `view_label`, and `filename` may remain in the file.
Vehicle selection preserves these fields. Metadata `status` labels such as
`À valider` do not establish API scan completion.

## Vehicle identity and compatibility

Use `model_id`, not `modele_id`. Numeric identifiers belong in `marque_id` and
`model_id`; `marque` and `modele` contain names. A lone ID, a nonpositive ID, a
numeric string ID, or a numeric name value is invalid metadata.

When IDs exist, they take precedence over labels. Vehicle selection validates
the brand/model relationship against AutoEstimate and saves canonical labels.
Scan admission repeats this validation before capturing its input snapshot.
ID-only records are readable with empty names; reads neither resolve names over
the network nor rewrite the file.

Legacy name-only records remain readable and can be scanned without automatic
ID migration. Do not guess IDs from names. Use
`PUT /api/dossiers/{dossier_id}/vehicle` with a verified pair of IDs to save a
selection; see [api.md](api.md#vehicle-selection) for validation and scan locks.

API vehicle responses include both IDs, names, matricule, year, and accident
date. Legacy records return `null` IDs. In dossier details, top-level `vehicle`
reflects current metadata; completed `result.vehicle` retains the analysis
snapshot. Metadata has no `schema_version` field requirement; the saved analysis
envelope separately uses `schema_version: 1` as described in
[analysis-artifacts.md](analysis-artifacts.md).
