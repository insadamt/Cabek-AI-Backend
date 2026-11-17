"""Verify interior lighting before photographs using the Shelly RPC API.

Depends on backend environment settings and the existing no-redirect HTTP client.
The tunnel lock must cover activation, capture, and cleanup to prevent stale OFFs.
"""
from dataclasses import dataclass
import json
import logging
import math
import os
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from api.errors import DossierError
from api.lighting_client import NoRedirect

logger = logging.getLogger(__name__)
MESSAGES = {
    'internal_lighting_unavailable': "Impossible d'activer l'éclairage intérieur du tunnel.",
    'internal_lighting_not_confirmed': "L'éclairage intérieur du tunnel n'est pas confirmé.",
    'internal_lighting_power_low': "La consommation de l'éclairage intérieur est insuffisante ou invalide.",
}


@dataclass(frozen=True)
class InternalLightingSettings:
    enabled: bool = False
    url: str = ''
    switch_id: int = 0
    timeout: float = 3
    stabilization_ms: float = 500
    min_power_w: float | None = None

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if self.enabled and (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Configure CABEK_INTERNAL_LIGHTS_URL as a direct device base URL.')
        if self.switch_id < 0 or isinstance(self.switch_id, bool):
            raise ValueError('Internal lighting switch ID must be nonnegative.')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 30:
            raise ValueError('Internal lighting timeout must be between zero and 30 seconds.')
        if not math.isfinite(self.stabilization_ms) or not 0 <= self.stabilization_ms <= 30000:
            raise ValueError('Internal lighting stabilization must be between zero and 30000 ms.')
        if self.min_power_w is not None and (not math.isfinite(self.min_power_w) or self.min_power_w < 0):
            raise ValueError('Internal lighting minimum power must be finite and nonnegative.')

    @classmethod
    def from_environment(cls):
        enabled = os.getenv('CABEK_INTERNAL_LIGHTS_ENABLED', 'true').lower()
        if enabled not in ('true', 'false', '1', '0'):
            raise ValueError('CABEK_INTERNAL_LIGHTS_ENABLED must be true or false.')
        power = os.getenv('CABEK_INTERNAL_LIGHTS_MIN_POWER_W', '').strip()
        return cls(enabled in ('true', '1'), os.getenv('CABEK_INTERNAL_LIGHTS_URL', ''),
                   int(os.getenv('CABEK_INTERNAL_LIGHTS_SWITCH_ID', '0')),
                   float(os.getenv('CABEK_INTERNAL_LIGHTS_TIMEOUT', '3')),
                   float(os.getenv('CABEK_INTERNAL_LIGHTS_STABILIZATION_MS', '500')),
                   float(power) if power else None)


class InternalLighting:
    def __init__(self, settings):
        self.settings = settings

    def rpc(self, method, payload=None):
        if not self.settings.enabled:
            raise DossierError(503, 'internal_lighting_disabled', "L'éclairage intérieur est désactivé.")
        suffix = f'?id={self.settings.switch_id}' if payload is None else ''
        request = Request(self.settings.url.rstrip('/') + '/rpc/' + method + suffix,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'},
                          method='GET' if payload is None else 'POST')
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=self.settings.timeout) as response:
                result = json.loads(response.read(65536))
                if not 200 <= response.status < 300 or not isinstance(result, dict) or 'error' in result or 'code' in result:
                    raise ValueError('Shelly RPC rejected')
                return result
        except (OSError, ValueError) as error:
            raise DossierError(503, 'internal_lighting_unavailable', MESSAGES['internal_lighting_unavailable']) from error

    def set_output(self, on):
        self.rpc('Switch.Set', {'id': self.settings.switch_id, 'on': on})

    def status(self):
        result = self.rpc('Switch.GetStatus')
        return {key: result.get(key) for key in ('output', 'apower', 'voltage', 'current')}

    def activate_and_verify(self):
        if not self.settings.enabled:
            return
        self.set_output(True)
        status = self.status()
        if status['output'] is not True:
            raise DossierError(503, 'internal_lighting_not_confirmed', MESSAGES['internal_lighting_not_confirmed'])
        threshold = self.settings.min_power_w
        power = status['apower']
        if threshold is not None and (isinstance(power, bool) or not isinstance(power, (int, float))
                or not math.isfinite(power) or power <= threshold):
            raise DossierError(503, 'internal_lighting_power_low', MESSAGES['internal_lighting_power_low'])

    def cleanup(self):
        if self.settings.enabled:
            try:
                self.set_output(False)
            except Exception:
                logger.warning('Internal lighting OFF failed', exc_info=True)
