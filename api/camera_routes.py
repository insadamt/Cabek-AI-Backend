"""Serve application camera previews while keeping Tiandy credentials server-side.

Depends on CameraService and HTTPX; upstream streams close on cancellation or timeout.
"""

import asyncio

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from api.camera_registry import camera_identity
from api.errors import DossierError
from api.workflow_schemas import PhotoPhase


router = APIRouter(prefix='/api/cameras', tags=['Cameras'])
NO_CACHE = {'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}


@router.get('')
def cameras(request: Request, phase: PhotoPhase | None = None):
    service = request.app.state.capture_service.cameras
    result = service.catalog(phase, request.scope.get('root_path', ''))
    demo = request.app.state.private_demo
    if demo.managed() and demo.enabled():
        result['prepared_demo'] = True
    return result


def demo_frame(service, channel, phase):
    camera_identity(channel)
    return service.demo.read(service.demo.plan([channel], phase)[0])


@router.get('/{channel}/latest.jpg')
def snapshot(channel: int, request: Request, phase: PhotoPhase | None = None):
    service = request.app.state.capture_service.cameras
    camera_identity(channel)
    source = service.source()
    if source == 'disabled':
        raise DossierError(503, 'tunnel_unavailable', 'Capture désactivée.')
    frame = demo_frame(service, channel, phase) if source == 'demo' else service.client.snapshot(channel)
    # The shared API serves JPEG consistently even when an essential demo source is PNG.

    content = frame['content']
    if frame['extension'] != '.jpg':
        from io import BytesIO
        from PIL import Image
        output = BytesIO()
        with Image.open(BytesIO(content)) as image:
            image.convert('RGB').save(output, format='JPEG', quality=95)
        content = output.getvalue()
    return Response(content, media_type='image/jpeg', headers={**NO_CACHE, 'X-Frame-Id': frame['frame_id']})


@router.get('/{channel}/stream')
async def stream(channel: int, request: Request, phase: PhotoPhase | None = None):
    service = request.app.state.capture_service.cameras
    camera_identity(channel)
    source = service.source()
    if source == 'disabled':
        raise DossierError(503, 'tunnel_unavailable', 'Capture désactivée.')
    if source == 'demo':
        response = await asyncio.to_thread(snapshot, channel, request, phase)

        async def demo_stream():
            while not await request.is_disconnected():
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + response.body + b'\r\n'
                await asyncio.sleep(1)

        return StreamingResponse(demo_stream(), media_type='multipart/x-mixed-replace; boundary=frame', headers=NO_CACHE)
    settings = service.client.settings
    if not settings.key:
        raise DossierError(503, 'camera_not_configured', 'La connexion Tiandy n’est pas configurée.')
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout), trust_env=False, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request(
            'GET', settings.url.rstrip('/') + f'/api/cameras/{channel}/stream',
            headers={'X-API-Key': settings.key}), stream=True)
        content_type = upstream.headers.get('content-type', '')
        if upstream.status_code != 200 or not content_type.startswith('multipart/x-mixed-replace') or 'boundary=' not in content_type:
            await upstream.aclose()
            raise DossierError(503, 'camera_unavailable', 'Aperçu caméra indisponible.')
    except BaseException as error:
        await client.aclose()
        if isinstance(error, httpx.HTTPError):
            raise DossierError(503, 'camera_unavailable', 'Aperçu caméra injoignable.') from None
        raise

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                if await request.is_disconnected():
                    break
                yield chunk
        except httpx.HTTPError:
            return
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), headers={**NO_CACHE, 'Content-Type': content_type})
