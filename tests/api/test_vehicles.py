"""Regress canonical AutoEstimate selections, offline reads, and scan validation.

Depends on mocked upstream reference responses and isolated dossier storage;
full registration dates extend the existing vehicle response contract.
"""

import json
from unittest.mock import Mock

from filelock import FileLock
import pytest
import requests

from api.vehicle_catalog import VehicleCatalog
from api.errors import DossierError
from conftest import write_json


# ============================================================
# REFERENCE DATA AND CANONICAL METADATA
# ============================================================

@pytest.mark.parametrize("state", ["not_analyzed", "queued", "processing", "failed", "completed"])
@pytest.mark.parametrize("names_present", [False, True])
def test_detail_exposes_metadata_vehicle_without_reference_lookup(
    client, create_dossier, complete_dossier, autoestimate, state, names_present,
):
    directory = complete_dossier()[0] if state == "completed" else create_dossier()
    path = directory / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(marque_id=12, model_id=34)
    if names_present:
        metadata.update(marque="Dacia", modele="Sandero")
    else:
        metadata.pop("marque")
        metadata.pop("modele")
    write_json(path, metadata)
    if state in {"queued", "processing", "failed"}:
        write_json(directory / "analysis/status.json", {"status": state})
    before = path.read_bytes()

    response = client.get("/api/dossiers/DOS-001/result")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == state
    assert data["vehicle"] == {
        "marque_id": 12, "model_id": 34,
        "marque": "Dacia" if names_present else "",
        "modele": "Sandero" if names_present else "",
        "matricule": "TEST-123", "annee": None, "date_sinistre": None,
        "date_mise_en_circulation": None,
    }
    assert data["vehicle"] == client.get("/api/dossiers").json()["items"][0]["vehicle"]

    # Completed analysis keeps the vehicle snapshot used for its calculation.

    if state == "completed":
        assert data["result"]["vehicle"]["modele"] == "Logan"
    else:
        assert data["result"] is None
    assert path.read_bytes() == before
    autoestimate.assert_not_called()

@pytest.fixture
def autoestimate(monkeypatch):
    monkeypatch.setenv("AUTOESTIMATE_BASE_URL", "http://pricing.test/api/v1")
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "test-private-token")

    def respond(url, **kwargs):
        response = Mock(status_code=200)
        data = ([{"id": 12, "libelle": "Dacia"}] if url.endswith("/marques") else
                [{"id": 34, "marque_id": 12, "libelle": "Sandero"}])
        response.json.return_value = {"data": data}
        return response

    transport = Mock(side_effect=respond)
    monkeypatch.setattr("api.vehicle_catalog.requests.get", transport)
    return transport


def test_reference_routes_supply_backend_credentials(client, autoestimate):
    assert client.get("/api/vehicles/marques").json() == [{"id": 12, "libelle": "Dacia"}]
    response = client.get("/api/vehicles/models", params={"marque_id": 12})
    assert response.json() == [{"id": 34, "marque_id": 12, "libelle": "Sandero"}]
    assert autoestimate.call_args.kwargs["params"] == {"marque_id": 12}
    assert autoestimate.call_args.kwargs["headers"]["Authorization"] == "Bearer test-private-token"
    assert "test-private-token" not in response.text


def test_save_selection_preserves_metadata_and_exposes_ids(client, create_dossier, autoestimate):
    directory = create_dossier()
    path = directory / "metadata.json"
    before = json.loads(path.read_text())
    response = client.put("/api/dossiers/DOS-001/vehicle", json={"marque_id": 12, "model_id": 34})
    assert response.status_code == 200
    assert response.json() == {"marque_id": 12, "model_id": 34, "marque": "Dacia", "modele": "Sandero"}
    saved = json.loads(path.read_text())
    for key in ("photos", "matricule", "status", "dossier_id"):
        assert saved[key] == before[key]
    vehicle = client.get("/api/dossiers").json()["items"][0]["vehicle"]
    assert vehicle["model_id"] == 34 and vehicle["modele"] == "Sandero"


