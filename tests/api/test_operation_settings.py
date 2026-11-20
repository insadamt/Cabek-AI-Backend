"""Exercise saved mode snapshots, delayed relay cleanup and ownership races.

Depends on isolated storage, fixture workers and mocked RGB/Shelly transports.
"""
import json
import time
from unittest.mock import Mock

from fastapi.testclient import TestClient
from filelock import FileLock, Timeout
import pytest

from api.config import ApiSettings
from api.internal_lighting import InternalLightingSettings
from api.lighting_client import LightingClient, LightingSettings
from api.main import create_app
from test_capture import sample_photos, wait_capture
from test_scans import FixtureBackend, wait_for_terminal


def wait_until(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail('Lifecycle did not settle')


def create_test_dossier(client):
    result = client.post('/api/dossiers', json={'type': 'chiffrage_rapide', 'marque_id': 12,
        'model_id': 34, 'matricule': '14554-B-4578', 'date_mise_en_circulation': '2020-05-17'})
    assert result.status_code == 201, result.text
    return result.json()['dossier_id']


@pytest.fixture
def operating_app(storage, tmp_path, monkeypatch):
    samples = tmp_path / 'samples'
    samples.mkdir()
    sample_photos(samples, ('front_01.jpg',))
    lights = LightingClient(LightingSettings(enabled=True), send=lambda *args: (200, {'success': True}))
    settings = ApiSettings(storage, tunnel_mode='mock', mock_photos_dir=samples,
        scan_timeout_seconds=3, internal_lighting=InternalLightingSettings(True, 'http://shelly', stabilization_ms=0))
    app = create_app(settings, FixtureBackend(), lighting=lights)
    commands = []
    monkeypatch.setattr(app.state.internal_lighting, 'rpc', lambda method, payload=None:
        commands.append((method, payload)) or {'output': True, 'apower': 12})
    return app, commands


def test_mode_is_strict_persistent_and_local(storage):
    settings = ApiSettings(storage)
    with TestClient(create_app(settings), client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/settings/operation').json() == {'slow_mode': False, 'demo_mode': False}
        assert client.patch('/api/settings/operation', json={'slow_mode': 'yes'}).status_code == 422
        assert client.patch('/api/settings/operation', json={'slow_mode': True}).json() == {'slow_mode': True, 'demo_mode': False}
        assert client.patch('/api/settings/operation', json={'slow_mode': False}, headers={'Origin': 'https://remote.example'}).status_code == 403
    with TestClient(create_app(settings), client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/settings/operation').json() == {'slow_mode': True, 'demo_mode': False}
    with TestClient(create_app(settings), client=('192.168.1.5', 1234)) as client:
        assert client.get('/api/settings/operation').status_code == 403


@pytest.mark.parametrize('slow', [False, True])
def test_capture_owns_its_mode_and_interior_cleanup(operating_app, workflow_catalog, slow):
    app, commands = operating_app
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        client.patch('/api/settings/operation', json={'slow_mode': slow})
        dossier = create_test_dossier(client)
        assert client.post(f'/api/dossiers/{dossier}/capture').status_code == 202
        assert wait_capture(client, dossier)['slow_mode'] is slow
        client.patch('/api/settings/operation', json={'slow_mode': not slow})
        assert client.get(f'/api/dossiers/{dossier}/capture').json()['slow_mode'] is slow
        if slow:
            assert app.state.tunnel_lifecycle.snapshot()['dossier_id'] == dossier
            assert app.state.lighting.prepare_command()[2]['color'] == '#FF0000'
            assert not any(payload == {'id': 0, 'on': False} for _, payload in commands)
            other = create_test_dossier(client)
            assert client.post(f'/api/dossiers/{other}/capture').json()['error']['code'] == 'tunnel_busy'
            assert client.post('/api/settings/lights/off').status_code == 409
            assert client.post('/api/settings/internal-lights/off').status_code == 409
        else:
            wait_until(lambda: app.state.tunnel_lifecycle.snapshot() is None)
            assert app.state.lighting.prepare_command()[2]['color'] == '#00FF49'
            assert commands[-1] == ('Switch.Set', {'id': 0, 'on': False})
        result, _ = wait_for_terminal(client, dossier)
        assert result['status'] == 'completed'
        wait_until(lambda: app.state.tunnel_lifecycle.snapshot() is None)
        assert commands[-1] == ('Switch.Set', {'id': 0, 'on': False})
        assert app.state.lighting.prepare_command()[2]['color'] == '#00FF49'


@pytest.mark.parametrize('worker_mode,timeout', [('fail', 3), ('hang', .6)])
def test_slow_failure_releases_reservation(operating_app, workflow_catalog, worker_mode, timeout):
    app, commands = operating_app
    app.state.scan_manager.backend = FixtureBackend(worker_mode)
    from dataclasses import replace
    app.state.scan_manager.settings = replace(app.state.scan_manager.settings, scan_timeout_seconds=timeout)
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        client.patch('/api/settings/operation', json={'slow_mode': True})
        dossier = create_test_dossier(client)
        client.post(f'/api/dossiers/{dossier}/capture')
        wait_capture(client, dossier)
        result, _ = wait_for_terminal(client, dossier)
        assert result['status'] == 'failed'
        wait_until(lambda: app.state.tunnel_lifecycle.snapshot() is None)
        assert commands[-1] == ('Switch.Set', {'id': 0, 'on': False})
        assert app.state.lighting.prepare_command()[2]['color'] == '#FF9800'


def test_stale_callbacks_and_recovery_keep_matching_owner(storage, create_dossier, tmp_path):
    from api.repository import DossierRepository
    from api.tunnel_lifecycle import TunnelLifecycle

    directory = create_dossier()
    metadata = json.loads((directory / 'metadata.json').read_text())
    metadata['captures'] = {'single': {'capture_id': 'capture', 'status': 'completed', 'slow_mode': True}}
    (directory / 'metadata.json').write_text(json.dumps(metadata))
    (directory / 'analysis').mkdir()
    (directory / 'analysis/status.json').write_text(json.dumps({'status': 'processing', 'run_id': 'run'}))
    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'tunnel-lease.json').write_text(json.dumps({'dossier_id': 'DOS-001', 'capture_id': 'capture',
                                                      'slow_mode': True, 'run_id': 'run'}))
    exterior = LightingClient(LightingSettings())
    interior = Mock()
    lifecycle = TunnelLifecycle(DossierRepository(storage), root, exterior, interior)
    lifecycle.recover()
    assert lifecycle.snapshot()['run_id'] == 'run'
    lifecycle.finish_analysis('older', True)
    interior.cleanup.assert_not_called()
    with pytest.raises(Timeout):
        FileLock(str(storage / '.tunnel.lock')).acquire(timeout=0)
    lifecycle.finish_analysis('run', True)
    assert lifecycle.snapshot() is None
    interior.cleanup.assert_called_once()
    assert exterior.prepare_command()[2]['color'] == '#00FF49'
    with FileLock(str(storage / '.tunnel.lock'), timeout=0):
        pass


@pytest.mark.parametrize('slow', [False, True])
def test_verification_finishes_at_capture_without_waiting_for_ai(operating_app, workflow_catalog, slow):
    app, commands = operating_app
    sample_photos(app.state.capture_service.settings.mock_photos_dir,
                  ('rear_01.jpg', 'left_01.jpg', 'right_01.jpg'))
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        client.patch('/api/settings/operation', json={'slow_mode': slow})
        response = client.post('/api/dossiers', json={'type': 'verification'})
        dossier = response.json()['dossier_id']
        assert client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'before'}).status_code == 202
        assert wait_capture(client, dossier, 'before')['status'] == 'completed'
        wait_until(lambda: app.state.tunnel_lifecycle.snapshot() is None)
        assert app.state.lighting.prepare_command()[2]['color'] == '#00FF49'
        assert commands[-1] == ('Switch.Set', {'id': 0, 'on': False})
        assert client.get(f'/api/dossiers/{dossier}/result').json()['status'] == 'not_analyzed'


def test_slow_rejected_analysis_cleans_up(operating_app, workflow_catalog):
    app, commands = operating_app
    app.state.scan_manager.backend = FixtureBackend('unavailable')
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        client.patch('/api/settings/operation', json={'slow_mode': True})
        dossier = create_test_dossier(client)
        client.post(f'/api/dossiers/{dossier}/capture')
        wait_capture(client, dossier)
        wait_until(lambda: app.state.tunnel_lifecycle.snapshot() is None)
        capture = client.get(f'/api/dossiers/{dossier}/capture').json()
        assert capture['analysis_error'] == 'worker_unavailable'
        assert commands[-1] == ('Switch.Set', {'id': 0, 'on': False})
        assert app.state.lighting.prepare_command()[2]['color'] == '#FF9800'
