"""Verify pipeline reuse, registration-year mapping, and the runner's Type 3 guard.

Depends on optional engine/report dependencies. Models and detections are mocked,
while actual annotation, financial-result, and PDF publication still run.
"""

import json
from unittest.mock import Mock

from PIL import Image
import pytest

pytest.importorskip("cv2")
pytest.importorskip("reportlab")

import numpy as np

from api.schemas import StoredPhoto
from scans.contracts import ScanRequest
from scans.pricing_client import get_estimation
from scans.runner import analyze_dossier


# ============================================================
# SHARED PIPELINE AND AUTOESTIMATE CONTRACT
# ============================================================

def scan_request(tmp_path, category):
    directory = tmp_path / "DOS-TEST"
    (directory / "photos").mkdir(parents=True)
    Image.new("RGB", (20, 16), "white").save(directory / "photos/front.jpg")
    return ScanRequest(
        run_id="fixture", dossier_id="DOS-TEST", dossier_dir=directory,
        output_dir=directory / "analysis", models_dir=tmp_path / "models",
        metadata={"dossier_id": "DOS-TEST", "type": category, "marque_id": 12,
                  "model_id": 34, "marque": "Dacia", "modele": "Sandero",
                  "matricule": "TEST", "date_mise_en_circulation": "2020-12-31", "annee": "1999"},
        photos=[StoredPhoto(photo_id="front", original_path="photos/front.jpg",
                            parts_path="analysis/annotated/front_pieces.png",
                            damages_path="analysis/annotated/front_dommages.png",
                            critical_path="analysis/annotated/front_critical.png")],
        feedback=[], input_signatures={},
    )


@pytest.mark.parametrize("category", ["forfait_expertise", "chiffrage_rapide"])
def test_single_original_reuses_existing_pipeline_and_keeps_complete_date(tmp_path, monkeypatch, category):
    request = scan_request(tmp_path, category)
    models = Mock(return_value=(object(), object(), object()))
    pricing = Mock(side_effect=lambda instances, *args: instances)
    image = np.zeros((16, 20, 3), dtype=np.uint8)
    monkeypatch.setattr("scans.runner.load_models", models)
    monkeypatch.setattr("scans.runner.analyser", Mock(return_value={
        "img_pieces": image, "img_dommages": image, "img_zones_critiques": image, "instances": [],
    }))
    monkeypatch.setattr("scans.runner.enrichir_prix_pieces", pricing)
    summary = analyze_dossier(request)
    assert summary.photo_count == 1
    assert pricing.call_args.args[1:4] == (12, 34, 2020)
    result = json.loads((request.output_dir / "result.json").read_text())
    assert result["vehicle"]["date_mise_en_circulation"] == "2020-12-31"
    assert len(result["photos"]) == 1
    assert (request.output_dir / "report.pdf").is_file()


def test_verification_guard_runs_before_models_or_output_creation(tmp_path, monkeypatch):
    request = scan_request(tmp_path, "verification")
    models = Mock()
    monkeypatch.setattr("scans.runner.load_models", models)
    with pytest.raises(ValueError, match="disabled"):
        analyze_dossier(request)
    models.assert_not_called()
    assert not request.output_dir.exists()


def test_autoestimate_estimation_sends_supported_year_only(monkeypatch):
    monkeypatch.setenv("AUTOESTIMATE_BASE_URL", "http://pricing.test/api/v1")
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "fixture-token")
    response = Mock()
    response.json.return_value = {"data": {"estimation": {"median": 500}}}
    transport = Mock(return_value=response)
    monkeypatch.setattr("scans.pricing_client.requests.get", transport)
    assert get_estimation(12, 34, 1, "occasion", 2020)["median"] == 500
    assert transport.call_args.kwargs["params"] == {
        "marque_id": 12, "model_id": 34, "part_id": 1, "type": "occasion", "year": 2020,
    }
