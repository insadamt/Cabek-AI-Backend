"""Expose local dossier creation, permitted metadata edits, and side/phase uploads.

Depends on workflow/photo services and existing dossier response assembly. Manual
uploads keep explicit scan admission; tunnel capture has its own automatic handoff.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from api.deletion import delete_dossier
from pydantic import BaseModel

from api.photo_service import PhotoService
from api.routes import DossierId, ServiceDependency
from api.schemas import DossierResult, ErrorResponse, OriginalPhoto, WorkflowPhoto, VerificationComparison
from api.workflow_schemas import CreateDossier, PatchDossier, PhotoPhase, PhotoSide
from api.workflow_service import WorkflowService


router = APIRouter(prefix="/api/dossiers", tags=["Dossier workflows"], responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
    413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


def workflow_service(request: Request) -> WorkflowService:
    return request.app.state.workflow_service


WorkflowDependency = Annotated[WorkflowService, Depends(workflow_service)]


class PhotoCollection(BaseModel):
    dossier_id: str
    photos: list[WorkflowPhoto | OriginalPhoto]
    comparison: VerificationComparison | None = None


# ============================================================
# MANUALLY CREATED CATEGORIES AND PERMITTED UPDATES
# ============================================================

@router.post("", status_code=201, response_model=DossierResult)
def create_dossier(payload: CreateDossier, request: Request, workflows: WorkflowDependency,
                   service: ServiceDependency) -> DossierResult:
    dossier_id = workflows.create_dossier(payload, request.headers.get("Idempotency-Key"))
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


@router.patch("/{dossier_id}", response_model=DossierResult)
@router.patch("/{dossier_id}/metadata", response_model=DossierResult)
def patch_dossier(dossier_id: DossierId, payload: PatchDossier, request: Request,
                  workflows: WorkflowDependency, service: ServiceDependency) -> DossierResult:
    workflows.patch_dossier(dossier_id, payload)
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


# ============================================================
# CURRENT PHOTOS, UPLOADS, AND EXPLICIT REPLACEMENTS
# ============================================================

@router.get("/{dossier_id}/photos", response_model=PhotoCollection)
def list_photos(dossier_id: DossierId, request: Request, service: ServiceDependency,
                phase: PhotoPhase | None = None) -> PhotoCollection:
    result = service.read_result(dossier_id, request.scope.get("root_path", ""))
    return PhotoCollection(
        dossier_id=dossier_id,
        photos=[photo for photo in result.photos if phase is None or getattr(photo, "phase", None) == phase],
        comparison=result.comparison,
    )


@router.post("/{dossier_id}/photos", status_code=201, response_model=DossierResult)
def upload_photo(dossier_id: DossierId, request: Request, workflows: WorkflowDependency,
                 service: ServiceDependency, file: Annotated[UploadFile, File()],
                 side: Annotated[PhotoSide, Form()], phase: Annotated[PhotoPhase | None, Form()] = None) -> DossierResult:
    PhotoService(workflows).upload_photo(dossier_id=dossier_id, upload=file, side=side, phase=phase)
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


@router.put("/{dossier_id}/photos/{photo_id}", response_model=DossierResult)
def replace_photo(dossier_id: DossierId, photo_id: str, request: Request, workflows: WorkflowDependency,
                  service: ServiceDependency, file: Annotated[UploadFile, File()],
                  side: Annotated[PhotoSide, Form()], phase: Annotated[PhotoPhase | None, Form()] = None) -> DossierResult:
    PhotoService(workflows).upload_photo(
        dossier_id=dossier_id, upload=file, side=side, phase=phase, replace_photo_id=photo_id,
    )
    return service.read_result(dossier_id, request.scope.get("root_path", ""))


@router.delete("/{dossier_id}", status_code=204, response_class=Response)
def remove_dossier(dossier_id: DossierId, request: Request):
    delete_dossier(request.app.state.workflow_service.manager, dossier_id)
    return Response(status_code=204)
