"""Exercise private authentication, native cloning and fail-closed hardware isolation.

Depends on real temporary files, native API routes and prepared fixtures; physical
capture, pricing and inference raise if any matched demonstration invokes them.
"""
import io
import json
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from api.config import ApiSettings
from api.main import create_app
from api.errors import DossierError
from api.private_demo.auth import password_record, save_secret
from api.private_demo.bundles import export_zip, extract_zip
from api.workflow_schemas import normalize_plate

PASSWORD = 'test-only-private-password'
HEADERS = {'X-Demo-Admin':'1'}


@pytest.fixture
def private_app(storage, tmp_path, monkeypatch):
    root = tmp_path / 'private'
    monkeypatch.setenv('CABEK_DEMO_DIR', str(root))
    secret = root / 'secret.json'
    save_secret(secret, password_record(PASSWORD))
    app = create_app(ApiSettings(storage, tunnel_mode='tiandy'))
    return app


@pytest.fixture
def private_client(private_app):
    with TestClient(private_app, client=('127.0.0.1',1234)) as client:
        response = client.post('/api/private-demo/login',json={'password':PASSWORD},headers=HEADERS)
        assert response.status_code == 200, response.text
        yield client


@pytest.fixture
def native_template(complete_dossier, tmp_path):
    source, result = complete_dossier('TEMPLATE-EXAMPLE')
    metadata = json.loads((source / 'metadata.json').read_text())
    metadata.update(type='chiffrage_rapide',source='cabek_ai',matricule='84547-H-06',marque_id=12,model_id=34,date_mise_en_circulation='2020-01-01')
    metadata['photos'][0]['side']='front'
    result['vehicle'].update(matricule=metadata['matricule'],marque_id=12,model_id=34,date_mise_en_circulation='2020-01-01')
    (source / 'metadata.json').write_text(json.dumps(metadata))
    (source / 'analysis/result.json').write_text(json.dumps(result))
    destination = tmp_path / 'template-input'; source.rename(destination)
    return destination


def register(client, source, active=True):
    response = client.post('/api/private-demo/import',files={'file':('template.zip',export_zip(source),'application/zip')},headers=HEADERS)
    assert response.status_code == 201, response.text
    item=response.json()
    if active:
        response=client.patch('/api/private-demo/templates/'+item['id'],json={'active':True},headers=HEADERS)
        assert response.status_code==200,response.text
        item=response.json()
    return item


def test_authentication_cookie_expiration_logout_and_rotation(private_app):
    with TestClient(private_app,client=('127.0.0.1',1234)) as client:
        assert client.get('/api/private-demo/templates').status_code==401
        assert client.post('/api/private-demo/login',json={'password':PASSWORD}).status_code==403
        response=client.post('/api/private-demo/login',json={'password':PASSWORD},headers=HEADERS)
        assert response.status_code==200
        assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
        assert client.get('/api/private-demo/session').status_code==200
        assert client.put('/api/private-demo/mode',json={'enabled':True}).status_code==403
        assert client.post('/api/private-demo/password',json={'password':'another-test-password'},headers=HEADERS).status_code==200
        assert client.get('/api/private-demo/session').status_code==401
        client.post('/api/private-demo/login',json={'password':'another-test-password'},headers=HEADERS)
        auth=private_app.state.private_demo.auth
        state=auth.state()
        for value in state['sessions'].values():value['expires']=0
        auth.path.write_text(json.dumps(state))
        assert client.get('/api/private-demo/session').status_code==401
        client.post('/api/private-demo/login',json={'password':'another-test-password'},headers=HEADERS)
        assert client.post('/api/private-demo/logout',headers=HEADERS).status_code==200
        assert client.get('/api/private-demo/templates').status_code==401


def test_repeated_guessing_is_persistently_rate_limited(private_app):
    with TestClient(private_app,client=('127.0.0.1',1234)) as client:
        for _ in range(5):assert client.post('/api/private-demo/login',json={'password':'wrong'},headers=HEADERS).status_code==401
        assert client.post('/api/private-demo/login',json={'password':PASSWORD},headers=HEADERS).status_code==429
        assert private_app.state.private_demo.auth.state()['blocked_until']>0


@pytest.mark.parametrize('category',['forfait_expertise','chiffrage_rapide','verification'])
def test_all_categories_can_be_prepared_without_public_expertise_creation(private_client, category):
    response=private_client.post('/api/private-demo/templates',json={'metadata':{'dossier_id':'TEMPLATE-ONE','type':category,'matricule':'84547-H-06','photos':[]}},headers=HEADERS)
    assert response.status_code==201,response.text
    assert private_client.get('/api/dossiers').json()['total']==0
    assert private_client.get('/api/dossiers/TEMPLATE-ONE').status_code==404
    if category=='forfait_expertise':assert private_client.post('/api/dossiers',json={'type':category}).status_code==422


