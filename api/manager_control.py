"""Provide readiness and atomic admission draining for Cabek AI Manager updates.

Depends on an installation-specific manager key and existing persistent job states.
Maintenance closes write admission before checking active work, avoiding an idle-check race.
"""
import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


def manager_authenticated(request):
    expected = os.getenv('CABEK_MANAGER_KEY', '')
    received = request.headers.get('x-cabek-manager-key', '')
    return bool(expected and received and secrets.compare_digest(expected, received))


class MaintenanceMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, runtime_root):
        super().__init__(app)
        self.path = Path(runtime_root) / "manager-maintenance.lock"
        self.lock = asyncio.Lock()
        self.enabled = self.path.exists()
        self.requests = 0

    async def dispatch(self, request, call_next):
        request.app.state.maintenance = self
        protected = request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.url.path.startswith('/api/manager/')
        if not protected:
            return await call_next(request)
        async with self.lock:
            if self.enabled:
                return JSONResponse(status_code=503, content={'error': {
                    'code': 'maintenance', 'message': 'An update is in progress. Try again shortly.'}})
            self.requests += 1
        try:
            return await call_next(request)
        finally:
            async with self.lock:
                self.requests -= 1


def busy_state(request):
    state = request.app.state
    jobs = (state.maintenance.requests + state.capture_service.executor.active_count
            + state.scan_manager.executor.active_count)
    for dossier_id in state.dossier_service.repository.dossier_ids():
        folder = state.dossier_service.repository.dossiers_dir / dossier_id
        try:
            metadata = json.loads((folder / 'metadata.json').read_text())
            jobs += sum(item.get('status') == 'capturing' for item in metadata.get('captures', {}).values())
            status = folder / 'analysis/status.json'
            if status.exists():
                jobs += json.loads(status.read_text()).get('status') in ('queued', 'processing')
        except (OSError, ValueError, TypeError):
            continue
    with state.econstat_service.lock:
        jobs += sum(not future.done() for future in state.econstat_service.jobs.values())
    return {'maintenance': state.maintenance.enabled, 'busy': bool(jobs), 'active_jobs': jobs}


router = APIRouter()


@router.get('/health')
def health():
    return {'status': 'ok'}


@router.get('/api/manager/status')
def status(request: Request):
    if not manager_authenticated(request):
        return JSONResponse(status_code=403, content={'detail': 'Manager credentials required.'})
    return busy_state(request)


@router.post('/api/manager/maintenance')
async def maintenance(payload: dict, request: Request):
    if not manager_authenticated(request):
        return JSONResponse(status_code=403, content={'detail': 'Manager credentials required.'})
    if set(payload) != {'enabled'} or type(payload['enabled']) is not bool:
        return JSONResponse(status_code=422, content={'detail': 'An enabled boolean is required.'})
    gate = request.app.state.maintenance
    async with gate.lock:
        if payload['enabled']:
            gate.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(gate.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        else:
            gate.path.unlink(missing_ok=True)
        gate.enabled = payload['enabled']
    return busy_state(request)


@router.post('/api/manager/verify')
def verify(request: Request):
    if not manager_authenticated(request):
        return JSONResponse(status_code=403, content={'detail': 'Manager credentials required.'})
    return request.app.state.diagnostics.run()
