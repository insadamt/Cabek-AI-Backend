"""Regress atomic storage failures, cross-operation locks, and safe upstream errors.

Depends on temporary files, injected I/O failures, and existing scan fixtures;
each failure must preserve published metadata and previously stored evidence.
"""

import json
import asyncio

from filelock import FileLock
from fastapi.testclient import TestClient
import pytest
import requests

from api.config import ApiSettings
from api.main import create_app
from api.upload_limits import UploadLimitMiddleware
from test_scans import FixtureBackend, wait_for_terminal


# ============================================================
# ATOMIC PUBLICATION AND INPUT LOCKS
# ============================================================

def test_failed_create_leaves_no_partial_folder(client, storage, workflow_catalog, monkeypatch):
    def fail(*args):
        raise OSError("private disk error")

    monkeypatch.setattr("api.workflow_service.publish_json", fail)
    response = client.post("/api/dossiers", json={
        "type": "verification", "marque_id": 12, "model_id": 34,
        "matricule": "TEST", "date_mise_en_circulation": "2020-01-01",
    })
    assert response.status_code == 503
    assert list(storage.iterdir()) == []
    assert "private" not in response.text


def test_failed_metadata_commit_preserves_previous_photo_and_manifest(
    client, storage, new_dossier, upload_photo, monkeypatch,
):
    dossier_id = new_dossier()["dossier_id"]
    photo = upload_photo(dossier_id).json()["photos"][0]
    directory = storage / dossier_id
    path = directory / "metadata.json"
    before = path.read_bytes()
    originals = list((directory / "photos").iterdir())

    def fail(*args):
        raise OSError("private storage failure")

    monkeypatch.setattr("api.workflow_service.publish_json", fail)
    assert upload_photo(dossier_id, photo_id=photo["photo_id"]).status_code == 503
    assert path.read_bytes() == before
    assert list((directory / "photos").iterdir()) == originals
    assert client.get(photo["original_url"]).status_code == 200
    assert client.patch(f"/api/dossiers/{dossier_id}", json={"matricule": "CHANGE"}).status_code == 503
    assert path.read_bytes() == before


@pytest.mark.parametrize("lock_name", [".scan.lock", ".worker.lock"])
@pytest.mark.parametrize("category", ["chiffrage_rapide", "verification"])
def test_all_input_writes_share_scan_locks(client, storage, new_dossier, upload_photo, category, lock_name):
    dossier_id = new_dossier(category)["dossier_id"]
    directory = storage / dossier_id
    (directory / "analysis").mkdir()
    before = (directory / "metadata.json").read_bytes()
    with FileLock(str(directory / "analysis" / lock_name)):
        assert upload_photo(dossier_id, phase="before" if category == "verification" else None).status_code == 409
        assert client.patch(f"/api/dossiers/{dossier_id}", json={"matricule": "CHANGE"}).status_code == 409
        assert client.put(f"/api/dossiers/{dossier_id}/vehicle", json={"marque_id": 12, "model_id": 34}).status_code == 409
    assert (directory / "metadata.json").read_bytes() == before


def test_network_failure_during_patch_preserves_inputs(client, storage, new_dossier, monkeypatch):
    dossier_id = new_dossier()["dossier_id"]
    before = (storage / dossier_id / "metadata.json").read_bytes()
    monkeypatch.undo()
    monkeypatch.setenv("AUTOESTIMATE_BASE_URL", "http://private.test")
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "private-token")

    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("private-token")

    monkeypatch.setattr("api.vehicle_catalog.requests.get", unavailable)
    response = client.patch(f"/api/dossiers/{dossier_id}", json={"marque_id": 12, "model_id": 34})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "autoestimate_unavailable"
    assert "private-token" not in response.text
    assert (storage / dossier_id / "metadata.json").read_bytes() == before


def test_quick_failure_retry_and_empty_photo_admission(storage, new_dossier, upload_photo, workflow_catalog):
    dossier_id = new_dossier()["dossier_id"]
    backend = FixtureBackend("fail")
    with TestClient(create_app(ApiSettings(storage), backend)) as client:
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 422
        upload_photo(dossier_id)
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 202
        assert wait_for_terminal(client, dossier_id)[0]["workflow_status"] == "failed"
        backend.mode = "success"
        assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 202
        assert wait_for_terminal(client, dossier_id)[0]["workflow_status"] == "estimated"


