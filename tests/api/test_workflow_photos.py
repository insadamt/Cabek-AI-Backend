"""Exercise before/after slot completeness, immutable evidence, and photo security.

Depends on temporary storage and generated images; no browser automation or real
vehicle photographs are used. Assertions cover externally visible behavior.
"""

import io
import json

from PIL import Image
import pytest

from api.photo_service import MAX_UPLOAD_BYTES
from api.upload_limits import MAX_MULTIPART_BYTES


SIDES = ("front", "rear", "left", "right")


# ============================================================
# VERIFICATION COLLECTION, PAIRS, AND FINALIZATION
# ============================================================

def test_four_before_and_after_sides_without_analysis(client, storage, new_dossier, upload_photo):
    dossier_id = new_dossier("verification")["dossier_id"]
    directory = storage / dossier_id
    assert upload_photo(dossier_id, phase="after").json()["error"]["code"] == "before_photos_incomplete"
    for index, side in enumerate(SIDES):
        response = upload_photo(dossier_id, side, "before")
        assert response.status_code == 201, response.text
        assert response.json()["workflow_status"] == ("awaiting_after" if index == 3 else "before_incomplete")
        assert len(response.json()["comparison"]["missing_before"]) == 3 - index
    before_ids = {photo["side"]: photo["photo_id"] for photo in response.json()["photos"]}
    for index, side in enumerate(SIDES):
        response = upload_photo(dossier_id, side, "after")
        assert response.status_code == 201, response.text
        assert response.json()["workflow_status"] == ("comparison_ready" if index == 3 else "after_incomplete")
        assert response.json()["status"] == "not_analyzed"
        assert response.json()["result"] is None
        assert response.json()["progress"] is None
    result = response.json()
    assert result["comparison"]["missing_before"] == result["comparison"]["missing_after"] == []
    for slot in result["comparison"]["slots"]:
        assert slot["before"]["photo_id"] == before_ids[slot["side"]]
        assert slot["after"]["side"] == slot["side"]
        assert client.get(slot["before"]["original_url"]).status_code == 200
        assert client.get(slot["after"]["preview_url"]).status_code == 200
    filtered = client.get(f"/api/dossiers/{dossier_id}/photos", params={"phase": "before"}).json()
    assert len(filtered["photos"]) == 4
    assert len(filtered["comparison"]["slots"]) == 4
    assert client.post(f"/api/dossiers/{dossier_id}/scan").json()["error"]["code"] == "scan_disabled_for_type"
    assert not (directory / "analysis/status.json").exists()
    assert not (directory / "analysis/result.json").exists()
    assert not (directory / "analysis/report.pdf").exists()
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["before_finalized_at"] and metadata["evidence_finalized_at"]
    for photo in metadata["photos"]:
        assert photo["path"].startswith(f"photos/{photo['phase']}/")
    photo = result["photos"][-1]
    assert upload_photo(dossier_id, photo["side"], photo["phase"], photo_id=photo["photo_id"]).status_code == 409
    assert client.patch(f"/api/dossiers/{dossier_id}", json={"matricule": "14554-C-4578"}).status_code == 409
    assert client.put(f"/api/dossiers/{dossier_id}/vehicle", json={"marque_id": 12, "model_id": 34}).status_code == 409


def test_explicit_replacement_retains_old_bytes_and_history(client, storage, new_dossier, upload_photo):
    dossier_id = new_dossier("verification")["dossier_id"]
    first = upload_photo(dossier_id, phase="before").json()["photos"][0]
    directory = storage / dossier_id
    metadata_path = directory / "metadata.json"
    before = metadata_path.read_bytes()
    assert upload_photo(dossier_id, phase="before").json()["error"]["code"] == "photo_slot_occupied"
    assert metadata_path.read_bytes() == before
    old_metadata = json.loads(before)
    old_path = directory / old_metadata["photos"][0]["path"]
    old_bytes = old_path.read_bytes()
    replacement = upload_photo(dossier_id, phase="before", photo_id=first["photo_id"])
    assert replacement.status_code == 200
    assert replacement.json()["photos"][0]["photo_id"] != first["photo_id"]
    saved = json.loads(metadata_path.read_text())
    assert len(saved["photos"]) == len(saved["photo_history"]) == 1
    assert saved["photo_history"][0]["replaced_by"] == saved["photos"][0]["photo_id"]
    assert old_path.read_bytes() == old_bytes
    assert client.get(first["original_url"]).status_code == 404
    assert upload_photo(dossier_id, "left", "before", photo_id=saved["photos"][0]["photo_id"]).status_code == 422


def test_before_evidence_locks_when_after_collection_starts(new_dossier, upload_photo):
    dossier_id = new_dossier("verification")["dossier_id"]
    for side in SIDES:
        response = upload_photo(dossier_id, side, "before")
    first = response.json()["photos"][0]
    assert upload_photo(dossier_id, phase="after").status_code == 201
    denied = upload_photo(dossier_id, phase="before", photo_id=first["photo_id"])
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "evidence_finalized"


