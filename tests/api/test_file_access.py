import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api.config import ApiSettings
from api.main import create_app
from conftest import write_json


@pytest.mark.parametrize("path", [
    "metadata.json", "feedback.json", "analysis/result.json", "analysis/status.json",
    "analysis/report.pdf", "photos/undeclared.jpg", "photos/avant_01.jpg.tmp",
    "photos/%2e%2e/metadata.json", "%2fetc/passwd", "photos%5cavant_01.jpg",
    "photos/%252e%252e/metadata.json",
])
def test_only_declared_image_paths_are_served(client, create_dossier, path):
    directory = create_dossier()
    Image.new("RGB", (4, 4)).save(directory / "photos/undeclared.jpg")
    response = client.get(f"/api/dossiers/DOS-001/media/{path}")
    assert response.status_code == 404
    assert "error" in response.json()


def test_original_symlink_cannot_leave_dossier(client, create_dossier, storage):
    directory = create_dossier()
    outside = storage.parent / "private.jpg"
    Image.new("RGB", (4, 4)).save(outside)
    original = directory / "photos/avant_01.jpg"
    original.unlink()
    original.symlink_to(outside)
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422
    assert client.get("/api/dossiers/DOS-001/media/photos/avant_01.jpg").status_code == 422


def test_dossier_symlink_cannot_leave_storage(client, storage):
    outside = storage.parent / "outside"
    outside.mkdir()
    write_json(outside / "metadata.json", {"dossier_id": "DOS-001", "photos": []})
    (storage / "DOS-001").symlink_to(outside, target_is_directory=True)
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 404
    assert str(outside) not in response.text
    assert client.get("/api/dossiers").json()["items"] == []


@pytest.mark.parametrize("artifact", ["result.json", "report.pdf", "annotated/avant_01_pieces.png"])
def test_saved_artifact_symlinks_cannot_leave_dossier(client, complete_dossier, storage, artifact):
    directory, _ = complete_dossier()
    original = directory / "analysis" / artifact
    outside = storage.parent / original.name
    outside.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(outside)
    assert client.get("/api/dossiers/DOS-001/result").status_code in {404, 422}
    assert client.get("/api/dossiers/DOS-001/report").status_code in {404, 422}


@pytest.mark.parametrize("path", ["../private.jpg", "/etc/passwd", "photos/../private.jpg", "C:\\private.jpg"])
def test_unsafe_metadata_paths_are_rejected(client, create_dossier, path):
    directory = create_dossier()
    write_json(directory / "metadata.json", {
        "dossier_id": "DOS-001", "photos": [{"path": path}],
    })
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_duplicate_photo_ids_are_rejected(client, create_dossier):
    directory = create_dossier()
    write_json(directory / "metadata.json", {
        "dossier_id": "DOS-001",
        "photos": [{"path": "photos/avant_01.jpg"}, {"path": "photos/avant_01.png"}],
    })
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_invalid_image_is_not_served(client, create_dossier):
    directory = create_dossier()
    (directory / "photos/avant_01.jpg").write_bytes(b"not an image")
    response = client.get("/api/dossiers/DOS-001/media/photos/avant_01.jpg")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_image"


def test_invalid_pdf_prevents_completed_output(client, complete_dossier):
    directory, _ = complete_dossier()
    (directory / "analysis/report.pdf").write_bytes(b"%PDF-broken")
    assert client.get("/api/dossiers/DOS-001/report").status_code == 422
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999"])
def test_nonfinite_engine_payload_is_rejected(client, complete_dossier, value):
    directory, result = complete_dossier()
    result["parts"] = [{"confidence": "REPLACE"}]
    payload = json.dumps(result).replace('"REPLACE"', value)
    (directory / "analysis/result.json").write_text(payload)
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_import_and_read_requests_do_not_mutate_storage(client, create_dossier, storage):
    create_dossier()
    before = {p.relative_to(storage): p.read_bytes() for p in storage.rglob("*") if p.is_file()}
    client.get("/api/dossiers")
    client.get("/api/dossiers/DOS-001/result")
    client.get("/api/dossiers/DOS-001/report")
    client.get("/api/dossiers/DOS-001/media/photos/avant_01.jpg")
    after = {p.relative_to(storage): p.read_bytes() for p in storage.rglob("*") if p.is_file()}
    assert after == before
    assert not (storage / "DOS-001/analysis").exists()


def test_proxy_root_path_is_included_in_generated_urls(complete_dossier, storage):
    complete_dossier()
    with TestClient(create_app(ApiSettings(storage)), root_path="/cabek") as client:
        response = client.get("/api/dossiers/DOS-001/result")
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["report_url"] == "/cabek/api/dossiers/DOS-001/report"
        assert result["photos"][0]["parts_url"].startswith("/cabek/api/dossiers/")
