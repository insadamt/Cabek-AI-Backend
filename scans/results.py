from copy import deepcopy
import json
from math import isfinite

from api.schemas import FinancialBreakdown, FinancialResult, StoredAnalysis, Vehicle
from core.expertise_engine import detection_rejetee
from scans.contracts import ScanRequest


# ============================================================
# BILLABLE COPIES AND FINANCIAL COMPLETENESS
# ============================================================

def prepare_invoice_instances(instances: list[dict]) -> list[dict]:
    invoice = deepcopy(instances)
    replacements = set()
    for item in invoice:
        if detection_rejetee(item) or not item.get("remplacement_requis"):
            continue
        key = item.get("piece_ai_brut") or item.get("detection_id")
        if key in replacements:

            # Different damage types on one replaced part must not bill its supply twice.

            item.update(chiffrage_bloque=True, remplacement_requis=False, cout=None)
            item["cout_detail"] = {"type": "inclus_dans_remplacement", "total": None}
        else:
            replacements.add(key)
    return invoice


def monetary_amount(value) -> float | None:
    if value is None:
        return None
    amount = float(value)
    if not isfinite(amount) or amount < 0:
        raise ValueError("Engine returned an invalid financial amount.")
    return round(amount, 2)


def build_financial(instances: list[dict]) -> FinancialResult:
    values = {key: [] for key in FinancialBreakdown.model_fields}
    warnings = []
    for item in instances:
        if detection_rejetee(item) or item.get("chiffrage_bloque"):
            continue
        piece = str(item.get("piece") or "Unidentified part")
        detail = item.get("cout_detail") or {}
        if item.get("remplacement_requis"):
            components = {"replacement_labor": item.get("cout_mot"), "parts_supply": item.get("prix_piece")}
            if detail.get("avertissement_api"):
                warnings.append(str(detail["avertissement_api"]))
        else:
            components = {
                "repair_labor": detail.get("cout_reparation"),
                "paint_labor": detail.get("cout_mop"),
                "paint_materials": detail.get("cout_met"),
            }
            for category, hours in (("repair_labor", "heures_reparation"), ("paint_labor", "heures_mop")):
                if hours in detail and detail[hours] is None:
                    components[category] = None
            warning = str(detail.get("erreur") or detail.get("avertissement") or "")
            if warning:
                warnings.append(warning)
                if "prix MET" in warning:
                    components["paint_materials"] = None
        for category, value in components.items():
            amount = monetary_amount(value)
            values[category].append(amount)
            if amount is None:
                warnings.append(f"{piece}: {category} unavailable.")
    breakdown = {}
    for category, amounts in values.items():
        known = [amount for amount in amounts if amount is not None]

        # A partially known category retains its known subtotal and an explicit warning.

        breakdown[category] = round(sum(known), 2) if known or not amounts else None
    subtotal = round(sum(value for value in breakdown.values() if value is not None), 2)
    warnings = list(dict.fromkeys(warnings))
    return FinancialResult(
        status="incomplete" if warnings else "complete", currency="MAD",
        estimated_total=None if warnings else subtotal, known_subtotal=subtotal,
        breakdown=FinancialBreakdown(**breakdown), warnings=warnings,
    )


# ============================================================
# FRONTEND RESULT SERIALIZATION
# ============================================================

def json_scalar(value):
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Unsupported engine output: {type(value).__name__}")


def build_result(request: ScanRequest, instances: list[dict], financial: FinancialResult) -> StoredAnalysis:
    groups = {}
    for item in instances:
        if detection_rejetee(item):
            continue
        key = item.get("piece_ai_brut") or item.get("detection_id")
        groups.setdefault(str(key), []).append(item)
    parts = []
    for key, damages in groups.items():
        part_financial = build_financial(damages)
        parts.append({
            "part_id": key, "name": damages[0].get("piece"),
            "action": "replace" if any(item.get("remplacement_requis") for item in damages) else "repair",
            "financial": part_financial.model_dump(),
            "detection_ids": [item.get("detection_id") for item in damages],
            "source_images": sorted({image for item in damages for image in item.get("images_sources", [])}),
        })
    payload = {
        "schema_version": 1, "dossier_id": request.dossier_id,
        "vehicle": Vehicle.model_validate(request.metadata).model_dump(),
        "financial": financial.model_dump(), "parts": parts, "detections": instances,
        "photos": [photo.model_dump() for photo in request.photos],
    }
    plain_json = json.dumps(payload, default=json_scalar, allow_nan=False)
    return StoredAnalysis.model_validate_json(plain_json)
