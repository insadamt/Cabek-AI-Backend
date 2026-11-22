"""Read legacy and typed dossiers and validate allowlisted original/analysis files.

Depends on file confinement, Pydantic schemas, and saved artifacts; reads never migrate data.
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from api.errors import DossierError
from api.econstat_contracts import EconstatAttachment
from api.files import (
    DOSSIER_ID_PATTERN,
    IMAGE_TYPES,
    confined_path,
    normalize_relative_path,
    read_json_object,
    validate_image,
    validate_pdf,
)
from api.schemas import ScanProgress, ScanState, StoredAnalysis, StoredPhoto, Vehicle
from api.workflow_schemas import DossierType, PhotoSide, PhotoPhase, validate_registration_date


# ============================================================
# METADATA AND EXPECTED ARTIFACTS
# ============================================================

class PhotoMetadata(BaseModel):
    path: str
    side: PhotoSide | None = None
    phase: PhotoPhase | None = None
    uploaded_at: str | None = None
    capture_id: str | None = None
    original_filename: str | None = None
    source: str | None = None
    camera_channel: int | None = None
    camera_name: str | None = None
    camera_role: str | None = None
    capture_source: str | None = None
    frame_id: str | None = None


class DossierMetadata(Vehicle):
    imported_capture_resume: bool = True
    demo: bool = False
    demo_provenance: dict | None = None
    dossier_id: str
    type: DossierType = "forfait_expertise"
    source: str = "external"
    created_at: str | None = None
    updated_at: str | None = None
    before_finalized_at: str | None = None
    evidence_finalized_at: str | None = None
    photos: list[PhotoMetadata] = Field(default_factory=list)
    captures: dict[str, dict] = Field(default_factory=dict)
    econstat: EconstatAttachment | None = None

    @field_validator("marque", "modele", mode="before")
    @classmethod
    def normalize_missing_vehicle_names(cls, value):

        # Legacy metadata uses null for unknown names; IDs remain the source of vehicle identity.

        return "" if value is None else value

    @model_validator(mode="after")
    def validate_vehicle_and_photo_identity(self):
        if (self.marque_id is None) != (self.model_id is None):
            raise ValueError("Vehicle IDs must be provided together.")
        if self.date_mise_en_circulation is not None:
            validate_registration_date(self.date_mise_en_circulation)
        if self.type != "forfait_expertise":
            for photo in self.photos:
                if photo.side is None:
                    raise ValueError("Workflow photos require a side.")
                if self.type == "verification":
                    if photo.phase is None:
                        raise ValueError("Verification requires before/after phases.")
                elif photo.phase is not None:
                    raise ValueError("Quick estimates have no before/after phase.")
        return self


@dataclass(frozen=True)
class DossierRecord:
    directory: Path
    metadata: DossierMetadata
    photos: dict[str, StoredPhoto]


# ============================================================
# DOSSIER STORAGE ACCESS
# ============================================================

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

            # Prevent copied or misplaced metadata from being served under another dossier ID.

            if metadata.dossier_id != dossier_id:
                raise ValueError("Dossier identity does not match its folder.")
            photos = self.photo_manifest(directory, metadata)
        except (ValidationError, ValueError) as error:
            raise DossierError(422, "invalid_metadata", "Dossier metadata is invalid.") from error
        return DossierRecord(directory, metadata, photos)

    # ============================================================
    # EXPECTED PHOTO OUTPUTS
    # ============================================================

    def photo_manifest(
        self, directory: Path, metadata: DossierMetadata
    ) -> dict[str, StoredPhoto]:
        photos = {}
        for photo in metadata.photos:
            try:
                original = normalize_relative_path(photo.path, legacy=True)
                path = PurePosixPath(original)
                verification_path = (metadata.type == "verification" and len(path.parts) == 3
                                     and path.parts[:2] == ("photos", photo.phase))
                if not verification_path and (metadata.type == "verification" or len(path.parts) != 2 or path.parts[0] != "photos"):
                    raise ValueError("Original must be directly under photos.")
                if path.suffix.lower() not in IMAGE_TYPES or path.stem in photos:
                    raise ValueError("Unsupported image or duplicate photo ID.")
                confined_path(directory, original)
            except DossierError as error:
                raise DossierError(422, "invalid_metadata", "Dossier photo paths are invalid.") from error

            # Derive annotation names from originals instead of trusting arbitrary result paths.

            prefix = f"analysis/annotated/{path.stem}"
            photos[path.stem] = StoredPhoto(
                photo_id=path.stem,
                original_path=original,
                parts_path=f"{prefix}_pieces.png",
                damages_path=f"{prefix}_dommages.png",
                critical_path=f"{prefix}_critical.png",
            )
        return photos

    # ============================================================
    # SAVED ANALYSIS VALIDATION
    # ============================================================

    def read_analysis(self, dossier: DossierRecord) -> StoredAnalysis | None:
        if dossier.metadata.type == "verification":
            return None

        # A managed worker can write JSON just before failing; wait for its validated verdict.

        status_path = confined_path(dossier.directory, "analysis/status.json")
        if status_path.is_file():
            status = read_json_object(status_path, "invalid_status")
            if status.get("managed_by") == "cabek_scan_v1" and status.get("status") != "completed":
                return None

        path = confined_path(dossier.directory, "analysis/result.json")
        if not path.exists():
            return None
        try:
            analysis = StoredAnalysis.model_validate(read_json_object(path, "invalid_result"))
            if analysis.dossier_id != dossier.metadata.dossier_id:
                raise ValueError("Result identity mismatch.")
            expected = dossier.photos
            actual = {photo.photo_id: photo for photo in analysis.photos}

            # Comparing manifests catches missing, duplicated, and unrelated photos, not just counts.

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

    # ============================================================
    # SCAN STATE FROM SAVED FILES
    # ============================================================

    def read_analysis_state(self, dossier: DossierRecord) -> tuple[StoredAnalysis | None, ScanState]:
        analysis = self.read_analysis(dossier)
        try:
            state = self.read_state(dossier, analysis)
        except DossierError as error:
            if error.code != "invalid_status" or analysis is not None:
                raise

            # The supervisor can publish completion between these reads. Revalidate
            # artifacts once; a genuinely invalid completed folder still raises.

            analysis = self.read_analysis(dossier)
            state = self.read_state(dossier, analysis)
        return analysis, state

    def read_state(self, dossier: DossierRecord, analysis: StoredAnalysis | None) -> ScanState:
        if dossier.metadata.type == "verification":
            return "not_analyzed"

        # Validated artifacts establish completion even if a saved status file is stale.

        if analysis is not None:
            return "completed"
        path = confined_path(dossier.directory, "analysis/status.json")
        if not path.exists():

            # Legacy labels such as À valider describe expert review, not durable scan output.

            return "not_analyzed"
        state = read_json_object(path, "invalid_status").get("status")
        if not isinstance(state, str) or state not in {"not_analyzed", "queued", "processing", "failed"}:
            raise DossierError(422, "invalid_status", "Saved scan status is invalid.")
        return state

    # ============================================================
    # SAVED PROGRESS WITHOUT WORKER ERROR DETAILS
    # ============================================================

    def read_progress(self, dossier: DossierRecord) -> ScanProgress | None:
        if dossier.metadata.type == "verification":
            return None
        path = confined_path(dossier.directory, "analysis/status.json")
        if not path.is_file():
            return None
        record = read_json_object(path, "invalid_status")
        if record.get("managed_by") != "cabek_scan_v1":
            return None
        try:
            return ScanProgress.model_validate(record["progress"])
        except (ValidationError, KeyError) as error:
            raise DossierError(422, "invalid_status", "Saved progress is invalid.") from error

    def read_failure_code(self, dossier: DossierRecord) -> str:
        path = confined_path(dossier.directory, "analysis/status.json")
        record = read_json_object(path, "invalid_status") if path.is_file() else {}
        code = record.get("error_code")
        allowed = ("scan_failed", "scan_timeout", "scan_interrupted", "scan_inputs_changed")
        return code if code in allowed else "scan_failed"

    # ============================================================
    # ALLOWLISTED IMAGE ACCESS
    # ============================================================

    def media_path(self, dossier: DossierRecord, requested_path: str) -> Path:
        relative_path = normalize_relative_path(requested_path)

        # Never expose the entire dossier: metadata, feedback, and unrelated files stay private.

        allowlist = {
            value
            for photo in dossier.photos.values()
            for key, value in photo.model_dump().items()
            if key != "photo_id" and (dossier.metadata.type != "verification" or key == "original_path")
        }
        if relative_path not in allowlist:
            raise DossierError(404, "artifact_not_found", "Artifact not found.")
        path = confined_path(dossier.directory, relative_path)
        if not path.is_file():
            raise DossierError(404, "artifact_not_found", "Artifact not found.")
        validate_image(path)
        return path
