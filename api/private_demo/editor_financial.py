"""Calculate manual part totals without camera, inference or pricing dependencies.

Depends on native financial DTOs; decimal arithmetic bills a part once and keeps
unknown costs distinct from explicit zero amounts.
"""
from decimal import Decimal, ROUND_HALF_UP
from api.schemas import FinancialResult
from api.private_demo.editor_schemas import CATEGORIES, CATEGORY_LABELS, LABOR_CATEGORIES


def rounded_sum(values):
    return float(sum((Decimal(str(value)) for value in values), Decimal(0)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def part_financial(part):
    amounts = {key: rounded_sum([value]) if value is not None else None for key, value in part.amounts.items()}
    warnings = [f'{part.name or "Pièce"} : {CATEGORY_LABELS[key]} non renseigné.' for key, value in amounts.items() if value is None]
    total = rounded_sum(value for value in amounts.values() if value is not None)
    return FinancialResult(
        status='incomplete' if warnings else 'complete', currency='MAD',
        estimated_total=None if warnings else total, known_subtotal=total,
        breakdown=amounts, labor_hours=part.hours, warnings=warnings,
    )


def aggregate_financial(parts):
    finances = [part_financial(part) for part in parts]
    warnings = [warning for item in finances for warning in item.warnings]
    amounts = {}
    for key in CATEGORIES:
        values = [getattr(item.breakdown, key) for item in finances]
        known = [value for value in values if value is not None]
        amounts[key] = rounded_sum(known) if known or not values else None
    hours = {}
    for key in LABOR_CATEGORIES:
        values = [part.hours[key] for part in parts]
        hours[key] = None if None in values else rounded_sum(values)
    total = rounded_sum(value for value in amounts.values() if value is not None)
    return FinancialResult(
        status='incomplete' if warnings else 'complete', currency='MAD',
        estimated_total=None if warnings else total, known_subtotal=total,
        breakdown=amounts, labor_hours=hours, warnings=warnings,
    )
