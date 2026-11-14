"""Verify USB controls remain outside CABEK AI; depends on its isolated API fixture."""

def test_backend_has_no_lighting_routes(client):
    paths = client.get('/openapi.json').json()['paths']
    assert not any(path.startswith(('/api/lights', '/api/v1/lights')) for path in paths)
