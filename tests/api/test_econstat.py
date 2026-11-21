"""Verify document-driven camera suggestions, asynchronous caching and private previews.

Depends on temporary dossiers and mocked OCR transport; no real hardware or external
credentials are used. Real PDF rendering ensures that page two is never served.
"""
import copy
import io
import json
from threading import Event
import time
from PIL import Image
import pymupdf
import pytest
from api.econstat_contracts import ZONE_CHANNELS, validate_recommendation
from api.econstat_client import EconstatSettings, EconstatClient
from api.econstat_documents import read_document
from api.econstat_service import EconstatService
from api.errors import DossierError
from api.repository import DossierRepository


def payload(registration='97131-A-13', selected=('Avant_gauche', 'gauche_avant'), uncertain=()):
    zones = [{'id': zone, 'status': 'uncertain' if zone in uncertain else 'damaged' if zone in selected else 'healthy'} for zone in ZONE_CHANNELS]
    return {'schema_version': '1.0', 'registration': {'value': registration},
            'vehicle': {'registration_match': True, 'constat_reference': 'vehicle_A'}, 'zones': zones,
            'inspection': {'damaged_zone_ids': [z['id'] for z in zones if z['status'] == 'damaged'],
                           'uncertain_zone_ids': list(uncertain), 'selected_zone_ids': sorted(set(selected) | set(uncertain))}}


def attach(directory, registration='97131-A-13'):
    path = directory / 'documents/econstat.pdf'
    path.parent.mkdir(exist_ok=True)
    with pymupdf.open() as pdf:
        first = pdf.new_page(width=100, height=150)
        first.draw_rect(first.rect, fill=(1, 0, 0), color=None)
        second = pdf.new_page(width=100, height=150)
        second.draw_rect(second.rect, fill=(0, 0, 1), color=None)
        pdf.save(path)
    metadata = json.loads((directory / 'metadata.json').read_text())
    metadata.update(matricule=registration, econstat={'path': 'documents/econstat.pdf', 'original_filename': 'sample.pdf'})
    (directory / 'metadata.json').write_text(json.dumps(metadata))
    return path


def await_ready(service, dossier_id='DOS-001'):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = service.describe(dossier_id)
        if value['status'] != 'processing':
            return value
        time.sleep(.01)
    raise AssertionError('Preparation did not complete')


@pytest.mark.parametrize('zone,channel', ZONE_CHANNELS.items())
def test_all_commissioned_mappings(zone, channel):
    result = validate_recommendation(payload(selected=(zone,)), '97131-A-13')
    assert result['recommended_zoom_channels'] == ([] if channel in (4, 13) else [channel])
    assert 3 not in result['recommended_zoom_channels']


def test_uncertain_is_included():
    assert validate_recommendation(payload(selected=(), uncertain=('gauche_arrière',)), '97131-A-13')['recommended_zoom_channels'] == [11]


@pytest.mark.parametrize('change', ['duplicate', 'unknown', 'status', 'plate', 'match', 'inspection', 'short', 'version'])
def test_reject_invalid_ocr(change):
    value = payload()
    if change == 'duplicate': value['zones'][0] = value['zones'][1]
    if change == 'unknown': value['zones'][0]['id'] = 'unrecognized'
    if change == 'status': value['zones'][0]['status'] = 'certain'
    if change == 'plate': value['registration']['value'] = 'OTHER'
    if change == 'match': value['vehicle']['registration_match'] = 1
    if change == 'inspection': value['inspection']['selected_zone_ids'] = []
    if change == 'short': value['zones'].pop()
    if change == 'version': value['schema_version'] = '2'
    with pytest.raises(ValueError): validate_recommendation(value, '97131-A-13')


def test_async_deduplication_cache_and_registration_invalidation(storage, create_dossier, monkeypatch):
    directory = create_dossier(); attach(directory)
    service = EconstatService(DossierRepository(storage), EconstatSettings())
    started, release = Event(), Event()
    calls = []
    def analyze(content, registration):
        calls.append(registration); started.set(); release.wait(2)
        return payload(registration)
    monkeypatch.setattr(service.client, 'analyze', analyze)
    try:
        first = service.start_analysis('DOS-001')
        assert started.wait(1)
        assert first['status'] == 'processing'
        assert service.start_analysis('DOS-001')['status'] == 'processing'
        assert len(calls) == 1
        release.set()
        result = await_ready(service)
        assert result['recommended_zoom_channels'] == [2, 5]
        assert service.start_analysis('DOS-001')['status'] == 'ready'
        assert len(calls) == 1
        version = result['document_version']
        metadata = json.loads((directory/'metadata.json').read_text())
        metadata['matricule'] = '11168-B-72'
        (directory/'metadata.json').write_text(json.dumps(metadata))
        assert service.describe('DOS-001')['status'] == 'not_started'
        assert service.describe('DOS-001')['document_version'] != version
        service.start_analysis('DOS-001'); await_ready(service)
        assert len(calls) == 2
    finally:
        release.set(); service.close()


