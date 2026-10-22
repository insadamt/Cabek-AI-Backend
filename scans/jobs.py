from concurrent.futures import ThreadPoolExecutor
import json
import logging
from math import isfinite
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from uuid import uuid4

from filelock import FileLock, Timeout

from api.config import ApiSettings
from api.errors import DossierError
from api.files import confined_path, read_json_object, validate_image
from api.repository import DossierRepository
from api.schemas import DossierResult, ScanProgress
from api.vehicle_catalog import VehicleCatalog, VehicleSelection
from api.vehicle_service import save_vehicle_selection
from scans.artifacts import input_signatures, publish_json, validate_outputs
from scans.backend import ModelProcessBackend, ScanBackend
from scans.cleanup import remove_incomplete_outputs
from scans.contracts import ScanRequest
from scans.supervisor import MANAGED_BY, ScanJob, execute_job, save_job_status


logger = logging.getLogger(__name__)


# ============================================================
# SCAN QUEUE AND PER-DOSSIER LOCKS
# ============================================================

class ScanManager:
    def __init__(self, settings: ApiSettings, backend: ScanBackend | None = None):
        if not isfinite(settings.scan_timeout_seconds) or settings.scan_timeout_seconds <= 0 or settings.scan_max_pending < 1:
            raise ValueError("Scan timeout and queue capacity must be positive.")
        self.settings = settings
        self.repository = DossierRepository(settings.dossiers_dir)
        self.backend = backend or ModelProcessBackend()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cabek-scan")
        self.capacity = BoundedSemaphore(settings.scan_max_pending)
        self.stopping = Event()
        self.lifecycle_lock = Lock()
        self.recovery_thread: Thread | None = None
        self.available = False

    def start(self) -> None:
        self.recover_interrupted_jobs()
        self.available = True
        self.recovery_thread = Thread(target=self.monitor_interrupted_jobs, daemon=True)
        self.recovery_thread.start()

    def close(self) -> None:
        with self.lifecycle_lock:
            self.available = False
            self.stopping.set()

        # Supervisors stop their children before releasing the dossier locks.

        self.executor.shutdown(wait=True)
        if self.recovery_thread is not None:
            self.recovery_thread.join()

    def start_scan(self, dossier_id: str) -> DossierResult:
        dossier = self.repository.read_dossier(dossier_id)
        analysis_dir = self.prepare_analysis_directory(dossier.directory)
        dossier_lock = FileLock(str(analysis_dir / ".scan.lock"), thread_local=False)
        reserved = False
        try:
            dossier_lock.acquire(timeout=0)
            with self.lifecycle_lock:
                if not self.available:
                    raise DossierError(503, "worker_unavailable", "The scan worker is unavailable.")

                # An orphaned child may outlive the API that owned the admission lock.

                with FileLock(str(analysis_dir / ".worker.lock"), timeout=0):
                    dossier = self.repository.read_dossier(dossier_id)
                    self.reject_completed_scan(dossier, analysis_dir)
                    self.reject_external_scan(analysis_dir)
                    if not dossier.photos:
                        raise DossierError(422, "no_valid_photos", "The dossier has no valid photos.")
                    for photo in dossier.photos.values():
                        validate_image(confined_path(dossier.directory, photo.original_path))
                    self.backend.ensure_available(self.settings.models_dir)
                    reserved = self.capacity.acquire(blocking=False)
                    if not reserved:
                        raise DossierError(503, "worker_unavailable", "The scan queue is full.")
                    if dossier.metadata.marque_id is not None:

                        # Resolve manually supplied IDs before snapshotting inputs; names never override IDs.

                        save_vehicle_selection(dossier.directory, VehicleSelection(
                            marque_id=dossier.metadata.marque_id, model_id=dossier.metadata.model_id,
                        ), VehicleCatalog())
                        dossier = self.repository.read_dossier(dossier_id)
                    job = self.prepare_job(dossier, analysis_dir, dossier_lock)
                    remove_incomplete_outputs(analysis_dir)
                    publish_json(analysis_dir / ".request.json", job.request.model_dump(mode="json"))
                    record = save_job_status(job, "queued")

                # Release the child lock before launching; retain the admission lock for supervision.

                try:
                    self.executor.submit(self.run_job, job)
                except RuntimeError as error:
                    save_job_status(job, "failed", "scan_interrupted")
                    raise DossierError(503, "worker_unavailable", "The scan worker is unavailable.") from error
            return DossierResult(
                dossier_id=dossier_id, status="queued",
                vehicle=dossier.metadata,
                progress=ScanProgress.model_validate(record["progress"]),
            )
        except Exception as error:
            dossier_lock.release()
            if reserved:
                self.capacity.release()
            if isinstance(error, Timeout):
                raise DossierError(409, "scan_in_progress", "A scan is already queued or processing.") from error
            if isinstance(error, OSError):
                raise DossierError(503, "storage_unavailable", "Scan storage is unavailable.") from error
            raise

    def run_job(self, job: ScanJob) -> None:
        try:
            execute_job(job, self.backend, self.repository, self.stopping, self.settings.scan_timeout_seconds)
        finally:
            self.capacity.release()

    def prepare_analysis_directory(self, dossier_dir: Path) -> Path:
        analysis_dir = confined_path(dossier_dir, "analysis")
        protected = ("analysis", "analysis/.scan.lock", "analysis/.worker.lock",
                     "analysis/status.json", "analysis/.request.json", "analysis/.worker.log",
                     "analysis/annotated", "analysis/.runtime", "analysis/result.json", "analysis/report.pdf")
        for relative_path in protected:
            if (dossier_dir / relative_path).is_symlink():
                raise DossierError(422, "invalid_analysis", "Scan storage must not use symlinks.")
        try:
            analysis_dir.mkdir(exist_ok=True)
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Scan storage is unavailable.") from error
        return analysis_dir

    def reject_completed_scan(self, dossier, analysis_dir: Path) -> None:
        status_path = analysis_dir / "status.json"
        record = read_json_object(status_path, "invalid_status") if status_path.is_file() else {}
        if record.get("status") == "completed":
            raise DossierError(409, "already_analyzed", "This dossier already has a saved analysis.")
        if not (analysis_dir / "result.json").is_file():
            return
        try:
            validate_outputs(analysis_dir, dossier)
        except (DossierError, OSError, ValueError):
            return

        # Recover a valid completion marker instead of deleting it during a retry.

        if record.get("managed_by") == MANAGED_BY:
            self.save_recovered_status(dossier, record, "completed")
        raise DossierError(409, "already_analyzed", "This dossier already has a saved analysis.")

    def reject_external_scan(self, analysis_dir: Path) -> None:
        path = analysis_dir / "status.json"
        if not path.exists():
            return
        record = read_json_object(path, "invalid_status")
        if record.get("status") not in ("not_analyzed", "queued", "processing", "failed", "completed"):
            raise DossierError(422, "invalid_status", "Saved scan status is invalid.")
        if record.get("status") in ("queued", "processing") and record.get("managed_by") != MANAGED_BY:
            raise DossierError(409, "scan_in_progress", "A scan is already queued or processing.")

    def prepare_job(self, dossier, analysis_dir: Path, dossier_lock: FileLock) -> ScanJob:
        feedback_path = confined_path(dossier.directory, "feedback.json")
        try:
            feedback = json.loads(feedback_path.read_text()) if feedback_path.is_file() else []
        except (OSError, ValueError) as error:
            raise DossierError(422, "invalid_feedback", "Saved feedback is invalid.") from error
        if not isinstance(feedback, list) or any(not isinstance(item, dict) for item in feedback):
            raise DossierError(422, "invalid_feedback", "Saved feedback is invalid.")
        request = ScanRequest(
            run_id=uuid4().hex, dossier_id=dossier.metadata.dossier_id,
            dossier_dir=dossier.directory, output_dir=analysis_dir,
            models_dir=self.settings.models_dir.resolve(),
            metadata=read_json_object(dossier.directory / "metadata.json", "invalid_metadata"),
            photos=list(dossier.photos.values()), feedback=feedback,
            input_signatures=input_signatures(dossier),
        )
        return ScanJob(dossier, request, dossier_lock)

    # ============================================================
    # RECOVERY AFTER API RESTART
    # ============================================================

    def monitor_interrupted_jobs(self) -> None:

        # Recheck after a surviving child exits; its files must remain protected while it runs.

        while not self.stopping.wait(1):
            self.recover_interrupted_jobs()

    def save_recovered_status(self, dossier, record: dict, state: str) -> None:
        from scans.progress import inspect_progress

        progress = inspect_progress(dossier.directory / "analysis", list(dossier.photos.values()), record.get("started_at"))
        progress.stage = state
        record.update(status=state, error_code=None if state == "completed" else "scan_interrupted",
                      progress=progress.model_dump())
        publish_json(dossier.directory / "analysis/status.json", record)

    def recover_interrupted_jobs(self) -> None:
        if not self.repository.dossiers_dir.is_dir():
            return
        for dossier_id in self.repository.dossier_ids():
            try:
                dossier = self.repository.read_dossier(dossier_id)
                path = confined_path(dossier.directory, "analysis/status.json")
                if not path.is_file():
                    continue
                record = read_json_object(path, "invalid_status")
                if record.get("managed_by") != MANAGED_BY or record.get("status") not in ("queued", "processing"):
                    continue
                analysis_dir = self.prepare_analysis_directory(dossier.directory)
                with FileLock(str(analysis_dir / ".scan.lock"), timeout=0):
                    with FileLock(str(analysis_dir / ".worker.lock"), timeout=0):
                        try:
                            validate_outputs(analysis_dir, dossier)
                            request_path = analysis_dir / ".request.json"
                            if request_path.is_file():
                                request = ScanRequest.model_validate_json(request_path.read_bytes())
                                if input_signatures(dossier) != request.input_signatures:
                                    raise ValueError("Inputs changed during the interrupted scan.")
                        except (DossierError, OSError, ValueError):
                            (analysis_dir / "result.json").unlink(missing_ok=True)
                            self.save_recovered_status(dossier, record, "failed")
                        else:
                            self.save_recovered_status(dossier, record, "completed")
            except Timeout:
                continue
            except DossierError as error:
                logger.warning("Could not recover saved scan state for %s: %s", dossier_id, error.code)
            except (OSError, ValueError):
                logger.warning("Could not recover saved scan state for %s", dossier_id)
