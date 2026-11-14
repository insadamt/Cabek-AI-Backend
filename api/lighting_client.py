"""Publish application lifecycle snapshots to the independent ESP32 lighting API.

Depends only on stdlib HTTP and the existing readiness probe. Two bounded workers
keep network/readiness I/O outside capture requests; no serial or effects run here.
"""
from dataclasses import dataclass, field
import json
import logging
import os
from threading import Event, RLock, Thread
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from uuid import uuid4

logger = logging.getLogger(__name__)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Lighting redirects are forbidden', headers, fp)

@dataclass(frozen=True)
class LightingSettings:
    enabled: bool = False
    url: str = 'http://127.0.0.1:8001'
    token: str = field(default='', repr=False)
    timeout: float = 1
    health_seconds: float = 5

    def __post_init__(self):
        from urllib.parse import urlsplit
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Configure a direct lighting API URL without credentials/query/fragment.')
        if not 0 < self.timeout <= 10 or not 0 < self.health_seconds <= 60:
            raise ValueError('Lighting client intervals are out of range.')
        if self.enabled and len(self.token) < 32:
            raise ValueError('A server-side lighting control token is required.')

    @classmethod
    def from_environment(cls):
        return cls(enabled=os.getenv('CABEK_LIGHTS_ENABLED','0') == '1',
                   url=os.getenv('CABEK_LIGHTS_API_URL','http://127.0.0.1:8001'),
                   token=os.getenv('CABEK_LIGHTS_TOKEN',''),
                   timeout=float(os.getenv('CABEK_LIGHTS_TIMEOUT','1')),
                   health_seconds=float(os.getenv('CABEK_LIGHTS_HEALTH_SECONDS','5')))

class LightingClient:
    def __init__(self, settings, clock=time.monotonic, send=None):
        self.settings = settings
        self.clock = clock
        self.lock = RLock()
        self.state = self
        self.lifecycle = 'OFF'
        self.entered_at = clock()
        self.ready = False
        self.capture_id = None
        self.warning = False
        self.session_id = uuid4().hex
        self.sequence = 0
        self.stop = Event()
        self.wake = Event()
        self.workers = []
        self.probe = None
        self.send = send or self.send_snapshot

    def transition(self, state):
        with self.lock:
            if state != self.lifecycle:
                self.lifecycle = state
                self.entered_at = self.clock()
                self.warning = False
            self.wake.set()

    def start(self):
        if not self.settings.enabled or self.workers:
            return
        self.transition('BOOTING')
        self.workers = [Thread(target=self.run, daemon=True, name='lighting-http'),
                        Thread(target=self.monitor_readiness, daemon=True, name='lighting-readiness')]
        for worker in self.workers:
            worker.start()

    def initialize(self):
        self.transition('INITIALIZING')

    def set_readiness_probe(self, probe):
        self.probe = probe

    def monitor_readiness(self):
        while not self.stop.is_set():
            if self.probe:
                try:
                    self.probe()
                    ready = True
                except Exception:
                    ready = False
                with self.lock:
                    self.ready = ready
                    if not self.capture_id and self.lifecycle not in ('OFF','BOOTING','COMPLETED'):
                        self.transition('READY' if ready else 'INITIALIZING')
            self.stop.wait(self.settings.health_seconds)

    def begin_capture(self, capture_id):
        with self.lock:
            self.capture_id = capture_id
            self.transition('SCANNING')

    def finish_capture(self, capture_id, successful):
        with self.lock:
            if capture_id != self.capture_id:
                return
            self.capture_id = None
            self.transition('COMPLETED' if successful else ('READY' if self.ready else 'INITIALIZING'))
            self.warning = not successful

    def snapshot(self):
        with self.lock:
            if self.lifecycle == 'COMPLETED' and self.clock() - self.entered_at >= 10:
                self.transition('READY' if self.ready else 'INITIALIZING')
            self.sequence += 1
            return dict(session_id=self.session_id, sequence=self.sequence, state=self.lifecycle,
                        ready=self.ready, capture_id=self.capture_id, warning=self.warning,
                        elapsed_ms=max(0, (self.clock() - self.entered_at) * 1000))

    def send_snapshot(self, snapshot):
        request = Request(self.settings.url.rstrip('/') + '/api/v1/lights/events',
                          data=json.dumps(snapshot).encode(), method='POST',
                          headers={'Authorization':'Bearer ' + self.settings.token,
                                   'Content-Type':'application/json'})
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=self.settings.timeout) as response:
            if response.status != 202:
                raise OSError('Lighting event was not accepted')

    def run(self):
        while True:
            self.wake.clear()
            try:
                snapshot = self.snapshot()
                self.send(snapshot)
            except Exception as error:
                logger.warning('Lighting API unavailable: %s', type(error).__name__)
            if self.stop.is_set():
                if snapshot['state'] != 'OFF':
                    try:
                        self.send(self.snapshot())
                    except Exception:
                        logger.warning('Could not publish lighting shutdown')
                return
            self.wake.wait(1)

    def close(self):
        if not self.workers:
            return
        with self.lock:
            self.capture_id = None
            self.transition('OFF')
        self.stop.set()
        self.wake.set()
        for worker in self.workers:
            worker.join(timeout=self.settings.timeout * 2 + 1)
