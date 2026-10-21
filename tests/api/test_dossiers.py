from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.main import create_app
from conftest import write_json


def test_search_filter_counts_and_pagination(client, create_dossier, complete_dossier):
    create_dossier("DOS-001", marque="Toyota")
    create_dossier("DOS-002")
    complete_dossier("DOS-003")
    data = client.get("/api/dossiers", params={"status": "not_analyzed", "limit": 1}).json()
    assert data["total"] == 2
    assert data["counts"] == {
        "all": 3, "not_analyzed": 2, "queued": 0,
        "processing": 0, "completed": 1, "failed": 0,
    }
    assert [item["dossier_id"] for item in data["items"]] == ["DOS-002"]
    next_page = client.get("/api/dossiers", params={"status": "not_analyzed", "offset": 1}).json()
    assert [item["dossier_id"] for item in next_page["items"]] == ["DOS-001"]
    for search in ("toyOTA", "DOS-001"):
        matches = client.get("/api/dossiers", params={"search": search}).json()
        assert matches["total"] == matches["counts"]["all"] == 1
        assert matches["items"][0]["dossier_id"] == "DOS-001"
    assert client.get("/api/dossiers", params={"search": "logan"}).json()["total"] == 3
    assert client.get("/api/dossiers", params={"search": "test-123"}).json()["total"] == 3
    assert client.get("/api/dossiers", params={"search": "unmatched"}).json()["items"] == []


def test_existing_legacy_folder_is_not_analyzed(client, create_dossier):
    create_dossier(legacy_paths=True)
    data = client.get("/api/dossiers/DOS-001/result").json()
    assert data == {
        "dossier_id": "DOS-001", "status": "not_analyzed", "progress": None,
        "error": None, "result": None,
    }
    assert client.get("/api/dossiers").json()["items"][0]["photo_count"] == 1
    assert client.get("/api/dossiers/DOS-001/media/photos/avant_01.jpg").status_code == 200


def test_saved_result_urls_media_report_and_restart(client, complete_dossier, storage):
    complete_dossier()
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert result["result"]["financial"]["estimated_total"] == 500
    for key, url in result["result"]["photos"][0].items():
        if key != "photo_id":
            media = client.get(url)
            assert media.status_code == 200
            assert media.headers["content-type"] in {"image/jpeg", "image/png"}
            assert media.headers["x-content-type-options"] == "nosniff"
    report = client.get(result["result"]["report_url"])
    assert report.status_code == 200
    assert report.headers["content-type"] == "application/pdf"
    assert 'filename="rapport_DOS-001.pdf"' in report.headers["content-disposition"]
    with TestClient(create_app(ApiSettings(storage))) as restarted:
        assert restarted.get("/api/dossiers/DOS-001/result").json() == result


def test_completed_scan_can_have_incomplete_finances(client, complete_dossier):
    complete_dossier(incomplete=True)
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["result"]["financial"]["estimated_total"] is None
    assert data["result"]["financial"]["warnings"]


@pytest.mark.parametrize("change", ["false_complete", "wrong_total", "missing_warning", "wrong_subtotal"])
def test_rejects_misleading_financial_results(client, complete_dossier, change):
    directory, result = complete_dossier(incomplete=change != "wrong_total")
    finances = result["financial"]
    if change == "false_complete":
        finances["status"] = "complete"
    elif change == "wrong_total":
        finances["estimated_total"] = 1
    elif change == "wrong_subtotal":
        finances["known_subtotal"] = 1
    else:
        finances["warnings"] = []
    write_json(directory / "analysis/result.json", result)
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_result"


@pytest.mark.parametrize("state", ["queued", "processing", "failed"])
def test_reads_saved_states_without_inventing_progress(client, create_dossier, state):
    directory = create_dossier()
    write_json(directory / "analysis/status.json", {
        "status": state, "progress": {"percent": 80}, "error": "/private/secret/token",
    })
    response = client.get("/api/dossiers/DOS-001/result")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == state
    assert data["progress"] is None
    assert "secret" not in response.text
    assert client.get("/api/dossiers", params={"status": state}).json()["total"] == 1


