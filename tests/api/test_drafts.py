"""Verify durable empty drafts, idempotent creation, and Unicode metadata writes.

Depends on isolated API storage and a catalog fixture; legacy metadata is read
unchanged while all new registration writes use the Moroccan plate validator.
"""

from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.main import create_app


@pytest.mark.parametrize("category", ["chiffrage_rapide", "verification"])
def test_empty_draft_survives_restart_and_category_filters(client, storage, category):
    response = client.post("/api/dossiers", json={"type": category})
    assert response.status_code == 201
    draft = response.json()
    dossier_id = draft["dossier_id"]
    assert draft["workflow_status"] == "draft"
    assert draft["status"] == "not_analyzed"
    assert draft["vehicle"]["marque_id"] is None
    assert draft["vehicle"]["matricule"] == ""
    assert draft["photos"] == []
    metadata = json.loads((storage / dossier_id / "metadata.json").read_text())
    assert metadata["workflow_status"] == "draft"
    assert metadata["created_at"]
    with TestClient(create_app(ApiSettings(storage))) as restarted:
        assert restarted.get(f"/api/dossiers/{dossier_id}").json() == draft
        listing = restarted.get("/api/dossiers", params={"type": category, "workflow_status": "draft"}).json()
        assert listing["total"] == 1


def test_idempotency_survives_restart_and_metadata_edits(client, storage):
    headers = {"Idempotency-Key": "operator-create-1"}
    payload = {"type": "chiffrage_rapide"}
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: client.post("/api/dossiers", json=payload, headers=headers), range(2)))
    assert [result.status_code for result in results] == [201, 201]
    dossier_id = results[0].json()["dossier_id"]
    assert results[1].json()["dossier_id"] == dossier_id
    client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": "84654-ب-454"})
    with TestClient(create_app(ApiSettings(storage))) as restarted:
        assert restarted.post("/api/dossiers", json=payload, headers=headers).json()["dossier_id"] == dossier_id
        assert restarted.post("/api/dossiers", json={"type": "verification"}, headers=headers).status_code == 409
    assert len([path for path in storage.iterdir() if not path.name.startswith(".")]) == 1


@pytest.mark.parametrize("plate,normalized", [("14554-B-4578", "14554-B-4578"),
                                              ("14554-b-4578", "14554-B-4578"),
                                              ("84654-ب-454", "84654-ب-454")])
def test_plate_preserved_in_storage_search_and_responses(client, storage, plate, normalized):
    draft = client.post("/api/dossiers", json={"type": "chiffrage_rapide", "matricule": plate}).json()
    dossier_id = draft["dossier_id"]
    assert draft["vehicle"]["matricule"] == normalized
    response = client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": plate})
    assert response.status_code == 200
    assert response.json()["vehicle"]["matricule"] == normalized
    assert json.loads((storage / dossier_id / "metadata.json").read_text())["matricule"] == normalized
    assert client.get("/api/dossiers", params={"search": normalized}).json()["total"] == 1


@pytest.mark.parametrize("plate", ["BAD", "123-B", "123-AB-456", "123--456", "123/ب/456", "123-ـ-456", "123-B-456/../x"])
def test_invalid_plate_does_not_change_draft(client, storage, plate):
    dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
    path = storage / dossier_id / "metadata.json"
    before = path.read_bytes()
    assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": plate}).status_code == 422
    assert path.read_bytes() == before
    assert client.post("/api/dossiers", json={"type": "verification", "matricule": plate}).status_code == 422


def test_partial_updates_preserve_unknown_fields_and_legacy_plates(client, storage, workflow_catalog):
    dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    path = storage / dossier_id / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(matricule="LEGACY", expert="preserve-me")
    path.write_text(json.dumps(metadata))
    for changes in ({"date_sinistre": "2026-01-01"}, {"annee": "2019"},
                    {"marque_id": 12, "model_id": 34}, {"date_mise_en_circulation": "2020-05-17"}):
        assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json=changes).status_code == 200
    saved = json.loads(path.read_text())
    assert saved["expert"] == "preserve-me"
    assert saved["matricule"] == "LEGACY"
    assert saved["annee"] == "2020"
    assert saved["marque"] == "Dacia"
    assert saved["date_sinistre"] == "2026-01-01"
