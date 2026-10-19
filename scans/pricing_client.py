import os
from math import isfinite

import requests


# ============================================================
# AUTOESTIMATE CLIENT WITHOUT STREAMLIT
# ============================================================

class AutoEstimateError(Exception):
    pass


def get_estimation(
    marque_id: int, model_id: int, part_id: int, type_piece: str,
    year: int | None = None,
) -> dict | None:
    base_url = os.environ.get("AUTOESTIMATE_BASE_URL", "").rstrip("/")
    token = os.environ.get("AUTOESTIMATE_TOKEN", "")
    if not base_url or not token:
        raise AutoEstimateError("AutoEstimate URL or token is not configured.")
    params = {"marque_id": marque_id, "model_id": model_id, "part_id": part_id, "type": type_piece}
    if year is not None:
        params["year"] = year
    try:
        response = requests.get(
            f"{base_url}/estimation", params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()["data"]
        estimation = payload["estimation"] if payload else None
        if estimation is not None and not isinstance(estimation, dict):
            raise ValueError("Invalid estimation payload.")
        if estimation is not None and estimation.get("median") is not None:
            median = float(estimation["median"])
            if not isfinite(median) or median < 0:
                raise ValueError("Invalid median price.")
            estimation = {**estimation, "median": median}
        return estimation
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:

        # Results must not contain private service URLs, response bodies, or credentials.

        raise AutoEstimateError("AutoEstimate pricing is unavailable.") from error
