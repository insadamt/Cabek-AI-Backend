"""Run independent maintenance checks without switching lights or launching models.

Depends on existing clients, filesystem probes and bounded HTTP timeouts. Optional
hardware and mock cameras are reported explicitly rather than assumed healthy.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import importlib.util
import os
import shutil
import tempfile
from threading import Lock
import time
from urllib.request import Request, build_opener, ProxyHandler

from api.errors import DossierError
from api.lighting_client import NoRedirect


CHECKS = {
    'backend': ('API CABEK AI', 'Vérifiez le processus API et ses paramètres.'),
    'storage': ('Stockage des dossiers', 'Vérifiez les permissions et l’espace disque.'),
    'capture': ('Source de capture', 'Configurez le fournisseur et les photographies de test.'),
    'ai': ('Modèles et worker IA', 'Vérifiez les dépendances, les modèles et la file de traitement.'),
    'catalogue': ('Catalogue AutoEstimate', 'Vérifiez le service AutoEstimate et le jeton serveur.'),
    'pricing': ('Configuration du chiffrage', 'Vérifiez la configuration puis lancez une requête de prix.'),
    'interior': ('Éclairage intérieur · Shelly', 'Vérifiez le réseau et la configuration du relais.'),
    'exterior': ('Éclairage extérieur · ESP32', 'Vérifiez le service RGB puis lancez un test manuel.'),
}


def outcome(status, message, **details):
    return {'status': status, 'message': message, **details}


class DiagnosticsService:
    def __init__(self, settings, manager, captures, catalogue, lifecycle, logs):
        self.settings = settings
        self.manager = manager
        self.captures = captures
        self.catalogue = catalogue
        self.lifecycle = lifecycle
        self.logs = logs
        self.run_lock = Lock()
        self.cache_lock = Lock()
        self.latest = {}

    def snapshot(self):
        with self.cache_lock:
            checks = [self.latest.get(key, {'id': key, 'label': label, 'action': action,
                       'status': 'not_tested', 'message': 'Test non exécuté.', 'checked_at': None,
                       'duration_ms': None, 'code': None}) for key, (label, action) in CHECKS.items()]
        return {'checks': checks, 'reservation': self.lifecycle.snapshot(),
                'capture_provider': self.captures.cameras.source(), 'logs_persistent': self.logs.disk_available}

    def run(self, ids=None):
        ids = list(dict.fromkeys(ids if ids is not None else CHECKS))
        if not ids or any(key not in CHECKS for key in ids):
            raise DossierError(422, 'invalid_request', 'Sélection de diagnostics inconnue.')
        if not self.run_lock.acquire(blocking=False):
            raise DossierError(409, 'diagnostics_busy', 'Des diagnostics sont déjà en cours.')
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='cabek-diagnostics') as executor:
                results = list(executor.map(self.run_check, ids))
            with self.cache_lock:
                self.latest.update({item['id']: item for item in results})
            return self.snapshot()
        finally:
            self.run_lock.release()

    def run_check(self, key):
        started = time.monotonic()
        try:
            result = getattr(self, 'check_' + key)()
        except DossierError as error:
            result = outcome('error', error.message, code=error.code)
        except Exception as error:
            result = outcome('error', 'Le composant ne répond pas ou sa réponse est invalide.',
                             code=type(error).__name__)
        label, action = CHECKS[key]
        item = {'id': key, 'label': label, 'action': action, 'code': None,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'duration_ms': round((time.monotonic() - started) * 1000), **result}
        self.logs.record('ERROR' if result['status'] == 'error' else 'INFO', 'diagnostics.' + key,
                         result['message'], code=item.get('code') or result['status'])
        return item

    def check_backend(self):
        self.captures.operation_settings.read()
        return outcome('ok', 'API disponible ; paramètres lisibles.',
                       details={'scan_timeout_seconds': self.settings.scan_timeout_seconds,
                                'queue_capacity': self.settings.scan_max_pending})

    def check_storage(self):
        root = self.settings.dossiers_dir
        if not root.is_dir():
            return outcome('error', 'Le répertoire des dossiers est indisponible.', code='storage_unavailable')
        with tempfile.NamedTemporaryFile(dir=root, prefix='.diagnostic-', delete=True) as stream:
            stream.write(b'cabek-storage-check')
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read() != b'cabek-storage-check':
                raise OSError('Storage verification failed')
        usage = shutil.disk_usage(root)
        return outcome('warning' if usage.free < 1024 ** 3 else 'ok',
                       'Lecture et écriture vérifiées. Espace libre : %.2f Gio.' % (usage.free / 1024 ** 3),
                       details={'free_bytes': usage.free, 'total_bytes': usage.total})

    def check_capture(self):
        if self.settings.tunnel_mode == 'tiandy' or self.captures.cameras.source() == 'tiandy':
            catalog = self.captures.cameras.catalog()
            missing = [item['name'] for item in catalog['cameras']
                       if item['role'] == 'essential' and not item['online']]
            return outcome('error' if missing else 'warning' if catalog['source'] == 'demo' else 'ok',
                           'Caméras indisponibles : ' + ', '.join(missing) if missing else
                           'Source démonstration prête.' if catalog['source'] == 'demo' else 'Caméras essentielles disponibles.',
                           details=catalog)
        if self.settings.tunnel_mode == 'disabled':
            return outcome('disabled', 'Capture désactivée sur le serveur.')
        counts = {}
        checked = 0
        deadline = time.monotonic() + 3
        for phase in (None, 'before', 'after'):
            samples = self.captures.provider.plan_capture(phase)
            for sample in samples:
                if checked >= 32 or time.monotonic() >= deadline:
                    return outcome('warning', 'Source simulée : contrôle partiel limité à 32 photographies / 3 secondes.',
                                   details={'provider': 'mock', 'checked_count': checked})
                self.captures.provider.read_photo(sample['source'])
                checked += 1
            counts[phase or 'single'] = len(samples)
        return outcome('warning', 'Source simulée : photographies lisibles. Aucune caméra physique testée.',
                       details={'provider': 'mock', 'photo_counts': counts})

    def check_ai(self):
        self.manager.backend.ensure_available(self.settings.models_dir)
        from scans.backend import ModelProcessBackend

        if isinstance(self.manager.backend, ModelProcessBackend):
            missing = [name for name in ('bareme_c2', 'bareme_t1') if importlib.util.find_spec(name) is None]
            if missing:
                return outcome('error', 'Module du moteur IA manquant : ' + ', '.join(missing),
                               code='pipeline_dependency_missing')
        if not self.manager.available or self.manager.stopping.is_set():
            return outcome('error', 'Worker IA indisponible.', code='worker_unavailable')
        return outcome('ok', 'Dépendances, fichiers modèles et worker disponibles. Inférence non exécutée.')

    def check_catalogue(self):
        brands = self.catalogue.brands()
        if brands:
            self.catalogue.models(brands[0].id)
        return outcome('ok', 'Catalogue accessible et réponse valide.', details={'brand_count': len(brands)})

    def check_pricing(self):
        if not os.getenv('AUTOESTIMATE_BASE_URL') or not os.getenv('AUTOESTIMATE_TOKEN'):
            return outcome('error', 'URL ou jeton AutoEstimate non configuré.', code='autoestimate_unavailable')
        return outcome('warning', 'Configuration présente. Utilisez le test de prix pour vérifier une cotation réelle.')

    def check_interior(self):
        lights = self.captures.internal_lighting
        if not lights.settings.enabled:
            return outcome('disabled', 'Relais intérieur désactivé.')
        status = lights.status()
        if type(status.get('output')) is not bool:
            return outcome('error', 'Réponse du relais invalide.', code='internal_lighting_not_confirmed')
        return outcome('ok', 'Relais joignable ; aucune commutation effectuée.', details=status)

    def check_exterior(self):
        lights = self.lifecycle.exterior
        if not lights.settings.enabled:
            return outcome('disabled', 'Service RGB désactivé.')
        request = Request(lights.settings.url.rstrip('/') + '/openapi.json', headers={'Accept': 'application/json'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=2) as response:
            payload = json.loads(response.read(65536))
        if not all(path in payload.get('paths', {}) for path in ('/api/lights/set', '/api/lights/off')):
            raise ValueError('Unexpected lighting API')
        return outcome('warning', 'API RGB joignable. L’ESP32 et la lumière physique nécessitent un test manuel.',
                       details={'last_acknowledged_at': lights.last_acknowledged_at,
                                'last_command_error': lights.last_command_error})
