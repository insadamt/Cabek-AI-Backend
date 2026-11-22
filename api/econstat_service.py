"""Prepare durable, versioned E-Constat suggestions independently of tunnel capture.

Depends on the OCR client and confined document cache. One bounded executor keeps
slow OCR outside request handling; stale jobs cannot publish against changed inputs.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import RLock, BoundedSemaphore
from urllib.parse import quote
from api.econstat_client import EconstatClient
from api.econstat_contracts import validate_recommendation
from api.econstat_documents import read_document
from api.errors import DossierError
from api.files import confined_path, read_json_object
from scans.artifacts import publish_json


class EconstatService:
    def __init__(self, repository, settings):
        self.repository = repository
        self.client = EconstatClient(settings)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='econstat')
        self.lock = RLock()
        self.capacity = BoundedSemaphore(8)
        self.jobs = {}

    def close(self):
        self.executor.shutdown(wait=True)

    def result_path(self, document):
        return confined_path(document.directory, f'.econstat/{document.version}/preparation.json')

    def cached_result(self, document):
        path = self.result_path(document)
        if not path.is_file():
            return None
        try:
            result = read_json_object(path, 'invalid_econstat_cache')
            if result.get('version') != document.version or result.get('status') not in ('ready', 'manual_required', 'failed'):
                return None
            if result['status'] == 'ready':
                recommendation = validate_recommendation(result['ocr_payload'], document.registration)
                return {'status': 'ready', 'message': 'Caméras proposées à partir de l’E-Constat.', **recommendation}
            return {'status': result['status'], 'message': result['message']}
        except (DossierError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def describe(self, dossier_id, root_path=''):
        document = read_document(self.repository, dossier_id)
        with self.lock:
            result = self.cached_result(document) or {'status': 'not_started', 'message': ''}
            future = self.jobs.get((dossier_id, document.version))
            if future is not None and not future.done():
                result = {'status': 'processing', 'message': 'Analyse de l’E-Constat…'}
        base = f"{root_path.rstrip('/')}/api/dossiers/{quote(dossier_id, safe='')}/econstat"
        return {'document_version': document.version, 'registration': document.registration,
                'filename': document.filename, 'preview_url': f'{base}/page-1.webp?version={document.version}',
                'zones': [], 'recommended_zoom_channels': [], 'vehicle_reference': None, **result}

    def start_analysis(self, dossier_id, root_path=''):
        document = read_document(self.repository, dossier_id)
        key = (dossier_id, document.version)
        with self.lock:
            cached = self.cached_result(document)
            if self.repository.read_dossier(dossier_id).metadata.demo and not cached:
                raise DossierError(409, 'demo_ocr_missing', 'Résultat OCR préparé requis ; aucun OCR réel ne sera lancé.')
            active = self.jobs.get(key)
            if (cached and cached['status'] in ('ready', 'manual_required')) or (active and not active.done()):
                return self.describe(dossier_id, root_path)
            if not self.capacity.acquire(blocking=False):
                raise DossierError(503, 'econstat_busy', 'Analyse E-Constat occupée. Sélectionnez les caméras manuellement.')
            try:
                self.jobs[key] = self.executor.submit(self.prepare, document)
                self.jobs[key].add_done_callback(lambda future: self.forget_job(key))
            except Exception:
                self.capacity.release()
                raise
        return self.describe(dossier_id, root_path)

    def forget_job(self, key):
        with self.lock:
            self.jobs.pop(key, None)

    def prepare(self, document):
        result = {'version': document.version}
        try:
            payload = self.client.analyze(document.content, document.registration)
            if payload is None:
                result.update(status='manual_required', message='Aucun véhicule correspondant. Vérifiez l’E-Constat et sélectionnez les caméras manuellement.')
            else:
                validate_recommendation(payload, document.registration)
                result.update(status='ready', ocr_payload=payload)
        except DossierError as error:
            result.update(status='failed', message=error.message)
        except Exception:
            result.update(status='failed', message='Analyse E-Constat impossible. Sélectionnez les caméras manuellement.')
        try:
            # Importers can replace a document while OCR is running; discard that obsolete result.

            current = read_document(self.repository, document.dossier_id)
            if current.version == document.version:
                publish_json(self.result_path(current), result)
        except (DossierError, OSError):
            pass
        finally:
            self.capacity.release()
