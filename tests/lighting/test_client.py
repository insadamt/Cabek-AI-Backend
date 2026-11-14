"""Verify direct lighting calls and application-owned completion timing.

Depends on fake HTTP and a controllable clock; no network or USB is used.
"""
import io
import json
from threading import Event


from api.lighting_client import LightingClient, LightingSettings


def test_application_events_map_to_new_contract_and_completion_is_owned_here():
    now = [0.0]
    client = LightingClient(LightingSettings(), clock=lambda: now[0])
    client.application_ready()
    assert client.prepare_command()[1:] == ('/set', {'effect': 'solid', 'color': '#FFFFFF'})
    client.begin_capture('first')
    assert client.prepare_command()[2] == {'effect': 'pulse', 'color': '#FF0000', 'repeat': True}
    client.finish_capture('first', True)
    now[0] = 9
    assert client.prepare_command()[2] == {'effect': 'mission_complete'}
    now[0] = 10
    assert client.prepare_command()[2] == {'effect': 'solid', 'color': '#FFFFFF'}
    client.begin_capture('second')
    now[0] = 100
    client.finish_capture('first', True)
    assert client.prepare_command()[2]['effect'] == 'pulse'
    client.finish_capture('second', False)
    assert client.prepare_command()[2] == {'effect': 'warning'}


def test_slow_http_never_blocks_capture_and_shutdown_sends_off():
    entered, release = Event(), Event()
    sent = []
    def send(path, payload):
        sent.append((path, payload))
        if len(sent) == 1:
            entered.set()
            assert release.wait(2)
    client = LightingClient(LightingSettings(enabled=True), send=send)
    client.start()
    try:
        assert entered.wait(1)
        client.begin_capture('capture-while-network-blocked')
        assert client.prepare_command()[2]['effect'] == 'pulse'
    finally:
        release.set()
        client.close()
    assert sent[0] == ('/set', {'effect': 'system_awakening'})
    assert sent[-1] == ('/off', None)
    assert not client.worker.is_alive()


def test_http_transport_posts_only_set_and_bodyless_off(monkeypatch):
    requests = []
    class Response(io.BytesIO):
        status = 200
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(json.dumps({'success': True}).encode())
    monkeypatch.setattr('api.lighting_client.build_opener', lambda *args: Opener())
    client = LightingClient(LightingSettings())
    client.send_command('/set', {'effect': 'solid'})
    client.send_command('/off', None)
    assert [request.full_url for request in requests] == [
        'http://127.0.0.1:8001/api/lights/set', 'http://127.0.0.1:8001/api/lights/off']
    assert all(request.method == 'POST' for request in requests)
    assert requests[-1].data is None
    assert json.loads(requests[0].data) == {'effect': 'solid'}


def test_successful_command_is_not_repeated_and_failures_retry():
    retried, accepted = Event(), Event()
    calls = []
    def send(path, payload):
        calls.append((path, payload))
        if len(calls) == 1:
            retried.set()
            raise OSError('offline')
        accepted.set()
    client = LightingClient(LightingSettings(enabled=True), send=send)
    client.start()
    try:
        assert retried.wait(1)
        client.wake.set()
        assert accepted.wait(2)
        revision = client.prepare_command()[0]
        assert client.prepare_command()[0] == revision
    finally:
        client.close()
    assert calls[-1] == ('/off', None)
