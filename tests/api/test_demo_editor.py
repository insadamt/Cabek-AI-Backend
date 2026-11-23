"""Exercise atomic manual authoring and native demo output without hardware.

Depends on temporary native fixtures and authenticated routes; PDF text and image
pixels verify that edited evidence and money reach the published artifacts.
"""
from copy import deepcopy
from io import BytesIO
import json
from unittest.mock import Mock
from PIL import Image
import pymupdf
from test_private_demo import HEADERS, private_app, private_client, native_template, register


def image_bytes(color='white', size=(120, 80)):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return output.getvalue()


def create_editor(client, category='chiffrage_rapide'):
    response = client.post('/api/private-demo/templates', headers=HEADERS, json={
        'title': 'Dacia — choc avant',
        'metadata': {'dossier_id': 'TEMPLATE-MANUAL', 'type': category, 'source': 'cabek_ai',
                     'marque': 'Dacia', 'modele': 'Sandero', 'matricule': '84547-H-06',
                     'legacy_business_field': 'Keep this', 'photos': []},
    })
    assert response.status_code == 201, response.text
    return client.get('/api/private-demo/templates/' + response.json()['id']).json()


def save_editor(client, template, document, uploads=None):
    return client.put('/api/private-demo/templates/' + template['id'] + '/editor', headers=HEADERS,
                      data={'document': json.dumps({'expected_version': template['version'], 'document': document})},
                      files=[('files', (name, content, 'application/octet-stream')) for name, content in (uploads or {}).items()])


def prepared_document(template):
    document = deepcopy(template['editor'])
    document['photos'] = [{
        'id': 'manual-front', 'upload_key': 'front.png', 'side': 'front', 'camera_channel': 4,
        'camera_name': 'Avant', 'camera_role': 'essential', 'original_filename': 'front.png',
        'regions': [
            {'id': 'part-zone', 'layer': 'parts', 'shape': 'rectangle', 'points': [{'x': .1, 'y': .1}, {'x': .8, 'y': .8}], 'part_id': 'bumper'},
            {'id': 'damage-zone', 'layer': 'damages', 'shape': 'polygon', 'points': [{'x': .2, 'y': .2}, {'x': .6, 'y': .2}, {'x': .4, 'y': .6}], 'part_id': 'bumper', 'detection_id': 'scratch'},
        ],
    }]
    document['parts'] = [{
        'id': 'bumper', 'name': 'Pare-chocs avant', 'action': 'repair', 'photo_ids': ['manual-front'],
        'amounts': {'repair_labor': 125.25, 'paint_labor': 100, 'paint_materials': 30, 'replacement_labor': 0, 'parts_supply': 0},
        'hours': {'repair_labor': 2, 'paint_labor': 1, 'replacement_labor': 0},
        'damages': [{'id': 'scratch', 'type': 'd_rayure', 'severity': 'medium', 'notes': 'Marque manuelle', 'photo_ids': ['manual-front']}],
    }]
    return document


def test_manual_save_generates_linked_native_output_and_private_media(private_client, private_app, monkeypatch):
    for target, method in [(private_app.state.capture_service, 'start_capture'),
                           (private_app.state.capture_service.cameras.client, 'snapshot'),
                           (private_app.state.scan_manager.backend, 'ensure_available'),
                           (private_app.state.vehicle_catalog, 'resolve')]:
        if hasattr(target, method):
            monkeypatch.setattr(target, method, Mock(side_effect=AssertionError('External work forbidden')))
    template = create_editor(private_client)
    response = save_editor(private_client, template, prepared_document(template), {'front.png': image_bytes()})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['active'] is False and saved['version'] != template['version']
    assert saved['editor']['metadata']['legacy_business_field'] == 'Keep this'
    assert saved['issues'] == []
    assert saved['analysis']['financial']['estimated_total'] == 255.25
    assert saved['analysis']['parts'][0]['source_images'] == ['manual-front']
    assert saved['analysis']['detections'][0]['images_sources'] == ['manual-front']
    assert saved['editor']['photos'][0]['regions'][1]['detection_id'] == 'scratch'
    image_url = saved['media']['analysis/annotated/manual-front_dommages.png']
    image = Image.open(BytesIO(private_client.get(image_url).content))
    assert image.size == (120, 80) and image.getpixel((48, 28)) != (255, 255, 255)
    report = private_client.get(saved['media']['analysis/report.pdf'])
    with pymupdf.open(stream=report.content, filetype='pdf') as pdf:
        text = ''.join(page.get_text() for page in pdf)
    assert '255.25 MAD' in text and 'Pare-chocs avant' in text and 'Marque manuelle' in text
    denied = private_client.get(f'/api/private-demo/templates/{saved["id"]}/media/.demo-editor.json?version={saved["version"]}')
    assert denied.status_code == 404
    activate = private_client.patch('/api/private-demo/templates/' + saved['id'], headers=HEADERS, json={'active': True})
    assert activate.status_code == 200, activate.text
    copied = private_client.post('/api/private-demo/templates/' + saved['id'] + '/test', headers=HEADERS)
    assert copied.status_code == 201, copied.text
    dossier = copied.json()
    assert dossier['result']['parts'][0]['source_images'][0] != 'manual-front'
    root = private_app.state.private_demo.repository.dossiers_dir / dossier['dossier_id']
    assert not (root / '.demo-editor.json').exists() and not (root / '.editor-assets').exists()
    with pymupdf.open(root / 'analysis/report.pdf') as report:
        text = ''.join(page.get_text() for page in report)
    assert dossier['dossier_id'] in text and 'TEMPLATE-MANUAL' not in text


