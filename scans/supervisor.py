"""Supervise real workers, persist artifact progress, and validate completion.

Depends on backend processes, input fingerprints, and the optional pricing gate;
failures keep original photographs and retain the existing safe retry behavior.
"""

from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
from threading import Event
import time

from filelock import FileLock

from api.repository import DossierRecord, DossierRepository
from scans.artifacts import input_signatures, publish_json, validate_outputs
from scans.backend import ScanBackend
from scans.contracts import ScanRequest
from scans.progress import inspect_progress


logger = logging.getLogger(__name__)
MANAGED_BY = "cabek_scan_v1"


# ============================================================
# ACCEPTED JOB AND DURABLE STATUS
# ============================================================

@dataclass
class ScanJob:
    dossier: DossierRecord
    request: ScanRequest
    lock: FileLock
    started_at: float | None = None


def save_job_status(job: ScanJob, status: str, error_code: str | None = None) -> dict:
    progress = inspect_progress(job.request.output_dir, job.request.photos, job.started_at)
    if status in {"completed", "failed"}:
        progress.stage = status
    if status == "completed":
        progress.result_ready = True
    record = {
        "managed_by": MANAGED_BY, "run_id": job.request.run_id, "status": status,
        "started_at": job.started_at, "error_code": error_code,
        "progress": progress.model_dump(),
    }
    publish_json(job.dossier.directory / "analysis/status.json", record)
    return record


# ============================================================
# PROCESS SUPERVISION AND COMPLETION VALIDATION
# ============================================================

def terminate_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def execute_job(
    job: ScanJob, backend: ScanBackend, repository: DossierRepository,
    stopping: Event, timeout_seconds: float,
) -> bool:
    process = None
    failure_code = "scan_failed"
    try:
        if stopping.is_set():
            raise InterruptedError("API is stopping.")
        job.started_at = time.time()
        save_job_status(job, "processing")
        request_path = job.request.output_dir / ".request.json"
        with (job.request.output_dir / ".worker.log").open("w", encoding="utf-8") as log:
            process = backend.launch(request_path, log)
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if stopping.wait(0.25):
                    raise InterruptedError("API stopped during scan.")
                if time.monotonic() >= deadline:
                    failure_code = "scan_timeout"
                    raise TimeoutError("Scan timed out.")
                save_job_status(job, "processing")
            if process.returncode != 0:
                raise RuntimeError("Worker failed; see its private log.")
        current = repository.read_dossier(job.request.dossier_id)
        if input_signatures(current) != job.request.input_signatures:
            failure_code = "scan_inputs_changed"
            raise ValueError("Dossier inputs changed while scanning.")
        from scans.metadata_gate import validate_pricing_snapshot
        validate_pricing_snapshot(job.request, current)
        validate_outputs(job.request.output_dir, current)

        # Files are already in analysis/; only a verified successful worker establishes completion.

        save_job_status(job, "completed")
        return True
    except Exception:
        if stopping.is_set():
            failure_code = "scan_interrupted"
        logger.exception("Scan %s failed", job.request.run_id)
        if process is not None:
            terminate_worker(process)

        # A worker may fail after writing JSON; remove that marker before allowing a retry.

        (job.request.output_dir / "result.json").unlink(missing_ok=True)
        save_job_status(job, "failed", failure_code)
        return False
    finally:
        if process is not None:
            terminate_worker(process)
        job.lock.release()
