"""Verify maintenance exit restores temporary effects without changing saved operation mode.

Depends on isolated storage and mocked device commands; no physical lights are used.
"""
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.config import ApiSettings
from api.main import create_app


def test_exit_restores_manual_tests_and_keeps_slow_mode(tmp_path):
    root = tmp_path / 'dossiers'
    root.mkdir()
    app = create_app(ApiSettings(root))
    app.state.lighting.send = Mock(return_value=(200, {'success': True}))
    app.state.internal_lighting.set_output = Mock()
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        client.patch('/api/settings/operation', json={'slow_mode': True})
        app.state.lighting.send_manual_command('/set', {'effect': 'police', 'repeat': True})
        app.state.internal_lights_manual_override = True
        assert client.post('/api/settings/restore-defaults').json() == {'restored': True}
        assert app.state.lighting.prepare_command()[2]['color'] == '#FFFFFF'
        assert not app.state.lighting.manual_override
        app.state.internal_lighting.set_output.assert_called_once_with(False)
        assert client.get('/api/settings/operation').json()['slow_mode'] is True


def test_exit_preserves_automatic_lights_and_active_reservation(tmp_path):
    root = tmp_path / 'dossiers'
    root.mkdir()
    app = create_app(ApiSettings(root))
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        app.state.lighting.begin_capture('capture')
        app.state.lighting.finish_capture('capture', True)
        assert client.post('/api/settings/restore-defaults').json()['restored']
        assert app.state.lighting.prepare_command()[2]['color'] == '#00FF49'
        app.state.tunnel_lifecycle.snapshot = lambda: {'capture_id': 'busy'}
        assert client.post('/api/settings/restore-defaults').json()['reason'] == 'tunnel_busy'
        assert app.state.lighting.prepare_command()[2]['color'] == '#00FF49'


def test_exit_rejects_remote_origin(tmp_path):
    root = tmp_path / 'dossiers'
    root.mkdir()
    with TestClient(create_app(ApiSettings(root)), client=('127.0.0.1', 1234)) as client:
        assert client.post('/api/settings/restore-defaults', headers={'Origin': 'https://remote.example'}).status_code == 403
