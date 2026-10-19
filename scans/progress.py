from pathlib import Path
import time

from api.errors import DossierError
from api.files import validate_image, validate_pdf
from api.schemas import ScanProgress, StoredPhoto
from api.schemas import StoredAnalysis
from scans.artifacts import annotation_paths


# ============================================================
# PROGRESS FROM VALID FINAL FILES
# ============================================================

def inspect_progress(
    output_dir: Path, photos: list[StoredPhoto], started_at: float | None
) -> ScanProgress:
    ready = 0
    completed_photos = 0
    for photo in photos:
        photo_ready = 0
        for path in annotation_paths(output_dir, photo):
            if not path.is_file():
                continue
            try:
                validate_image(path)
                photo_ready += 1
            except DossierError:
                continue
        ready += photo_ready
        completed_photos += photo_ready == 3
    report_ready = False
    if (output_dir / "report.pdf").is_file():
        try:
            validate_pdf(output_dir / "report.pdf")
            report_ready = True
        except DossierError:
            pass

    # Artifact counts describe output readiness, not elapsed computation percentage.

    stage = "queued" if started_at is None else "preparing"
    if ready:
        stage = "finalizing" if completed_photos == len(photos) else "analyzing_photos"
    if report_ready and started_at is not None:
        stage = "finalizing"
    result_ready = False
    result_path = output_dir / "result.json"
    if result_path.is_file() and report_ready and ready == 3 * len(photos):
        try:
            result = StoredAnalysis.model_validate_json(result_path.read_bytes())
            result_ready = result.photos == photos
        except (OSError, ValueError):
            pass
    return ScanProgress(
        stage=stage, annotations_ready=ready, annotations_expected=3 * len(photos),
        photos_completed=completed_photos, photos_total=len(photos),
        elapsed_seconds=round(max(0, time.time() - started_at), 1) if started_at else 0,
        eta_seconds=None, report_ready=report_ready,
        result_ready=result_ready,
    )
