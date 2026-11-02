"""Verify server-side category/workflow/date filters, deterministic sorting, and counts.

Depends on isolated mixed-category fixtures; pagination assertions ensure filters
are applied before slicing and old scan-state counter semantics remain useful.
"""

import json

import pytest


def test_filter_sort_totals_and_category_counters(client, storage, create_dossier, new_dossier, upload_photo):
    create_dossier("LEGACY")
    quick_one = new_dossier(matricule="MATCH-1")["dossier_id"]
    quick_two = new_dossier(matricule="MATCH-2")["dossier_id"]
    verification = new_dossier("verification", matricule="MATCH-3")["dossier_id"]
    upload_photo(quick_two, "rear")
    for dossier_id, created in [(quick_one, "2026-01-01T10:00:00Z"),
                                (quick_two, "2026-02-01T10:00:00Z"),
                                (verification, "2026-03-01T10:00:00Z")]:
        path = storage / dossier_id / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata.update(created_at=created, updated_at=created)
        path.write_text(json.dumps(metadata))
    query = {"search": "match", "type": "chiffrage_rapide", "sort": "created_at_asc", "limit": 1}
    result = client.get("/api/dossiers", params=query).json()
    assert result["total"] == result["counts"]["all"] == 2
    assert result["type_counts"] == {"forfait_expertise": 0, "chiffrage_rapide": 2, "verification": 1}
    assert result["workflow_counts"] == {"awaiting_photos": 1, "ready_to_scan": 1}
    assert result["items"][0]["dossier_id"] == quick_one
    assert client.get("/api/dossiers", params={**query, "offset": 1}).json()["items"][0]["dossier_id"] == quick_two
    filtered = client.get("/api/dossiers", params={**query, "workflow_status": "ready_to_scan"}).json()
    assert filtered["total"] == filtered["counts"]["all"] == 1
    assert filtered["workflow_counts"]["awaiting_photos"] == 1
    assert filtered["items"][0]["dossier_id"] == quick_two
    assert client.get("/api/dossiers", params={**query, "offset": 9}).json()["total"] == 2
    assert client.get("/api/dossiers", params={**query, "offset": 9}).json()["items"] == []
    dates = client.get("/api/dossiers", params={
        "created_from": "2026-02-01", "created_to": "2026-03-01", "sort": "updated_at_desc",
    }).json()
    assert [item["dossier_id"] for item in dates["items"]] == [verification, quick_two]
    assert dates["total"] == 2
    ids = client.get("/api/dossiers", params={"sort": "id_asc"}).json()["items"]
    assert [item["dossier_id"] for item in ids] == sorted(item["dossier_id"] for item in ids)


@pytest.mark.parametrize("params", [
    {"type": "bad"}, {"workflow_status": "bad"}, {"sort": "bad"},
    {"created_from": "bad"}, {"created_from": "2026-02-01", "created_to": "2026-01-01"},
])
def test_invalid_filter_queries(client, params):
    assert client.get("/api/dossiers", params=params).status_code == 422
