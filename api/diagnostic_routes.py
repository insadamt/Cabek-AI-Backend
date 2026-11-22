"""Expose local operating preferences, safe checks, price probes and filtered logs.

Depends on the same loopback/origin boundary as manual lighting settings. Real
capture and AI tests continue to use the existing dossier endpoints.
"""
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from api.errors import DossierError
from api.settings_routes import require_local_settings
from scans.pricing_client import AutoEstimateError, get_estimation

router = APIRouter(prefix='/api/settings', tags=['Local settings'],
                   dependencies=[Depends(require_local_settings)])


class OperationUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slow_mode: StrictBool | None = None
    demo_mode: StrictBool | None = None


class DiagnosticSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    checks: list[str] | None = Field(default=None, max_length=20)


class PriceProbe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    marque_id: int = Field(gt=0, strict=True)
    model_id: int = Field(gt=0, strict=True)
    part_id: int = Field(gt=0, strict=True)
    type_piece: Literal['original', 'adaptable', 'occasion'] = 'occasion'
    year: int | None = Field(default=None, ge=1886, le=2100)


@router.get('/operation')
def operation(request: Request):
    return request.app.state.operation_settings.read()


@router.patch('/operation')
def update_operation(payload: OperationUpdate, request: Request):
    changes = payload.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise DossierError(422, 'invalid_request', 'Indiquez au moins un paramètre booléen.')
    if 'demo_mode' in changes:
        raise DossierError(403, 'demo_admin_required', 'Utilisez l’administration privée pour modifier le mode démonstration.')
    result = request.app.state.operation_settings.update(**changes)
    request.app.state.diagnostic_logs.record('INFO', 'settings.operation',
                                           'Préférences de fonctionnement sauvegardées')
    return result


@router.get('/diagnostics')
def diagnostics(request: Request):
    return request.app.state.diagnostics.snapshot()


@router.post('/diagnostics/run')
def run_diagnostics(payload: DiagnosticSelection, request: Request):
    return request.app.state.diagnostics.run(payload.checks)


@router.post('/diagnostics/pricing')
def price_probe(payload: PriceProbe, request: Request):
    from api.vehicle_catalog import VehicleSelection

    request.app.state.vehicle_catalog.resolve(VehicleSelection(
        marque_id=payload.marque_id, model_id=payload.model_id))
    try:
        result = get_estimation(**payload.model_dump())
    except AutoEstimateError as error:
        raise DossierError(503, 'pricing_unavailable', 'La cotation AutoEstimate est indisponible.') from error
    request.app.state.diagnostic_logs.record('INFO', 'diagnostics.pricing', 'Requête de prix terminée')
    return {'status': 'ok' if result is not None else 'warning',
            'message': 'Cotation reçue.' if result is not None else 'Service joignable ; aucune cotation disponible.',
            'estimation': result}


@router.get('/logs')
def logs(request: Request, limit: Annotated[int, Query(ge=1, le=500)] = 100,
         cursor: Annotated[str | None, Query(max_length=100)] = None,
         level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] | None = None,
         component: Annotated[str | None, Query(max_length=100)] = None,
         query: Annotated[str | None, Query(max_length=200)] = None,
         dossier_id: Annotated[str | None, Query(max_length=100)] = None,
         run_id: Annotated[str | None, Query(max_length=100)] = None,
         since: datetime | None = None,
         until: datetime | None = None,
         source: Literal['backend', 'worker'] = 'backend'):
    store = request.app.state.diagnostic_logs
    if source == 'worker':
        if not dossier_id:
            raise DossierError(422, 'invalid_request', 'Choisissez un dossier pour lire son journal worker.')
        return store.read_worker(dossier_id)
    return store.read(limit=limit, cursor=cursor, level=level, component=component, query=query,
                      dossier_id=dossier_id, run_id=run_id,
                      since=since.replace(tzinfo=timezone.utc) if since and since.tzinfo is None else since,
                      until=until.replace(tzinfo=timezone.utc) if until and until.tzinfo is None else until)


@router.post('/restore-defaults')
def restore_defaults(request: Request):
    from filelock import FileLock, Timeout

    state = request.app.state
    if state.tunnel_lifecycle.snapshot() is not None:
        return {'restored': False, 'reason': 'tunnel_busy'}
    try:
        # Wait for a pending manual command, but never overwrite an active vehicle's lights.

        with FileLock(str(state.workflow_service.repository.dossiers_dir / '.tunnel.lock'), timeout=5):
            if getattr(state, 'internal_lights_manual_override', False):
                state.internal_lighting.set_output(False)
                state.internal_lights_manual_override = False
            lights = state.lighting
            with lights.command_lock, lights.lock:
                if lights.manual_override:
                    lights.ready_deadline = None
                    lights.completion_deadline = None
                    lights.transition_to('#FFFFFF')
            state.diagnostic_logs.record('INFO', 'settings', 'Tests temporaires réinitialisés à la sortie des paramètres')
            return {'restored': True}
    except Timeout:
        return {'restored': False, 'reason': 'tunnel_busy'}