def test_incomplete_draft_reopens_and_stale_save_preserves_current_version(private_client):
    template = create_editor(private_client)
    document = deepcopy(template['editor'])
    document['title'] = 'Brouillon sans photos'
    response = save_editor(private_client, template, document)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['issues'] and saved['analysis'] is None
    assert private_client.get('/api/private-demo/templates/' + saved['id']).json()['editor']['title'] == document['title']
    assert save_editor(private_client, template, document).status_code == 409
    assert private_client.patch('/api/private-demo/templates/' + saved['id'], headers=HEADERS, json={'active': True}).status_code == 422
    assert private_client.get('/api/private-demo/templates/' + saved['id']).json()['version'] == saved['version']


def test_estimate_changes_regenerate_report_and_unknowns_remain_unknown(private_client):
    template = create_editor(private_client)
    response = save_editor(private_client, template, prepared_document(template), {'front.png': image_bytes()})
    assert response.status_code == 200, response.text
    saved = response.json()
    document = saved['editor']
    document['parts'][0]['amounts']['repair_labor'] = 250
    document['parts'][0]['amounts']['parts_supply'] = None
    response = save_editor(private_client, saved, document)
    assert response.status_code == 200, response.text
    newer = response.json()
    financial = newer['analysis']['financial']
    assert financial['status'] == 'incomplete' and financial['estimated_total'] is None
    assert financial['known_subtotal'] == 380 and financial['breakdown']['parts_supply'] is None
    with pymupdf.open(stream=private_client.get(newer['media']['analysis/report.pdf']).content, filetype='pdf') as pdf:
        text = ''.join(page.get_text() for page in pdf)
    assert '380.00 MAD' in text and '255.25 MAD' not in text


def test_photo_replacement_removal_and_failed_upload_are_atomic(private_client, private_app):
    template = create_editor(private_client)
    saved = save_editor(private_client, template, prepared_document(template), {'front.png': image_bytes()}).json()
    document = deepcopy(saved['editor'])
    document['photos'][0].update(upload_key='replacement.png', regions=[], flattened={})
    failed = save_editor(private_client, saved, document, {'replacement.png': b'invalid'})
    assert failed.status_code == 422
    assert private_client.get('/api/private-demo/templates/' + saved['id']).json()['version'] == saved['version']
    replacement = save_editor(private_client, saved, document, {'replacement.png': image_bytes('green', (70, 50))})
    assert replacement.status_code == 200, replacement.text
    newer = replacement.json()
    assert newer['editor']['photos'][0]['regions'] == []
    assert Image.open(BytesIO(private_client.get(newer['media']['photos/manual-front.png']).content)).size == (70, 50)
    document = newer['editor']; document['photos'] = []; document['parts'] = []
    removed = save_editor(private_client, newer, document)
    assert removed.status_code == 200, removed.text
    directory = private_app.state.private_demo.templates.directory(removed.json())
    assert not (directory / 'analysis/result.json').exists() and not list((directory / 'photos').glob('*'))


