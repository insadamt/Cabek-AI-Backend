"""Verify local settings forwarding and hardware failures without USB or network.

Depends on TestClient, temporary dossier storage and a mocked LightingClient.
"""
import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.lighting_client import LightingClient, LightingSettings
from api.main import create_app


@pytest.fixture
def settings_api(storage):
    lighting = LightingClient(LightingSettings(enabled=True), send=lambda *args: (200, {'success': True}))
    lighting.send_manual_command = Mock(return_value=(202, {'success': True, 'effect': 'pulse', 'status': 'running'}))
    app = create_app(ApiSettings(storage), lighting=lighting)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        yield client, lighting


def test_forwards_effect_fields_without_readiness_checks(settings_api):
    client, lighting = settings_api
    payload = {'effect': 'pulse', 'color': '#FF0000', 'brightness': 70, 'speed': 2, 'repeat': True}
    response = client.post('/api/settings/lights/set', json=payload)
    assert response.status_code == 202 and response.json()['status'] == 'running'
    lighting.send_manual_command.assert_called_once_with('/set', payload)


def test_off_is_bodyless_and_opening_settings_does_not_send_hardware_commands(settings_api):
    client, lighting = settings_api
    client.get('/openapi.json')
    lighting.send_manual_command.assert_not_called()
    lighting.send_manual_command.return_value = (200, {'success': True, 'status': 'off'})
    assert client.post('/api/settings/lights/off').json() == {'success': True, 'status': 'off'}
    lighting.send_manual_command.assert_called_once_with('/off', None)
    assert client.post('/api/settings/lights/set', json=[]).status_code == 422


@pytest.mark.parametrize('code', [409, 422, 501, 503])
def test_preserves_upstream_hardware_and_validation_errors(settings_api, code):
    client, lighting = settings_api
    detail = {'success': False, 'error': {'code': 'hardware_error', 'message': 'Firmware or hardware unavailable'}}
    lighting.send_manual_command.side_effect = HTTPError('http://local', code, '', {}, io.BytesIO(json.dumps(detail).encode()))
    response = client.post('/api/settings/lights/set', json={})
    assert response.status_code == code and response.json() == detail


@pytest.mark.parametrize('error, code', [(OSError('offline'), 503), (TimeoutError(), 503),
    (ValueError('bad JSON'), 502), (HTTPError('http://local', 302, '', {}, io.BytesIO(b'')), 502)])
def test_transport_errors_never_claim_success(settings_api, error, code):
    client, lighting = settings_api
    lighting.send_manual_command.side_effect = error
    response = client.post('/api/settings/lights/off')
    assert response.status_code == code and 'error' in response.json()


def test_blocks_lan_clients_and_disabled_lighting(settings_api):
    client, lighting = settings_api
    with TestClient(client.app, client=('192.168.1.20', 50000)) as remote:
        assert remote.post('/api/settings/lights/off').status_code == 403
    lighting.send_manual_command.assert_not_called()
    lighting.settings = LightingSettings(enabled=False)
    assert client.post('/api/settings/lights/off').status_code == 503


def test_rejects_remote_browser_origin_but_allows_local_settings(settings_api):
    client, lighting = settings_api
    assert client.post('/api/settings/lights/off', headers={'Origin': 'https://outside.example'}).status_code == 403
    lighting.send_manual_command.assert_not_called()
    assert client.post('/api/settings/lights/off', headers={'Origin': 'http://localhost:5173'}).status_code == 202
