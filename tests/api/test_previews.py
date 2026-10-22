from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api.config import ApiSettings
from api.main import create_app
from api.previews import encode_cached_preview


def test_webp_preview_resizes_without_changing_source(client, create_dossier):
    directory = create_dossier()
    source = directory / "photos/avant_01.jpg"
    Image.new("RGB", (2400, 1800), "blue").save(source)
    before = source.read_bytes()
    photo = client.get("/api/dossiers/DOS-001/result").json()["photos"][0]
    response = client.get(photo["preview_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    with Image.open(BytesIO(response.content)) as image:
        assert image.format == "WEBP"
        assert image.size == (1600, 1200)
    assert len(response.content) < len(before)
    assert source.read_bytes() == before
    assert client.get(photo["original_url"]).content == before
    assert not (directory / "analysis").exists()


def test_cached_previews_revalidate_and_refresh_when_source_changes(client, create_dossier):
    directory = create_dossier()
    encode_cached_preview.cache_clear()
    url = "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp"
    first = client.get(url)
    hits_before = encode_cached_preview.cache_info().hits
    cached = client.get(url, headers={"If-None-Match": f'"other", W/{first.headers["etag"]}'})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["cache-control"] == "private, no-cache"
    assert encode_cached_preview.cache_info().hits == hits_before + 1
    Image.new("RGB", (16, 12), "red").save(directory / "photos/avant_01.jpg")
    changed = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200
    assert changed.headers["etag"] != first.headers["etag"]
    assert changed.content != first.content
    (directory / "photos/avant_01.jpg").unlink()
    assert client.get(url, headers={"If-None-Match": "*"}).status_code == 404


@pytest.mark.parametrize("path", [
    "metadata.json.webp", "photos/undeclared.jpg.webp", "photos/avant_01.jpg",
    "photos%5cavant_01.jpg.webp", "photos/%2e%2e/metadata.json.webp",
])
def test_preview_uses_existing_media_allowlist(client, create_dossier, path):
    create_dossier()
    assert client.get(f"/api/dossiers/DOS-001/preview/{path}").status_code == 404


def test_invalid_source_cannot_use_cached_preview(client, create_dossier):
    directory = create_dossier()
    url = "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp"
    assert client.get(url).status_code == 200
    (directory / "photos/avant_01.jpg").write_bytes(b"broken")
    assert client.get(url, headers={"If-None-Match": "*"}).status_code == 422


def test_preview_symlink_cannot_escape_dossier(client, create_dossier, storage):
    directory = create_dossier()
    url = "/api/dossiers/DOS-001/preview/photos/avant_01.jpg.webp"
    assert client.get(url).status_code == 200
    outside = storage.parent / "private.jpg"
    Image.new("RGB", (16, 12)).save(outside)
    source = directory / "photos/avant_01.jpg"
    source.unlink()
    source.symlink_to(outside)
    assert client.get(url).status_code in {404, 422}


def test_completed_preview_urls_include_proxy_prefix_and_do_not_upscale(complete_dossier, storage):
    complete_dossier()
    with TestClient(create_app(ApiSettings(storage)), root_path="/service") as client:
        data = client.get("/api/dossiers/DOS-001/result").json()
        urls = [data["photos"][0]["preview_url"]]
        urls.extend(value for key, value in data["result"]["photos"][0].items() if key.endswith("_preview_url"))
        assert len(urls) == 5
        for url in urls:
            assert url.startswith("/service/api/dossiers/DOS-001/preview/")
            response = client.get(url)
            assert response.status_code == 200
            with Image.open(BytesIO(response.content)) as image:
                assert image.size == (16, 12)
