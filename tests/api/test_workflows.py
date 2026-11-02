"""Cover dossier identity, metadata validation, ownership, and one-side scan reuse.

Depends on temporary API fixtures and the existing subprocess scan fixture; real
model inference and AutoEstimate network requests are deliberately substituted.
"""

import json

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.main import create_app
from scans.vehicle import pricing_year
from test_scans import FixtureBackend, wait_for_terminal


# ============================================================
# CREATION AND VEHICLE IDENTITY
# ============================================================

@pytest.mark.parametrize("category,state", [("chiffrage_rapide", "awaiting_photos"),
                                           ("verification", "before_incomplete")])
def test_create_saved_canonical_metadata(client, storage, new_dossier, category, state):
    created = new_dossier(category)
    assert created["type"] == category
    assert created["source"] == "cabek_ai"
    assert created["workflow_status"] == state
    assert created["status"] == "not_analyzed"
    assert created["photos"] == []
    assert created["vehicle"]["marque"] == "Dacia"
    assert created["vehicle"]["modele"] == "Sandero"
    assert created["vehicle"]["date_mise_en_circulation"] == "2020-05-17"
    directory = storage / created["dossier_id"]
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["annee"] == "2020"
    assert metadata["created_at"] == metadata["updated_at"]
    assert not (directory / "analysis").exists()
    assert client.get(f"/api/dossiers/{created['dossier_id']}").json() == created
    assert new_dossier(category)["dossier_id"] != created["dossier_id"]
    if category == "verification":
        assert created["comparison"]["missing_before"] == ["front", "rear", "left", "right"]


@pytest.mark.parametrize("changes", [
    {"type": "forfait_expertise"}, {"type": "unknown"}, {"marque_id": None},
    {"marque_id": "12"}, {"marque_id": True}, {"model_id": 0}, {"matricule": "  "},
    {"date_mise_en_circulation": "2020"}, {"date_mise_en_circulation": "2020-02-30"},
    {"date_mise_en_circulation": "20200517"}, {"date_mise_en_circulation": "2020-05-17T00:00:00"},
    {"date_mise_en_circulation": None}, {"marque": "Forged"}, {"source": "external"},
])
def test_invalid_creation_leaves_storage_empty(client, storage, workflow_catalog, changes):
    response = client.post("/api/dossiers", json={
        "type": "chiffrage_rapide", "marque_id": 12, "model_id": 34,
        "matricule": "14554-B-4578", "date_mise_en_circulation": "2020-05-17", **changes,
    })
    assert response.status_code == 422
    assert list(storage.iterdir()) == []


@pytest.mark.parametrize("missing", ["type", "marque_id", "model_id"])
def test_creation_requires_category_and_paired_ids(client, workflow_catalog, missing):
    payload = {"type": "verification", "marque_id": 12, "model_id": 34,
               "matricule": "14554-B-4578", "date_mise_en_circulation": "2020-05-17"}
    payload.pop(missing)
    assert client.post("/api/dossiers", json=payload).status_code == 422


