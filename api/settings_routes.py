"""Forward local settings light tests through CABEK AI instead of browser USB URLs.

Depends on the existing LightingClient and its no-redirect HTTP transport. The
independent service validates effect fields and remains the only serial owner.
"""
import ipaddress
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.errors import DossierError


def is_loopback_host(host):
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def require_local_settings(request: Request):
    if not is_loopback_host(getattr(request.client, 'host', None)):
        raise DossierError(403, 'local_settings_only', 'Hardware settings are available on the tunnel PC only.')
    origin = request.headers.get('origin')
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed_origin = parsed.scheme in ('http', 'https') and is_loopback_host(parsed.hostname)
        except ValueError:
            allowed_origin = False
        # A remote website must not issue simple POSTs against a local hardware endpoint.

        if not allowed_origin:
            raise DossierError(403, 'local_settings_only', 'Hardware settings require a local browser origin.')
    if not request.app.state.lighting.settings.enabled:
        raise DossierError(503, 'lighting_disabled', 'Lighting control is disabled on the backend.')


router = APIRouter(prefix='/api/settings/lights', tags=['Local settings'],
                   dependencies=[Depends(require_local_settings)])


def forward_test(request, path, payload=None):
    try:
        status, result = request.app.state.lighting.send_manual_command(path, payload)
        return JSONResponse(status_code=status, content=result)
    except HTTPError as error:
        if error.code in (409, 422, 501, 503):
            try:
                detail = json.loads(error.read(8192))['error']
                if not isinstance(detail['code'], str) or not isinstance(detail['message'], str):
                    raise ValueError('Invalid upstream error')
                return JSONResponse(status_code=error.code, content={
                    'success': False, 'error': {'code': detail['code'], 'message': detail['message']},
                })
            except (ValueError, KeyError, TypeError):
                pass
        raise DossierError(502, 'lighting_response_invalid', 'Lighting service returned an invalid response.') from error
    except OSError as error:
        raise DossierError(503, 'lighting_unavailable', 'Could not reach the lighting service or deliver the command.') from error
    except (ValueError, TypeError) as error:
        raise DossierError(502, 'lighting_response_invalid', 'Lighting service returned an invalid response.') from error


@router.post('/set')
def set_lights(payload: dict, request: Request):
    return forward_test(request, '/set', payload)


@router.post('/off')
def off(request: Request):
    return forward_test(request, '/off')


@router.post('/startup')
def startup(request: Request):
    request.app.state.lighting.begin_startup()
    return {'success': True}


@router.post('/ready')
def ready(request: Request):
    request.app.state.lighting.application_ready()
    return {'success': True}
