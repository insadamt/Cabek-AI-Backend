"""Verify the pricing snapshot protects vehicle edits without weakening image safety.

Depends on temporary metadata and the real fingerprint/gate implementations;
only AutoEstimate resolution is replaced for deterministic local validation.
"""

from fastapi.testclient import TestClient
from filelock import FileLock
import pytest

from api.config import ApiSettings
from api.main import create_app
from api.repository import DossierRepository
from api.vehicle_catalog import ResolvedVehicle
from scans.artifacts import input_signatures, publish_json
from scans.contracts import ScanRequest
from scans.metadata_gate import await_pricing_metadata, validate_pricing_snapshot


def test_vehicle_edit_window_closes_atomically_before_pricing(tmp_path, monkeypatch):
    directory = tmp_path / "DOS-GATE"
    output = directory / "analysis"
    output.mkdir(parents=True)
    metadata = {"dossier_id": "DOS-GATE", "type": "chiffrage_rapide", "marque_id": 12, "model_id": 34,
                "marque": "Dacia", "modele": "Sandero", "date_mise_en_circulation": "2020-05-17",
                "matricule": "84654-ب-454", "photos": []}
    publish_json(directory / "metadata.json", metadata)
    request = ScanRequest(run_id="run-1", dossier_id="DOS-GATE", dossier_dir=directory, output_dir=output,
                          models_dir=tmp_path, metadata={}, photos=[], feedback=[], input_signatures={},
                          allow_metadata_updates=True)
    monkeypatch.setattr("api.vehicle_catalog.VehicleCatalog.resolve", lambda self, selection: ResolvedVehicle(
        **selection.model_dump(), marque="Dacia", modele="Sandero",
    ))
    await_pricing_metadata(request)
    assert request.metadata["matricule"] == "84654-ب-454"
    publish_json(output / "status.json", {"status": "processing", "managed_by": "cabek_scan_v1"})
    repository = DossierRepository(tmp_path)
    validate_pricing_snapshot(request, repository.read_dossier("DOS-GATE"))
    with FileLock(str(output / ".scan.lock")), TestClient(create_app(ApiSettings(tmp_path))) as client:
        response = client.patch("/api/dossiers/DOS-GATE/metadata", json={"matricule": "14554-A-123"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "pricing_in_progress"
    metadata["matricule"] = "14554-A-123"
    publish_json(directory / "metadata.json", metadata)
    with pytest.raises(ValueError, match="pricing"):
        validate_pricing_snapshot(request, repository.read_dossier("DOS-GATE"))


def test_quick_fingerprints_allow_vehicle_edits_and_detect_photo_or_feedback_changes(tmp_path):
    from PIL import Image

    directory = tmp_path / "DOS-SIGN"
    (directory / "photos").mkdir(parents=True)
    original = directory / "photos/front.jpg"
    Image.new("RGB", (12, 12), "blue").save(original)
    metadata = {"dossier_id": "DOS-SIGN", "type": "chiffrage_rapide", "matricule": "",
                "photos": [{"path": "photos/front.jpg", "side": "front"}]}
    publish_json(directory / "metadata.json", metadata)
    repository = DossierRepository(tmp_path)
    before = input_signatures(repository.read_dossier("DOS-SIGN"))
    metadata.update(matricule="84654-ب-454", updated_at="now", workflow_status="awaiting_metadata")
    publish_json(directory / "metadata.json", metadata)
    assert input_signatures(repository.read_dossier("DOS-SIGN")) == before
    Image.new("RGB", (12, 12), "red").save(original)
    assert input_signatures(repository.read_dossier("DOS-SIGN")) != before
    publish_json(directory / "feedback.json", {"unexpected": "change"})
    assert "feedback.json" in input_signatures(repository.read_dossier("DOS-SIGN"))