def test_editor_geometry_validation_and_dangling_links(private_client):
    template = create_editor(private_client)
    document = prepared_document(template)
    document['photos'][0]['regions'][0]['points'][0]['x'] = 2
    assert save_editor(private_client, template, document, {'front.png': image_bytes()}).status_code == 422
    document = prepared_document(template)
    document['photos'][0]['regions'][1]['points'] = [{'x': .1, 'y': .1}, {'x': .2, 'y': .2}, {'x': .3, 'y': .3}]
    assert save_editor(private_client, template, document, {'front.png': image_bytes()}).status_code == 422
    document = prepared_document(template)
    document['parts'][0]['photo_ids'] = ['missing-photo']
    saved = save_editor(private_client, template, document, {'front.png': image_bytes()})
    assert saved.status_code == 200, saved.text
    assert saved.json()['issues'] and saved.json()['analysis'] is None


def test_editor_roundtrip_preserves_geometry_and_flattened_bases(private_client, native_template):
    template = register(private_client, native_template, active=False)
    detail = private_client.get('/api/private-demo/templates/' + template['id']).json()
    assert detail['editor']['photos'][0]['flattened']
    response = save_editor(private_client, detail, detail['editor'])
    assert response.status_code == 200, response.text
    saved = response.json()
    exported = private_client.get('/api/private-demo/templates/' + saved['id'] + '/export')
    imported = private_client.post('/api/private-demo/import', headers=HEADERS, files={'file': ('roundtrip.zip', exported.content, 'application/zip')})
    assert imported.status_code == 201, imported.text
    restored = private_client.get('/api/private-demo/templates/' + imported.json()['id']).json()
    assert restored['editor'] == saved['editor']
    image_url = restored['media'][restored['editor']['photos'][0]['flattened']['parts']]
    assert private_client.get(image_url).status_code == 200


def test_manual_export_retains_title_and_editable_geometry(private_client):
    template = create_editor(private_client)
    response = save_editor(private_client, template, prepared_document(template), {'front.png': image_bytes()})
    assert response.status_code == 200, response.text
    saved = response.json()
    exported = private_client.get('/api/private-demo/templates/' + saved['id'] + '/export')
    imported = private_client.post('/api/private-demo/import', headers=HEADERS, files={'file': ('manual.zip', exported.content, 'application/zip')})
    assert imported.status_code == 201, imported.text
    restored = private_client.get('/api/private-demo/templates/' + imported.json()['id']).json()
    assert restored['editor'] == saved['editor'] and restored['title'] == saved['title']


def test_rotated_originals_keep_display_annotations_aligned():
    from PIL import ImageOps
    from api.private_demo.editor_render import render_annotation
    from api.private_demo.editor_schemas import Region
    original = BytesIO()
    image = Image.new('RGB', (120, 80), 'white')
    exif = image.getexif(); exif[274] = 6
    image.save(original, 'JPEG', exif=exif)
    region = Region(id='orientation', layer='damages', shape='rectangle', points=[{'x': .1, 'y': .1}, {'x': .5, 'y': .4}])
    annotation = Image.open(BytesIO(render_annotation(original.getvalue(), [region], 'damages')))
    assert annotation.size == (120, 80) and annotation.getexif()[274] == 6
    displayed = ImageOps.exif_transpose(annotation)
    assert displayed.size == (80, 120)
    assert displayed.getpixel((24, 30)) != (255, 255, 255)
    assert displayed.getpixel((64, 100)) == (255, 255, 255)


def test_verification_manual_authoring_keeps_photos_only(private_client):
    template = create_editor(private_client, 'verification')
    document = template['editor']
    document['photos'] = [{'id': f'{phase}-{side}', 'side': side, 'phase': phase, 'upload_key': f'{phase}-{side}.png'}
                          for phase in ('before', 'after') for side in ('front', 'rear', 'left', 'right')]
    saved = save_editor(private_client, template, document, {photo['upload_key']: image_bytes() for photo in document['photos']})
    assert saved.status_code == 200, saved.text
    assert saved.json()['issues'] == [] and saved.json()['analysis'] is None
    assert private_client.patch('/api/private-demo/templates/' + template['id'], headers=HEADERS, json={'active': True}).status_code == 200
    copied = private_client.post('/api/private-demo/templates/' + template['id'] + '/test', headers=HEADERS)
    assert copied.status_code == 201, copied.text
    assert copied.json()['comparison']['missing_after'] == [] and copied.json()['result'] is None


def test_expired_session_rejects_saves_and_media(private_client, private_app):
    template = create_editor(private_client)
    state = private_app.state.private_demo.auth.state()
    for session in state['sessions'].values():
        session['expires'] = 0
    private_app.state.private_demo.auth.path.write_text(json.dumps(state))
    assert save_editor(private_client, template, template['editor']).status_code == 401
    assert private_client.get(f'/api/private-demo/templates/{template["id"]}/media/photos/test.png?version={template["version"]}').status_code == 401
