from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock, Timeout

from api.errors import DossierError
from api.files import confined_path, read_json_object
from api.vehicle_catalog import ResolvedVehicle, VehicleCatalog, VehicleSelection
from scans.artifacts import publish_json
if TYPE_CHECKING:
    from scans.jobs import ScanManager


# ============================================================
# IDENTITY VALIDATION AND ATOMIC METADATA UPDATES
# ============================================================

def save_vehicle_selection(directory: Path, selection: VehicleSelection, catalog: VehicleCatalog) -> ResolvedVehicle:
    vehicle = catalog.resolve(selection)
    path = confined_path(directory, "metadata.json")
    metadata = read_json_object(path, "invalid_metadata")
    metadata.update(vehicle.model_dump())
    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
    publish_json(path, metadata)
    return vehicle


class VehicleSelectionService:
    def __init__(self, manager: "ScanManager", catalog: VehicleCatalog):
        self.manager = manager
        self.catalog = catalog

    def update_vehicle(self, dossier_id: str, selection: VehicleSelection) -> ResolvedVehicle:
        dossier = self.manager.repository.read_dossier(dossier_id)
        analysis_dir = self.manager.prepare_analysis_directory(dossier.directory)
        try:

            # Share both scan locks so queued jobs and surviving workers keep their input snapshot.

            with FileLock(str(analysis_dir / ".scan.lock"), timeout=0):
                with FileLock(str(analysis_dir / ".worker.lock"), timeout=0):
                    dossier = self.manager.repository.read_dossier(dossier_id)
                    self.manager.reject_completed_scan(dossier, analysis_dir)
                    self.manager.reject_external_scan(analysis_dir)
                    return save_vehicle_selection(dossier.directory, selection, self.catalog)
        except Timeout as error:
            raise DossierError(409, "scan_in_progress", "Vehicle selection cannot change during a scan.") from error
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Dossier metadata could not be saved.") from error