def test_photo_directory_symlink_cannot_escape_dossier(client, storage, tmp_path, new_dossier, upload_photo):
    dossier_id = new_dossier()["dossier_id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    photos = storage / dossier_id / "photos"
    photos.rmdir()
    photos.symlink_to(outside, target_is_directory=True)
    assert upload_photo(dossier_id).status_code == 404
    assert list(outside.iterdir()) == []


def test_pixel_limit_and_invalid_side_leave_no_upload(storage, new_dossier, upload_photo, monkeypatch):
    dossier_id = new_dossier()["dossier_id"]
    assert upload_photo(dossier_id, "diagonal").status_code == 422
    monkeypatch.setattr("api.photo_service.MAX_IMAGE_PIXELS", 100)
    assert upload_photo(dossier_id).status_code == 422
    assert list((storage / dossier_id / "photos").iterdir()) == []


def test_finalized_evidence_stays_locked_after_external_file_loss(client, storage, new_dossier, upload_photo):
    dossier_id = new_dossier("verification")["dossier_id"]
    for phase in ("before", "after"):
        for side in ("front", "rear", "left", "right"):
            upload_photo(dossier_id, side, phase)
    path = storage / dossier_id / "metadata.json"
    metadata = json.loads(path.read_text())
    photo = metadata["photos"][-1]
    (storage / dossier_id / photo["path"]).unlink()
    assert client.get(f"/api/dossiers/{dossier_id}").json()["workflow_status"] == "after_incomplete"
    assert upload_photo(dossier_id, "right", "after", photo_id=photo["photo_id"]).status_code == 409


def test_registration_date_required_before_quick_scan(storage, new_dossier, upload_photo, workflow_catalog):
    dossier_id = new_dossier()["dossier_id"]
    upload_photo(dossier_id)
    path = storage / dossier_id / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.pop("date_mise_en_circulation")
    path.write_text(json.dumps(metadata))
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        assert client.get(f"/api/dossiers/{dossier_id}").json()["workflow_status"] == "draft"
        response = client.post(f"/api/dossiers/{dossier_id}/scan")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "incomplete_vehicle_metadata"


@pytest.mark.parametrize("over_limit", [False, True])
def test_multipart_limit_counts_chunked_bytes_without_content_length(monkeypatch, over_limit):
    monkeypatch.setattr("api.upload_limits.MAX_MULTIPART_BYTES", 8)
    messages = iter([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789" if over_limit else b"5678", "more_body": False},
    ])
    sent = []
    delivered = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def application(scope, receive, send):
        delivered.append(await receive())

    asyncio.run(UploadLimitMiddleware(application)(
        {"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=test")]}, receive, send,
    ))
    if over_limit:
        assert delivered == []
        assert sent[0]["status"] == 413
    else:
        assert delivered == [{"type": "http.request", "body": b"12345678", "more_body": False}]


def test_upload_and_patch_cors_preflights(storage):
    with TestClient(create_app(ApiSettings(storage, ("http://localhost:5173",)))) as client:
        for method, path in [("PATCH", "/api/dossiers/LOCAL"), ("POST", "/api/dossiers/LOCAL/photos"),
                             ("PUT", "/api/dossiers/LOCAL/photos/PHOTO")]:
            response = client.options(path, headers={
                "Origin": "http://localhost:5173", "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "Content-Type",
            })
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_verification_is_rechecked_under_scan_lock(storage, new_dossier, monkeypatch):
    dossier_id = new_dossier()["dossier_id"]
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        manager = client.app.state.scan_manager
        read_dossier = manager.repository.read_dossier
        reads = 0

        def replace_category_before_locked_read(identifier):
            nonlocal reads
            reads += 1
            if reads == 2:
                path = storage / dossier_id / "metadata.json"
                metadata = json.loads(path.read_text())
                metadata["type"] = "verification"
                path.write_text(json.dumps(metadata))
            return read_dossier(identifier)

        monkeypatch.setattr(manager.repository, "read_dossier", replace_category_before_locked_read)
        response = client.post(f"/api/dossiers/{dossier_id}/scan")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "scan_disabled_for_type"
        assert not (storage / dossier_id / "analysis/status.json").exists()
