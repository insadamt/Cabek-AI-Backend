"""Exercise actual engine rules on prepared annotations with mocked price transport.

Depends on native runner artifacts and barèmes; inference and live pricing calls
are forbidden so totals prove the annotation-to-engine conversion independently.
"""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from api.private_demo.editor_schemas import EditorDocument, EditorPhoto, EditorPart, EditorDamage, Region
from scans.artifacts import publish_json
from scans.runner import analyze_dossier
from test_runner_workflows import scan_request


def prepared_request(tmp_path, damage_type='d_rayure', *, critical=False, duplicate=False):
    request = scan_request(tmp_path, 'chiffrage_rapide')
    request.metadata['mock_engine'] = True
    evidence = request.dossier_dir / '.mock-evidence'
    (evidence / 'annotated').mkdir(parents=True)
    photo = EditorPhoto(id='front', path='photos/front.jpg')
    photo.regions = [Region(id='damage', layer='damages', shape='rectangle', part_id='bumper',
        detection_id='scratch', points=[{'x': .1, 'y': .1}, {'x': .6, 'y': .6}])]
    if critical:
        photo.regions.append(Region(id='critical', layer='critical', shape='rectangle',
            points=[{'x': .2, 'y': .2}, {'x': .5, 'y': .5}]))
    part = EditorPart(id='bumper', name='Pare-chocs avant', model_class='p_Pare-chocs avant',
        action='replace', photo_ids=['front'], damages=[EditorDamage(id='scratch', type=damage_type,
        severity='medium', photo_ids=['front'])], amounts={key:999999 for key in
        ('repair_labor','paint_labor','paint_materials','replacement_labor','parts_supply')})
    if duplicate:
        part.damages.append(EditorDamage(id='duplicate', type=damage_type, severity='medium', photo_ids=['front']))
    document = EditorDocument(title='Prepared', category='chiffrage_rapide', metadata=request.metadata,
        photos=[photo], parts=[part])
    publish_json(evidence / 'document.json', document.model_dump())
    for path in (request.photos[0].parts_path, request.photos[0].damages_path, request.photos[0].critical_path):
        Image.new('RGB',(20,16),'blue').save(evidence / 'annotated' / Path(path).name)
    return request


def run_prepared(request, monkeypatch, *, median=1234):
    models = Mock(side_effect=AssertionError('YOLO must not run'))
    pricing = Mock(return_value={'median': median} if median is not None else None)
    monkeypatch.setattr('scans.runner.load_models', models)
    monkeypatch.setattr('scans.pricing_client.get_estimation', pricing)
    analyze_dossier(request)
    models.assert_not_called()
    return json.loads((request.output_dir / 'result.json').read_text()), pricing


def test_repairs_use_engine_costs_ignore_manual_totals_and_decisions(tmp_path, monkeypatch):
    from bareme_c2 import calculer_cout_dommage
    request = prepared_request(tmp_path, duplicate=True)
    result, pricing = run_prepared(request, monkeypatch)
    assert result['financial']['estimated_total'] == calculer_cout_dommage('p_Pare-chocs avant','d_rayure','mid')['total']
    assert result['parts'][0]['action'] == 'repair'
    assert len(result['detections']) == 1
    pricing.assert_not_called()


@pytest.mark.parametrize('model_id,median', [(34,1234),(35,2468)])
def test_replacement_uses_current_vehicle_and_registration_year(tmp_path, monkeypatch, model_id, median):
    request = prepared_request(tmp_path, 'd_casse', duplicate=True)
    request.metadata['model_id'] = model_id
    result, pricing = run_prepared(request, monkeypatch, median=median)
    assert result['parts'][0]['action'] == 'replace'
    assert result['financial']['breakdown']['parts_supply'] == median
    assert pricing.call_count == 1
    assert pricing.call_args.args[:2] == (12, model_id)
    assert pricing.call_args.args[-1] == 2020


def test_critical_region_overlap_triggers_engine_replacement(tmp_path, monkeypatch):
    request = prepared_request(tmp_path, 'd_bosse', critical=True)
    result, pricing = run_prepared(request, monkeypatch)
    assert result['detections'][0]['zone_critique_detectee']
    assert result['detections'][0]['niveau'] == 'high'
    assert result['parts'][0]['action'] == 'replace'
    assert pricing.called


def test_missing_quote_keeps_incomplete_total(tmp_path, monkeypatch):
    result, _ = run_prepared(prepared_request(tmp_path, 'd_casse'), monkeypatch, median=None)
    assert result['financial']['status'] == 'incomplete'
    assert result['financial']['estimated_total'] is None
    assert result['financial']['warnings']


def test_two_replacement_damages_bill_one_supply_and_labor(tmp_path, monkeypatch):
    request = prepared_request(tmp_path, 'd_casse')
    path = request.dossier_dir / '.mock-evidence/document.json'
    document = EditorDocument.model_validate_json(path.read_bytes())
    document.parts[0].damages.append(EditorDamage(id='missing', type='d_piece_manquante', severity='high', photo_ids=['front']))
    publish_json(path, document.model_dump())
    result, pricing = run_prepared(request, monkeypatch)
    assert pricing.call_count == 1
    assert result['financial']['breakdown']['parts_supply'] == 1234
    assert len(result['detections']) == 2
    assert sum(not item['chiffrage_bloque'] for item in result['detections']) == 1
