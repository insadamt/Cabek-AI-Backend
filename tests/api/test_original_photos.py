from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.main import create_app
from conftest import write_json


@pytest.mark.parametrize("legacy_paths", [False, True])
def test_originals_are_available_without_analysis(client, create_dossier, legacy_paths):
    directory = create_dossier(legacy_paths=legacy_paths)
    before = (directory / "metadata.json").read_bytes()
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    assert response.json()["photos"] == [{
        "photo_id": "avant_01",
        "original_url": "/api/dossiers/DOS-001/media/photos/avant_01.jpg",
        "preview_url": "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp",
    }]
    assert client.get(response.json()["photos"][0]["original_url"]).status_code == 200
    assert client.get("/api/dossiers/DOS-001/result").json()["result"] is None
    assert (directory / "metadata.json").read_bytes() == before
    assert not (directory / "analysis").exists()


def test_originals_are_included_with_completed_analysis(client, complete_dossier):
    complete_dossier()
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    assert response.json()["result"]["photos"][0]["parts_url"]
    assert set(response.json()["photos"][0]) == {"photo_id", "original_url", "preview_url"}
    assert client.get(response.json()["photos"][0]["original_url"]).status_code == 200


def test_original_listing_omits_missing_photos(client, create_dossier):
    directory = create_dossier()
    (directory / "photos/avant_01.jpg").unlink()
    assert client.get("/api/dossiers/DOS-001/result").json()["photos"] == []
    assert client.get("/api/dossiers/MISSING/result").status_code == 404


def test_original_urls_include_reverse_proxy_prefix(storage, create_dossier):
    create_dossier()
    with TestClient(create_app(ApiSettings(storage)), root_path="/service") as client:
        response = client.get("/api/dossiers/DOS-001/result")
        assert response.json()["photos"][0]["original_url"] == "/service/api/dossiers/DOS-001/media/photos/avant_01.jpg"


@pytest.mark.parametrize("state", ["queued", "processing", "failed"])
def test_originals_remain_available_during_and_after_failed_scans(client, create_dossier, state):
    directory = create_dossier()
    write_json(directory / "analysis/status.json", {"status": state})
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == state
    assert data["result"] is None
    assert len(data["photos"]) == 1
    assert client.get(data["photos"][0]["original_url"]).status_code == 200
