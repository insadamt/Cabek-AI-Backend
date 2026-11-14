"""Send direct lighting commands without blocking CABEK AI capture requests.

Depends on stdlib HTTP and one worker. CABEK AI alone owns the ten-second return
from successful capture to white; the lighting service receives no lifecycle data.
"""
from dataclasses import dataclass
import json
import logging
import math
import os
from threading import Event, Lock, RLock, Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

logger = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Lighting redirects are forbidden', headers, fp)


@dataclass(frozen=True)
class LightingSettings:
    enabled: bool = False
    url: str = 'http://127.0.0.1:8001'
    timeout: float = 1

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Configure a direct lighting API URL without credentials/query/fragment.')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 10:
            raise ValueError('Lighting timeout must be positive and at most ten seconds.')

    @classmethod
    def from_environment(cls):
        return cls(enabled=os.getenv('CABEK_LIGHTS_ENABLED', '0') == '1',
                   url=os.getenv('CABEK_LIGHTS_API_URL', 'http://127.0.0.1:8001'),
                   timeout=float(os.getenv('CABEK_LIGHTS_TIMEOUT', '1')))


class LightingClient:
    def __init__(self, settings, clock=time.monotonic, send=None):
        self.settings = settings
        self.clock = clock
        self.lock = RLock()
        self.command_lock = Lock()
        self.sent_revision = -1
        self.capture_id = None
        self.completion_deadline = None
        self.path = '/off'
        self.payload = None
        self.revision = 0
        self.wake = Event()
        self.stopping = False
        self.worker = None
        self.send = send or self.send_command

    def set_effect(self, effect='solid', **parameters):
        with self.lock:
            self.path = '/set'
            self.payload = {'effect': effect, **parameters}
            self.revision += 1
            self.wake.set()

    def start(self):
        with self.lock:
            if not self.settings.enabled or self.worker is not None:
                return
            self.set_effect('system_awakening')
            self.worker = Thread(target=self.run, daemon=True, name='lighting-http')
            self.worker.start()

    def application_ready(self):
        self.set_effect(color='#FFFFFF')

    def begin_capture(self, capture_id):
        with self.lock:
            self.capture_id = capture_id
            self.completion_deadline = None
            self.set_effect('pulse', color='#FF0000', repeat=True)

    def finish_capture(self, capture_id, successful):
        with self.lock:
            if capture_id != self.capture_id:
                return
            self.capture_id = None
            self.completion_deadline = self.clock() + 10 if successful else None
            self.set_effect('mission_complete' if successful else 'warning')

    def prepare_command(self):
        with self.lock:
            if self.completion_deadline is not None and self.clock() >= self.completion_deadline:
                self.completion_deadline = None
                self.application_ready()
            return self.revision, self.path, self.payload

    def send_command(self, path, payload):
        request = Request(self.settings.url.rstrip('/') + '/api/lights' + path,
                          data=None if payload is None else json.dumps(payload).encode(), method='POST',
                          headers={} if payload is None else {'Content-Type': 'application/json'})
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=self.settings.timeout) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError('Invalid lighting response')
            if response.status not in (200, 202) or result.get('success') is not True:
                raise OSError('Lighting command was not accepted')
            return response.status, result

    def send_manual_command(self, path, payload):
        # Serialize with automatic HTTP calls so stale retries cannot overwrite a manual test.

        with self.command_lock:
            with self.lock:
                previous_revision = self.revision
            result = self.send(path, payload)
            with self.lock:
                # Preserve a newer capture event that arrived during the manual HTTP request.

                if self.revision == previous_revision:
                    self.completion_deadline = None
                    self.path, self.payload = path, payload
                    self.revision += 1
                    self.sent_revision = self.revision
            return result

    def run(self):
        while True:
            self.wake.clear()
            with self.command_lock:
                revision, path, payload = self.prepare_command()
                if revision != self.sent_revision:
                    try:
                        self.send(path, payload)
                        self.sent_revision = revision
                    except Exception as error:
                        logger.warning('Lighting API unavailable: %s', type(error).__name__)
            with self.lock:
                # Recheck after slow HTTP so shutdown arriving during a request still sends OFF.

                if self.stopping and revision == self.revision:
                    return
                delay = 1 if self.completion_deadline is None else min(1, max(0, self.completion_deadline - self.clock()))
            self.wake.wait(delay)

    def close(self):
        with self.lock:
            if self.worker is None:
                return
            self.capture_id = None
            self.completion_deadline = None
            self.path, self.payload = '/off', None
            self.revision += 1
            self.stopping = True
            self.wake.set()
        self.worker.join(timeout=self.settings.timeout * 2 + 1)
