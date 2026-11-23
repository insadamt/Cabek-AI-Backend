"""Verify supported AI classifications and canonical private vehicle edits.

Depends on authenticated temporary templates and mocked AutoEstimate responses;
checkpoint parity inspects pickle opcodes without deserializing model objects.
"""
from copy import deepcopy
from pathlib import Path
import os
import pickletools
import zipfile
from unittest.mock import Mock
import pytest
from api.errors import DossierError
from api.private_demo.catalog import PART_CLASSES, DAMAGE_CLASSES, CRITICAL_CLASSES
from api.vehicle_catalog import ResolvedVehicle
from test_private_demo import HEADERS, private_app, private_client
from test_demo_editor import create_editor, prepared_document, save_editor, image_bytes


def test_catalog_requires_authentication_and_exposes_complete_french_labels(private_client):
    response = private_client.get('/api/private-demo/catalog')
    assert response.status_code == 200
    data = response.json()
    assert (len(data['parts']), len(data['damages']), len(data['critical'])) == (25, 8, 1)
    assert {'key': 'p_Capot', 'label': 'Capot'} in data['parts']
    assert {'key': 'd_rayure', 'label': 'Rayure'} in data['damages']
    for options in data.values():
        assert len({option['key'] for option in options}) == len(options)
        assert all(option['label'] for option in options)
    private_client.post('/api/private-demo/logout', headers=HEADERS)
    assert private_client.get('/api/private-demo/catalog').status_code == 401


@pytest.mark.parametrize('filename,classes', [
    ('best_pieces.pt', PART_CLASSES), ('best.pt', DAMAGE_CLASSES), ('best_zone_critique.pt', CRITICAL_CLASSES),
])
def test_catalog_matches_deployed_model_vocabulary(filename, classes):
    directory = Path(os.environ.get('CABEK_TEST_MODELS_DIR', Path(__file__).parents[2] / 'models'))
    path = directory / filename
    if not path.exists():
        pytest.skip('Model checkpoints are optional in the API-only test environment.')
    with zipfile.ZipFile(path) as archive:
        content = archive.read(next(name for name in archive.namelist() if name.endswith('data.pkl')))
    operations = iter(pickletools.genops(content))
    for _, value, _ in operations:
        if value == 'names':
            break
    names = []
    for operation, value, _ in operations:
        if operation.name in ('SETITEM', 'SETITEMS'):
            break
        if isinstance(value, str):
            names.append(value)
    assert names == [key for key, _ in classes]


def test_classification_survives_native_output_export_and_reopen(private_client):
    template = create_editor(private_client)
    document = prepared_document(template)
    document['parts'][0]['model_class'] = 'p_Pare-chocs avant'
    response = save_editor(private_client, template, document, {'front.png': image_bytes()})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['editor']['parts'][0]['id'] == 'bumper'
    assert saved['editor']['parts'][0]['model_class'] == 'p_Pare-chocs avant'
    assert saved['analysis']['parts'][0]['piece_ai_brut'] == 'p_Pare-chocs avant'
    assert saved['analysis']['detections'][0]['piece_ai_brut'] == 'p_Pare-chocs avant'
    restored = private_client.get('/api/private-demo/templates/' + saved['id']).json()
    assert restored['editor'] == saved['editor']
    document = deepcopy(saved['editor'])
    document['parts'][0]['model_class'] = 'invented_class'
    assert save_editor(private_client, saved, document).status_code == 422


def test_changed_selection_resolves_canonical_labels_and_reuses_them_offline(private_client, private_app, monkeypatch):
    template = create_editor(private_client)
    document = deepcopy(template['editor'])
    document['metadata'].update(marque_id=12, model_id=34, marque='Untrusted', modele='Untrusted')
    resolve = Mock(return_value=ResolvedVehicle(marque_id=12, model_id=34, marque='Dacia', modele='Sandero'))
    monkeypatch.setattr(private_app.state.vehicle_catalog, 'resolve', resolve)
    response = save_editor(private_client, template, document)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['editor']['metadata']['marque'] == 'Dacia'
    assert saved['editor']['metadata']['modele'] == 'Sandero'
    assert saved['editor']['metadata']['legacy_business_field'] == 'Keep this'
    resolve.assert_called_once()
    resolve.side_effect = AssertionError('Unchanged vehicle must remain editable offline')
    document = deepcopy(saved['editor'])
    document['title'] = 'Edited offline'
    assert save_editor(private_client, saved, document).status_code == 200


@pytest.mark.parametrize('status,code', [(503, 'autoestimate_unavailable'), (422, 'invalid_vehicle_selection')])
def test_failed_vehicle_validation_preserves_revision(private_client, private_app, monkeypatch, status, code):
    template = create_editor(private_client)
    document = deepcopy(template['editor'])
    document['metadata'].update(marque_id=12, model_id=999)
    monkeypatch.setattr(private_app.state.vehicle_catalog, 'resolve', Mock(side_effect=DossierError(status, code, 'Catalogue indisponible')))
    response = save_editor(private_client, template, document)
    assert response.status_code == status
    assert private_client.get('/api/private-demo/templates/' + template['id']).json()['version'] == template['version']


def test_partial_ids_are_rejected_but_clearing_selection_is_valid_for_drafts(private_client):
    template = create_editor(private_client)
    document = deepcopy(template['editor'])
    document['metadata'].update(marque_id=12, model_id=None)
    assert save_editor(private_client, template, document).status_code == 422
    document['metadata'].update(marque_id=None, model_id=None, marque='', modele='')
    response = save_editor(private_client, template, document)
    assert response.status_code == 200, response.text
    assert response.json()['editor']['metadata']['marque_id'] is None
