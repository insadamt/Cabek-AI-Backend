"""Verify Shelly gating without electrical switching.

Depends on mocked HTTP responses, temporary captures, and existing scan fixtures.
"""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api.config import ApiSettings
from api.errors import DossierError
from api.internal_lighting import InternalLighting, InternalLightingSettings
from api.main import create_app
from test_capture import sample_photos, wait_capture
from test_scans import FixtureBackend


@pytest.mark.parametrize('status,threshold,code', [
    ({'output': True, 'apower': 12}, 10, None),
    ({'output': False}, None, 'internal_lighting_not_confirmed'),
    ({'output': True, 'apower': 5}, 10, 'internal_lighting_power_low'),
    ({'output': True}, 10, 'internal_lighting_power_low'),
    ({'output': True, 'apower': 'bad'}, 10, 'internal_lighting_power_low'),
    ({'output': True, 'apower': 10}, 10, 'internal_lighting_power_low'),
])
def test_mocked_shelly_http(monkeypatch, status, threshold, code):
    lights = InternalLighting(InternalLightingSettings(True, 'http://shelly', min_power_w=threshold))
    opener = Mock()
    responses = []
    for result in ({'was_on': False}, status):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps(result).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        responses.append(response)
    opener.open.side_effect = responses
    monkeypatch.setattr('api.internal_lighting.build_opener', lambda *args: opener)
    if code:
        with pytest.raises(DossierError) as error:
            lights.activate_and_verify()
        assert error.value.code == code
    else:
        lights.activate_and_verify()
    calls = opener.open.call_args_list
    assert calls[0].args[0].data == b'{"id": 0, "on": true}'
    assert calls[1].args[0].full_url == 'http://shelly/rpc/Switch.GetStatus?id=0'


def test_disabled_never_calls_http(monkeypatch):
    opener = Mock(side_effect=AssertionError('No network'))
    monkeypatch.setattr('api.internal_lighting.build_opener', opener)
    lights = InternalLighting(InternalLightingSettings())
    lights.activate_and_verify()
    lights.cleanup()
    opener.assert_not_called()


@pytest.mark.parametrize('warning_fails', [False, True])
def test_failure_blocks_photos_and_retry_succeeds(storage, tmp_path, monkeypatch, warning_fails):
    samples = tmp_path / 'samples'
    samples.mkdir()
    sample_photos(samples, ('front_01.jpg',))
    settings = ApiSettings(storage, tunnel_mode='mock', mock_photos_dir=samples,
                           internal_lighting=InternalLightingSettings(True, 'http://shelly', stabilization_ms=50))
    app = create_app(settings, FixtureBackend())
    lights = app.state.internal_lighting
    monkeypatch.setattr(lights, 'rpc', Mock(side_effect=DossierError(
        503, 'internal_lighting_unavailable', 'Indisponible')))
    def send_effect(effect, **parameters):
        if warning_fails and effect == 'warning':
            raise RuntimeError()
    warning = Mock(side_effect=send_effect)
    monkeypatch.setattr(app.state.lighting, 'set_effect', warning)
    with TestClient(app) as client:
        dossier = client.post('/api/dossiers', json={'type': 'chiffrage_rapide'}).json()['dossier_id']
        client.post(f'/api/dossiers/{dossier}/capture')
        failed = wait_capture(client, dossier)
        assert failed['saved_count'] == 0
        assert failed['error_code'] == 'internal_lighting_unavailable'
        assert failed['error_message']
        warning.assert_called_with('warning', repeat=True, brightness=100)
        events = []
        monkeypatch.setattr(lights, 'rpc', lambda method, payload=None: events.append((method, payload)) or {'output': True})
        read = app.state.capture_service.provider.read_photo
        def read_photo(source):
            assert events == [('Switch.Set', {'id': 0, 'on': True}), ('Switch.GetStatus', None)]
            return read(source)
        monkeypatch.setattr(app.state.capture_service.provider, 'read_photo', read_photo)
        import time
        started = time.monotonic()
        client.post(f'/api/dossiers/{dossier}/capture')
        assert wait_capture(client, dossier)['status'] == 'completed'
        assert time.monotonic() - started >= .05
    assert events[-1] == ('Switch.Set', {'id': 0, 'on': False})


def test_environment_requires_explicit_disable_or_device_url(monkeypatch):
    monkeypatch.delenv('CABEK_INTERNAL_LIGHTS_ENABLED', raising=False)
    monkeypatch.delenv('CABEK_INTERNAL_LIGHTS_URL', raising=False)
    with pytest.raises(ValueError):
        InternalLightingSettings.from_environment()
    monkeypatch.setenv('CABEK_INTERNAL_LIGHTS_ENABLED', 'false')
    assert not InternalLightingSettings.from_environment().enabled


def test_transport_failure_is_safe(monkeypatch):
    opener = Mock()
    opener.open.side_effect = TimeoutError()
    monkeypatch.setattr('api.internal_lighting.build_opener', lambda *args: opener)
    with pytest.raises(DossierError) as error:
        InternalLighting(InternalLightingSettings(True, 'http://shelly')).activate_and_verify()
    assert error.value.code == 'internal_lighting_unavailable'


def test_local_maintenance_shares_tunnel_lock(storage, monkeypatch):
    from filelock import FileLock

    app = create_app(ApiSettings(storage), FixtureBackend())
    command = Mock()
    monkeypatch.setattr(app.state.internal_lighting, 'set_output', command)
    monkeypatch.setattr(app.state.internal_lighting, 'status', lambda: {'output': True, 'apower': 12})
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        assert client.get('/api/settings/internal-lights/status').json()['apower'] == 12
        with FileLock(str(storage / '.tunnel.lock')):
            assert client.post('/api/settings/internal-lights/off').status_code == 409
            assert client.post('/api/settings/internal-lights/on').status_code == 409
        command.assert_not_called()
        assert client.post('/api/settings/internal-lights/on').status_code == 200
        command.assert_called_once_with(True)
        assert client.post('/api/settings/internal-lights/off', headers={'Origin': 'https://remote.example'}).status_code == 403
    with TestClient(app, client=('192.168.1.20', 50000)) as client:
        assert client.get('/api/settings/internal-lights/status').status_code == 403


def test_cleanup_failure_does_not_replace_result(monkeypatch, caplog):
    lights = InternalLighting(InternalLightingSettings(True, 'http://shelly'))
    monkeypatch.setattr(lights, 'set_output', Mock(side_effect=OSError('offline')))
    lights.cleanup()
    assert 'Internal lighting OFF failed' in caplog.text
