from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.main import create_app


@pytest.mark.parametrize("legacy_paths", [False, True])
def test_originals_are_available_without_analysis(client, create_dossier, legacy_paths):
    directory = create_dossier(legacy_paths=legacy_paths)
    before = (directory / "metadata.json").read_bytes()
    response = client.get("/api/dossiers/DOS-001/photos")
    assert response.status_code == 200
    assert response.json() == [{
        "photo_id": "avant_01",
        "original_url": "/api/dossiers/DOS-001/media/photos/avant_01.jpg",
    }]
    assert client.get(response.json()[0]["original_url"]).status_code == 200
    assert client.get("/api/dossiers/DOS-001/result").json()["result"] is None
    assert (directory / "metadata.json").read_bytes() == before
    assert not (directory / "analysis").exists()


def test_originals_do_not_require_valid_annotations(client, complete_dossier):
    directory, _ = complete_dossier()
    (directory / "analysis/annotated/avant_01_pieces.png").unlink()
    response = client.get("/api/dossiers/DOS-001/photos")
    assert response.status_code == 200
    assert set(response.json()[0]) == {"photo_id", "original_url"}
    assert client.get(response.json()[0]["original_url"]).status_code == 200


def test_original_listing_omits_missing_photos(client, create_dossier):
    directory = create_dossier()
    (directory / "photos/avant_01.jpg").unlink()
    assert client.get("/api/dossiers/DOS-001/photos").json() == []
    assert client.get("/api/dossiers/MISSING/photos").status_code == 404


def test_original_urls_include_reverse_proxy_prefix(storage, create_dossier):
    create_dossier()
    with TestClient(create_app(ApiSettings(storage)), root_path="/service") as client:
        response = client.get("/api/dossiers/DOS-001/photos")
        assert response.json()[0]["original_url"] == "/service/api/dossiers/DOS-001/media/photos/avant_01.jpg"