@pytest.mark.parametrize("selection", [
    {"marque_id": 99, "model_id": 34}, {"marque_id": 12, "model_id": 99},
    {"marque_id": 0, "model_id": 34}, {"marque_id": True, "model_id": 34},
    {"marque_id": "12", "model_id": 34}, {"marque_id": 12},
    {"marque_id": 12, "model_id": 34, "marque": "Forged name"},
])
def test_invalid_selection_never_modifies_metadata(client, create_dossier, autoestimate, selection):
    path = create_dossier() / "metadata.json"
    before = path.read_bytes()
    assert client.put("/api/dossiers/DOS-001/vehicle", json=selection).status_code == 422
    assert path.read_bytes() == before


@pytest.mark.parametrize("lock_name", [".scan.lock", ".worker.lock"])
def test_vehicle_changes_rejected_while_scan_lock_held(client, create_dossier, autoestimate, lock_name):
    directory = create_dossier()
    (directory / "analysis").mkdir()
    with FileLock(str(directory / "analysis" / lock_name)):
        response = client.put("/api/dossiers/DOS-001/vehicle", json={"marque_id": 12, "model_id": 34})
    assert response.status_code == 409
    autoestimate.assert_not_called()


def test_completed_vehicle_cannot_change(client, complete_dossier, autoestimate):
    directory, _ = complete_dossier()
    before = (directory / "metadata.json").read_bytes()
    response = client.put("/api/dossiers/DOS-001/vehicle", json={"marque_id": 12, "model_id": 34})
    assert response.status_code == 409
    assert (directory / "metadata.json").read_bytes() == before
    autoestimate.assert_not_called()


def test_upstream_failure_is_safe_and_preserves_metadata(client, create_dossier, autoestimate):
    autoestimate.side_effect = requests.ConnectionError("private-token-and-url")
    path = create_dossier() / "metadata.json"
    before = path.read_bytes()
    response = client.put("/api/dossiers/DOS-001/vehicle", json={"marque_id": 12, "model_id": 34})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "autoestimate_unavailable"
    assert "private-token" not in response.text
    assert path.read_bytes() == before


def test_mismatched_upstream_models_are_rejected(autoestimate):
    response = Mock(status_code=200)
    response.json.return_value = {"data": [{"id": 34, "marque_id": 99, "libelle": "Wrong brand"}]}
    autoestimate.side_effect = None
    autoestimate.return_value = response
    with pytest.raises(DossierError) as caught:
        VehicleCatalog().models(12)
    assert caught.value.status_code == 503


def test_partial_vehicle_ids_are_invalid_but_legacy_names_still_work(client, create_dossier):
    path = create_dossier() / "metadata.json"
    assert client.get("/api/dossiers/DOS-001/result").status_code == 200
    metadata = json.loads(path.read_text())
    metadata["marque_id"] = 12
    path.write_text(json.dumps(metadata))
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_scan_snapshots_canonical_names_from_manually_supplied_ids(storage, create_dossier, autoestimate):
    from fastapi.testclient import TestClient
    from api.config import ApiSettings
    from api.main import create_app
    from test_scans import FixtureBackend, wait_for_terminal

    directory = create_dossier(marque="Outdated name")
    path = directory / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(marque_id=12, model_id=34)
    path.write_text(json.dumps(metadata))
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        response = client.post("/api/dossiers/DOS-001/scan")
        assert response.status_code == 202
        assert response.json()["vehicle"]["marque_id"] == 12
        assert response.json()["vehicle"]["model_id"] == 34
        assert response.json()["vehicle"]["modele"] == "Sandero"
        snapshot = json.loads((directory / "analysis/.request.json").read_text())
        assert snapshot["metadata"]["marque"] == "Dacia"
        assert snapshot["metadata"]["modele"] == "Sandero"
        assert snapshot["metadata"]["model_id"] == 34
        terminal, _ = wait_for_terminal(client)
        assert terminal["status"] == "completed"


def test_scan_rejects_invalid_ids_without_queueing(storage, create_dossier, autoestimate):
    from fastapi.testclient import TestClient
    from api.config import ApiSettings
    from api.main import create_app
    from test_scans import FixtureBackend

    directory = create_dossier()
    path = directory / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(marque_id=12, model_id=99)
    path.write_text(json.dumps(metadata))
    before = path.read_bytes()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        response = client.post("/api/dossiers/DOS-001/scan")
        assert response.status_code == 422
        assert not (directory / "analysis/.request.json").exists()
    assert path.read_bytes() == before
