import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
import pytest

from api.config import ApiSettings
from api.main import create_app


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def storage(tmp_path):
    root = tmp_path / "dossiers"
    root.mkdir()
    return root


@pytest.fixture
def client(storage):
    with TestClient(create_app(ApiSettings(storage))) as api_client:
        yield api_client


@pytest.fixture
def create_dossier(storage):
    def create(dossier_id="DOS-001", *, marque="Dacia", legacy_paths=False):
        directory = storage / dossier_id
        (directory / "photos").mkdir(parents=True)
        Image.new("RGB", (16, 12), "white").save(directory / "photos/avant_01.jpg")
        write_json(directory / "metadata.json", {
            "dossier_id": dossier_id, "marque": marque, "modele": "Logan",
            "matricule": "TEST-123", "status": "À valider",
            "photos": [{"path": "photos\\avant_01.jpg" if legacy_paths else "photos/avant_01.jpg"}],
        })
        return directory
    return create


@pytest.fixture
def complete_dossier(create_dossier):
    def complete(dossier_id="DOS-001", *, incomplete=False):
        directory = create_dossier(dossier_id)
        annotated = directory / "analysis/annotated"
        annotated.mkdir(parents=True)
        for suffix in ("pieces", "dommages", "critical"):
            Image.new("RGB", (16, 12), "blue").save(annotated / f"avant_01_{suffix}.png")
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(directory / "analysis/report.pdf")
        result = {
            "schema_version": 1, "dossier_id": dossier_id,
            "vehicle": {"marque": "Dacia", "modele": "Logan", "matricule": "TEST-123"},
            "financial": {
                "status": "incomplete" if incomplete else "complete", "currency": "MAD",
                "estimated_total": None if incomplete else 500, "known_subtotal": 500,
                "breakdown": {
                    "repair_labor": 100, "paint_labor": 100, "paint_materials": 100,
                    "replacement_labor": 200, "parts_supply": None if incomplete else 0,
                },
                "warnings": ["Part price unavailable."] if incomplete else [],
            },
            "parts": [{"name": "Front bumper", "action": "replace"}], "detections": [],
            "photos": [{
                "photo_id": "avant_01", "original_path": "photos/avant_01.jpg",
                "parts_path": "analysis/annotated/avant_01_pieces.png",
                "damages_path": "analysis/annotated/avant_01_dommages.png",
                "critical_path": "analysis/annotated/avant_01_critical.png",
            }],
        }
        write_json(directory / "analysis/result.json", result)
        return directory, result
    return complete
