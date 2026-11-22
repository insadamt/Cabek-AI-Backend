"""Expose E-Constat preparation and a first-page-only image through Cabek.

Depends on the versioned preparation service; no original PDF or OCR secret is served.
"""
from typing import Literal
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from api.routes import DossierId
from api.errors import DossierError
from api.econstat_documents import read_document, first_page_preview


class EconstatZone(BaseModel):
    id: str
    status: Literal['damaged', 'uncertain', 'healthy']


class EconstatPreparation(BaseModel):
    document_version: str
    registration: str
    filename: str
    preview_url: str
    status: Literal['not_started', 'processing', 'ready', 'manual_required', 'failed']
    message: str
    zones: list[EconstatZone] = Field(default_factory=list)
    recommended_zoom_channels: list[int] = Field(default_factory=list)
    vehicle_reference: str | None = None


router = APIRouter(prefix='/api/dossiers', tags=['E-Constat'])


@router.get('/{dossier_id}/econstat', response_model=EconstatPreparation)
def describe_econstat(dossier_id: DossierId, request: Request):
    return request.app.state.econstat_service.describe(dossier_id, request.scope.get('root_path', ''))


@router.post('/{dossier_id}/econstat/analysis', response_model=EconstatPreparation)
def analyze_econstat(dossier_id: DossierId, request: Request, response: Response):
    demo = request.app.state.private_demo
    if demo.managed() and demo.enabled() and not demo.repository.read_dossier(dossier_id).metadata.demo:
        result = request.app.state.econstat_service.describe(dossier_id, request.scope.get('root_path', ''))
        result.update(status='manual_required', message='Les recommandations préparées seront chargées avec le modèle ; aucun OCR réel lancé.')
    else:
        result = request.app.state.econstat_service.start_analysis(dossier_id, request.scope.get('root_path', ''))
    response.status_code = 202 if result['status'] == 'processing' else 200
    return result


@router.get('/{dossier_id}/econstat/page-1.webp')
def econstat_preview(dossier_id: DossierId, request: Request, version: str | None = None):
    document = read_document(request.app.state.econstat_service.repository, dossier_id)
    if version is not None and version != document.version:
        raise DossierError(409, 'econstat_changed', 'L’E-Constat a changé. Rechargez le dossier.')
    return FileResponse(first_page_preview(document), media_type='image/webp', headers={'Cache-Control': 'private, no-store'})
