from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, ValidationError

from api.errors import DossierError
from api.files import (
    DOSSIER_ID_PATTERN,
    IMAGE_TYPES,
    confined_path,
    normalize_relative_path,
    read_json_object,
    validate_image,
    validate_pdf,
)
from api.schemas import ScanState, StoredAnalysis, StoredPhoto, Vehicle


class PhotoMetadata(BaseModel):
    path: str


class DossierMetadata(Vehicle):
    dossier_id: str
    created_at: str | None = None
    photos: list[PhotoMetadata] = Field(default_factory=list)


@dataclass(frozen=True)
class DossierRecord:
    directory: Path
    metadata: DossierMetadata
    photos: dict[str, StoredPhoto]


class DossierRepository:
    def __init__(self, dossiers_dir: Path):
        self.dossiers_dir = dossiers_dir.resolve()

    def dossier_ids(self) -> list[str]:
        self.validate_root()
        try:
            return sorted(
                (
                    folder.name
                    for folder in self.dossiers_dir.iterdir()
                    if not folder.name.startswith(".") and folder.is_dir()
                ),
                reverse=True,
            )
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Dossier storage is unavailable.") from error

    def validate_root(self) -> None:
        if not self.dossiers_dir.is_dir():
            raise DossierError(503, "storage_unavailable", "Dossier storage is unavailable.")

    def read_dossier(self, dossier_id: str) -> DossierRecord:
        if not DOSSIER_ID_PATTERN.fullmatch(dossier_id):
            raise DossierError(422, "invalid_dossier_id", "Invalid dossier identifier.")
        self.validate_root()
        directory = confined_path(self.dossiers_dir, dossier_id)
        metadata_path = confined_path(directory, "metadata.json")
        if not directory.is_dir() or not metadata_path.is_file():
            raise DossierError(404, "dossier_not_found", "Dossier not found.")
        try:
            metadata = DossierMetadata.model_validate(
                read_json_object(metadata_path, "invalid_metadata")
            )
            if metadata.dossier_id != dossier_id:
                raise ValueError("Dossier identity does not match its folder.")
            photos = self.photo_manifest(directory, metadata)
        except (ValidationError, ValueError) as error:
            raise DossierError(422, "invalid_metadata", "Dossier metadata is invalid.") from error
        return DossierRecord(directory, metadata, photos)

    def photo_manifest(
        self, directory: Path, metadata: DossierMetadata
    ) -> dict[str, StoredPhoto]:
        photos = {}
        for photo in metadata.photos:
            try:
                original = normalize_relative_path(photo.path, legacy=True)
                path = PurePosixPath(original)
                if len(path.parts) != 2 or path.parts[0] != "photos":
                    raise ValueError("Original must be directly under photos.")
                if path.suffix.lower() not in IMAGE_TYPES or path.stem in photos:
                    raise ValueError("Unsupported image or duplicate photo ID.")
                confined_path(directory, original)
            except DossierError as error:
                raise DossierError(422, "invalid_metadata", "Dossier photo paths are invalid.") from error
            prefix = f"analysis/annotated/{path.stem}"
            photos[path.stem] = StoredPhoto(
                photo_id=path.stem,
                original_path=original,
                parts_path=f"{prefix}_pieces.png",
                damages_path=f"{prefix}_dommages.png",
                critical_path=f"{prefix}_critical.png",
            )
        return photos

    def read_analysis(self, dossier: DossierRecord) -> StoredAnalysis | None:
        path = confined_path(dossier.directory, "analysis/result.json")
        if not path.exists():
            return None
        try:
            analysis = StoredAnalysis.model_validate(read_json_object(path, "invalid_result"))
            if analysis.dossier_id != dossier.metadata.dossier_id:
                raise ValueError("Result identity mismatch.")
            expected = dossier.photos
            actual = {photo.photo_id: photo for photo in analysis.photos}
            if len(actual) != len(analysis.photos) or actual != expected:
                raise ValueError("Result photo manifest mismatch.")
            for photo in analysis.photos:
                for field, relative_path in photo.model_dump().items():
                    if field != "photo_id":
                        artifact = confined_path(dossier.directory, relative_path)
                        if not artifact.is_file():
                            raise ValueError("Required artifact missing.")
                        validate_image(artifact)
            report = confined_path(dossier.directory, "analysis/report.pdf")
            if not report.is_file():
                raise ValueError("Required report missing.")
            validate_pdf(report)
            return analysis
        except (ValidationError, ValueError, DossierError) as error:
            raise DossierError(422, "invalid_result", "Saved analysis or its artifacts are invalid.") from error

    def read_state(self, dossier: DossierRecord, analysis: StoredAnalysis | None) -> ScanState:
        if analysis is not None:
            return "completed"
        path = confined_path(dossier.directory, "analysis/status.json")
        if not path.exists():
            return "not_analyzed"
        state = read_json_object(path, "invalid_status").get("status")
        if not isinstance(state, str) or state not in {"not_analyzed", "queued", "processing", "failed"}:
            raise DossierError(422, "invalid_status", "Saved scan status is invalid.")
        return state

    def media_path(self, dossier: DossierRecord, requested_path: str) -> Path:
        relative_path = normalize_relative_path(requested_path)
        allowlist = {
            value
            for photo in dossier.photos.values()
            for key, value in photo.model_dump().items()
            if key != "photo_id"
        }
        if relative_path not in allowlist:
            raise DossierError(404, "artifact_not_found", "Artifact not found.")
        path = confined_path(dossier.directory, relative_path)
        if not path.is_file():
            raise DossierError(404, "artifact_not_found", "Artifact not found.")
        validate_image(path)
        return path
