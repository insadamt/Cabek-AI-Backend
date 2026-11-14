"""Regress tunnel capture into externally created expertise dossiers.

Depends on temporary images and the existing subprocess scan fixture; verifies
metadata ownership, durable identities, and actual saved artifacts without YOLO.
"""

import json
from threading import Event

from PIL import Image

import pytest

from conftest import write_json
from test_capture import capture_environment, sample_photos, wait_capture
from test_scans import FixtureBackend, wait_for_terminal


# ============================================================
# IMPORTED DOSSIERS AND IMMUTABLE ANALYSIS INPUTS
# ============================================================

@pytest.fixture
def imported_dossier(storage):
    def create(dossier_id="EXPERT-1", *, legacy=True):
        directory = storage / dossier_id
        directory.mkdir()
        metadata = {
            "dossier_id": dossier_id, "marque": "Dacia", "modele": "Sandero",
            "matricule": "84654-ب-454", "annee": "2020",
            "external_reference": "CABEK-REF", "expert": {"name": "Imported expert"},
        }
        if not legacy:
            metadata.update(type="forfait_expertise", source="external", photos=[])
        write_json(directory / "metadata.json", metadata)
        return directory, metadata
    return create


@pytest.mark.parametrize("legacy", [False, True])
def test_imported_metadata_only_dossier_captures_then_analyzes_without_changed_inputs(
    capture_environment, imported_dossier, legacy,
):
    samples, open_client = capture_environment
    sample_photos(samples)
    directory, imported = imported_dossier(legacy=legacy)
    client = open_client(delay=0.05)
    detail = client.get("/api/dossiers/EXPERT-1/result").json()
    assert detail["workflow_status"] == "awaiting_photos"
    assert detail["photos"] == []
    assert client.get("/api/dossiers", params={
        "type": "forfait_expertise", "workflow_status": "awaiting_photos", "search": "CABEK-REF",
    }).json()["total"] == 0
    selected = client.get("/api/dossiers", params={
        "type": "forfait_expertise", "workflow_status": "awaiting_photos", "search": "EXPERT-1",
    }).json()
    assert selected["total"] == 1
    accepted = client.post("/api/dossiers/EXPERT-1/capture")
    assert accepted.status_code == 202, accepted.text
    captured = wait_capture(client, "EXPERT-1")
    assert captured["status"] == "completed"
    assert captured["saved_count"] == captured["expected_count"] == 2
    assert captured["phase"] is None
    result, _ = wait_for_terminal(client, "EXPERT-1")
    assert result["status"] == result["workflow_status"] == "completed", result
    assert result["result"]["vehicle"]["matricule"] == imported["matricule"]
    assert client.get(result["result"]["report_url"]).status_code == 200
    assert len(result["photos"]) == 2
    for photo in captured["photos"]:
        assert client.get(photo["original_url"]).status_code == 200
    saved = json.loads((directory / "metadata.json").read_text())
    for key, value in imported.items():
        if key != "photos":
            assert saved[key] == value
    if legacy:
        assert "type" not in saved and "source" not in saved
    before = (directory / "metadata.json").read_bytes()
    duplicate = client.post("/api/dossiers/EXPERT-1/capture")
    assert duplicate.status_code == 202
    assert duplicate.json()["capture_id"] == captured["capture_id"]
    assert (directory / "metadata.json").read_bytes() == before
    assert len(list((directory / "photos").iterdir())) == 2


def test_existing_evidence_and_external_mutations_remain_protected(
    capture_environment, create_dossier, upload_photo,
):
    samples, open_client = capture_environment
    sample_photos(samples)
    directory = create_dossier()
    client = open_client()
    original = (directory / "metadata.json").read_bytes()
    response = client.post("/api/dossiers/DOS-001/capture")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capture_not_allowed"
    assert client.get("/api/dossiers/DOS-001/capture").json()["status"] == "not_started"
    assert client.post("/api/dossiers/DOS-001/capture", json={"phase": "before"}).status_code == 422
    assert client.post("/api/dossiers", json={"type": "forfait_expertise"}).status_code == 422
    assert client.patch("/api/dossiers/DOS-001/metadata", json={"matricule": "123-A-456"}).status_code == 409
    assert client.delete("/api/dossiers/DOS-001").status_code == 409
    assert upload_photo("DOS-001").status_code == 409
    assert (directory / "metadata.json").read_bytes() == original


# ============================================================
# PARTIAL CAPTURE, SHARED TUNNEL, AND RECOVERY
# ============================================================

def test_failed_expertise_capture_restarts_all_photos_with_a_fresh_identity(
    capture_environment, imported_dossier,
):
    samples, open_client = capture_environment
    sample_photos(samples, ("front_01.jpg",))
    (samples / "rear_01.png").write_bytes(b"broken")
    directory, _ = imported_dossier()
    client = open_client()
    assert client.post("/api/dossiers/EXPERT-1/capture").status_code == 202
    failed = wait_capture(client, "EXPERT-1")
    assert failed["status"] == "failed" and failed["saved_count"] == 1
    assert client.get("/api/dossiers/EXPERT-1/result").json()["workflow_status"] == "awaiting_photos"
    original = json.loads((directory / "metadata.json").read_text())["photos"][0]
    old_bytes = (directory / original["path"]).read_bytes()
    Image.new("RGB", (24, 20), "blue").save(samples / "front_01.jpg")
    assert client.post("/api/dossiers/EXPERT-1/scan").status_code == 409
    sample_photos(samples, ("rear_01.png",))
    assert client.post("/api/dossiers/EXPERT-1/capture").status_code == 202
    completed = wait_capture(client, "EXPERT-1")
    assert completed["capture_id"] != failed["capture_id"]
    assert failed["photos"][0]["photo_id"] not in {photo["photo_id"] for photo in completed["photos"]}
    assert completed["saved_count"] == 2
    new_front = next(photo for photo in completed["photos"] if photo["original_filename"] == "front_01.jpg")
    assert client.get(new_front["original_url"]).content != old_bytes
    saved = json.loads((directory / "metadata.json").read_text())
    assert saved["photo_history"][0]["capture_id"] == failed["capture_id"]
    assert (directory / original["path"]).read_bytes() == old_bytes
    assert wait_for_terminal(client, "EXPERT-1")[0]["status"] == "completed"


