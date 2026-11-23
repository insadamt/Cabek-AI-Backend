"""Send direct lighting commands without blocking CABEK AI capture requests.

Depends on stdlib HTTP and one worker. CABEK AI alone owns the ten-second return
from validated AI completion to white; the lighting service receives no lifecycle data.
"""
from dataclasses import dataclass
import json
import logging
import math
import os
from threading import Event, Lock, RLock, Thread
import time
from datetime import datetime, timezone
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
        self.last_acknowledged_at = None
        self.last_command_error = None
        self.last_warning_at = float('-inf')
        self.last_warning_reason = None
        self.manual_override = False
        self.sent_revision = -1
        self.capture_id = None
        self.analysis_ids = set()
        self.awaiting_analysis = False
        self.startup_minimum_deadline = None
        self.ready_deadline = None
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
            self.manual_override = False
            self.path = '/set'
            self.payload = {'effect': effect, **parameters}
            self.revision += 1
            self.wake.set()

    def start(self):
        with self.lock:
            if not self.settings.enabled or self.worker is not None:
                return
            self.begin_startup()
            self.worker = Thread(target=self.run, daemon=True, name='lighting-http')
            self.worker.start()

    def transition_to(self, color):
        self.set_effect('transition', color=color, duration_ms=900, easing='ease_in_out')

    def begin_startup(self):
        with self.lock:
            if self.capture_id is not None or self.analysis_ids or self.awaiting_analysis or self.has_completion_signal():
                return
            self.completion_deadline = None
            self.startup_minimum_deadline = self.clock() + 4
            self.ready_deadline = None
            self.set_effect('system_awakening', duration_ms=5000, repeat=True)

    def application_ready(self):
        with self.lock:
            if self.has_completion_signal():
                return
            if self.capture_id is None and not self.analysis_ids and not self.awaiting_analysis:
                if self.startup_minimum_deadline is not None and self.clock() < self.startup_minimum_deadline:
                    self.ready_deadline = self.startup_minimum_deadline
                    return
                self.ready_deadline = None
                self.startup_minimum_deadline = None
                self.transition_to('#FFFFFF')

    def has_completion_signal(self):
        # A late browser startup/ready message cannot erase a success hold or failure warning.

        return self.completion_deadline is not None or bool(self.payload and (
            self.payload.get('color') == '#FF9800' or self.payload.get('effect') == 'warning'))

    def begin_capture(self, capture_id):
        with self.lock:
            self.ready_deadline = None
            self.capture_id = capture_id
            self.completion_deadline = None
            self.transition_to('#FF0000')

    def finish_capture(self, capture_id, successful, awaiting_analysis=False):
        with self.lock:
            if capture_id != self.capture_id:
                return
            self.capture_id = None
            if successful and awaiting_analysis:
                self.awaiting_analysis = True
                return
            self.finish_activity(successful)

    def begin_analysis(self, run_id):
        with self.lock:
            self.awaiting_analysis = False
            self.ready_deadline = None
            self.analysis_ids.add(run_id)
            self.completion_deadline = None
            self.transition_to('#FF0000')

    def finish_analysis(self, run_id, successful):
        with self.lock:
            if run_id not in self.analysis_ids:
                return
            self.analysis_ids.remove(run_id)
            self.finish_activity(successful)

    def analysis_rejected(self):
        with self.lock:
            self.awaiting_analysis = False
            self.finish_activity(False)

    def finish_activity(self, successful):
        if self.capture_id is not None or self.analysis_ids or self.awaiting_analysis:
            return
        self.completion_deadline = self.clock() + 10 if successful else None
        self.transition_to('#00FF49' if successful else '#FF9800')

    def prepare_command(self):
        with self.lock:
            if self.ready_deadline is not None and self.clock() >= self.ready_deadline:
                self.ready_deadline = None
                self.application_ready()
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
            self.last_acknowledged_at = datetime.now(timezone.utc).isoformat()
            self.last_command_error = None
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
                    self.manual_override = True
                    self.ready_deadline = None
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
                        reason = lighting_failure_reason(error)
                        self.last_command_error = reason
                        # Keep disconnected hardware visible without flooding station logs.

                        if reason != self.last_warning_reason or self.clock() - self.last_warning_at >= 30:
                            logger.warning('Lighting command failed: %s', reason)
                            self.last_warning_at, self.last_warning_reason = self.clock(), reason
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
            self.analysis_ids.clear()
            self.awaiting_analysis = False
            self.ready_deadline = None
            self.completion_deadline = None
            self.path, self.payload = '/off', None
            self.revision += 1
            self.stopping = True
            self.wake.set()
        self.worker.join(timeout=self.settings.timeout * 2 + 1)


def lighting_failure_reason(error):
    if isinstance(error, HTTPError):
        if error.code == 503:
            return 'ESP32 unavailable; lighting HTTP service responded (HTTP 503)'
        if error.code == 501:
            return 'ESP32 firmware does not support the requested effect (HTTP 501)'
        return f'Lighting HTTP API rejected the command (HTTP {error.code})'
    if isinstance(error, OSError):
        return 'Lighting HTTP service could not be reached'
    return 'Lighting HTTP service returned an invalid response'
