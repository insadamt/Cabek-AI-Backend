"""Validate bounded JPEG/PNG uploads and publish revisioned photo evidence.

Depends on workflow edit locks, Pillow, and atomic file publishers. User filenames
never determine storage paths; replaced bytes are retained outside the active manifest.
"""

from io import BytesIO
from pathlib import Path
from uuid import uuid4
import warnings

from fastapi import UploadFile
from PIL import Image

from api.errors import DossierError
from api.files import IMAGE_MEDIA_TYPES, IMAGE_TYPES, confined_path, read_json_object
from api.repository import DossierRecord
from api.workflow_schemas import PhotoPhase, PhotoSide
from api.workflow_service import WorkflowService, timestamp
from api.workflow_state import SIDES, available_photo_records
from scans.artifacts import publish_bytes


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


# ============================================================
# BOUNDED INPUT VALIDATION
# ============================================================

def read_validated_upload(upload: UploadFile) -> tuple[bytes, str]:
    filename = upload.filename or ""
    if not filename or any(character in filename for character in ("/", "\\", ":", "\x00")):
        raise DossierError(422, "invalid_filename", "Use a plain JPEG or PNG filename.")
    extension = Path(filename).suffix.lower()
    if extension not in IMAGE_TYPES or upload.content_type != IMAGE_MEDIA_TYPES[extension]:
        raise DossierError(422, "unsupported_image", "Upload a JPEG or PNG with matching content type and extension.")
    content = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise DossierError(413, "upload_too_large", "Images must not exceed 10 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != IMAGE_TYPES[extension] or image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Unsupported image content.")
                image.verify()
            with Image.open(BytesIO(content)) as image:

                # Decode once so truncated compressed data cannot enter an otherwise valid manifest.

                image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise DossierError(422, "invalid_image", "Upload a valid single-frame image of at most 20 megapixels.") from error
    return content, extension


# ============================================================
# PHOTO SLOT RULES AND EXPLICIT REPLACEMENT
# ============================================================

class PhotoService:
    def __init__(self, workflows: WorkflowService):
        self.workflows = workflows

    def upload_photo(self, dossier_id: str, upload: UploadFile, side: PhotoSide,
                     phase: PhotoPhase | None, replace_photo_id: str | None = None) -> None:
        content, extension = read_validated_upload(upload)
        with self.workflows.editable_dossier(dossier_id) as dossier:
            metadata = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
            photos = metadata.setdefault("photos", [])
            previous = None
            if replace_photo_id is not None:
                previous = next((photo for photo in photos if Path(photo["path"]).stem == replace_photo_id), None)
                if previous is None:
                    raise DossierError(404, "photo_not_found", "Photo not found.")
                if (previous.get("side"), previous.get("phase")) != (side, phase):
                    raise DossierError(422, "invalid_photo_slot", "Replacement must use the original side and phase.")
            if dossier.metadata.type == "verification":
                self.validate_verification_slot(dossier, photos, side, phase, previous)
            elif phase is not None:
                raise DossierError(422, "invalid_photo_phase", "Quick estimates do not use before/after phases.")
            photo_id = uuid4().hex
            relative_path = f"photos/{phase + '/' if phase else ''}{photo_id}{extension}"
            target = confined_path(dossier.directory, relative_path)
            record = {"path": relative_path, "photo_id": photo_id, "side": side,
                      "phase": phase, "uploaded_at": timestamp()}
            if previous is not None:

                # Old bytes remain immutable; only an explicit replacement changes the active pointer.

                photos.remove(previous)
                metadata.setdefault("photo_history", []).append({
                    **previous, "replaced_at": timestamp(), "replaced_by": photo_id,
                })
            photos.append(record)
            publish_bytes(target, content)
            try:
                self.workflows.save_metadata(dossier, metadata)
            except Exception:

                # A failed metadata commit must leave the old manifest and evidence usable.

                target.unlink(missing_ok=True)
                raise

    def validate_verification_slot(self, dossier: DossierRecord, photos: list[dict],
                                   side: PhotoSide, phase: PhotoPhase | None,
                                   previous: dict | None) -> None:
        if phase is None:
            raise DossierError(422, "invalid_photo_phase", "Verification photos require before or after.")
        available = available_photo_records(dossier)
        if phase == "after" and {photo.side for photo in available if photo.phase == "before"} != set(SIDES):
            raise DossierError(409, "before_photos_incomplete", "Complete all four before sides first.")
        if phase == "before" and any(photo.get("phase") == "after" for photo in photos):
            raise DossierError(409, "evidence_finalized", "Before evidence is immutable once after collection starts.")
        if previous is None and any(photo.get("phase") == phase and photo.get("side") == side for photo in photos):
            raise DossierError(409, "photo_slot_occupied", "Use explicit replacement for an occupied phase/side slot.")
