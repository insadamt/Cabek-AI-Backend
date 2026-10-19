from PIL import Image

from api.repository import DossierRepository
from scans.progress import inspect_progress


def test_progress_ignores_temporary_invalid_and_unrelated_images(create_dossier, storage, tmp_path):
    create_dossier()
    dossier = DossierRepository(storage).read_dossier("DOS-001")
    output = tmp_path / "analysis"
    annotated = output / "annotated"
    annotated.mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(annotated / "avant_01_pieces.png")
    Image.new("RGB", (4, 4)).save(annotated / "unrelated.png")
    Image.new("RGB", (4, 4)).save(annotated / "avant_01_dommages.png.tmp", format="PNG")
    (annotated / "avant_01_critical.png").write_bytes(b"unfinished")
    (output / "result.json").write_text("{unfinished")
    progress = inspect_progress(output, list(dossier.photos.values()), None)
    assert progress.annotations_ready == 1
    assert progress.annotations_expected == 3
    assert progress.photos_completed == 0
    assert progress.result_ready is False


def test_scan_rejects_annotation_storage_symlink(client, create_dossier, tmp_path):
    directory = create_dossier()
    (directory / "analysis").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "analysis/annotated").symlink_to(outside, target_is_directory=True)
    response = client.post("/api/dossiers/DOS-001/scan")
    assert response.status_code == 422
    assert list(outside.iterdir()) == []
