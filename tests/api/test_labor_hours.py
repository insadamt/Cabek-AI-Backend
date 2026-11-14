"""Verify working-hour aggregation and compatibility of saved dossier API results.

Depends on pure labor aggregation and isolated API fixtures; no models, network,
repository data, or browser automation are used.
"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from api.labor import build_labor_hours
from api.schemas import LaborHours
from conftest import write_json


# ============================================================
# AGGREGATION AND UNKNOWN DURATIONS
# ============================================================

def repair(detection_id="repair", **overrides):
    return {
        "detection_id": detection_id, "piece_ai_brut": "p_aile",
        "cout_detail": {"heures_reparation": 2.5, "heures_mop": 1.25},
        **overrides,
    }


def replacement(**overrides):
    return {
        "detection_id": "replace", "piece_ai_brut": "p_capot",
        "remplacement_requis": True, "heures_mot": 3,
        "cout_detail": {"heures_reparation": 99, "heures_mop": 99},
        **overrides,
    }


def test_hours_sum_only_billable_categories_without_mutating_evidence():
    instances = [repair(), repair("second"), replacement(),
                 replacement(chiffrage_bloque=True),
                 repair(validation_expert=" Rejetee ")]
    original = deepcopy(instances)
    assert build_labor_hours(instances).model_dump() == {
        "repair_labor": 5, "paint_labor": 2.5, "replacement_labor": 3,
    }
    assert instances == original


@pytest.mark.parametrize("unknown", [None, -1, float("nan"), float("inf"), "invalid", True, {}])
def test_partial_or_invalid_hours_never_look_like_a_complete_duration(unknown):
    result = build_labor_hours([
        repair(), repair("unknown", cout_detail={"heures_reparation": unknown, "heures_mop": 0}),
    ])
    assert result.repair_labor is None
    assert result.paint_labor == 1.25
    assert result.replacement_labor == 0


def test_empty_billable_set_is_zero_and_absent_durations_are_unknown():
    assert set(build_labor_hours([]).model_dump().values()) == {0}
    assert build_labor_hours([repair(cout_detail={})]).repair_labor is None
    assert build_labor_hours([replacement(heures_mot=None)]).replacement_labor is None
    assert build_labor_hours([{
        "remplacement_requis": True, "cout_detail": {"heures_mot": "2.5"},
    }]).replacement_labor == 2.5


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_api_hours_contract_rejects_invalid_durations(value):
    with pytest.raises(ValidationError):
        LaborHours(repair_labor=value)


# ============================================================
# EXISTING RESULTS AND PART ASSOCIATION
# ============================================================

def test_saved_analysis_returns_global_and_part_hours_without_rewriting(client, complete_dossier):
    directory, stored = complete_dossier()
    stored["detections"] = [repair(), replacement()]
    stored["parts"] = [
        {"part_id": "p_aile", "detection_ids": ["repair"], "financial": deepcopy(stored["financial"])},
        {"part_id": "p_capot", "financial": deepcopy(stored["financial"])},
        {"part_id": "unknown", "detection_ids": ["missing"], "financial": deepcopy(stored["financial"])},
    ]
    path = directory / "analysis/result.json"
    write_json(path, stored)
    original = path.read_bytes()
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["financial"]["labor_hours"] == {
        "repair_labor": 2.5, "paint_labor": 1.25, "replacement_labor": 3,
    }
    assert result["parts"][0]["financial"]["labor_hours"]["repair_labor"] == 2.5
    assert result["parts"][0]["financial"]["labor_hours"]["replacement_labor"] == 0
    assert result["parts"][1]["financial"]["labor_hours"]["replacement_labor"] == 3
    assert set(result["parts"][2]["financial"]["labor_hours"].values()) == {None}
    assert result["financial"]["estimated_total"] == stored["financial"]["estimated_total"]
    assert path.read_bytes() == original


def test_legacy_result_without_evidence_does_not_invent_hours(client, complete_dossier):
    complete_dossier()
    financial = client.get("/api/dossiers/DOS-001/result").json()["result"]["financial"]
    assert set(financial["labor_hours"].values()) == {None}


def test_explicit_saved_hours_remain_authoritative(client, complete_dossier):
    directory, stored = complete_dossier()
    stored["detections"] = [repair()]
    hours = {"repair_labor": 7, "paint_labor": None, "replacement_labor": 0}
    stored["financial"]["labor_hours"] = hours
    stored["parts"] = [{"detection_ids": ["repair"], "financial": deepcopy(stored["financial"])}]
    write_json(directory / "analysis/result.json", stored)
    result = client.get("/api/dossiers/DOS-001/result").json()["result"]
    assert result["financial"]["labor_hours"] == hours
    assert result["parts"][0]["financial"]["labor_hours"] == hours
