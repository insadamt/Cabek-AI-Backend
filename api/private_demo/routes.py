"""Expose authenticated private template operations, never public template assets.

Depends on server-side sessions and the local settings boundary; every management
route checks authorization and every mutation requires a same-origin custom header.
"""
from io import BytesIO
import json
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from api.errors import DossierError
from api.private_demo.auth import COOKIE, password_record, save_secret
from api.private_demo.bundles import export_zip
from api.settings_routes import require_local_settings

class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def serve(request):
            try:
                return await handler(request)
            except (ValueError, TypeError, KeyError):
                raise DossierError(422, 'invalid_demo_data', 'Données privées invalides. Vérifiez les métadonnées et le format natif des résultats.') from None
            except Timeout:
                raise DossierError(409, 'demo_busy', 'Une modification privée est déjà en cours.') from None
            except OSError:
                raise DossierError(503, 'demo_storage_unavailable', 'Stockage privé indisponible ; les versions précédentes sont conservées.') from None
        return serve


router = APIRouter(prefix='/api/private-demo', tags=['Private administration'], route_class=PrivateRoute)


def service(request: Request):
    return request.app.state.private_demo


def authenticated(request: Request):
    service(request).auth.require(request)


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=256)


class TemplateEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    metadata: dict | None = None
    active: bool | None = None
    title: str | None = Field(default=None, max_length=120)


class ModeEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool


@router.post('/login', dependencies=[Depends(require_local_settings)])
def login(payload: Credentials, request: Request, response: Response):
    if request.headers.get('x-demo-admin') != '1':
        raise DossierError(403, 'demo_csrf', 'Requête privée invalide.')
    token = service(request).auth.login(payload.password)
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=request.url.scheme == 'https', max_age=1800, path='/api/private-demo')
    response.headers['Cache-Control'] = 'no-store'
    return {'authenticated': True, 'expires_in': 1800}


@router.get('/session', dependencies=[Depends(authenticated)])
def session(request: Request):
    return {'authenticated':True, 'enabled':service(request).enabled()}


@router.post('/logout', dependencies=[Depends(authenticated)])
def logout(request: Request, response: Response):
    service(request).auth.logout(request)
    response.delete_cookie(COOKIE, path='/api/private-demo')
    return {'authenticated':False}


@router.post('/password', dependencies=[Depends(authenticated)])
def rotate(payload: Credentials, request: Request, response: Response):
    auth = service(request).auth
    with __import__('filelock').FileLock(str(auth.root / '.auth.lock'), timeout=10):
        save_secret(auth.secret, password_record(payload.password))
    response.delete_cookie(COOKIE, path='/api/private-demo')
    return {'authenticated':False}


@router.put('/mode', dependencies=[Depends(authenticated)])
def mode(payload: ModeEdit, request: Request):
    service(request).state.operation_settings.update(demo_mode=payload.enabled)
    return {'enabled':payload.enabled}


@router.get('/templates', dependencies=[Depends(authenticated)])
def templates(request: Request):
    return service(request).templates.list()


@router.post('/templates', dependencies=[Depends(authenticated)], status_code=201)
def create(payload: TemplateEdit, request: Request):
    return service(request).templates.commit(**payload.model_dump())


@router.get('/templates/{identity}', dependencies=[Depends(authenticated)])
def inspect(identity: str, request: Request):
    return service(request).templates.inspect(identity)


@router.patch('/templates/{identity}', dependencies=[Depends(authenticated)])
def edit(identity: str, payload: TemplateEdit, request: Request):
    return service(request).templates.commit(identity=identity, **payload.model_dump())


@router.post('/templates/{identity}/assets', dependencies=[Depends(authenticated)])
async def asset(identity: str, request: Request, path: str = Query(max_length=240), file: UploadFile = File()):
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise DossierError(413, 'upload_too_large', 'Fichier limité à 10 Mio.')
    return service(request).templates.commit(identity=identity, asset=(path, content), active=False)


@router.post('/import', dependencies=[Depends(authenticated)], status_code=201)
async def import_template(request: Request, file: UploadFile = File()):
    content = await file.read(128 * 1024 * 1024 + 1)
    if len(content) > 128 * 1024 * 1024:
        raise DossierError(413, 'upload_too_large', 'Archive limitée à 128 Mio.')
    return service(request).templates.commit(archive=content, active=False)


@router.get('/templates/{identity}/export', dependencies=[Depends(authenticated)])
def export(identity: str, request: Request):
    store = service(request).templates
    return StreamingResponse(BytesIO(export_zip(store.directory(store.get(identity)))), media_type='application/zip', headers={'Cache-Control':'no-store','Content-Disposition':'attachment; filename="cabek-template.zip"'})


@router.post('/templates/{identity}/duplicate', dependencies=[Depends(authenticated)], status_code=201)
def duplicate(identity: str, request: Request):
    return service(request).templates.duplicate(identity)


@router.delete('/templates/{identity}', dependencies=[Depends(authenticated)])
def delete(identity: str, request: Request):
    service(request).templates.delete(identity)
    return {'deleted':True}


@router.post('/templates/{identity}/test', dependencies=[Depends(authenticated)], status_code=201)
def test_template(identity: str, request: Request):
    from uuid import uuid4
    import shutil
    from api.private_demo.materialize import prepare_clone
    from scans.artifacts import publish_json
    demo = service(request)
    with demo.templates.locked():
        item = demo.templates.get(identity)
        dossier_id = 'DOS-DEMO-' + uuid4().hex
        root = demo.repository.dossiers_dir
        stage = root / ('.demo-test-' + uuid4().hex)
        stage.mkdir()
        try:
            if item['type'] == 'verification':
                before = prepare_clone(demo.templates, item, stage, dossier_id, 'before')
                after = root / ('.demo-test-after-' + uuid4().hex)
                after.mkdir()
                try:
                    prepare_clone(demo.templates, item, after, dossier_id, 'after', before)
                    for photo in before['photos']:
                        destination = after / photo['path']
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(stage / photo['path'], destination)
                    shutil.rmtree(stage)
                    stage = after
                finally:
                    if after.exists() and after != stage:
                        shutil.rmtree(after)
            else:
                prepare_clone(demo.templates, item, stage, dossier_id)
            stage.rename(root / dossier_id)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return demo.state.dossier_service.read_result(dossier_id)
