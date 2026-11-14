"""Verify the application exposes no lighting API; depends on its API fixture."""

def test_backend_has_no_lighting_routes(client):
    paths=client.get('/openapi.json').json()['paths']
    assert not any(path.startswith('/api/v1/lights') for path in paths)
