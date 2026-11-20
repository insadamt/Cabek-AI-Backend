"""Own one vehicle's lights and camera reservation through its selected completion gate.

Depends on the existing tunnel file lock, atomic leases, Shelly and RGB clients.
Only a matching capture/run may release ownership, including after API recovery.
"""
import json
import logging
from threading import RLock

from filelock import FileLock, Timeout

from api.errors import DossierError
from scans.artifacts import publish_json

logger = logging.getLogger(__name__)


class TunnelLifecycle:
    def __init__(self, repository, root, exterior, interior):
        self.repository = repository
        self.path = root / 'tunnel-lease.json'
        self.exterior = exterior
        self.interior = interior
        self.lock = RLock()
        self.lease = None
        self.tunnel_lock = None

    def snapshot(self):
        with self.lock:
            return dict(self.lease) if self.lease else None

    def persist(self):
        publish_json(self.path, self.lease)

    def begin_capture(self, dossier_id, capture, tunnel_lock):
        with self.lock:
            self.lease = {'dossier_id': dossier_id, 'capture_id': capture['capture_id'],
                          'slow_mode': bool(capture.get('slow_mode')), 'run_id': None}
            self.tunnel_lock = tunnel_lock
            self.persist()
            self.exterior.begin_capture(capture['capture_id'])
            logger.info('Capture started', extra=self.lease)

    def holds(self, capture_id):
        with self.lock:
            return bool(self.lease and self.lease['capture_id'] == capture_id)

    def finish_capture(self, capture_id, successful, expects_analysis):
        with self.lock:
            if not self.holds(capture_id):
                return
            wait_for_ai = successful and expects_analysis and self.lease['slow_mode']
            self.exterior.finish_capture(capture_id, successful, awaiting_analysis=wait_for_ai)
            if wait_for_ai:
                logger.info('Capture complete; tunnel reserved for AI', extra=self.lease)
            else:
                self.release(successful)

    def begin_analysis(self, run_id, dossier_id):
        with self.lock:
            logger.info('Analysis queued', extra={'dossier_id': dossier_id, 'run_id': run_id})
            if not self.lease or self.lease['dossier_id'] != dossier_id or not self.lease['slow_mode']:
                return
            self.lease['run_id'] = run_id
            self.persist()
            self.exterior.begin_analysis(run_id)

    def finish_analysis(self, run_id, successful):
        with self.lock:
            logger.log(logging.INFO if successful else logging.ERROR, 'Analysis finished',
                       extra={'run_id': run_id, 'code': 'analysis_completed' if successful else 'analysis_failed'})
            if not self.lease or self.lease.get('run_id') != run_id:
                return
            self.exterior.finish_analysis(run_id, successful)
            self.release(successful)

    def reject_analysis(self, dossier_id):
        with self.lock:
            if self.lease and self.lease['dossier_id'] == dossier_id:
                self.exterior.analysis_rejected()
                self.release(False)

    def release(self, successful):
        # OFF completes while the lock is held, so it cannot extinguish a newer capture.

        self.interior.cleanup()
        logger.log(logging.INFO if successful else logging.WARNING, 'Tunnel released', extra=self.lease or {})
        self.lease = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.exception('Could not remove completed tunnel lease')
        finally:
            if self.tunnel_lock is not None:
                self.tunnel_lock.release()
                self.tunnel_lock = None

    def recover(self):
        with self.lock:
            if not self.path.exists():
                return
            tunnel_lock = FileLock(str(self.repository.dossiers_dir / '.tunnel.lock'), thread_local=False)
            try:
                tunnel_lock.acquire(timeout=0)
            except Timeout as error:
                raise DossierError(503, 'tunnel_busy', 'Un autre processus possède le tunnel.') from error
            self.tunnel_lock = tunnel_lock
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict) or not isinstance(saved.get('dossier_id'), str) or not isinstance(saved.get('capture_id'), str):
                    raise ValueError('Invalid tunnel lease')
                self.lease = saved
                dossier = self.repository.read_dossier(self.lease['dossier_id'])
                capture = next((item for item in dossier.metadata.captures.values()
                                if item.get('capture_id') == self.lease['capture_id']), {})
                if capture.get('status') != 'completed' or capture.get('analysis_error') or not self.lease['slow_mode']:
                    raise ValueError('Interrupted capture')
                status_path = dossier.directory / 'analysis/status.json'
                record = json.loads(status_path.read_text()) if status_path.exists() else {}
                state = record.get('status', 'not_analyzed')
                self.exterior.begin_capture(self.lease['capture_id'])
                self.exterior.finish_capture(self.lease['capture_id'], True, awaiting_analysis=True)
                if state in ('queued', 'processing') and record.get('run_id'):
                    if self.lease.get('run_id') not in (None, record['run_id']):
                        raise ValueError('Superseded run')
                    self.begin_analysis(record['run_id'], dossier.metadata.dossier_id)
                elif state == 'completed' and record.get('run_id') == self.lease.get('run_id'):
                    from scans.artifacts import validate_outputs

                    validate_outputs(dossier.directory / 'analysis', dossier)
                    self.exterior.begin_analysis(record['run_id'])
                    self.finish_analysis(record['run_id'], True)
                elif state != 'not_analyzed':
                    self.exterior.analysis_rejected()
                    self.release(False)
            except (OSError, ValueError, KeyError, TypeError, DossierError):
                logger.warning('Interrupted tunnel reservation recovered')
                self.exterior.analysis_rejected()
                self.release(False)

    def close(self):
        with self.lock:
            if self.lease:
                self.exterior.analysis_rejected()
                self.release(False)
