"""Verify capture precedes prepared results with temporary files and fake cameras.

Depends on private API fixtures; physical hardware and real AI never run.
"""
import io
import json
import time
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest
from PIL import Image
from scans.artifacts import publish_json
from api.private_demo.mock_results import MockResults
from api.private_demo.editor_document import load_document
from api.private_demo.editor_schemas import EditorPart, EditorDamage, Region
from api.private_demo.editor_save import write_editor_revision
from test_private_demo import private_app, private_client, native_template, register, HEADERS


def enable(client, source):
    document = load_document(source, {'title': 'Prepared', 'type': 'chiffrage_rapide'})
    identity = document.photos[0].id
    document.parts = [EditorPart(id='bumper', name='Pare-chocs avant', model_class='p_Pare-chocs avant',
        action='replace', photo_ids=[identity], damages=[EditorDamage(id='scratch', type='d_rayure', severity='medium', photo_ids=[identity])],
        amounts={key: 999999 for key in ('repair_labor', 'paint_labor', 'paint_materials', 'replacement_labor', 'parts_supply')})]
    document.photos[0].regions = [Region(id='scratch-region', layer='damages', shape='rectangle',
        points=[{'x': .1, 'y': .1}, {'x': .4, 'y': .4}], part_id='bumper', detection_id='scratch')]
    write_editor_revision(source, document, {})
    item = register(client, source, active=False)
    response = client.put('/api/private-demo/mock-results', json={'enabled': True, 'template_id': item['id']}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return item


@pytest.mark.parametrize('category', ['chiffrage_rapide', 'forfait_expertise'])
def test_capture_then_prepared_result_for_different_plates(private_client, private_app, native_template, workflow_catalog, monkeypatch, category):
    enable(private_client, native_template)
    private_app.state.operation_settings.update(demo_mode=True, slow_mode=True)
    buffer = io.BytesIO()
    Image.new('RGB', (20, 20), 'blue').save(buffer, 'JPEG')
    camera = private_app.state.capture_service.cameras
    plan = Mock(return_value=('tiandy', [{'source': 'camera-1.jpg', 'side': 'front', 'camera_channel': 1, 'camera_name': 'Front', 'camera_role': 'essential'}]))
    read = Mock(return_value={'content': buffer.getvalue(), 'extension': '.jpg', 'frame_id': 'live-frame'})
    monkeypatch.setattr(camera, 'plan', plan)
    monkeypatch.setattr(camera, 'baseline', Mock(return_value={}))
    monkeypatch.setattr(camera, 'read', read)
    monkeypatch.setattr(private_app.state.scan_manager.backend, 'ensure_available', Mock(side_effect=AssertionError('Real AI forbidden')))
    def launch(request_path, log):
        script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from api.vehicle_catalog import VehicleCatalog
from scans.worker import run_accepted_scan
from scans.runner import analyze_dossier
import scans.runner
scans.runner.load_models = lambda *args: (_ for _ in ()).throw(AssertionError('YOLO forbidden'))
VehicleCatalog.resolve = lambda self, selection: SimpleNamespace(model_dump=lambda: dict(marque_id=selection.marque_id, model_id=selection.model_id, marque='Dacia', modele='Sandero'))
run_accepted_scan(Path(sys.argv[1]), sys.argv[2], analyze_dossier)
"""
        request = json.loads(request_path.read_text())
        return subprocess.Popen([sys.executable, '-c', script, str(request_path), request['run_id']], stdout=log, stderr=log)
    monkeypatch.setattr(private_app.state.scan_manager.backend, 'launch', launch)

    for index in range(2):
        plate = f'{index + 100}-B-45'
        if category == 'chiffrage_rapide':
            draft = private_client.post('/api/dossiers', json={'type': category, 'matricule': plate, 'marque_id': 12, 'model_id': 34, 'date_mise_en_circulation': '2021-01-01'}).json()
            identity = draft['dossier_id']
        else:
            identity = f'DOS-EXTERNAL-{index}'
            directory = private_app.state.private_demo.repository.dossiers_dir / identity
            directory.mkdir()
            publish_json(directory / 'metadata.json', {'dossier_id': identity, 'type': category, 'matricule': plate, 'source': 'external', 'marque': 'Dacia', 'modele': 'Sandero', 'photos': []})
        response = private_client.post(f'/api/dossiers/{identity}/capture', json={})
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = private_client.get(f'/api/dossiers/{identity}').json()
            if detail['status'] == 'completed':
                break
            time.sleep(.01)
        assert detail['status'] == 'completed', detail
        assert detail['dossier_id'] == identity and detail['type'] == category
        assert detail['demo'] and detail['result']['vehicle']['matricule'] == plate
        from bareme_c2 import calculer_cout_dommage
        expected = calculer_cout_dommage('p_Pare-chocs avant', 'd_rayure', 'mid')['total']
        assert detail['result']['financial']['estimated_total'] == expected
        assert detail['result']['parts'][0]['action'] == 'repair'
        photo = detail['result']['photos'][0]
        assert private_client.get(photo['original_url']).content != buffer.getvalue()
        assert private_client.get(photo['parts_url']).status_code == 200
        assert private_client.get(detail['result']['report_url']).status_code == 200
        directory = private_app.state.private_demo.repository.dossiers_dir / identity
        metadata = json.loads((directory / 'metadata.json').read_text())
        assert (directory / metadata['photo_history'][0]['path']).read_bytes() == buffer.getvalue()
        assert private_client.get(f'/api/dossiers/{identity}/capture').json()['saved_count'] == 1
        assert private_app.state.tunnel_lifecycle.lease is None
    assert read.call_count == 2 and plan.call_args.args[2] == 'tiandy'


def test_empty_dossier_requires_capture(private_client, native_template):
    enable(private_client, native_template)
    draft = private_client.post('/api/dossiers', json={'type': 'chiffrage_rapide'}).json()
    assert private_client.post(f"/api/dossiers/{draft['dossier_id']}/scan").status_code == 409


def test_selected_revision_stays_pinned_after_draft_save(private_client, private_app, native_template):
    item = enable(private_client, native_template)
    store = private_app.state.private_demo.templates
    store.commit(identity=item['id'], title='Draft change')
    configuration = MockResults(private_app.state.private_demo).read()
    assert configuration['version'] == item['version'] != store.get(item['id'])['version']


def test_disable_and_minimal_creation(private_client, private_app, native_template):
    enable(private_client, native_template)
    response = private_client.put('/api/private-demo/mock-results', json={'enabled': False}, headers=HEADERS)
    assert response.status_code == 200
    assert private_app.state.private_demo.mock_results.apply('unused') is None
    response = private_client.post('/api/private-demo/mock-template', json={}, headers=HEADERS)
    assert response.status_code == 201, response.text
    assert private_client.put('/api/private-demo/mock-results', json={'enabled': True, 'template_id': response.json()['id']}, headers=HEADERS).status_code == 422
    assert private_client.put('/api/private-demo/mock-results', json={'enabled': False}).status_code == 403


def test_failed_result_publication_restores_camera_metadata(private_client, private_app, native_template, create_dossier, monkeypatch):
    enable(private_client, native_template)
    directory = create_dossier('DOS-PUBLISH-FAIL')
    before = (directory / 'metadata.json').read_bytes()
    import api.private_demo.mock_results as module
    publish = module.publish_bytes
    def fail_report(path, content):
        if path.name.endswith('_pieces.png'):
            raise OSError('Simulated storage failure')
        return publish(path, content)
    monkeypatch.setattr(module, 'publish_bytes', fail_report)
    with pytest.raises(Exception, match='Simulated storage failure|Dossier storage'):
        private_app.state.private_demo.mock_results.apply('DOS-PUBLISH-FAIL')
    assert (directory / 'metadata.json').read_bytes() == before
    assert not (directory / 'analysis/result.json').exists()
    assert not (directory / 'analysis/status.json').exists()
    assert not list(directory.parent.glob('.mock-stage-*'))


def test_failed_capture_keeps_originals_until_successful_retry(private_client, private_app, native_template, create_dossier):
    enable(private_client, native_template)
    directory = create_dossier('DOS-PARTIAL')
    path = directory / 'metadata.json'
    metadata = json.loads(path.read_text())
    metadata['captures'] = {'single': {'status': 'failed', 'capture_id': 'partial'}}
    publish_json(path, metadata)
    before = path.read_bytes()
    response = private_client.post('/api/dossiers/DOS-PARTIAL/scan')
    assert response.status_code == 409
    assert path.read_bytes() == before
    assert not (directory / '.mock-evidence').exists()


def test_prepared_publication_remains_readable_while_files_are_written(private_client, private_app, native_template, create_dossier, monkeypatch):
    enable(private_client, native_template)
    create_dossier('DOS-PUBLICATION-POLL')
    import api.private_demo.mock_results as module
    publish = module.publish_bytes
    observed = []
    def observe_publication(path, content):
        response = private_client.get('/api/dossiers/DOS-PUBLICATION-POLL')
        assert response.status_code == 200, response.text
        observed.append(response.json())
        return publish(path, content)
    monkeypatch.setattr(module, 'publish_bytes', observe_publication)
    private_app.state.private_demo.mock_results.apply('DOS-PUBLICATION-POLL')
    assert any(item['progress'] and item['progress']['stage'] == 'preparing' for item in observed)
    assert not any(item.get('result') for item in observed)
