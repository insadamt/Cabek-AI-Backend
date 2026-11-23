"""Verify USB controls remain outside CABEK AI; depends on its isolated API fixture."""

def test_backend_has_no_lighting_routes(client):
    paths = client.get('/openapi.json').json()['paths']
    assert not any(path.startswith(('/api/lights', '/api/v1/lights')) for path in paths)


def test_lighting_failures_distinguish_service_hardware_and_firmware_without_secrets():
    from urllib.error import HTTPError
    from api.lighting_client import lighting_failure_reason
    hardware = lighting_failure_reason(HTTPError('http://private.invalid?token=secret', 503, 'failure', {}, None))
    firmware = lighting_failure_reason(HTTPError('http://private.invalid', 501, 'failure', {}, None))
    route = lighting_failure_reason(HTTPError('http://private.invalid', 404, 'failure', {}, None))
    service = lighting_failure_reason(OSError('private raw details'))
    assert 'ESP32 unavailable' in hardware and 'secret' not in hardware
    assert 'firmware' in firmware and '501' in firmware
    assert 'HTTP API rejected' in route and '404' in route
    assert 'HTTP service could not be reached' in service and 'private raw details' not in service
