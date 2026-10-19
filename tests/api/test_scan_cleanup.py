from unittest.mock import Mock

from fastapi.testclient import TestClient
from filelock import FileLock
from PIL import Image
import pytest

from api.config import ApiSettings
from api.main import create_app
from api.repository import DossierRepository
from conftest import write_json
from scans.contracts import ScanRequest
from scans.worker import run_accepted_scan
from test_scans import FixtureBackend, wait_for_terminal


def processing_record():
    return {
        "managed_by": "cabek_scan_v1", "status": "processing", "run_id": "current",
        "progress": {"stage": "preparing", "annotations_ready": 0, "annotations_expected": 3,
                     "photos_completed": 0, "photos_total": 1, "elapsed_seconds": 1,
                     "eta_seconds": None, "report_ready": False, "result_ready": False},
    }


def test_retry_cleans_only_generated_outputs_and_preserves_inputs(storage, create_dossier):
    directory = create_dossier()
    write_json(directory / "feedback.json", [])
    before = {name: (directory / name).read_bytes() for name in
              ("metadata.json", "feedback.json", "photos/avant_01.jpg")}
    analysis = directory / "analysis"
    annotated = analysis / "annotated"
    annotated.mkdir(parents=True)
    for name in ("old_pieces.png", "old_dommages.png", "old_critical.png"):
        Image.new("RGB", (4, 4)).save(annotated / name)
    for name in ("report.pdf", "result.json", ".scan-old.tmp", "result.json.tmp", ".worker.log"):
        (analysis / name).write_bytes(b"incomplete")
    (annotated / ".scan-old.tmp").write_bytes(b"incomplete")
    (analysis / "notes.txt").write_text("Keep manual notes")
    (annotated / "reference.png").write_bytes(b"Keep unrelated images")
    write_json(analysis / "status.json", {**processing_record(), "status": "failed"})
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("hang"))) as client:
        response = client.post("/api/dossiers/DOS-001/scan")
        assert response.status_code == 202
        assert response.json()["progress"]["annotations_ready"] == 0
        for name in ("report.pdf", "result.json", ".scan-old.tmp", "result.json.tmp"):
            assert not (analysis / name).exists()
        assert list(annotated.glob("old_*.png")) == []
        assert not (annotated / ".scan-old.tmp").exists()
        assert (analysis / "notes.txt").read_text() == "Keep manual notes"
        assert (annotated / "reference.png").read_bytes() == b"Keep unrelated images"
        assert (analysis / ".scan.lock").exists()
        assert not (analysis / "attempts").exists()
    assert before == {name: (directory / name).read_bytes() for name in before}


def test_completed_output_is_never_cleaned_even_without_models(storage, complete_dossier):
    directory, _ = complete_dossier()
    analysis = directory / "analysis"
    before = {path.name: path.read_bytes() for path in analysis.glob("*") if path.is_file()}
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("unavailable"))) as client:
        response = client.post("/api/dossiers/DOS-001/scan")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "already_analyzed"
    assert all((analysis / name).read_bytes() == content for name, content in before.items())


def test_invalid_completion_marker_recovers_as_failed_and_can_retry(storage, create_dossier):
    directory = create_dossier()
    write_json(directory / "analysis/status.json", processing_record())
    (directory / "analysis/result.json").write_text("{broken")
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        data = client.get("/api/dossiers/DOS-001/result").json()
        assert data["status"] == "failed"
        assert data["result"] is None
        assert not (directory / "analysis/result.json").exists()
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        assert wait_for_terminal(client)[0]["status"] == "completed"


def test_restart_preserves_valid_output_awaiting_confirmation(storage, complete_dossier):
    directory, _ = complete_dossier()
    analysis = directory / "analysis"
    write_json(analysis / "status.json", processing_record())
    before = (analysis / "result.json").read_bytes()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        data = client.get("/api/dossiers/DOS-001/result").json()
        assert data["status"] == "completed"
        assert data["progress"]["result_ready"] is True
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 409
    assert (analysis / "result.json").read_bytes() == before


def test_live_worker_lock_prevents_cleanup_after_parent_is_gone(storage, create_dossier):
    directory = create_dossier()
    analysis = directory / "analysis"
    write_json(analysis / "status.json", processing_record())
    (analysis / "report.pdf").write_bytes(b"worker still writing")
    with FileLock(str(analysis / ".worker.lock")):
        with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
            assert client.get("/api/dossiers/DOS-001/result").json()["status"] == "processing"
            response = client.post("/api/dossiers/DOS-001/scan")
            assert response.status_code == 409
            assert (analysis / "report.pdf").read_bytes() == b"worker still writing"
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        assert client.get("/api/dossiers/DOS-001/result").json()["status"] == "failed"


def test_valid_files_are_not_exposed_before_worker_confirmation(storage, complete_dossier):
    directory, _ = complete_dossier()
    analysis = directory / "analysis"
    write_json(analysis / "status.json", processing_record())
    lock = FileLock(str(analysis / ".worker.lock"))
    with lock:
        with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
            response = client.get("/api/dossiers/DOS-001/result").json()
            assert response["status"] == "processing"
            assert response["result"] is None
            assert client.get("/api/dossiers/DOS-001/report").status_code == 404
            lock.release()
            assert wait_for_terminal(client)[0]["status"] == "completed"


def test_delayed_worker_with_old_identity_cannot_write_new_run(storage, create_dossier):
    directory = create_dossier()
    dossier = DossierRepository(storage).read_dossier("DOS-001")
    analysis = directory / "analysis"
    request = ScanRequest(
        run_id="current", dossier_id="DOS-001", dossier_dir=directory,
        output_dir=analysis, models_dir=directory, metadata={},
        photos=list(dossier.photos.values()), feedback=[], input_signatures={},
    )
    write_json(analysis / ".request.json", request.model_dump(mode="json"))
    write_json(analysis / "status.json", processing_record())
    analyze = Mock()
    with pytest.raises(RuntimeError, match="no longer owns"):
        run_accepted_scan(analysis / ".request.json", "old", analyze)
    analyze.assert_not_called()
    assert not (analysis / "result.json").exists()
