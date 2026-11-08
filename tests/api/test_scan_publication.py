"""Regress the processing-to-completed read boundary without timing-dependent subprocesses.

Depends on saved result fixtures and repository read adapters; all artifacts are validated normally.
"""

import pytest
from conftest import write_json


@pytest.mark.parametrize("endpoint", ["/api/dossiers/DOS-001/result", "/api/dossiers"])
def test_completion_between_analysis_and_status_reads(client, complete_dossier, monkeypatch, endpoint):
    directory, _ = complete_dossier()
    write_json(directory / "analysis/status.json", {"managed_by": "cabek_scan_v1", "status": "completed"})
    from scans.progress import inspect_progress
    repository = client.app.state.dossier_service.repository
    dossier = repository.read_dossier("DOS-001")
    progress = inspect_progress(directory / "analysis", list(dossier.photos.values()), None)
    progress.stage = "completed"
    write_json(directory / "analysis/status.json", {
        "managed_by": "cabek_scan_v1", "status": "completed", "progress": progress.model_dump(),
    })
    read = repository.read_analysis
    first = True
    def published_after_first_read(dossier):
        nonlocal first
        if first:
            first = False
            return None
        return read(dossier)
    monkeypatch.setattr(repository, "read_analysis", published_after_first_read)
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    saved = response.json()
    if endpoint.endswith("/result"):
        assert saved["status"] == "completed"
        assert saved["result"]["financial"]["estimated_total"] == 500
    else:
        assert saved["counts"]["completed"] == 1
        assert saved["items"][0]["status"] == "completed"


def test_completion_marker_without_results_still_fails_validation(client, create_dossier):
    directory = create_dossier()
    write_json(directory / "analysis/status.json", {"managed_by": "cabek_scan_v1", "status": "completed"})
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_status"
