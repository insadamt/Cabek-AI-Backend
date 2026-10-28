"""Map saved vehicle registration to AutoEstimate's existing year query parameter.

Depends only on ISO date parsing. Full dates stay in metadata and result snapshots;
historical dossiers continue using annee when a complete date is absent.
"""

from datetime import date


def pricing_year(metadata: dict) -> int | None:
    registration = metadata.get("date_mise_en_circulation")
    if registration:
        return date.fromisoformat(registration).year
    historical_year = str(metadata.get("annee") or "")
    return int(historical_year) if historical_year.isdigit() else None
