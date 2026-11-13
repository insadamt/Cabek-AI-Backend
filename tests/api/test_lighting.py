"""Verify lighting credentials, authoritative capture hooks and startup health.

Depends on isolated TestClient apps and real capture fixtures; the serial worker
is replaced by a deterministic service so these tests never access hardware.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.lighting_config import LightingSettings
from api.lighting_readiness import check_capture_readiness
from api.lighting_service import LightingService
from api.main import create_app
from test_capture import sample_photos, wait_capture
from test_scans import FixtureBackend


CONTROL = {"Authorization": "Bearer " + "c" * 32}
MAINTENANCE = {"Authorization": "Bearer " + "m" * 32}


@contextmanager
def lighting_client(storage, samples, *, ready=True):
    config = LightingSettings(enabled=True, token="c" * 32, maintenance_token="m" * 32)
    lighting = LightingService(config)
    lighting.start = lambda: lighting.state.transition("BOOTING")
    settings = ApiSettings(storage, tunnel_mode="mock", mock_photos_dir=samples, lighting=config)
    app = create_app(settings, FixtureBackend(), lighting)
    with TestClient(app) as client:
        if ready:
            lighting.check_readiness()
            assert lighting.state.ready
        yield client, lighting


def test_authentication_authorization_validation_and_conflicts(storage, tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, lighting):
        assert client.get("/api/v1/lights/status").status_code == 401
        assert client.get("/api/v1/lights/status", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get("/api/v1/lights/status", headers=CONTROL).status_code == 200
        assert client.post("/api/v1/lights/test", headers=CONTROL, json={"color": "RED"}).status_code == 403
        assert client.post("/api/v1/lights/test", headers=MAINTENANCE, json={"color": "RED"}).status_code == 202
        assert client.post("/api/v1/lights/state", headers=CONTROL, json={"state": "FAKE"}).status_code == 422
        assert client.post("/api/v1/lights/state", headers=CONTROL, json={"state": "READY", "extra": 1}).status_code == 422
        for state in ("SCANNING", "COMPLETED"):
            assert client.post("/api/v1/lights/state", headers=CONTROL, json={"state": state}).status_code == 409
        lighting.state.begin_capture("live")
        assert client.post("/api/v1/lights/test", headers=MAINTENANCE, json={"color": "RED"}).status_code == 409

        def conflict(index):
            return client.post("/api/v1/lights/state", headers=CONTROL, json={"state": "OFF"}).status_code

        with ThreadPoolExecutor(max_workers=6) as executor:
            assert list(executor.map(conflict, range(30))) == [409] * 30
        assert client.get("/api/v1/lights/status", headers=CONTROL).json()["state"] == "SCANNING"


def test_real_capture_start_and_success_without_hardware(storage, tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, lighting):
        entered, release = Event(), Event()
        captures = client.app.state.capture_service
        original = captures.save_sample

        def blocked(*arguments):
            entered.set()
            assert release.wait(5)
            return original(*arguments)

        monkeypatch.setattr(captures, "save_sample", blocked)
        dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
        try:
            assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
            assert entered.wait(3)
            assert lighting.status()["state"] == "SCANNING"
            assert not lighting.status()["esp32_available"]
            duplicate = client.post(f"/api/dossiers/{dossier_id}/capture")
            assert duplicate.status_code == 202
            assert lighting.status()["state"] == "SCANNING"
        finally:
            release.set()
        assert wait_capture(client, dossier_id)["status"] == "completed"
        client.app.state.capture_service.executor.submit(lambda: None).result(timeout=5)
        assert lighting.status()["state"] == "COMPLETED"
        deadline = lighting.state.completion_deadline
        assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
        assert lighting.state.completion_deadline == deadline
        assert lighting.status()["last_successful_command"] is None


def test_failed_capture_never_green_and_restart_does_not_replay_success(storage, tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples, ("front_01.jpg",))
    (samples / "rear_01.jpg").write_bytes(b"broken")
    with lighting_client(storage, samples, ready=False) as (client, lighting):
        lighting.check_readiness()
        assert not lighting.state.ready
        assert lighting.status()["state"] == "INITIALIZING"
        assert client.post("/api/v1/lights/state", headers=CONTROL, json={"state": "READY"}).status_code == 409
        dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
        assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
        assert wait_capture(client, dossier_id)["status"] == "failed"
        client.app.state.capture_service.executor.submit(lambda: None).result(timeout=5)
        assert lighting.status()["state"] == "INITIALIZING"
        assert lighting.state.completion_deadline is None
    (samples / "rear_01.jpg").unlink()
    with lighting_client(storage, samples) as (_, lighting):
        assert lighting.status()["state"] == "READY"
        assert lighting.state.capture_id is None
        assert lighting.state.completion_deadline is None


def test_disabled_lighting_and_token_configuration(client):
    assert client.get("/api/v1/lights/status", headers=CONTROL).status_code == 503
    for kwargs in ({}, {"token": "x" * 32, "maintenance_token": "x" * 32}):
        with pytest.raises(ValueError):
            LightingSettings(enabled=True, **kwargs)


def test_readiness_requires_provider_models_supervisor_and_storage(storage, tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, lighting):
        manager = client.app.state.scan_manager
        captures = client.app.state.capture_service
        check_capture_readiness(manager, captures)
        manager.available = False
        with pytest.raises(RuntimeError):
            check_capture_readiness(manager, captures)
        manager.available = True
        monkeypatch.setattr(manager.backend, "ensure_available", lambda root: (_ for _ in ()).throw(OSError("missing model")))
        with pytest.raises(OSError):
            check_capture_readiness(manager, captures)


def test_interrupted_capture_restart_clears_stale_capture_ownership(storage, tmp_path):
    import json

    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, _):
        dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    metadata_path = storage / dossier_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["captures"] = {"single": {"capture_id": "interrupted", "status": "capturing", "phase": None,
                                         "samples": [{"source": "front_01.jpg", "side": "front"}]}}
    metadata_path.write_text(json.dumps(metadata))
    with lighting_client(storage, samples) as (client, lighting):
        result = client.get(f"/api/dossiers/{dossier_id}/capture").json()
        assert result["status"] == "failed" and result["error_code"] == "capture_interrupted"
        assert lighting.status()["state"] == "READY"


def test_ready_waits_for_capture_recovery(storage, tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    samples.mkdir()
    sample_photos(samples)
    config = LightingSettings(enabled=True, token="c" * 32, maintenance_token="m" * 32)
    lighting = LightingService(config)
    lighting.start = lambda: lighting.state.transition("BOOTING")
    settings = ApiSettings(storage, tunnel_mode="mock", mock_photos_dir=samples, lighting=config)
    app = create_app(settings, FixtureBackend(), lighting)
    entered, release = Event(), Event()
    original = app.state.capture_service.start

    def delayed_recovery():
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(app.state.capture_service, "start", delayed_recovery)

    def launch():
        with TestClient(app):
            lighting.check_readiness()
            assert lighting.state.state == "READY"

    with ThreadPoolExecutor(max_workers=1) as executor:
        launching = executor.submit(launch)
        try:
            assert entered.wait(1)
            assert lighting.state.state == "INITIALIZING"
            assert not lighting.state.ready
            assert lighting.readiness_probe is None
        finally:
            release.set()
        launching.result(timeout=5)
    assert lighting.state.state == "OFF"
