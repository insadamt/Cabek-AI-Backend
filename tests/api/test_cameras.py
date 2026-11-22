"""Verify camera admission, immutable retries and demo isolation without hardware.

Depends on temporary sample files, fixture workers and mocked Tiandy responses.
"""

from io import BytesIO
import json
from threading import Event
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api.camera_registry import CAMERAS, ESSENTIAL_CHANNELS, ZOOM_CHANNELS
from api.config import ApiSettings
from api.errors import DossierError
from api.main import create_app
from api.tiandy_client import TiandyClient, TiandySettings
from test_capture import sample_photos, wait_capture
from test_scans import FixtureBackend


@pytest.fixture
def camera_app(storage, tmp_path):
    samples = tmp_path / 'samples'
    samples.mkdir()
    for phase, color in [('before', 'red'), ('after', 'blue')]:
        for side in ('front', 'rear', 'left', 'right'):
            path = samples / phase / f'{side}_01.jpg'
            path.parent.mkdir(exist_ok=True)
            image = Image.new('RGB', (40, 20), color)
            image.paste('green', (20, 0, 40, 20))
            image.save(path)
    sample_photos(samples, ('front_01.jpg', 'rear_01.jpg', 'left_01.jpg', 'right_01.jpg'))
    app = create_app(ApiSettings(storage, tunnel_mode='tiandy', mock_photos_dir=samples), FixtureBackend())
    app.state.operation_settings.update(demo_mode=True)
    return app


def test_settings_partial_updates_and_legacy_file(camera_app):
    store = camera_app.state.operation_settings
    store.path.write_text(json.dumps({'slow_mode': True}))
    with TestClient(camera_app, client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/settings/operation').json() == {'slow_mode': True, 'demo_mode': False}
        assert client.patch('/api/settings/operation', json={'demo_mode': True}).status_code == 403
        store.update(demo_mode=True)
        for invalid in ({}, {'demo_mode': None}, {'demo_mode': 1}, {'demo_mode': 'true'}):
            assert client.patch('/api/settings/operation', json=invalid).status_code == 422
        assert client.patch('/api/settings/operation', json={'slow_mode': False}).json()['demo_mode']


def test_demo_catalog_and_crops_require_no_upstream(camera_app, monkeypatch):
    upstream = Mock(side_effect=AssertionError('Demo must not contact Tiandy'))
    monkeypatch.setattr(camera_app.state.capture_service.cameras.client, 'cameras', upstream)
    with TestClient(camera_app) as client:
        catalog = client.get('/api/cameras?phase=before').json()
        assert catalog['source'] == 'demo' and len(catalog['cameras']) == 13
        assert {c['channel'] for c in catalog['cameras'] if c['role'] == 'essential'} == set(ESSENTIAL_CHANNELS)
        assert {c['channel'] for c in catalog['cameras'] if c['role'] == 'zoom'} == set(ZOOM_CHANNELS)
        assert not next(c for c in catalog['cameras'] if c['channel'] == 3)['online']
        base = client.get('/api/cameras/13/latest.jpg?phase=before')
        zoom = client.get('/api/cameras/1/latest.jpg?phase=before')
        assert base.status_code == zoom.status_code == 200
        assert Image.open(BytesIO(base.content)).size == (40, 20)
        assert Image.open(BytesIO(zoom.content)).size == (20, 20)
        assert zoom.content != client.get('/api/cameras/1/latest.jpg?phase=after').content
        assert client.get('/api/cameras/99/latest.jpg').status_code == 404
        assert 'api_key' not in json.dumps(catalog).lower()


def test_demo_capture_counts_identity_and_essential_comparisons(camera_app):
    with TestClient(camera_app) as client:
        dossier = client.post('/api/dossiers', json={'type': 'verification'}).json()['dossier_id']
        for phase in ('before', 'after'):
            response = client.post(f'/api/dossiers/{dossier}/capture', json={'phase': phase, 'zoom_channels': [1, 2]})
            assert response.status_code == 202, response.text
            completed = wait_capture(client, dossier, phase)
            assert completed['status'] == 'completed' and completed['saved_count'] == completed['expected_count'] == 6
            assert completed['capture_source'] == 'demo' and completed['zoom_channels'] == [1, 2]
            assert all(photo['frame_id'].startswith('demo-') for photo in completed['photos'])
            duplicate = client.post(f'/api/dossiers/{dossier}/capture', json={'phase': phase, 'zoom_channels': []})
            assert duplicate.json()['capture_id'] == completed['capture_id']
        detail = client.get(f'/api/dossiers/{dossier}/result').json()
        assert detail['status'] == 'not_analyzed'
        assert all(slot['before']['camera_role'] == slot['after']['camera_role'] == 'essential' for slot in detail['comparison']['slots'])


@pytest.mark.parametrize('selection', [[3], [4], [1, 1], [99], [True], ['1'], list(range(9))])
def test_zoom_selection_is_server_validated(camera_app, selection):
    with TestClient(camera_app) as client:
        dossier = client.post('/api/dossiers', json={'type': 'verification'}).json()['dossier_id']
        assert client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'before', 'zoom_channels': selection}).status_code == 422


