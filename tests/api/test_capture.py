"""Exercise actual sample ingestion, capture recovery, and before/after evidence.

Depends on temporary JPEG/PNG files and the production capture provider. Tests
replace only the AI process backend and upstream catalog, never saved image bytes.
"""

from contextlib import ExitStack
import hashlib
import json
from threading import Event
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api.config import ApiSettings
from api.main import create_app
from test_scans import FixtureBackend, wait_for_terminal


@pytest.fixture
def capture_environment(storage, tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    with ExitStack() as clients:
        def open_client(delay=0, backend=None):
            app = create_app(ApiSettings(storage, tunnel_mode="mock", mock_photos_dir=samples,
                                         mock_photo_delay_seconds=delay), backend or FixtureBackend())
            return clients.enter_context(TestClient(app))
        yield samples, open_client


def sample_photos(samples, names=("front_01.jpg", "front_02.png")):
    for name in names:
        path = samples / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 20), "red").save(path)


def wait_capture(client, dossier_id, phase=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(f"/api/dossiers/{dossier_id}/capture", params={"phase": phase} if phase else {})
        assert response.status_code == 200, response.text
        status = response.json()
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(0.02)
    pytest.fail("Capture did not finish.")


def test_real_ingestion_counts_originals_and_duplicate_requests(capture_environment, storage):
    samples, open_client = capture_environment
    sample_photos(samples)
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in samples.iterdir()}
    client = open_client(delay=0.15)
    dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    start = client.post(f"/api/dossiers/{dossier_id}/capture")
    assert start.status_code == 202
    duplicate = client.post(f"/api/dossiers/{dossier_id}/capture")
    assert duplicate.status_code == 202
    assert duplicate.json()["capture_id"] == start.json()["capture_id"]
    terminal = wait_capture(client, dossier_id)
    assert terminal["status"] == "completed"
    assert terminal["expected_count"] == terminal["saved_count"] == 2
    for photo in terminal["photos"]:
        assert photo["side"] == "front" and photo["phase"] is None
        content = client.get(photo["original_url"])
        assert content.status_code == 200
        assert hashlib.sha256(content.content).hexdigest() == hashes[photo["original_filename"]]
    assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
    assert len(list((storage / dossier_id / "photos").iterdir())) == 2
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in samples.iterdir()} == hashes


def test_failure_retry_keeps_saved_photos_and_never_scans_partial_capture(capture_environment, storage):
    samples, open_client = capture_environment
    sample_photos(samples, ("front_01.jpg",))
    (samples / "rear_01.png").write_bytes(b"invalid image")
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
    failed = wait_capture(client, dossier_id)
    assert failed["status"] == "failed" and failed["saved_count"] == 1
    assert failed["error_code"] == "invalid_image"
    assert client.get(f"/api/dossiers/{dossier_id}").json()["workflow_status"] == "capture_failed"
    assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 409
    assert not (storage / dossier_id / "analysis/status.json").exists()
    sample_photos(samples, ("rear_01.png",))
    assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
    completed = wait_capture(client, dossier_id)
    assert completed["status"] == "completed" and completed["saved_count"] == 2
    assert completed["capture_id"] == failed["capture_id"]
    assert failed["photos"][0] in completed["photos"]


def test_single_tunnel_and_scan_admission_wait_for_all_photos(capture_environment, monkeypatch):
    samples, open_client = capture_environment
    sample_photos(samples)
    client = open_client()
    entered, release = Event(), Event()
    provider = client.app.state.capture_service.provider
    read = provider.read_photo

    def blocked(source):
        if source == "front_02.png":
            entered.set()
            assert release.wait(5)
        return read(source)

    monkeypatch.setattr(provider, "read_photo", blocked)
    first = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    second = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    try:
        assert client.post(f"/api/dossiers/{first}/capture").status_code == 202
        assert entered.wait(3)
        status = client.get(f"/api/dossiers/{first}/capture").json()
        assert status["saved_count"] == 1
        assert client.get(f"/api/dossiers/{first}").json()["status"] == "not_analyzed"
        assert client.post(f"/api/dossiers/{first}/scan").status_code == 409
        assert client.post(f"/api/dossiers/{second}/capture").status_code == 409
    finally:
        release.set()
    assert wait_capture(client, first)["status"] == "completed"


