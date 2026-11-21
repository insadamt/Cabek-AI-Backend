"""Send bounded PDF requests to the separate OCR API without exposing credentials.

Depends on requests and environment settings; redirects and automatic retries are disabled.
"""
from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit
import requests
from api.errors import DossierError


@dataclass(frozen=True)
class EconstatSettings:
    url: str = 'http://127.0.0.1:8002'
    token: str = field(default='', repr=False)
    timeout: float = 120

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Configure a direct E-Constat URL without credentials.')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 600:
            raise ValueError('E-Constat timeout must be between zero and 600 seconds.')

    @classmethod
    def from_environment(cls):
        return cls(os.getenv('CABEK_ECONSTAT_API_URL', 'http://127.0.0.1:8002'),
                   os.getenv('CABEK_ECONSTAT_API_TOKEN', ''),
                   float(os.getenv('CABEK_ECONSTAT_TIMEOUT_SECONDS', '120')))


class EconstatClient:
    def __init__(self, settings):
        self.settings = settings

    def analyze(self, content, registration):
        if not self.settings.token:
            raise DossierError(503, 'econstat_not_configured', 'Analyse E-Constat indisponible. Sélectionnez les caméras manuellement.')
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self.settings.url.rstrip('/') + '/api/v1/constats',
                                  headers={'Authorization': 'Bearer ' + self.settings.token},
                                  data={'immatriculation': registration},
                                  files={'constat': ('econstat.pdf', content, 'application/pdf')},
                                  timeout=self.settings.timeout, allow_redirects=False, stream=True) as response:
                    if response.status_code == 409:
                        return None
                    if response.status_code != 200:
                        raise ValueError('OCR rejected request')
                    chunks, size = [], 0
                    for chunk in response.iter_content(8192):
                        size += len(chunk)
                        if size > 131072:
                            raise ValueError('Oversized OCR response')
                        chunks.append(chunk)
                    import json
                    return json.loads(b''.join(chunks))
        except (requests.RequestException, ValueError, TypeError):
            raise DossierError(503, 'econstat_unavailable',
                               'Analyse E-Constat indisponible. Sélectionnez les caméras manuellement.') from None