@pytest.mark.parametrize("ids", [{"marque_id": 99}, {"model_id": 99}])
def test_creation_rejects_unknown_or_unrelated_vehicle(client, storage, workflow_catalog, ids):
    response = client.post("/api/dossiers", json={
        "type": "verification", "marque_id": 12, "model_id": 34,
        "matricule": "14554-B-4578", "date_mise_en_circulation": "2020-05-17", **ids,
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_vehicle_selection"
    assert list(storage.iterdir()) == []


def test_unavailable_catalog_leaves_no_dossier(client, storage, monkeypatch):
    monkeypatch.delenv("AUTOESTIMATE_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOESTIMATE_TOKEN", raising=False)
    response = client.post("/api/dossiers", json={
        "type": "verification", "marque_id": 12, "model_id": 34,
        "matricule": "14554-B-4578", "date_mise_en_circulation": "2020-05-17",
    })
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "autoestimate_unavailable"
    assert list(storage.iterdir()) == []


def test_network_failure_during_creation_is_safe(client, storage, monkeypatch):
    import requests

    monkeypatch.setenv("AUTOESTIMATE_BASE_URL", "http://pricing.test/api/v1")
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "private-token")

    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("private-token")

    monkeypatch.setattr("api.vehicle_catalog.requests.get", unavailable)
    response = client.post("/api/dossiers", json={
        "type": "verification", "marque_id": 12, "model_id": 34,
        "matricule": "14554-B-4578", "date_mise_en_circulation": "2020-05-17",
    })
    assert response.status_code == 503
    assert "private-token" not in response.text
    assert list(storage.iterdir()) == []


# ============================================================
# EDITS, LEGACY OWNERSHIP, AND EXISTING QUEUE
# ============================================================

def test_patch_preserves_inputs_and_resolves_canonical_names(client, storage, new_dossier, upload_photo):
    created = new_dossier()
    dossier_id = created["dossier_id"]
    upload_photo(dossier_id)
    path = storage / dossier_id / "metadata.json"
    before = json.loads(path.read_text())
    response = client.patch(f"/api/dossiers/{dossier_id}", json={
        "marque_id": 12, "model_id": 34, "date_mise_en_circulation": "2021-06-01", "matricule": "14554-C-4578",
    })
    assert response.status_code == 200
    assert response.json()["workflow_status"] == "ready_to_scan"
    saved = json.loads(path.read_text())
    assert saved["photos"] == before["photos"]
    assert saved["created_at"] == before["created_at"]
    assert saved["updated_at"] != before["updated_at"]
    assert saved["annee"] == "2021"


@pytest.mark.parametrize("payload", [{}, {"matricule": None}, {"type": "verification"},
                                    {"workflow_status": "estimated"}, {"marque_id": 12},
                                    {"marque_id": 12, "model_id": 99}])
def test_patch_validation_is_atomic(client, storage, new_dossier, payload):
    dossier_id = new_dossier()["dossier_id"]
    path = storage / dossier_id / "metadata.json"
    before = path.read_bytes()
    assert client.patch(f"/api/dossiers/{dossier_id}", json=payload).status_code == 422
    assert path.read_bytes() == before


def test_legacy_expertise_reads_without_migration_and_rejects_new_mutations(client, create_dossier, upload_photo):
    path = create_dossier() / "metadata.json"
    before = path.read_bytes()
    detail = client.get("/api/dossiers/DOS-001").json()
    assert detail["type"] == "forfait_expertise"
    assert detail["workflow_status"] == detail["status"] == "not_analyzed"
    assert client.patch("/api/dossiers/DOS-001", json={"matricule": "14554-C-4578"}).status_code == 409
    assert upload_photo("DOS-001").status_code == 409
    assert path.read_bytes() == before


def test_one_side_quick_scan_uses_existing_queue_and_protects_completed_inputs(
    storage, new_dossier, upload_photo, workflow_catalog,
):
    dossier_id = new_dossier()["dossier_id"]
    response = upload_photo(dossier_id, "left")
    assert response.json()["workflow_status"] == "ready_to_scan"
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        accepted = client.post(f"/api/dossiers/{dossier_id}/scan")
        assert accepted.status_code == 202
        assert accepted.json()["workflow_status"] == "analyzing"
        assert accepted.json()["type"] == "chiffrage_rapide"
        assert accepted.json()["progress"]["photos_total"] == 1
        edited = client.patch(f"/api/dossiers/{dossier_id}", json={"matricule": "14554-R-4578"})
        assert edited.status_code in (200, 409)
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 409
        terminal, _ = wait_for_terminal(client, dossier_id)
        assert terminal["status"] == "completed"
        assert terminal["workflow_status"] == "estimated"
        assert client.get(terminal["result"]["report_url"]).status_code == 200
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 409
        assert client.patch(f"/api/dossiers/{dossier_id}", json={"matricule": "14554-C-4578"}).status_code == 409
    assert upload_photo(dossier_id).status_code == 409


@pytest.mark.parametrize("metadata,expected", [
    ({"date_mise_en_circulation": "2020-12-31", "annee": "1999"}, 2020),
    ({"annee": "2015"}, 2015), ({"annee": None}, None), ({}, None),
])
def test_pricing_uses_supported_year_parameter(metadata, expected):
    assert pricing_year(metadata) == expected


def test_quick_detection_admission_does_not_depend_on_catalog(
    storage, new_dossier, upload_photo, monkeypatch,
):
    from api.errors import DossierError

    dossier_id = new_dossier()["dossier_id"]
    upload_photo(dossier_id)

    def unavailable(*args, **kwargs):
        raise DossierError(503, "autoestimate_unavailable", "Catalog unavailable.")

    monkeypatch.setattr("api.vehicle_catalog.VehicleCatalog.resolve", unavailable)
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 202
        assert wait_for_terminal(client, dossier_id)[0]["status"] == "completed"
