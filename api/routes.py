from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from api.files import IMAGE_MEDIA_TYPES
from api.schemas import DossierList, DossierResult, ErrorResponse, ScanState
from api.service import DossierListQuery, DossierService


router = APIRouter(
    prefix="/api/dossiers",
    tags=["Dossiers"],
    responses={
        404: {"model": ErrorResponse, "description": "Dossier or artifact not found"},
        422: {"model": ErrorResponse, "description": "Invalid request or saved dossier data"},
        503: {"model": ErrorResponse, "description": "Dossier storage unavailable"},
    },
)


def dossier_service(request: Request) -> DossierService:
    return request.app.state.dossier_service


ServiceDependency = Annotated[DossierService, Depends(dossier_service)]


@router.get("", response_model=DossierList, summary="List existing dossiers")
def list_dossiers(
    service: ServiceDependency,
    search: Annotated[str, Query(max_length=200)] = "",
    status: ScanState | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DossierList:
    return service.list_dossiers(DossierListQuery(search, status, limit, offset))


@router.get("/{dossier_id}/result", response_model=DossierResult, summary="Read saved expertise")
def read_result(dossier_id: str, request: Request, service: ServiceDependency) -> DossierResult:
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


@router.get(
    "/{dossier_id}/media/{path:path}", response_class=FileResponse,
    summary="Read an allowlisted original or annotated image",
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}},
)
def read_media(dossier_id: str, path: str, service: ServiceDependency) -> FileResponse:
    image = service.media_path(dossier_id, path)
    return FileResponse(
        image, media_type=IMAGE_MEDIA_TYPES[image.suffix.lower()],
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline",
    )


@router.get(
    "/{dossier_id}/report", response_class=FileResponse,
    summary="Download a saved expertise PDF",
    responses={200: {"content": {"application/pdf": {}}}},
)
def download_report(dossier_id: str, service: ServiceDependency) -> FileResponse:
    report = service.report_path(dossier_id)
    return FileResponse(
        report, media_type="application/pdf", filename=f"rapport_{dossier_id}.pdf",
        headers={"X-Content-Type-Options": "nosniff"},
    )
