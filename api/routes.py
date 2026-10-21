from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import FileResponse

from api.files import IMAGE_MEDIA_TYPES
from api.schemas import DossierList, DossierResult, ErrorResponse, ScanState
from api.service import DossierListQuery, DossierService


# ============================================================
# ROUTE CONFIGURATION AND ERROR DOCUMENTATION
# ============================================================

router = APIRouter(
    prefix="/api/dossiers",
    tags=["Dossiers"],
    responses={
        404: {"model": ErrorResponse, "description": "Dossier or artifact not found"},
        422: {"model": ErrorResponse, "description": "Invalid request or saved dossier data"},
        503: {"model": ErrorResponse, "description": "Dossier storage unavailable"},
    },
)


# ============================================================
# APPLICATION SERVICE DEPENDENCY
# ============================================================

def dossier_service(request: Request) -> DossierService:
    return request.app.state.dossier_service


ServiceDependency = Annotated[DossierService, Depends(dossier_service)]
DossierId = Annotated[str, Path(description="Existing dossier folder ID, for example DOS-001.")]


# These synchronous handlers let FastAPI run blocking filesystem reads off the event loop.

# ============================================================
# DOSSIER LIST
# ============================================================

@router.get(
    "", response_model=DossierList, summary="List existing dossiers",
    description=(
        "Search dashboard folders and filter by saved analysis state. "
        "Returns items, total matches, status counts, pagination, and skipped-folder warnings. "
        "Counts apply search but ignore the status filter and pagination. No request body."
    ),
    response_description="Dossier summaries sorted by ID descending.",
)
def list_dossiers(
    service: ServiceDependency,
    search: Annotated[str, Query(
        max_length=200, description="Case-insensitive match against ID, marque, modèle, or matricule."
    )] = "",
    status: Annotated[ScanState | None, Query(description="Filter by saved analysis state.")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Maximum items in this page.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Matching items to skip before this page.")] = 0,
) -> DossierList:
    return service.list_dossiers(DossierListQuery(search, status, limit, offset))


# ============================================================
# SAVED ANALYSIS
# ============================================================

@router.get(
    "/{dossier_id}/result", response_model=DossierResult, summary="Read saved expertise",
    description=(
        "Requires an existing dossier ID. Returns saved vehicle, financial, part, detection, "
        "and photo data with image/PDF URLs when final artifacts are valid. "
        "Original photos are always included; without final output, result is null. Managed scans return real artifact counts in progress. "
        "A completed scan may have incomplete finances. Does not start a scan."
    ),
    response_description="Saved analysis state and nullable result.",
)
def read_result(dossier_id: DossierId, request: Request, service: ServiceDependency) -> DossierResult:
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


# ============================================================
# ORIGINAL AND ANNOTATED IMAGES
# ============================================================

@router.get(
    "/{dossier_id}/media/{path:path}", response_class=FileResponse,
    summary="Read an allowlisted original or annotated image",
    description=(
        "Requires an existing dossier and a declared original or expected annotation path. "
        "Use the photo URLs returned by the result endpoint. Returns JPEG or PNG bytes, "
        "not JSON. Missing or disallowed paths return 404; invalid images return 422."
    ),
    response_description="Original or annotated image bytes.",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}},
)
def read_media(
    dossier_id: DossierId,
    path: Annotated[str, Path(description="Relative image path using forward slashes, e.g. photos/avant_01.jpg.")],
    service: ServiceDependency,
) -> FileResponse:
    image = service.media_path(dossier_id, path)
    return FileResponse(
        image, media_type=IMAGE_MEDIA_TYPES[image.suffix.lower()],
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline",
    )


# ============================================================
# PDF DOWNLOAD
# ============================================================

@router.get(
    "/{dossier_id}/report", response_class=FileResponse,
    summary="Download a saved expertise PDF",
    description=(
        "Requires an existing dossier with a valid completed saved analysis. "
        "Returns PDF bytes as an attachment named rapport_{dossier_id}.pdf. "
        "Does not generate a report. No final analysis returns 404; invalid final artifacts return 422."
    ),
    response_description="Saved PDF attachment.",
    responses={200: {"content": {"application/pdf": {}}}},
)
def download_report(dossier_id: DossierId, service: ServiceDependency) -> FileResponse:
    report = service.report_path(dossier_id)
    return FileResponse(
        report, media_type="application/pdf", filename=f"rapport_{dossier_id}.pdf",
        headers={"X-Content-Type-Options": "nosniff"},
    )
