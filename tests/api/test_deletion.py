"""Verify deletion ownership, durable evidence removal and admission races using isolated storage.

Depends on TestClient, real file locks and temporary dossiers; never touches saved demo folders.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from filelock import FileLock
import pytest

from api.dossier_lifecycle import dossier_admission
from api.errors import DossierError
from conftest import write_json


@pytest.mark.parametrize("category", ["chiffrage_rapide", "verification"])
def test_delete_local_dossier_and_media(client, storage, new_dossier, upload_photo, category):
    folder = new_dossier(category)
    identifier = folder["dossier_id"]
    detail = upload_photo(identifier, phase="before" if category == "verification" else None).json()
    media = detail["photos"][0]["original_url"]
    assert client.get(media).status_code == 200
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 204
    assert not (storage / identifier).exists()
    assert client.get(f"/api/dossiers/{identifier}").status_code == 404
    assert client.get(media).status_code == 404
    assert client.get("/api/dossiers").json()["total"] == 0
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 404


def test_external_expertise_cannot_be_deleted(client, create_dossier):
    directory = create_dossier()
    result = client.delete("/api/dossiers/DOS-001")
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "externally_owned_dossier"
    assert (directory / "metadata.json").exists()


@pytest.mark.parametrize("name,code", [
    (".capture.lock", "capture_in_progress"),
    ("analysis/.scan.lock", "scan_in_progress"),
    ("analysis/.worker.lock", "scan_in_progress"),
])
def test_active_owners_block_delete(client, storage, new_dossier, name, code):
    identifier = new_dossier()["dossier_id"]
    directory = storage / identifier
    (directory / "analysis").mkdir(exist_ok=True)
    before = (directory / "metadata.json").read_bytes()
    with FileLock(str(directory / name)):
        response = client.delete(f"/api/dossiers/{identifier}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    assert (directory / "metadata.json").read_bytes() == before


@pytest.mark.parametrize("state", ["queued", "processing"])
def test_saved_active_analysis_blocks_delete(client, storage, new_dossier, state):
    identifier = new_dossier()["dossier_id"]
    write_json(storage / identifier / "analysis/status.json", {"status": state})
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 409
    assert (storage / identifier).exists()


def test_terminal_analysis_does_not_block_delete(client, storage, new_dossier):
    identifier = new_dossier()["dossier_id"]
    write_json(storage / identifier / "analysis/status.json", {"status": "completed"})
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 204


def test_failed_removal_is_reported_and_remaining_folder_retained(client, storage, new_dossier, monkeypatch):
    identifier = new_dossier()["dossier_id"]
    def unavailable(path):
        raise OSError("fixture disk failure")
    monkeypatch.setattr("api.deletion.shutil.rmtree", unavailable)
    response = client.delete(f"/api/dossiers/{identifier}")
    assert response.status_code == 503
    assert (storage / identifier / "metadata.json").exists()
    assert client.get(f"/api/dossiers/{identifier}").status_code == 200


def test_stable_admission_blocks_handoff_race(client, new_dossier):
    identifier = new_dossier()["dossier_id"]
    repository = client.app.state.workflow_service.repository
    entered, release = Event(), Event()
    def handoff():
        with dossier_admission(repository, identifier):
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor() as executor:
        task = executor.submit(handoff)
        assert entered.wait(3)
        try:
            assert client.delete(f"/api/dossiers/{identifier}").status_code == 409
        finally:
            release.set()
        task.result()
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 204
    with pytest.raises(DossierError) as missing:
        with dossier_admission(repository, identifier):
            pytest.fail("Deleted dossiers must never be reinitialized by late writers.")
    assert missing.value.code == "dossier_not_found"


def test_symlink_lock_directory_is_rejected(client, storage, new_dossier, tmp_path):
    identifier = new_dossier()["dossier_id"]
    # Creation does not need mutation admission; the first write must reject an unsafe lock root.
    locks = storage / ".lifecycle-locks"
    if locks.exists():
        for item in locks.iterdir(): item.unlink()
        locks.rmdir()
    locks.symlink_to(tmp_path, target_is_directory=True)
    assert client.delete(f"/api/dossiers/{identifier}").status_code == 503
    assert (storage / identifier).exists()


def test_delete_cors_preflight(storage):
    from fastapi.testclient import TestClient
    from api.config import ApiSettings
    from api.main import create_app
    origin = "http://localhost:5173"
    with TestClient(create_app(ApiSettings(storage, cors_origins=(origin,)))) as browser:
        response = browser.options("/api/dossiers/LOCAL-1", headers={
            "Origin": origin, "Access-Control-Request-Method": "DELETE",
        })
        assert response.status_code == 200
        assert "DELETE" in response.headers["access-control-allow-methods"]


def test_metadata_lock_symlink_cannot_escape_on_delete(client, storage, new_dossier, tmp_path):
    identifier = new_dossier()["dossier_id"]
    target = tmp_path / "private"
    target.write_text("untouched")
    (storage / identifier / ".metadata.lock").symlink_to(target)
    response = client.delete(f"/api/dossiers/{identifier}")
    assert response.status_code == 422
    assert target.read_text() == "untouched"
    assert (storage / identifier).exists()
