"""Create and edit locally owned dossiers with atomic publication and scan locks.

Depends on the existing scan manager, AutoEstimate catalog, and file publishers.
Expertise metadata stays external; only capture-owned photo writes may edit its manifest.
"""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import shutil
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

from filelock import FileLock, Timeout

from api.errors import DossierError
from api.dossier_lifecycle import dossier_admission
from api.files import confined_path, read_json_object
from api.repository import DossierMetadata, DossierRecord
from api.vehicle_catalog import VehicleCatalog, VehicleSelection
from api.workflow_schemas import CreateDossier, PatchDossier
from api.workflow_state import workflow_status
from scans.artifacts import publish_json

if TYPE_CHECKING:
    from scans.jobs import ScanManager


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# LOCAL DOSSIER OWNERSHIP AND ATOMIC METADATA
# ============================================================

class WorkflowService:
    def __init__(self, manager: "ScanManager", catalog: VehicleCatalog):
        self.manager = manager
        self.repository = manager.repository
        self.catalog = catalog

    def create_dossier(self, request: CreateDossier, idempotency_key: str | None = None) -> str:
        from api.creation import create_once
        return create_once(self, request, idempotency_key)

    def publish_dossier(self, request: CreateDossier, creation_key: str | None = None) -> str:
        self.repository.validate_root()
        if (request.marque_id is None) != (request.model_id is None):
            raise DossierError(422, "invalid_vehicle_selection", "Provide both vehicle IDs together.")
        vehicle = self.catalog.resolve(VehicleSelection(
            marque_id=request.marque_id, model_id=request.model_id,
        )).model_dump() if request.marque_id else {"marque": "", "modele": ""}
        dossier_id = f"DOS-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid4().hex}"
        directory = self.repository.dossiers_dir / dossier_id
        staging = self.repository.dossiers_dir / f".create-{uuid4().hex}"
        now = timestamp()
        metadata = {
            **request.model_dump(), **vehicle, "dossier_id": dossier_id,
            "source": "cabek_ai", "created_at": now, "updated_at": now,
            "annee": request.date_mise_en_circulation[:4] if request.date_mise_en_circulation else None,
            "photos": [], "photo_history": [], "workflow_status": "draft",
            "creation_key": creation_key,
            "creation_request": request.model_dump() if creation_key else None,
        }
        try:
            (staging / "photos").mkdir(parents=True)
            if request.type == "verification":
                (staging / "photos/before").mkdir()
                (staging / "photos/after").mkdir()
            publish_json(staging / "metadata.json", metadata)

            # The hidden staging folder keeps list readers from seeing half-created dossiers.

            staging.rename(directory)
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Dossier could not be created.") from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return dossier_id

    @contextmanager
    def editable_dossier(self, dossier_id: str, *, metadata_only: bool = False,
                         capture_owned: bool = False) -> Iterator[DossierRecord]:
        with dossier_admission(self.repository, dossier_id):
            with self._editable_dossier(dossier_id, metadata_only=metadata_only, capture_owned=capture_owned) as dossier:
                yield dossier

    @contextmanager
    def _editable_dossier(self, dossier_id, *, metadata_only=False, capture_owned=False):
        dossier = self.repository.read_dossier(dossier_id)
        if dossier.metadata.type == "forfait_expertise" and (not capture_owned or metadata_only):
            raise DossierError(409, "externally_owned_dossier", "Expertise inputs are managed by the originating CABEK application.")
        analysis_dir = self.manager.prepare_analysis_directory(dossier.directory)
        try:
            with ExitStack() as locks:
                if not metadata_only:
                    if not capture_owned:
                        locks.enter_context(FileLock(str(dossier.directory / ".capture.lock"), timeout=0))
                    locks.enter_context(FileLock(str(analysis_dir / ".scan.lock"), timeout=0))
                    locks.enter_context(FileLock(str(analysis_dir / ".worker.lock"), timeout=0))
                locks.enter_context(FileLock(str(dossier.directory / ".metadata.lock"), timeout=5))

                # Only vehicle fields can bypass the scan locks; pricing closes that edit window.

                dossier = self.repository.read_dossier(dossier_id)
                self.manager.reject_completed_scan(dossier, analysis_dir)
                self.manager.reject_external_scan(analysis_dir)
                if metadata_only and (analysis_dir / ".vehicle.json").exists():
                    record = read_json_object(analysis_dir / "status.json", "invalid_status")
                    if record.get("status") in ("queued", "processing"):
                        raise DossierError(409, "pricing_in_progress", "Vehicle metadata is frozen for pricing.")
                saved = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
                if dossier.metadata.type == "verification" and not capture_owned and (saved.get("evidence_finalized_at") or workflow_status(dossier, "not_analyzed") == "comparison_ready"):
                    raise DossierError(409, "evidence_finalized", "The complete comparison is immutable.")
                yield dossier
        except Timeout as error:
            raise DossierError(409, "scan_in_progress", "Dossier inputs cannot change during a scan.") from error
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Dossier inputs could not be saved.") from error

    def save_metadata(self, dossier: DossierRecord, metadata: dict) -> None:
        metadata["updated_at"] = timestamp()
        validated = DossierMetadata.model_validate(metadata)
        candidate = DossierRecord(dossier.directory, validated,
                                  self.repository.photo_manifest(dossier.directory, validated))
        _, state = self.repository.read_analysis_state(candidate)
        metadata["workflow_status"] = workflow_status(candidate, state)
        if metadata.get("type", "forfait_expertise") == "verification":

            # A deleted or corrupted file must not unlock evidence that was already finalized.

            if any(photo.phase == "after" for photo in validated.photos):
                metadata.setdefault("before_finalized_at", timestamp())
            if metadata["workflow_status"] == "comparison_ready":
                metadata.setdefault("evidence_finalized_at", timestamp())
        publish_json(confined_path(dossier.directory, "metadata.json"), metadata)

    def patch_dossier(self, dossier_id: str, request: PatchDossier) -> None:
        changes = request.model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise DossierError(422, "invalid_metadata_update", "Provide at least one non-null editable field.")
        if ("marque_id" in changes) != ("model_id" in changes):
            raise DossierError(422, "invalid_vehicle_selection", "Provide both vehicle IDs together.")
        current = self.repository.read_dossier(dossier_id)
        if current.metadata.type == "forfait_expertise":
            raise DossierError(409, "externally_owned_dossier", "Expertise inputs are externally managed.")
        if "marque_id" in changes:
            changes.update(self.catalog.resolve(VehicleSelection(
                marque_id=changes["marque_id"], model_id=changes["model_id"],
            )).model_dump())
        with self.editable_dossier(dossier_id, metadata_only=True) as dossier:
            if "date_mise_en_circulation" in changes:
                changes["annee"] = changes["date_mise_en_circulation"][:4]
            metadata = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
            metadata.update(changes)
            self.save_metadata(dossier, metadata)
