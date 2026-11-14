"""Verify the effects API enforces authentication, schemas and capture ownership.

Depends on existing isolated API fixtures and the memory-only zone driver; these
HTTP tests never open a serial port or claim visible hardware behavior.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from api.lighting_drivers import SimulatedPixelDriver
from test_capture import sample_photos
from test_lighting import CONTROL, MAINTENANCE, lighting_client


BASE = '/api/v1/lights'


def enable_simulation(service):
    service.settings = replace(service.settings, driver='simulated_pixels', pixel_count=12)
    service.driver = SimulatedPixelDriver(service.settings)


def test_effect_security_capabilities_and_validation(storage, tmp_path):
    samples = tmp_path / 'samples'
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, service):
        assert client.get(BASE + '/effects').status_code == 401
        caps = client.get(BASE + '/capabilities', headers=CONTROL).json()
        assert not caps['supports_progressive_effects'] and caps['pixel_count'] is None
        assert not caps['supports_smooth_transitions']
        request = dict(effect='chase', color='#00AAFF', speed=2, segment_length=5, repeat=True)
        response = client.post(BASE + '/effects', headers=MAINTENANCE, json=request)
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'lighting_unsupported_capability'
        assert client.post(BASE + '/effects', headers=CONTROL, json=request).status_code == 403
        assert client.post(BASE + '/stop', headers=CONTROL, json={}).status_code == 403
        enable_simulation(service)
        effects = client.get(BASE + '/effects', headers=MAINTENANCE).json()
        assert len(effects) == 16 and all(effect['supported'] for effect in effects)
        assert next(effect for effect in effects if effect['effect'] == 'fade_in')['parameters']['duration_ms']['minimum'] == 100
        for invalid in ({'effect': 'solid', 'unknown': 1}, {'effect': 'solid', 'color': 'red'},
                        {'effect': 'chase', 'segment_length': 20}, {'effect': 'solid', 'brightness': 101}):
            assert client.post(BASE + '/effects', headers=MAINTENANCE, json=invalid).status_code == 422
        accepted = client.post(BASE + '/effects', headers=MAINTENANCE, json=request)
        assert accepted.status_code == 202
        assert accepted.json()['simulated'] and accepted.json()['preview_active']
        assert len(accepted.json()['pixels']) == 12
        assert not accepted.json()['synchronized']
        deadline = service.engine.preview_deadline
        assert client.post(BASE + '/effects', headers=MAINTENANCE, json=request).status_code == 202
        assert service.engine.preview_deadline == deadline
        assert client.post(BASE + '/stop', headers=MAINTENANCE, json={}).status_code == 202
        assert not service.status()['preview_active']


def test_concurrent_http_effects_and_stop_cannot_override_scan(storage, tmp_path):
    samples = tmp_path / 'samples'
    samples.mkdir()
    sample_photos(samples)
    with lighting_client(storage, samples) as (client, service):
        enable_simulation(service)
        def start(index):
            return client.post(BASE + '/effects', headers=MAINTENANCE,
                               json={'effect': 'solid', 'brightness': index}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(start, range(50))) == [202] * 50
        service.state.begin_capture('authoritative')
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(start, range(50))) == [409] * 50
        assert client.post(BASE + '/stop', headers=MAINTENANCE, json={}).status_code == 409
        result = client.get(BASE + '/status', headers=CONTROL).json()
        assert result['state'] == 'SCANNING' and not result['preview_active']
        assert result['active_effect'] == 'target_lock'