def test_matched_native_capture_returns_prepared_results_without_hardware(private_client,private_app,native_template,new_dossier,workflow_catalog,monkeypatch):
    item=register(private_client,native_template)
    frozen=export_zip(private_app.state.private_demo.templates.directory(item))
    for target,method in [(private_app.state.capture_service,'start_capture'),(private_app.state.capture_service.cameras.client,'snapshot'),(private_app.state.scan_manager.backend,'ensure_available')]:
        monkeypatch.setattr(target,method,Mock(side_effect=AssertionError('Real hardware / inference forbidden')))
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    results=[]
    for _ in range(2):
        draft=private_client.post('/api/dossiers',json={'type':'chiffrage_rapide','matricule':'84547-h-06','marque_id':12,'model_id':34,'date_mise_en_circulation':'2020-01-01'}).json()
        response=private_client.post('/api/dossiers/'+draft['dossier_id']+'/capture',json={})
        assert response.status_code==202,response.text
        detail=private_client.get('/api/dossiers/'+draft['dossier_id']).json()
        assert detail['demo'] is True and detail['status']=='completed',detail
        assert detail['result']['financial']['estimated_total']==500
        assert detail['result']['photos'][0]['photo_id']!='avant_01'
        assert private_client.get(detail['result']['photos'][0]['parts_url']).status_code==200
        assert private_client.get(detail['result']['report_url']).status_code==200
        assert private_client.post('/api/dossiers/'+draft['dossier_id']+'/scan').status_code==202
        results.append(detail)
    assert results[0]['photos'][0]['photo_id']!=results[1]['photos'][0]['photo_id']
    assert frozen==export_zip(private_app.state.private_demo.templates.directory(item))
    assert 'secret' not in private_client.get('/api/dossiers').text


def test_unmatched_and_unknown_registration_never_fall_back(private_client,private_app,workflow_catalog,monkeypatch):
    real=Mock(side_effect=AssertionError('No physical capture'))
    monkeypatch.setattr(private_app.state.capture_service,'start_capture',real)
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    draft=private_client.post('/api/dossiers',json={'type':'chiffrage_rapide','matricule':'123-A-45'}).json()
    assert private_client.post('/api/dossiers/'+draft['dossier_id']+'/capture',json={}).status_code==409
    assert private_client.post('/api/dossiers/'+draft['dossier_id']+'/scan').status_code==409
    real.assert_not_called()
    private_client.put('/api/private-demo/mode',json={'enabled':False},headers=HEADERS)
    real.side_effect=None
    private_client.post('/api/dossiers/'+draft['dossier_id']+'/capture',json={})
    real.assert_called_once()


def test_template_conflicts_and_corruption_fail_before_publication(private_client,private_app,native_template):
    item=register(private_client,native_template)
    duplicate=register(private_client,native_template,active=False)
    response=private_client.patch('/api/private-demo/templates/'+duplicate['id'],json={'active':True},headers=HEADERS)
    assert response.status_code==409
    path=private_app.state.private_demo.templates.directory(item)/'photos/avant_01.jpg'
    path.write_bytes(b'corrupt')
    with pytest.raises(DossierError):private_app.state.private_demo.templates.match('84547-H-06','chiffrage_rapide')
    assert private_client.get('/api/dossiers').json()['total']==0


def test_zip_traversal_and_links_are_rejected(tmp_path):
    import zipfile
    for name,mode in [('../escape.json',0),('link.json',0o120777<<16)]:
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            info=zipfile.ZipInfo(name);info.external_attr=mode;archive.writestr(info,b'{}')
        with pytest.raises(DossierError):extract_zip(buffer.getvalue(),tmp_path)
    assert not (tmp_path.parent/'escape.json').exists()


@pytest.mark.parametrize('value,expected',[('84547-h-06','84547-H-06'),('84654-ب-454','84654-ب-454')])
def test_registration_rules_reused(value,expected):
    assert normalize_plate(value)==expected


