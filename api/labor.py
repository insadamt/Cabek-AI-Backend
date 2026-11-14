"""Aggregate billable working hours for new scans and historical API results.

Depends only on saved engine dictionaries and API schemas so reading hours never
loads inference models, calls pricing services, or rewrites saved artifacts.
"""

from math import isfinite

from api.schemas import LaborHours, StoredAnalysis


# ============================================================
# BILLABLE LABOR DURATIONS
# ============================================================

def build_labor_hours(instances: list[dict]) -> LaborHours:
    values = {category: [] for category in LaborHours.model_fields}
    for item in instances:
        rejected = str(item.get("validation_expert") or "").strip().lower() == "rejetee"
        if rejected or item.get("chiffrage_bloque"):
            continue
        detail = item.get("cout_detail") or {}
        if item.get("remplacement_requis"):
            components = {"replacement_labor": item.get("heures_mot", detail.get("heures_mot"))}
        else:
            components = {
                "repair_labor": detail.get("heures_reparation"),
                "paint_labor": detail.get("heures_mop"),
            }
        for category, value in components.items():
            try:
                hours = float(value) if value is not None else None
            except (TypeError, ValueError, OverflowError):
                hours = None
            if hours is not None and (not isfinite(hours) or hours < 0 or isinstance(value, bool)):
                hours = None
            values[category].append(hours)

    # An incomplete sum must not look like a full duration; unused categories are zero.

    return LaborHours(**{
        category: None if None in hours else round(sum(hours), 2)
        for category, hours in values.items()
    })


# ============================================================
# READ-ONLY COMPATIBILITY FOR SAVED ANALYSES
# ============================================================

def result_with_labor_hours(analysis: StoredAnalysis) -> dict:
    payload = analysis.model_dump(exclude={"dossier_id", "photos"})
    if "labor_hours" not in analysis.financial.model_fields_set and analysis.detections:
        payload["financial"]["labor_hours"] = build_labor_hours(analysis.detections).model_dump()
    for part in payload["parts"]:
        financial = part.get("financial")
        if not isinstance(financial, dict) or "labor_hours" in financial:
            continue
        detection_ids = part.get("detection_ids") or []
        matching = [
            item for item in analysis.detections
            if (item.get("detection_id") in detection_ids if detection_ids else
                bool(part.get("part_id")) and str(item.get("piece_ai_brut")) == str(part["part_id"]))
        ]

        # Older results without matching evidence cannot establish a zero-hour duration.

        financial["labor_hours"] = (build_labor_hours(matching) if matching else LaborHours()).model_dump()
    return payload