def test_restart_recovers_interrupted_imported_capture(capture_environment, imported_dossier):
    samples, open_client = capture_environment
    sample_photos(samples)
    directory, metadata = imported_dossier()
    metadata["captures"] = {"single": {
        "capture_id": "durable-camera-id", "phase": None, "status": "capturing",
        "samples": [{"source": path.name, "side": "front"} for path in sorted(samples.iterdir())],
    }}
    write_json(directory / "metadata.json", metadata)
    client = open_client()
    restored = client.get("/api/dossiers/EXPERT-1/capture").json()
    assert restored["status"] == "failed" and restored["error_code"] == "capture_interrupted"
    assert client.post("/api/dossiers/EXPERT-1/capture").status_code == 202
    completed = wait_capture(client, "EXPERT-1")
    assert completed["capture_id"] != "durable-camera-id"
    assert wait_for_terminal(client, "EXPERT-1")[0]["status"] == "completed"


def test_analysis_admission_failure_preserves_capture_for_explicit_retry(
    capture_environment, imported_dossier,
):
    samples, open_client = capture_environment
    sample_photos(samples)
    imported_dossier()
    backend = FixtureBackend("unavailable")
    client = open_client(backend=backend)
    assert client.post("/api/dossiers/EXPERT-1/capture").status_code == 202
    captured = wait_capture(client, "EXPERT-1")
    client.app.state.capture_service.executor.submit(lambda: None).result(timeout=5)
    captured = client.get("/api/dossiers/EXPERT-1/capture").json()
    assert captured["status"] == "completed" and captured["analysis_error"] == "worker_unavailable"
    backend.mode = "success"
    assert client.post("/api/dossiers/EXPERT-1/capture").status_code == 202
    assert wait_for_terminal(client, "EXPERT-1")[0]["status"] == "completed"
    assert client.get("/api/dossiers/EXPERT-1/capture").json()["capture_id"] == captured["capture_id"]


def test_expertise_uses_the_single_tunnel_and_locks_partial_inputs(
    capture_environment, imported_dossier, monkeypatch,
):
    samples, open_client = capture_environment
    sample_photos(samples)
    imported_dossier()
    imported_dossier("EXPERT-2")
    client = open_client()
    entered, release = Event(), Event()
    provider = client.app.state.capture_service.provider
    read = provider.read_photo
    def hold(source):
        if source == "front_02.png":
            entered.set()
            assert release.wait(5)
        return read(source)
    monkeypatch.setattr(provider, "read_photo", hold)
    try:
        first = client.post("/api/dossiers/EXPERT-1/capture")
        assert first.status_code == 202
        assert entered.wait(3)
        duplicate = client.post("/api/dossiers/EXPERT-1/capture")
        assert duplicate.status_code == 202 and duplicate.json()["capture_id"] == first.json()["capture_id"]
        assert client.post("/api/dossiers/EXPERT-1/scan").status_code == 409
        assert client.get("/api/dossiers/EXPERT-1/result").json()["workflow_status"] == "capturing"
        conflict = client.post("/api/dossiers/EXPERT-2/capture")
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "tunnel_busy"
    finally:
        release.set()
    assert wait_capture(client, "EXPERT-1")["status"] == "completed"


@pytest.mark.parametrize("analysis_failed", [False, True])
def test_missing_legacy_originals_offer_capture_and_do_not_poison_the_next_scan(
    capture_environment, create_dossier, analysis_failed,
):
    samples, open_client = capture_environment
    sample_photos(samples)
    directory = create_dossier("2")
    path = directory / "metadata.json"
    original = json.loads(path.read_text())
    for photo in original["photos"]:
        (directory / photo["path"]).unlink()
    if analysis_failed:
        write_json(directory / "analysis/status.json", {"status": "failed"})
    client = open_client()
    listed = client.get("/api/dossiers", params={
        "type": "forfait_expertise", "workflow_status": "awaiting_photos", "search": "2",
    }).json()
    assert listed["total"] == 1
    assert listed["items"][0]["dossier_id"] == "2" and listed["items"][0]["photo_count"] == 0
    detail = client.get("/api/dossiers/2/result").json()
    assert detail["photos"] == [] and detail["workflow_status"] == "awaiting_photos"
    assert json.loads(path.read_text()) == original
    accepted = client.post("/api/dossiers/2/capture")
    assert accepted.status_code == 202, accepted.text
    assert wait_capture(client, "2")["status"] == "completed"
    result, _ = wait_for_terminal(client, "2")
    assert result["status"] == "completed", result
    saved = json.loads(path.read_text())
    assert len(saved["photos"]) == 2 and len(saved["photo_history"]) == 1
    assert saved["photo_history"][0]["path"] == original["photos"][0]["path"]