def test_verification_keeps_four_sides_and_phase_finalization(private_client,private_app,native_template,workflow_catalog):
    from PIL import Image
    source=native_template
    shutil.rmtree(source/'analysis')
    shutil.rmtree(source/'photos')
    metadata=json.loads((source/'metadata.json').read_text());metadata.update(type='verification',photos=[])
    for phase in ('before','after'):
        (source/'photos'/phase).mkdir(parents=True)
        for side in ('front','rear','left','right'):
            relative=f'photos/{phase}/{phase}-{side}.jpg';Image.new('RGB',(12,12),'white').save(source/relative)
            metadata['photos'].append({'path':relative,'side':side,'phase':phase})
    (source/'metadata.json').write_text(json.dumps(metadata))
    register(private_client,source)
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    draft=private_client.post('/api/dossiers',json={'type':'verification','matricule':'84547-H-06','marque_id':12,'model_id':34,'date_mise_en_circulation':'2020-01-01'}).json()
    url='/api/dossiers/'+draft['dossier_id']
    assert private_client.post(url+'/capture',json={'phase':'after'}).status_code==409
    response=private_client.post(url+'/capture',json={'phase':'before'});assert response.status_code==202,response.text
    before=private_client.get(url).json();assert before['workflow_status']=='awaiting_after'
    assert len(before['photos'])==4 and before['result'] is None
    response=private_client.post(url+'/capture',json={'phase':'after'});assert response.status_code==202,response.text
    after=private_client.get(url).json();assert after['workflow_status']=='comparison_ready',after
    assert len(after['photos'])==8 and after['comparison']['finalized'] and after['comparison']['before_locked']
    assert private_client.post(url+'/scan').status_code==409


def test_expertise_clones_without_altering_external_dossier_and_retains_econstat(private_client,private_app,native_template):
    from pypdf import PdfWriter
    source=native_template;metadata=json.loads((source/'metadata.json').read_text())
    metadata.update(type='forfait_expertise',source='external',econstat={'path':'documents/econstat.pdf'})
    (source/'documents').mkdir();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(source/'documents/econstat.pdf')
    (source/'metadata.json').write_text(json.dumps(metadata))
    register(private_client,source)
    root=private_app.state.private_demo.repository.dossiers_dir
    external=root/'DOS-EXTERNAL';external.mkdir()
    original={**metadata,'dossier_id':'DOS-EXTERNAL','photos':[],'econstat':None}
    (external/'metadata.json').write_text(json.dumps(original));original_bytes=(external/'metadata.json').read_bytes()
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    response=private_client.post('/api/dossiers/DOS-EXTERNAL/capture',json={});assert response.status_code==202,response.text
    new_id=response.json()['dossier_id'];assert new_id!='DOS-EXTERNAL'
    assert (external/'metadata.json').read_bytes()==original_bytes
    detail=private_client.get('/api/dossiers/'+new_id).json();assert detail['demo'] and detail['status']=='completed'
    assert private_client.get('/api/dossiers/'+new_id+'/econstat').status_code==200
    assert private_client.get('/api/dossiers/'+new_id+'/econstat/page-1.webp').status_code==200


def test_two_concurrent_active_mappings_cannot_both_publish(private_app,native_template):
    store=private_app.state.private_demo.templates
    items=[store.commit(archive=export_zip(native_template)) for _ in range(2)]
    def activate(item):
        try:store.commit(identity=item['id'],active=True);return True
        except DossierError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(activate,items))==1


def test_import_conflict_is_additive_and_rolls_back_before_publication(tmp_path,native_template):
    from scripts.import_dossiers import import_dossiers
    source=tmp_path/'imports';source.mkdir();shutil.copytree(native_template,source/'one')
    destination=tmp_path/'target';destination.mkdir()
    first=import_dossiers(source,destination,True);assert first['dossiers'][0]['state']=='new'
    assert import_dossiers(source,destination,True)['dossiers'][0]['state']=='duplicate'
    before=(destination/'TEMPLATE-EXAMPLE/metadata.json').read_bytes()
    metadata=json.loads((source/'one/metadata.json').read_text());metadata['modele']='Different'
    (source/'one/metadata.json').write_text(json.dumps(metadata))
    with pytest.raises(ValueError):import_dossiers(source,destination,True)
    assert (destination/'TEMPLATE-EXAMPLE/metadata.json').read_bytes()==before


def test_session_and_template_assets_survive_application_restart(private_client,private_app,native_template):
    item=register(private_client,native_template)
    state=private_app.state.private_demo
    from api.private_demo.templates import TemplateStore
    assert TemplateStore(state.root).get(item['id'])['version']==item['version']
    assert export_zip(TemplateStore(state.root).directory(item))==export_zip(state.templates.directory(item))
    assert private_client.get('/api/private-demo/templates').headers['cache-control']=='no-store'
    public_config=private_client.get('/api/settings/operation').text
    assert PASSWORD not in public_config and 'digest' not in public_config


