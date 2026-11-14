"""Verify lifecycle publication without embedding lighting routes or serial I/O.

Depends on a fake HTTP sender and monotonic clock; no device or network is used.
"""
from api.lighting_client import LightingClient, LightingSettings

def test_capture_completion_and_latest_state_snapshot():
    now=[0.0]
    client=LightingClient(LightingSettings(), clock=lambda:now[0])
    client.initialize()
    client.ready=True
    client.transition('READY')
    client.begin_capture('first')
    client.finish_capture('first',True)
    now[0]=9
    assert client.snapshot()['state']=='COMPLETED'
    client.begin_capture('second')
    now[0]=11
    assert client.snapshot()['state']=='SCANNING'
    client.finish_capture('first',True)
    assert client.snapshot()['capture_id']=='second'
    client.finish_capture('second',False)
    assert client.snapshot()['warning']


def test_slow_network_never_blocks_capture_and_shutdown_publishes_off():
    from threading import Event
    entered,release=Event(),Event()
    sent=[]
    def send(snapshot):
        sent.append(snapshot)
        if len(sent)==1:
            entered.set()
            assert release.wait(2)
    client=LightingClient(LightingSettings(enabled=True,token='c'*32),send=send)
    client.start()
    try:
        assert entered.wait(1)
        client.begin_capture('capture-while-network-blocked')
        assert client.snapshot()['state']=='SCANNING'
    finally:
        release.set()
        client.close()
    assert sent[-1]['state']=='OFF'
    assert all(not worker.is_alive() for worker in client.workers)