def test_metadata_during_real_capture_and_inference_then_saved_result(capture_environment, storage, workflow_catalog):
    samples, open_client = capture_environment
    sample_photos(samples, ("front_01.jpg",))
    client = open_client(delay=0.15)
    dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    assert client.post(f"/api/dossiers/{dossier_id}/capture").status_code == 202
    assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": "84654-ب-454"}).status_code == 200
    assert wait_capture(client, dossier_id)["status"] == "completed"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        data = client.get(f"/api/dossiers/{dossier_id}/result").json()
        if data.get("progress") and data["progress"]["stage"] == "awaiting_metadata":
            break
        time.sleep(0.03)
    else:
        pytest.fail("Inference did not reach metadata gate.")
    assert data["progress"]["annotations_ready"] == 3
    assert data["progress"]["report_ready"] is False and data["result"] is None
    assert data["workflow_status"] == "awaiting_metadata"
    assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json={
        "marque_id": 12, "model_id": 34, "date_mise_en_circulation": "2020-05-17",
    }).status_code == 200
    terminal, _ = wait_for_terminal(client, dossier_id)
    assert terminal["status"] == "completed"
    assert terminal["result"]["vehicle"]["matricule"] == "84654-ب-454"
    assert client.get(terminal["result"]["report_url"]).status_code == 200
    assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": "123-A-456"}).status_code == 409
    restarted = open_client()
    assert restarted.get(f"/api/dossiers/{dossier_id}/result").json()["result"] == terminal["result"]


def test_before_after_multiple_photos_per_side_and_no_ai(capture_environment, storage):
    samples, open_client = capture_environment
    names = ("front_01.jpg", "front_02.png", "rear_01.jpg", "left_01.jpg", "right_01.jpg")
    sample_photos(samples, tuple(f"{phase}/{name}" for phase in ("before", "after") for name in names))
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
    assert client.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "after"}).status_code == 409
    for phase in ("before", "after"):
        assert client.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": phase}).status_code == 202
        if phase == "before":
            assert client.patch(f"/api/dossiers/{dossier_id}/metadata", json={"matricule": "14554-B-4578"}).status_code == 200
        state = wait_capture(client, dossier_id, phase)
        assert state["status"] == "completed" and state["saved_count"] == 5
    detail = client.get(f"/api/dossiers/{dossier_id}").json()
    assert detail["workflow_status"] == "comparison_ready"
    assert len(detail["photos"]) == 10
    assert detail["vehicle"]["matricule"] == "14554-B-4578"
    assert detail["comparison"]["finalized"]
    assert detail["result"] is None and detail["status"] == "not_analyzed"
    assert client.post(f"/api/dossiers/{dossier_id}/scan").status_code == 409
    assert not (storage / dossier_id / "analysis/status.json").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "colon", "corrupt", "disguised", "side"])
def test_unsafe_sample_never_becomes_saved_photo(capture_environment, tmp_path, unsafe):
    samples, open_client = capture_environment
    if unsafe == "symlink":
        outside = tmp_path / "outside.jpg"
        Image.new("RGB", (12, 12)).save(outside)
        (samples / "front_01.jpg").symlink_to(outside)
    elif unsafe == "colon":
        sample_photos(samples, ("front:evil.jpg",))
    elif unsafe == "side":
        sample_photos(samples, ("unknown.jpg",))
    elif unsafe == "disguised":
        Image.new("RGB", (12, 12)).save(samples / "front_01.jpg", format="PNG")
    else:
        (samples / "front_01.jpg").write_bytes(b"broken")
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
    response = client.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"})
    assert response.status_code in (202, 422, 404)
    if response.status_code == 202:
        assert wait_capture(client, dossier_id, "before")["status"] == "failed"
    detail = client.get(f"/api/dossiers/{dossier_id}").json()
    assert detail["photos"] == [] and detail["status"] == "not_analyzed"