def test_public_demo_toggle_cannot_bypass_administration(private_client):
    assert private_client.patch('/api/settings/operation',json={'demo_mode':True}).status_code==403
    assert private_client.get('/api/private-demo/templates/nonexistent/export').status_code==404


def test_failed_registry_publication_keeps_previous_template_revision(private_app,native_template,monkeypatch):
    store=private_app.state.private_demo.templates
    item=store.commit(archive=export_zip(native_template))
    original=store.get(item['id'])
    import api.private_demo.templates as module
    publish=module.publish_json
    def failing(path,value):
        if path.name=='registry.json':raise OSError('Simulated disk failure')
        return publish(path,value)
    monkeypatch.setattr(module,'publish_json',failing)
    with pytest.raises(OSError):store.commit(identity=item['id'],title='Unpublished')
    assert store.get(item['id'])==original


def test_missing_optional_report_is_generated_with_native_report_service(private_client,native_template):
    (native_template/'analysis/report.pdf').unlink()
    item=register(private_client,native_template)
    assert item['active']


def test_annotation_alignment_is_validated(private_client,native_template):
    from PIL import Image
    Image.new('RGB',(5,5),'white').save(native_template/'analysis/annotated/avant_01_pieces.png')
    response=private_client.post('/api/private-demo/import',files={'file':('bad.zip',export_zip(native_template),'application/zip')},headers=HEADERS)
    assert response.status_code==422


def test_failed_materialization_does_not_change_existing_dossier(private_client,private_app,native_template,workflow_catalog,monkeypatch):
    register(private_client,native_template)
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    draft=private_client.post('/api/dossiers',json={'type':'chiffrage_rapide','matricule':'84547-H-06'}).json()
    path=private_app.state.private_demo.repository.dossiers_dir/draft['dossier_id']/'metadata.json'
    before=path.read_bytes()
    def failed(*args,**kwargs):raise OSError('Simulated disk failure')
    monkeypatch.setattr('api.private_demo.service.prepare_clone',failed)
    assert private_client.post('/api/dossiers/'+draft['dossier_id']+'/capture',json={}).status_code==503
    assert path.read_bytes()==before
    assert not list(path.parent.parent.glob('.demo-stage-*'))


def test_materialization_retains_draft_idempotency_instead_of_template_creation_identity(private_client,native_template,workflow_catalog):
    metadata=json.loads((native_template/'metadata.json').read_text())
    metadata.update(creation_key='template-only-key',creation_request={'type':'old-template'})
    (native_template/'metadata.json').write_text(json.dumps(metadata))
    register(private_client,native_template)
    private_client.put('/api/private-demo/mode',json={'enabled':True},headers=HEADERS)
    payload={'type':'chiffrage_rapide','matricule':'84547-H-06'}
    headers={'Idempotency-Key':'operator-creation-key'}
    draft=private_client.post('/api/dossiers',json=payload,headers=headers).json()
    assert private_client.post('/api/dossiers/'+draft['dossier_id']+'/capture',json={}).status_code==202
    repeated=private_client.post('/api/dossiers',json=payload,headers=headers)
    assert repeated.status_code==201,repeated.text
    assert repeated.json()['dossier_id']==draft['dossier_id']


def test_native_mpdf_attachment_preamble_is_preserved(private_client,native_template):
    from pypdf import PdfWriter
    output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(output)
    content=b'q\nQ\n'+output.getvalue()
    (native_template/'documents').mkdir();(native_template/'documents/econstat.pdf').write_bytes(content)
    metadata=json.loads((native_template/'metadata.json').read_text());metadata['econstat']={'path':'documents/econstat.pdf'}
    (native_template/'metadata.json').write_text(json.dumps(metadata))
    item=register(private_client,native_template)
    response=private_client.get('/api/private-demo/templates/'+item['id']+'/export')
    import zipfile
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('documents/econstat.pdf')==content


def test_invalid_econstat_pdf_fails_closed(private_client,native_template):
    (native_template/'documents').mkdir();(native_template/'documents/econstat.pdf').write_bytes(b'%PDF-broken')
    metadata=json.loads((native_template/'metadata.json').read_text());metadata['econstat']={'path':'documents/econstat.pdf'}
    (native_template/'metadata.json').write_text(json.dumps(metadata))
    response=private_client.post('/api/private-demo/import',files={'file':('invalid.zip',export_zip(native_template),'application/zip')},headers=HEADERS)
    assert response.status_code==422
