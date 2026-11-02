"""Wait for validated vehicle inputs after image inference, then freeze pricing.

Depends on atomic metadata writes and the shared metadata lock. The existing
supervisor owns timeout/shutdown; this stage never generates detection progress.
"""

import time

from filelock import FileLock, Timeout

from api.files import read_json_object
from api.repository import DossierMetadata
from api.schemas import Vehicle
from api.vehicle_catalog import VehicleCatalog, VehicleSelection
from api.workflow_state import vehicle_is_complete
from scans.artifacts import publish_json


VEHICLE_FIELDS = set(Vehicle.model_fields)
MUTABLE_WORKFLOW_FIELDS = {"updated_at", "workflow_status", "captures"}


def await_pricing_metadata(request) -> None:
    if not request.allow_metadata_updates:
        return
    publish_json(request.output_dir / ".pipeline.json", {"stage": "awaiting_metadata"})
    while True:
        try:
            with FileLock(str(request.dossier_dir / ".metadata.lock"), timeout=1):
                metadata = read_json_object(request.dossier_dir / "metadata.json", "invalid_metadata")
                validated = DossierMetadata.model_validate(metadata)
                if vehicle_is_complete(validated):
                    vehicle = VehicleCatalog().resolve(VehicleSelection(
                        marque_id=validated.marque_id, model_id=validated.model_id,
                    ))
                    metadata.update(vehicle.model_dump())
                    publish_json(request.dossier_dir / "metadata.json", metadata)
                    snapshot = Vehicle.model_validate(metadata).model_dump()

                    # Publication under this lock closes the edit window before pricing reads it.

                    publish_json(request.output_dir / ".vehicle.json", {"run_id": request.run_id, "vehicle": snapshot})
                    request.metadata.update(snapshot)
                    publish_json(request.output_dir / ".pipeline.json", {"stage": "finalizing"})
                    return
        except Timeout:
            pass
        time.sleep(0.2)


def validate_pricing_snapshot(request, dossier) -> None:
    if not request.allow_metadata_updates:
        return
    saved = read_json_object(request.output_dir / ".vehicle.json", "invalid_metadata")
    current = Vehicle.model_validate(dossier.metadata.model_dump()).model_dump()
    if saved.get("run_id") != request.run_id or saved.get("vehicle") != current:
        raise ValueError("Vehicle inputs changed after pricing began.")