def test_interrupted_capture_recovered_then_retried(capture_environment, storage):
    samples, open_client = capture_environment
    sample_photos(samples, ("front_01.jpg",))
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
    path = storage / dossier_id / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata["captures"] = {"before": {"capture_id": "interrupted", "phase": "before", "status": "capturing",
                                        "samples": [{"source": "front_01.jpg", "side": "front"}]}}
    path.write_text(json.dumps(metadata))
    restarted = open_client()
    status = restarted.get(f"/api/dossiers/{dossier_id}/capture?phase=before").json()
    assert status["status"] == "failed" and status["error_code"] == "capture_interrupted"
    assert restarted.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"}).status_code == 202
    assert wait_capture(restarted, dossier_id, "before")["error_code"] == "capture_sides_incomplete"
    sample_photos(samples, ("rear_01.jpg", "left_01.jpg", "right_01.jpg"))
    assert restarted.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"}).status_code == 202
    assert wait_capture(restarted, dossier_id, "before")["saved_count"] == 4


def test_shutdown_retains_partial_capture_and_restart_resumes_same_photos(capture_environment, storage):
    samples, _ = capture_environment
    sample_photos(samples, ("front_01.jpg", "left_01.jpg", "rear_01.jpg", "right_01.jpg"))
    settings = ApiSettings(storage, tunnel_mode="mock", mock_photos_dir=samples, mock_photo_delay_seconds=2)
    with TestClient(create_app(settings, FixtureBackend())) as client:
        dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
        client.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            partial = client.get(f"/api/dossiers/{dossier_id}/capture?phase=before").json()
            if partial["saved_count"] == 1:
                break
            time.sleep(0.02)
        assert partial["saved_count"] == 1
    with TestClient(create_app(ApiSettings(storage, tunnel_mode="mock", mock_photos_dir=samples), FixtureBackend())) as restarted:
        interrupted = restarted.get(f"/api/dossiers/{dossier_id}/capture?phase=before").json()
        assert interrupted["status"] == "failed" and interrupted["saved_count"] == 1
        assert restarted.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"}).status_code == 202
        completed = wait_capture(restarted, dossier_id, "before")
        assert completed["status"] == "completed" and completed["saved_count"] == 4
        assert completed["capture_id"] == partial["capture_id"]
        assert partial["photos"][0] in completed["photos"]


@pytest.mark.parametrize("limit", ["bytes", "pixels"])
def test_capture_uses_bounded_decoder(capture_environment, monkeypatch, limit):
    samples, open_client = capture_environment
    sample_photos(samples, ("front_01.jpg",))
    monkeypatch.setattr(f"api.photo_service.{'MAX_UPLOAD_BYTES' if limit == 'bytes' else 'MAX_IMAGE_PIXELS'}", 100)
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "verification"}).json()["dossier_id"]
    assert client.post(f"/api/dossiers/{dossier_id}/capture", json={"phase": "before"}).status_code == 202
    state = wait_capture(client, dossier_id, "before")
    assert state["status"] == "failed" and state["saved_count"] == 0
    assert state["error_code"] == ("upload_too_large" if limit == "bytes" else "invalid_image")


def test_source_paths_and_capture_payload_reject_traversal(capture_environment):
    from api.errors import DossierError

    samples, open_client = capture_environment
    client = open_client()
    dossier_id = client.post("/api/dossiers", json={"type": "chiffrage_rapide"}).json()["dossier_id"]
    for source in ("../outside.jpg", "/tmp/outside.jpg", "front/../../outside.jpg", "front\\outside.jpg"):
        with pytest.raises(DossierError) as error:
            client.app.state.capture_service.provider.read_photo(source)
        assert error.value.code == "unsafe_sample_path"
    assert client.post(f"/api/dossiers/{dossier_id}/capture", json={"source": "../outside.jpg"}).status_code == 422
