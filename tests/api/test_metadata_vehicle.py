import json

import pytest

from api.config import ApiSettings
from api.errors import DossierError
from api.repository import DossierRepository
from api.service import DossierService
from conftest import write_json
from scans.jobs import ScanManager


# ============================================================
# LEGACY METADATA COMPATIBILITY AND RECOVERY DIAGNOSTICS
# ============================================================

def test_null_names_are_read_without_rewriting_metadata(storage, create_dossier):
    directory = create_dossier()
    path = directory / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(marque=None, modele=None, marque_id=10, model_id=75)
    write_json(path, metadata)
    before = path.read_bytes()

    response = DossierService(DossierRepository(storage)).read_result("DOS-001")

    assert response.vehicle.marque == response.vehicle.modele == ""
    assert response.vehicle.marque_id == 10
    assert response.vehicle.model_id == 75
    assert response.status == "not_analyzed"
    assert path.read_bytes() == before


@pytest.mark.parametrize("fields", [
    {"marque": 10, "modele": 75},
    {"marque_id": 10, "modele_id": 75},
    {"marque_id": 10},
])
def test_misplaced_or_partial_ids_remain_invalid(storage, create_dossier, fields):
    path = create_dossier() / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(fields)
    write_json(path, metadata)

    with pytest.raises(DossierError) as caught:
        DossierRepository(storage).read_dossier("DOS-001")

    assert caught.value.code == "invalid_metadata"


def test_recovery_warning_identifies_invalid_metadata(storage, create_dossier, caplog):
    path = create_dossier() / "metadata.json"
    metadata = json.loads(path.read_text())
    metadata.update(marque_id=10)
    write_json(path, metadata)
    manager = ScanManager(ApiSettings(storage))
    try:
        manager.recover_interrupted_jobs()
    finally:
        manager.executor.shutdown(wait=True)

    assert "Could not recover saved scan state for DOS-001: invalid_metadata" in caplog.text
