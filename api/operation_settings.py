"""Persist machine-wide operating preferences independently of dossier fingerprints.

Depends on atomic publication and a file lock; capture admission snapshots settings.
"""
import json
from threading import RLock

from filelock import FileLock

from api.errors import DossierError
from scans.artifacts import publish_json


class OperationSettingsStore:
    def __init__(self, root, demo_default=False):
        self.root = root
        self.path = root / 'application-settings.json'
        self.lock = RLock()
        self.demo_default = demo_default

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {'slow_mode': False, 'demo_mode': self.demo_default}
            try:
                value = json.loads(self.path.read_text())
                if type(value['slow_mode']) is not bool:
                    raise ValueError('Invalid operation settings')
                demo_mode = value.get('demo_mode', self.demo_default)
                if type(demo_mode) is not bool:
                    raise ValueError('Invalid demo mode')
                return {'slow_mode': value['slow_mode'], 'demo_mode': demo_mode}
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise DossierError(503, 'settings_unavailable', 'Les paramètres sont illisibles.') from error

    def update(self, slow_mode=None, demo_mode=None):
        try:
            with self.lock:
                self.root.mkdir(parents=True, exist_ok=True)
                with FileLock(str(self.root / '.settings.lock'), timeout=3):
                    value = self.read()
                    for key, item in {'slow_mode': slow_mode, 'demo_mode': demo_mode}.items():
                        if item is not None:
                            if type(item) is not bool:
                                raise DossierError(422, 'invalid_request', 'Valeur de paramètre invalide.')
                            value[key] = item
                    publish_json(self.path, value)
                    return value
        except OSError as error:
            raise DossierError(503, 'settings_unavailable', 'Impossible de sauvegarder les paramètres.') from error
