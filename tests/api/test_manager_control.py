"""Verify updater admission draining and manager credentials without live hardware.

Depends on isolated dossier fixtures and the real application middleware.
"""
import json


def test_health_requires_no_secret(client):
    assert client.get('/health').json() == {'status': 'ok'}


def test_management_requires_installation_key(client, monkeypatch):
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    assert client.get('/api/manager/status').status_code == 403
    assert client.post('/api/manager/maintenance', json={'enabled': True}).status_code == 403
    response = client.get('/api/manager/status', headers={'X-Cabek-Manager-Key': 'private-manager-key'})
    assert response.status_code == 200
    assert response.json()['busy'] is False


def test_maintenance_blocks_writes_but_preserves_reads(client, create_dossier, monkeypatch):
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    create_dossier()
    headers = {'X-Cabek-Manager-Key': 'private-manager-key'}
    assert client.post('/api/manager/maintenance', json={'enabled': True}, headers=headers).json()['maintenance']
    assert client.post('/api/dossiers/DOS-001/scan').status_code == 503
    assert client.get('/api/dossiers').status_code == 200
    assert not client.post('/api/manager/maintenance', json={'enabled': False}, headers=headers).json()['maintenance']
    assert client.post('/api/dossiers', json={}).status_code == 422


def test_queued_analysis_prevents_idle_confirmation(client, create_dossier, monkeypatch):
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    folder = create_dossier()
    (folder / 'analysis').mkdir()
    (folder / 'analysis/status.json').write_text(json.dumps({'status': 'queued'}))
    response = client.get('/api/manager/status', headers={'X-Cabek-Manager-Key': 'private-manager-key'})
    assert response.json()['busy'] is True


def test_capture_handoff_is_counted_until_background_task_returns(client, monkeypatch):
    from threading import Event
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    started, finish = Event(), Event()
    def work():
        started.set()
        finish.wait(3)
    future = client.app.state.capture_service.executor.submit(work)
    try:
        assert started.wait(3)
        response = client.get('/api/manager/status', headers={'X-Cabek-Manager-Key': 'private-manager-key'})
        assert response.json()['busy'] is True
    finally:
        finish.set()
        future.result(timeout=3)


def test_maintenance_survives_backend_replacement(client, storage, monkeypatch):
    from api.config import ApiSettings
    from api.main import create_app
    from fastapi.testclient import TestClient
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    headers = {'X-Cabek-Manager-Key': 'private-manager-key'}
    client.post('/api/manager/maintenance', json={'enabled': True}, headers=headers)
    with TestClient(create_app(ApiSettings(storage))) as replacement:
        assert replacement.get('/api/manager/status', headers=headers).json()['maintenance'] is True
        assert replacement.post('/api/dossiers', json={}).status_code == 503
        replacement.post('/api/manager/maintenance', json={'enabled': False}, headers=headers)
        assert replacement.post('/api/dossiers', json={}).status_code == 422


def test_analysis_finalization_remains_busy_until_its_executor_returns(client, monkeypatch):
    from threading import Event
    monkeypatch.setenv('CABEK_MANAGER_KEY', 'private-manager-key')
    started, finish = Event(), Event()
    def finalize():
        started.set()
        finish.wait(3)
    future = client.app.state.scan_manager.executor.submit(finalize)
    try:
        assert started.wait(3)
        response = client.get('/api/manager/status', headers={'X-Cabek-Manager-Key': 'private-manager-key'})
        assert response.json()['busy'] is True
    finally:
        finish.set()
        future.result(timeout=3)