def test_changed_document_discards_late_result(storage, create_dossier, monkeypatch):
    directory = create_dossier(); path = attach(directory)
    service = EconstatService(DossierRepository(storage), EconstatSettings())
    release, started = Event(), Event()
    def analyze(*args):
        started.set(); release.wait(2); return payload()
    monkeypatch.setattr(service.client, 'analyze', analyze)
    try:
        old = read_document(service.repository, 'DOS-001')
        service.start_analysis('DOS-001'); assert started.wait(1)
        path.write_bytes(path.read_bytes() + b'\n%changed')
        release.set(); service.close()
        assert not service.result_path(old).exists()
        assert service.describe('DOS-001')['status'] == 'not_started'
    finally:
        release.set(); service.close()


@pytest.mark.parametrize('mode,expected', [('unmatched', 'manual_required'), ('failure', 'failed'), ('malformed', 'failed')])
def test_manual_fallback(storage, create_dossier, monkeypatch, mode, expected):
    directory = create_dossier(); attach(directory)
    service = EconstatService(DossierRepository(storage), EconstatSettings())
    def analyze(*args):
        if mode == 'failure': raise DossierError(503, 'econstat_unavailable', 'Sélection manuelle disponible.')
        return None if mode == 'unmatched' else {}
    monkeypatch.setattr(service.client, 'analyze', analyze)
    try:
        service.start_analysis('DOS-001'); result = await_ready(service)
        assert result['status'] == expected
        assert result['recommended_zoom_channels'] == []
    finally: service.close()


def test_first_page_and_dedicated_private_routes(client, create_dossier, monkeypatch):
    directory = create_dossier(); attach(directory)
    monkeypatch.setattr(client.app.state.econstat_service.client, 'analyze', lambda *args: payload())
    assert client.get('/api/dossiers/DOS-001/econstat').json()['status'] == 'not_started'
    response = client.post('/api/dossiers/DOS-001/econstat/analysis')
    assert response.status_code in (200, 202)
    result = await_ready(client.app.state.econstat_service)
    preview = client.get(result['preview_url'])
    assert preview.status_code == 200
    assert preview.headers['content-type'] == 'image/webp'
    image = Image.open(io.BytesIO(preview.content)).convert('RGB')
    assert image.getpixel((image.width//2, image.height//2))[0] > 240
    assert image.getpixel((image.width//2, image.height//2))[2] < 10
    assert client.get('/api/dossiers/DOS-001/media/documents/econstat.pdf').status_code == 404
    assert client.get('/api/dossiers/DOS-001/econstat/page-1.webp?version=obsolete').status_code == 409
    assert 'token' not in json.dumps(result).lower()


def test_missing_invalid_and_escaping_attachments(client, create_dossier, tmp_path):
    directory = create_dossier()
    assert client.get('/api/dossiers/DOS-001/econstat').status_code == 404
    path = attach(directory)
    outside = tmp_path/'outside.pdf'; outside.write_bytes(path.read_bytes())
    path.unlink(); path.symlink_to(outside)
    assert client.get('/api/dossiers/DOS-001/econstat').status_code == 404
    path.unlink(); path.write_bytes(b'%PDF-invalid')
    assert client.get('/api/dossiers/DOS-001/econstat/page-1.webp').status_code == 422
    metadata = json.loads((directory/'metadata.json').read_text())
    metadata['econstat']['path'] = '../outside.pdf'
    (directory/'metadata.json').write_text(json.dumps(metadata))
    assert client.get('/api/dossiers/DOS-001/econstat').status_code == 404


def test_ocr_configuration_and_connection_failure_do_not_disclose_token(monkeypatch):
    with pytest.raises(DossierError) as failure:
        EconstatClient(EconstatSettings()).analyze(b'%PDF-', 'TEST')
    assert failure.value.code == 'econstat_not_configured'
    import requests
    def fail(*args, **kwargs): raise requests.ConnectionError('private-secret upstream')
    monkeypatch.setattr(requests.Session, 'post', fail)
    with pytest.raises(DossierError) as failure:
        EconstatClient(EconstatSettings(token='private-secret')).analyze(b'%PDF-', 'TEST')
    assert 'private-secret' not in failure.value.message


def test_mpdf_preamble_is_accepted(client, create_dossier):
    directory = create_dossier(); path = attach(directory)
    path.write_bytes(b'/GS1 gs\n' + path.read_bytes())
    descriptor = client.get('/api/dossiers/DOS-001/econstat')
    assert descriptor.status_code == 200
    assert client.get(descriptor.json()['preview_url']).status_code == 200
