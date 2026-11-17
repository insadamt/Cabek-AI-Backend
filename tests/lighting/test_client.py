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
    assert client.prepare_command()[1:] == ('/set', {'effect': 'transition', 'color': '#FFFFFF', 'duration_ms': 900, 'easing': 'ease_in_out'})
    client.begin_capture('first')
    assert client.prepare_command()[2] == {'effect': 'transition', 'color': '#FF0000', 'duration_ms': 900, 'easing': 'ease_in_out'}
    client.finish_capture('first', True)
    now[0] = 9
    assert client.prepare_command()[2] == {'effect': 'transition', 'color': '#00FF49', 'duration_ms': 900, 'easing': 'ease_in_out'}
    now[0] = 10
    assert client.prepare_command()[2] == {'effect': 'transition', 'color': '#FFFFFF', 'duration_ms': 900, 'easing': 'ease_in_out'}
    client.begin_capture('second')
    now[0] = 100
    client.finish_capture('first', True)
    assert client.prepare_command()[2]['color'] == '#FF0000'
    client.finish_capture('second', False)
    assert client.prepare_command()[2] == {'effect': 'transition', 'color': '#FF9800', 'duration_ms': 900, 'easing': 'ease_in_out'}


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
        assert client.prepare_command()[2]['color'] == '#FF0000'
    finally:
        release.set()
        client.close()
    assert sent[0] == ('/set', {'effect': 'system_awakening', 'duration_ms': 5000, 'repeat': True})
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


def test_manual_test_cancels_completion_and_consumes_previous_pending_command():
    calls = []
    client = LightingClient(LightingSettings(), send=lambda path, payload: calls.append((path, payload)) or (200, {'success': True}))
    client.begin_capture('one')
    client.finish_capture('one', True)
    assert client.completion_deadline is not None
    result = client.send_manual_command('/off', None)
    assert result == (200, {'success': True})
    assert client.completion_deadline is None
    assert client.sent_revision == client.revision
    assert client.prepare_command()[1:] == ('/off', None)
    assert calls == [('/off', None)]


def test_failed_manual_test_preserves_the_previous_automatic_command():
    import pytest
    def unavailable(*args):
        raise OSError('offline')
    client = LightingClient(LightingSettings(), send=unavailable)
    client.begin_capture('one')
    client.finish_capture('one', True)
    previous = client.prepare_command()
    deadline = client.completion_deadline
    with pytest.raises(OSError):
        client.send_manual_command('/set', {'effect': 'solid'})
    assert client.prepare_command() == previous
    assert client.completion_deadline == deadline


def test_new_capture_during_manual_delivery_remains_the_latest_command():
    client = LightingClient(LightingSettings())
    def send(path, payload):
        client.begin_capture('new')
        return 200, {'success': True}
    client.send = send
    client.send_manual_command('/off', None)
    assert client.capture_id == 'new'
    assert client.prepare_command()[2] == {'effect': 'transition', 'color': '#FF0000', 'duration_ms': 900, 'easing': 'ease_in_out'}
    assert client.sent_revision != client.revision


def test_green_waits_for_ai_and_cannot_be_overridden_by_startup():
    now = [0.0]
    client = LightingClient(LightingSettings(), clock=lambda: now[0])
    client.begin_capture('photos')
    client.finish_capture('photos', True, awaiting_analysis=True)
    assert client.completion_deadline is None
    assert client.prepare_command()[2]['color'] == '#FF0000'
    client.application_ready()
    client.begin_startup()
    assert client.prepare_command()[2]['color'] == '#FF0000'
    client.begin_analysis('job')
    now[0] = 30
    assert client.prepare_command()[2]['color'] == '#FF0000'
    client.finish_analysis('stale-job', True)
    assert client.completion_deadline is None
    client.finish_analysis('job', True)
    assert client.prepare_command()[2]['color'] == '#00FF49'
    assert client.completion_deadline == 40
    now[0] = 40
    assert client.prepare_command()[2]['color'] == '#FFFFFF'


def test_failure_and_queued_jobs_never_signal_early_success():
    client = LightingClient(LightingSettings())
    client.begin_analysis('one')
    client.begin_analysis('two')
    client.finish_analysis('one', True)
    assert client.prepare_command()[2]['color'] == '#FF0000'
    client.finish_analysis('two', False)
    assert client.prepare_command()[2]['color'] == '#FF9800'
    assert client.completion_deadline is None
    client.begin_capture('capture')
    client.finish_capture('capture', True, awaiting_analysis=True)
    client.analysis_rejected()
    assert client.prepare_command()[2]['color'] == '#FF9800'
    assert not client.awaiting_analysis


def test_startup_animation_has_a_minimum_hold_and_capture_cancels_pending_white():
    now = [0.0]
    client = LightingClient(LightingSettings(), clock=lambda: now[0])
    client.begin_startup()
    client.application_ready()
    now[0] = 3.9
    assert client.prepare_command()[2]['effect'] == 'system_awakening'
    now[0] = 4
    assert client.prepare_command()[2]['color'] == '#FFFFFF'
    client.begin_startup()
    client.application_ready()
    client.begin_capture('new')
    now[0] = 20
    assert client.prepare_command()[2]['color'] == '#FF0000'
