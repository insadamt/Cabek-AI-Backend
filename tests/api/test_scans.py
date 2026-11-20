"""Verify queued scan lifecycle, artifact validation and lighting completion using isolated worker processes.

Depends on temporary dossiers and the fixture backend; no model inference or hardware is used.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.errors import DossierError
from api.main import create_app
from conftest import write_json


class FixtureBackend:
    def __init__(self, mode="success"):
        self.mode = mode

    def ensure_available(self, models_dir):
        if self.mode == "unavailable":
            raise DossierError(503, "worker_unavailable", "Worker unavailable.")

    def launch(self, request_path, log):
        run_id = json.loads(request_path.read_text())["run_id"]
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("scan_worker_fixture.py")),
             str(request_path), self.mode, run_id], stdout=log, stderr=subprocess.STDOUT,
        )


def wait_for_terminal(client, dossier_id="DOS-001", timeout=8):
    deadline = time.monotonic() + timeout
    observed = []
    while time.monotonic() < deadline:
        response = client.get(f"/api/dossiers/{dossier_id}/result")
        assert response.status_code == 200, response.text
        data = response.json()
        observed.append(data)
        if data["status"] in ("completed", "failed"):
            return data, observed
        time.sleep(0.05)
    pytest.fail("Scan did not reach a terminal state.")


def test_scan_produces_real_artifacts_progress_conflicts_and_saved_result(storage, create_dossier):
    directory = create_dossier(legacy_paths=True)
    app = create_app(ApiSettings(storage), FixtureBackend())
    with TestClient(app) as client:
        accepted = client.post("/api/dossiers/DOS-001/scan")
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "queued"
        assert accepted.json()["progress"]["annotations_expected"] == 3
        assert accepted.json()["progress"]["annotations_ready"] == 0
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 409
        data, observed = wait_for_terminal(client)
        assert data["status"] == "completed"
        assert any(0 < item["progress"]["annotations_ready"] < 3 for item in observed)
        assert data["progress"]["photos_completed"] == 1
        assert data["progress"]["result_ready"] is True
        assert (directory / "analysis/annotated/avant_01_pieces.png").is_file()
        assert not (directory / "analysis/attempts").exists()
        assert all(item["progress"]["eta_seconds"] is None for item in observed)
        assert client.get(data["result"]["report_url"]).status_code == 200
        before = (directory / "analysis/result.json").read_bytes()
        duplicate = client.post("/api/dossiers/DOS-001/scan")
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "already_analyzed"
        assert (directory / "analysis/result.json").read_bytes() == before
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as restarted:
        assert restarted.get("/api/dossiers/DOS-001/result").json()["status"] == "completed"


@pytest.mark.parametrize("mode", ["fail", "bad_output", "late_failure"])
def test_failed_scan_cleans_generated_files_before_retry(storage, create_dossier, mode):
    directory = create_dossier()
    backend = FixtureBackend(mode)
    with TestClient(create_app(ApiSettings(storage), backend)) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        failed, _ = wait_for_terminal(client)
        assert failed["status"] == "failed"
        assert "private" not in json.dumps(failed)
        assert not (directory / "analysis/result.json").exists()
        first_run = json.loads((directory / "analysis/status.json").read_text())["run_id"]
        backend.mode = "success"
        retry = client.post("/api/dossiers/DOS-001/scan")
        assert retry.status_code == 202
        assert retry.json()["progress"]["annotations_ready"] == 0
        second_run = json.loads((directory / "analysis/status.json").read_text())["run_id"]
        assert first_run != second_run
        assert not (directory / "analysis/attempts").exists()
        assert wait_for_terminal(client)[0]["status"] == "completed"


def test_timeout_terminates_the_worker(storage, create_dossier):
    directory = create_dossier()
    with TestClient(create_app(ApiSettings(storage, scan_timeout_seconds=0.3), FixtureBackend("hang"))) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        failed, _ = wait_for_terminal(client)
        assert failed["error"]["code"] == "scan_timeout"
        assert not (directory / "analysis/result.json").exists()


def test_shutdown_marks_active_scan_interrupted(storage, create_dossier):
    create_dossier()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("hang"))) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        failed = client.get("/api/dossiers/DOS-001/result").json()
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "scan_interrupted"


def test_cross_app_lock_rejects_concurrent_scan(storage, create_dossier):
    create_dossier()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("hang"))) as first:
        assert first.post("/api/dossiers/DOS-001/scan").status_code == 202
        with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as second:
            response = second.post("/api/dossiers/DOS-001/scan")
            assert response.status_code == 409
            assert second.get("/api/dossiers/DOS-001/result").json()["status"] in ("queued", "processing")


def test_queue_capacity_is_bounded(storage, create_dossier):
    create_dossier("DOS-001")
    create_dossier("DOS-002")
    with TestClient(create_app(ApiSettings(storage, scan_max_pending=1), FixtureBackend("hang"))) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        assert client.post("/api/dossiers/DOS-002/scan").status_code == 503


def test_second_dossier_stays_queued_until_worker_is_free(storage, create_dossier):
    create_dossier("DOS-001")
    create_dossier("DOS-002")
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("hang"))) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        queued = client.post("/api/dossiers/DOS-002/scan")
        assert queued.status_code == 202
        data = client.get("/api/dossiers/DOS-002/result").json()
        assert data["status"] == "queued"
        assert data["progress"]["annotations_ready"] == 0
        assert client.post("/api/dossiers/DOS-002/scan").status_code == 409


def test_invalid_dossier_photos_and_unavailable_worker(storage, create_dossier):
    directory = create_dossier()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend("unavailable"))) as client:
        assert client.post("/api/dossiers/DOS-UNKNOWN/scan").status_code == 404
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 503
        write_json(directory / "metadata.json", {"dossier_id": "DOS-001", "photos": []})
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 422


def test_frontend_can_preflight_scan_requests(storage):
    settings = ApiSettings(storage, ("http://localhost:5173",))
    with TestClient(create_app(settings, FixtureBackend())) as client:
        response = client.options("/api/dossiers/DOS-001/scan", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "POST" in response.headers["access-control-allow-methods"]


def test_changed_inputs_prevent_publication(storage, create_dossier):
    directory = create_dossier()
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        assert client.post("/api/dossiers/DOS-001/scan").status_code == 202
        metadata = json.loads((directory / "metadata.json").read_text())
        metadata["modele"] = "Changed during scan"
        write_json(directory / "metadata.json", metadata)
        failed, _ = wait_for_terminal(client)
        assert failed["error"]["code"] == "scan_inputs_changed"
        assert not (directory / "analysis/result.json").exists()


def test_restart_recovers_only_unlocked_managed_jobs(storage, create_dossier):
    directory = create_dossier()
    record = {
        "managed_by": "cabek_scan_v1", "status": "processing", "run_id": "old",
        "progress": {"stage": "preparing", "annotations_ready": 0, "annotations_expected": 3,
                     "photos_completed": 0, "photos_total": 1, "elapsed_seconds": 1,
                     "eta_seconds": None, "report_ready": False, "result_ready": False},
    }
    write_json(directory / "analysis/status.json", record)
    with TestClient(create_app(ApiSettings(storage), FixtureBackend())) as client:
        response = client.get("/api/dossiers/DOS-001/result").json()
        assert response["status"] == "failed"
        assert response["error"]["code"] == "scan_interrupted"
        assert response["progress"]["stage"] == "failed"


@pytest.mark.parametrize('mode, successful', [('success', True), ('late_failure', False)])
def test_default_analysis_preserves_tunnel_lighting(storage, create_dossier, mode, successful):
    from threading import Event
    from unittest.mock import Mock
    from api.lighting_client import LightingClient, LightingSettings

    create_dossier()
    finished = Event()
    lighting = LightingClient(LightingSettings())
    lighting.begin_analysis = Mock(wraps=lighting.begin_analysis)
    lighting.finish_analysis = Mock(side_effect=lambda *args: finished.set())
    app = create_app(ApiSettings(storage), FixtureBackend(mode), lighting=lighting)
    notify_finished = app.state.tunnel_lifecycle.finish_analysis
    def finish(run_id, outcome):
        notify_finished(run_id, outcome)
        finished.set()
    app.state.tunnel_lifecycle.finish_analysis = Mock(side_effect=finish)
    with TestClient(app) as client:
        assert client.post('/api/dossiers/DOS-001/scan').status_code == 202
        assert lighting.prepare_command()[2]['color'] == '#FFFFFF'
        data, _ = wait_for_terminal(client)
        assert finished.wait(1)
        lighting.begin_analysis.assert_not_called()
        lighting.finish_analysis.assert_not_called()
        assert app.state.tunnel_lifecycle.finish_analysis.call_args.args[1] == successful
        assert (data['status'] == 'completed') == successful