def mock_live_cameras(app, monkeypatch):
    cameras = app.state.capture_service.cameras
    monkeypatch.setattr(cameras.client, 'cameras', lambda: {
        channel: {'name': name, 'online': True} for channel, (name, _, _) in CAMERAS.items()})
    buffer = BytesIO()
    Image.new('RGB', (30, 20), 'white').save(buffer, format='JPEG')
    frame = {'content': buffer.getvalue(), 'extension': '.jpg', 'frame_id': 'fresh'}
    monkeypatch.setattr(cameras.client, 'snapshot', lambda ch: {**frame, 'frame_id': 'baseline'})
    monkeypatch.setattr(cameras.client, 'fresh_snapshot', lambda *args: frame)
    return cameras, frame


def test_real_retry_replaces_current_phase_and_preserves_before(camera_app, monkeypatch, storage):
    cameras, frame = mock_live_cameras(camera_app, monkeypatch)
    camera_app.state.operation_settings.update(demo_mode=False)
    with TestClient(camera_app) as client:
        dossier = client.post('/api/dossiers', json={'type': 'verification'}).json()['dossier_id']
        client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'before', 'zoom_channels': []})
        before = wait_capture(client, dossier, 'before')
        assert before['status'] == 'completed'
        def fail_zoom(channel, *args):
            if channel == 1:
                raise DossierError(503, 'camera_frame_stale', 'Aucune nouvelle image.')
            return frame
        monkeypatch.setattr(cameras.client, 'fresh_snapshot', fail_zoom)
        client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'after', 'zoom_channels': [1]})
        failed = wait_capture(client, dossier, 'after')
        assert failed['status'] == 'failed' and failed['saved_count'] == 4
        assert 'Arrière_gauche' in failed['error_message']
        assert camera_app.state.tunnel_lifecycle.snapshot() is None
        monkeypatch.setattr(cameras.client, 'fresh_snapshot', lambda *args: frame)
        client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'after', 'zoom_channels': [1]})
        completed = wait_capture(client, dossier, 'after')
        assert completed['status'] == 'completed' and completed['saved_count'] == 5
        assert completed['capture_id'] != failed['capture_id']
        assert client.get(f'/api/dossiers/{dossier}/capture?phase=before').json()['photos'] == before['photos']
        metadata = json.loads((storage / dossier / 'metadata.json').read_text())
        assert len(metadata['photo_history']) == 4 and len(metadata['photos']) == 9


def test_source_snapshot_survives_setting_change(camera_app, monkeypatch):
    entered, release = Event(), Event()
    read = camera_app.state.capture_service.cameras.demo.read
    def blocked(sample):
        entered.set()
        assert release.wait(5)
        return read(sample)
    monkeypatch.setattr(camera_app.state.capture_service.cameras.demo, 'read', blocked)
    with TestClient(camera_app, client=('127.0.0.1', 1234)) as client:
        dossier = client.post('/api/dossiers', json={'type': 'verification'}).json()['dossier_id']
        try:
            assert client.post(f'/api/dossiers/{dossier}/capture', json={'phase': 'before', 'zoom_channels': [1]}).status_code == 202
            assert entered.wait(2)
            camera_app.state.operation_settings.update(demo_mode=False)
        finally:
            release.set()
        result = wait_capture(client, dossier, 'before')
        assert result['status'] == 'completed' and result['capture_source'] == 'demo'


def test_fresh_frame_must_advance():
    camera = TiandyClient(TiandySettings(key='private-test-key', timeout=.01))
    camera.snapshot = lambda channel: {'frame_id': 'unchanged'}
    with pytest.raises(DossierError, match='nouvelle image'):
        camera.fresh_snapshot(4, 'unchanged', Event())