def test_scan_disabled_before_any_files_or_worker_checks(client, storage, new_dossier):
    dossier_id = new_dossier("verification")["dossier_id"]
    for _ in range(2):
        response = client.post(f"/api/dossiers/{dossier_id}/scan")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "scan_disabled_for_type"
    assert not (storage / dossier_id / "analysis").exists()
    assert client.get(f"/api/dossiers/{dossier_id}/report").status_code == 404


def test_quick_multiple_photos_same_side_and_phase_rules(new_dossier, upload_photo):
    dossier_id = new_dossier()["dossier_id"]
    for _ in range(2):
        assert upload_photo(dossier_id, "right").status_code == 201
    response = upload_photo(dossier_id, "left")
    assert len(response.json()["photos"]) == 3
    assert response.json()["workflow_status"] == "ready_to_scan"
    assert upload_photo(dossier_id, phase="before").status_code == 422
    verification_id = new_dossier("verification")["dossier_id"]
    assert upload_photo(verification_id).status_code == 422


# ============================================================
# CONTENT VALIDATION AND CONFINED URLS
# ============================================================

@pytest.mark.parametrize("filename,mime,content", [
    ("vehicle.svg", "image/svg+xml", b"<svg/>"), ("vehicle.png", "image/png", b"not an image"),
    ("vehicle.jpg", "image/jpeg", None), ("vehicle.png", "text/html", None),
    ("../../vehicle.png", "image/png", None), ("vehicle:evil.png", "image/png", None),
    ("vehicle.exe", "image/png", None),
])
def test_invalid_upload_does_not_change_manifest(storage, new_dossier, upload_photo, filename, mime, content):
    dossier_id = new_dossier()["dossier_id"]
    directory = storage / dossier_id
    before = (directory / "metadata.json").read_bytes()
    response = upload_photo(dossier_id, filename=filename, content_type=mime, content=content)
    assert response.status_code == 422
    assert (directory / "metadata.json").read_bytes() == before
    assert list((directory / "photos").iterdir()) == []


@pytest.mark.parametrize("size", [MAX_UPLOAD_BYTES + 1, MAX_MULTIPART_BYTES + 1])
def test_image_and_multipart_limits(storage, new_dossier, upload_photo, size):
    dossier_id = new_dossier()["dossier_id"]
    response = upload_photo(dossier_id, content=b"x" * size)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert list((storage / dossier_id / "photos").iterdir()) == []


def test_jpeg_and_allowlisted_urls(client, new_dossier, upload_photo):
    dossier_id = new_dossier()["dossier_id"]
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10)).save(buffer, "JPEG")
    response = upload_photo(dossier_id, filename="vehicle.JPEG", content_type="image/jpeg", content=buffer.getvalue())
    assert response.status_code == 201
    photo = response.json()["photos"][0]
    assert client.get(photo["original_url"]).headers["content-type"] == "image/jpeg"
    assert client.get(photo["preview_url"]).headers["content-type"] == "image/webp"
    assert client.get(f"/api/dossiers/{dossier_id}/media/metadata.json").status_code == 404
    assert client.get(f"/api/dossiers/{dossier_id}/media/photos/%2e%2e/metadata.json").status_code == 404
    assert client.get(f"/api/dossiers/{dossier_id}/media/photos/unlisted.jpg").status_code == 404


def test_windows_upload_name_never_controls_path(storage, new_dossier, upload_photo):
    dossier_id = new_dossier()["dossier_id"]
    response = upload_photo(dossier_id, filename="C:\\vehicle.png")

    # The multipart parser normalizes legacy Windows paths before our filename checks.

    assert response.status_code == 201
    saved = json.loads((storage / dossier_id / "metadata.json").read_text())
    assert "vehicle" not in saved["photos"][0]["path"]
    assert saved["photos"][0]["path"].startswith("photos/")


def test_workflow_urls_include_proxy_prefix(storage, new_dossier, upload_photo):
    from fastapi.testclient import TestClient
    from api.config import ApiSettings
    from api.main import create_app

    dossier_id = new_dossier("verification")["dossier_id"]
    upload_photo(dossier_id, phase="before")
    with TestClient(create_app(ApiSettings(storage)), root_path="/gateway") as client:
        result = client.get(f"/api/dossiers/{dossier_id}").json()
        assert result["photos"][0]["original_url"].startswith(f"/gateway/api/dossiers/{dossier_id}/media/photos/before/")
        assert result["comparison"]["slots"][0]["before"]["preview_url"].startswith("/gateway/")


def test_corrupt_or_missing_photo_is_reported_as_missing(client, storage, new_dossier, upload_photo):
    dossier_id = new_dossier("verification")["dossier_id"]
    for side in SIDES:
        upload_photo(dossier_id, side, "before")
    metadata = json.loads((storage / dossier_id / "metadata.json").read_text())
    (storage / dossier_id / metadata["photos"][0]["path"]).write_bytes(b"broken")
    response = client.get(f"/api/dossiers/{dossier_id}").json()
    assert response["workflow_status"] == "before_incomplete"
    assert response["comparison"]["missing_before"] == ["front"]
    assert upload_photo(dossier_id, phase="after").status_code == 409
