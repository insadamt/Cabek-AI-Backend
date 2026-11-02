"""Derive workflow progress from saved metadata, photos, and existing scan state.

Depends on dossier records and confined storage reads; never changes metadata.
"""

from typing import TYPE_CHECKING

from api.files import confined_path, validate_image
from api.errors import DossierError
from api.schemas import ComparisonSlot, VerificationComparison

if TYPE_CHECKING:
    from api.repository import DossierRecord


SIDES = ("front", "rear", "left", "right")


# ============================================================
# AVAILABLE EVIDENCE AND CATEGORY-SPECIFIC PROGRESS
# ============================================================

def available_photo_records(dossier: "DossierRecord") -> list:
    available = []
    for photo in dossier.metadata.photos:
        try:
            validate_image(confined_path(dossier.directory, photo.path.replace("\\", "/")))
        except DossierError:
            continue
        available.append(photo)
    return available


def vehicle_is_complete(metadata) -> bool:
    return bool(metadata.marque_id and metadata.model_id and metadata.matricule.strip()
                and metadata.date_mise_en_circulation)


def workflow_status(dossier: "DossierRecord", scan_state: str) -> str:
    metadata = dossier.metadata
    if metadata.type == "forfait_expertise":
        return scan_state
    captures = list(metadata.captures.values())
    if any(capture.get("status") == "capturing" for capture in captures):
        return "capturing"
    if any(capture.get("status") == "failed" for capture in captures):
        return "capture_failed"
    if metadata.type == "chiffrage_rapide":
        if scan_state in ("queued", "processing"):
            from api.files import read_json_object
            status = read_json_object(dossier.directory / "analysis/status.json", "invalid_status")
            if status.get("progress", {}).get("stage") == "awaiting_metadata":
                return "awaiting_metadata"
            return "analyzing"
        if scan_state == "completed":
            return "estimated"
        if scan_state == "failed":
            return "failed"
        if not vehicle_is_complete(metadata):
            return "awaiting_metadata" if available_photo_records(dossier) else "draft"
        return "ready_to_scan" if available_photo_records(dossier) else "awaiting_photos"
    photos = available_photo_records(dossier)
    if not photos:
        return "draft" if not vehicle_is_complete(metadata) else "before_incomplete"
    before = {photo.side for photo in photos if photo.phase == "before"}
    after = {photo.side for photo in photos if photo.phase == "after"}
    if before != set(SIDES):
        return "before_incomplete"
    if not after:
        return "awaiting_after"
    return "comparison_ready" if after == set(SIDES) else "after_incomplete"


def verification_comparison(photos: list) -> VerificationComparison:
    indexed = {(photo.phase, photo.side): photo.model_dump() for photo in photos}
    return VerificationComparison(
        slots=[ComparisonSlot(side=side, before=indexed.get(("before", side)),
                              after=indexed.get(("after", side))) for side in SIDES],
        missing_before=[side for side in SIDES if ("before", side) not in indexed],
        missing_after=[side for side in SIDES if ("after", side) not in indexed],
    )
