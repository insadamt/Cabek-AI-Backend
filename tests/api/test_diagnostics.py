"""Verify independent safe checks and persistent redacted diagnostic history.

Depends on temporary files and mocked clients; no real service or device is called.
"""
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from api.config import ApiSettings
from api.diagnostic_logs import DiagnosticLogStore, redact
from api.errors import DossierError
from api.main import create_app
from api.repository import DossierRepository
from test_scans import FixtureBackend


def test_safe_checks_isolate_failure_and_never_switch_hardware(storage, monkeypatch):
    app = create_app(ApiSettings(storage), FixtureBackend())
    activate = Mock(side_effect=AssertionError('No switching'))
    monkeypatch.setattr(app.state.internal_lighting, 'set_output', activate)
    monkeypatch.setattr(app.state.vehicle_catalog, 'brands', Mock(side_effect=DossierError(503, 'catalogue_down', 'Catalogue indisponible.')))
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        assert all(item['status'] == 'not_tested' for item in client.get('/api/settings/diagnostics').json()['checks'])
        response = client.post('/api/settings/diagnostics/run', json={})
        assert response.status_code == 200
        checks = {item['id']: item for item in response.json()['checks']}
        assert checks['backend']['status'] == 'ok'
        assert checks['storage']['status'] in ('ok', 'warning')
        assert checks['catalogue']['status'] == 'error'
        assert checks['interior']['status'] == checks['exterior']['status'] == 'disabled'
        assert checks['ai']['status'] == 'ok'
        assert client.post('/api/settings/diagnostics/run', json={'checks': ['unknown']}).status_code == 422
        assert client.post('/api/settings/diagnostics/run', json={'checks': ['backend']}).status_code == 200
        assert client.get('/api/settings/logs').json()['entries']
    activate.assert_not_called()


@pytest.mark.parametrize('method,path,payload', [
    ('get', '/api/settings/diagnostics', None), ('get', '/api/settings/logs', None),
    ('post', '/api/settings/diagnostics/run', {}),
    ('post', '/api/settings/diagnostics/pricing', {'marque_id': 1, 'model_id': 1, 'part_id': 1}),
])
def test_diagnostics_preserve_local_boundary(storage, method, path, payload):
    with TestClient(create_app(ApiSettings(storage)), client=('192.168.1.5', 1234)) as client:
        options = {'json': payload} if payload is not None else {}
        assert getattr(client, method)(path, **options).status_code == 403


def test_logs_rotate_persist_filter_and_redact(storage, tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOESTIMATE_TOKEN', 'super-private-token')
    root = tmp_path / 'runtime'
    repository = DossierRepository(storage)
    store = DiagnosticLogStore(root, repository, max_bytes=650)
    for index in range(12):
        store.record('ERROR' if index % 2 else 'INFO', 'capture',
                     f'index={index} Bearer super-private-token password=another-secret', dossier_id='TEST')
    page = store.read(limit=2, level='ERROR', dossier_id='TEST')
    assert len(page['entries']) == 2
    assert page['next_cursor']
    older = store.read(limit=2, level='ERROR', dossier_id='TEST', cursor=page['next_cursor'])
    assert not {entry['id'] for entry in page['entries']} & {entry['id'] for entry in older['entries']}
    assert 'super-private-token' not in str(page)
    assert 'another-secret' not in str(page)
    store.close()
    restored = DiagnosticLogStore(root, repository)
    assert restored.read()['entries']
    assert len(list((root / 'logs').glob('application.jsonl*'))) <= 5
    restored.close()


def test_worker_log_is_bounded_confined_and_redacted(storage, complete_dossier, tmp_path):
    directory, _ = complete_dossier()
    (directory / 'analysis/.worker.log').write_text('token=secret-value\n' + 'line\n' * 700)
    store = DiagnosticLogStore(tmp_path / 'runtime', DossierRepository(storage))
    entries = store.read_worker('DOS-001')['entries']
    assert len(entries) == 500
    with pytest.raises(DossierError):
        store.read_worker('../outside')
    assert 'secret-value' not in redact('token=secret-value')
    store.close()


@pytest.mark.parametrize('connected,last_error', [(False, None), (True, 'Serial acknowledgment failed')])
def test_lighting_bridge_without_working_serial_is_not_reported_ready(storage, monkeypatch, connected, last_error):
    import json
    from types import SimpleNamespace
    app = create_app(ApiSettings(storage))
    app.state.tunnel_lifecycle.exterior = SimpleNamespace(settings=SimpleNamespace(enabled=True, url='http://lighting:8001'))
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({'service': 'cabek-lighting', 'serial_connected': connected,
                                            'last_error': last_error}).encode()
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr('api.diagnostics.build_opener', lambda *args: opener)
    result = app.state.diagnostics.check_exterior()
    assert result['status'] == 'error'
    assert result['code'] == 'lighting_hardware_unavailable'
