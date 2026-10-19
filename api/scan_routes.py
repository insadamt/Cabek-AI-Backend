from typing import Annotated

from fastapi import APIRouter, Depends, Request

from api.routes import DossierId
from api.schemas import DossierResult, ErrorResponse
from scans.jobs import ScanManager


# ============================================================
# REAL SCAN ADMISSION
# ============================================================

router = APIRouter(prefix="/api/dossiers", tags=["Scans"])


def scan_manager(request: Request) -> ScanManager:
    return request.app.state.scan_manager


@router.post(
    "/{dossier_id}/scan", status_code=202, response_model=DossierResult,
    summary="Queue one real dossier analysis",
    description=(
        "No request body. Requires an existing dossier with valid photos and installed scan dependencies. "
        "Returns queued immediately; poll GET /api/dossiers/{dossier_id}/result for file-based progress. "
        "Failed scans may retry. Successful analyses cannot be scanned again."
    ),
    responses={
        404: {"model": ErrorResponse, "description": "Unknown dossier"},
        409: {"model": ErrorResponse, "description": "Already analyzed or scan in progress"},
        422: {"model": ErrorResponse, "description": "Invalid dossier, photos, or saved data"},
        503: {"model": ErrorResponse, "description": "Worker unavailable or queue full"},
    },
)
def start_scan(
    dossier_id: DossierId, manager: Annotated[ScanManager, Depends(scan_manager)],
) -> DossierResult:
    return manager.start_scan(dossier_id)
