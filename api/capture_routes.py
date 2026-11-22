"""Expose Tiandy/demo tunnel admission and durable per-phase camera progress.

Depends on CaptureService; response photo URLs use the existing media allowlist.
"""

from fastapi import APIRouter, Request

from api.capture_schemas import CaptureRequest, CaptureStatus
from api.routes import DossierId
from api.schemas import ErrorResponse
from api.workflow_schemas import PhotoPhase


router = APIRouter(prefix="/api/dossiers", tags=["Tunnel capture"], responses={
    code: {"model": ErrorResponse} for code in (404, 409, 422, 503)
})


@router.post("/{dossier_id}/capture", response_model=CaptureStatus, status_code=202)
def start_capture(dossier_id: DossierId, request: Request, payload: CaptureRequest = CaptureRequest()):
    captures = request.app.state.capture_service
    zooms = payload.zoom_channels if 'zoom_channels' in payload.model_fields_set else None
    matched = request.app.state.private_demo.capture(dossier_id, payload.phase)
    if matched:
        return captures.read_status(matched, payload.phase, request.scope.get("root_path", ""))
    captures.start_capture(dossier_id, payload.phase, zooms)
    return captures.read_status(dossier_id, payload.phase, request.scope.get("root_path", ""))


@router.get("/{dossier_id}/capture", response_model=CaptureStatus)
def capture_status(dossier_id: DossierId, request: Request, phase: PhotoPhase | None = None):
    return request.app.state.capture_service.read_status(dossier_id, phase, request.scope.get("root_path", ""))