@pytest.mark.parametrize("state", ["completed", "unknown", {}, None])
def test_rejects_invalid_or_unbacked_status(client, create_dossier, state):
    directory = create_dossier()
    write_json(directory / "analysis/status.json", {"status": state})
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_malformed_metadata_is_reported_without_breaking_listing(client, create_dossier):
    create_dossier("DOS-001")
    directory = create_dossier("DOS-002")
    (directory / "metadata.json").write_text("{broken")
    data = client.get("/api/dossiers").json()
    assert data["total"] == 1
    assert data["warnings"][0]["code"] == "invalid_metadata"
    assert client.get("/api/dossiers/DOS-002/result").status_code == 422


def test_missing_original_is_not_counted(client, create_dossier):
    directory = create_dossier()
    (directory / "photos/avant_01.jpg").unlink()
    assert client.get("/api/dossiers").json()["items"][0]["photo_count"] == 0
    assert client.get("/api/dossiers/DOS-001/media/photos/avant_01.jpg").status_code == 404


@pytest.mark.parametrize("artifact", [
    "analysis/report.pdf", "analysis/annotated/avant_01_critical.png", "photos/avant_01.jpg",
])
def test_final_result_requires_all_artifacts(client, complete_dossier, artifact):
    directory, _ = complete_dossier()
    (directory / artifact).unlink()
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_result_must_match_dossier_and_photo_manifest(client, complete_dossier):
    directory, result = complete_dossier()
    result["dossier_id"] = "DOS-OTHER"
    write_json(directory / "analysis/result.json", result)
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422
    result["dossier_id"] = "DOS-001"
    result["photos"][0]["parts_path"] = "analysis/annotated/unexpected.png"
    write_json(directory / "analysis/result.json", result)
    assert client.get("/api/dossiers/DOS-001/result").status_code == 422


def test_temporary_output_is_not_a_completed_result(client, create_dossier):
    directory = create_dossier()
    write_json(directory / "analysis/result.json.tmp", {"status": "completed"})
    assert client.get("/api/dossiers/DOS-001/result").json()["status"] == "not_analyzed"
    assert client.get("/api/dossiers/DOS-001/report").status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "bad"}])
def test_query_validation_uses_error_envelope(client, params):
    response = client.get("/api/dossiers", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_missing_and_empty_storage_are_distinct(storage):
    with TestClient(create_app(ApiSettings(storage))) as client:
        assert client.get("/api/dossiers").json()["total"] == 0
    missing = storage / "missing"
    with TestClient(create_app(ApiSettings(missing))) as client:
        assert client.get("/api/dossiers").status_code == 503
    assert not missing.exists()


def test_unknown_dossier_and_scan_route_documentation(client):
    assert client.get("/api/dossiers/DOS-UNKNOWN/result").status_code == 404
    assert client.post("/api/dossiers/DOS-001/scan").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert len(paths) == 9
    assert "get" in paths["/api/dossiers/{dossier_id}/photos"]
    assert "post" in paths["/api/dossiers/{dossier_id}/scan"]
    assert "put" in paths["/api/dossiers/{dossier_id}/vehicle"]
    assert all("get" in methods for path, methods in paths.items() if not path.endswith(("/scan", "/vehicle")))


def test_cors_is_explicit_and_exposes_pdf_filename(storage):
    app = create_app(ApiSettings(storage, ("http://localhost:5173",)))
    with TestClient(app) as client:
        allowed = client.get("/api/dossiers", headers={"Origin": "http://localhost:5173"})
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "Content-Disposition" in allowed.headers["access-control-expose-headers"]
        denied = client.get("/api/dossiers", headers={"Origin": "http://unapproved.example"})
        assert "access-control-allow-origin" not in denied.headers
