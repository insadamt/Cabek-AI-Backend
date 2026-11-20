"""Persist and supervise delivery from the single local Tiandy or demo tunnel.

Depends on workflow locks, validated sample bytes, and the existing scan manager.
Expertise retries start a fresh photo set; other workflows retain phase-based recovery.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
from math import isfinite
from pathlib import Path
from threading import Event
from urllib.parse import quote

from filelock import FileLock, Timeout

from api.internal_lighting import InternalLighting, MESSAGES
from api.capture_schemas import CaptureStatus, CapturedPhoto
from api.errors import DossierError
from api.dossier_lifecycle import dossier_admission, serialize_dossier
from api.files import confined_path, read_json_object, validate_image
from api.tunnel_provider import MockTunnelProvider
from api.camera_service import CameraService
from api.capture_camera_plan import prepare_capture_attempt
from api.workflow_service import timestamp
from api.workflow_state import SIDES, available_photo_records
from scans.artifacts import publish_bytes, publish_json


logger = logging.getLogger(__name__)


class CaptureService:
    def __init__(self, workflows, settings, lighting=None):
        if settings.tunnel_mode not in ("disabled", "mock", "tiandy"):
            raise ValueError("Use disabled, mock or tiandy tunnel mode.")
        if not isfinite(settings.mock_photo_delay_seconds) or settings.mock_photo_delay_seconds < 0:
            raise ValueError("Camera delivery delay must be finite and nonnegative.")
        self.internal_lighting = InternalLighting(settings.internal_lighting)
        self.lighting = lighting
        self.lifecycle = None
        self.operation_settings = None
        self.workflows = workflows
        self.settings = settings
        self.provider = MockTunnelProvider(settings.mock_photos_dir)
        self.cameras = CameraService(self)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cabek-capture")
        self.stopping = Event()

    def start(self):
        root = self.workflows.repository.dossiers_dir
        if not root.is_dir():
            return
        for dossier_id in self.workflows.repository.dossier_ids():
            try:
                dossier = self.workflows.repository.read_dossier(dossier_id)
                for key, capture in dossier.metadata.captures.items():
                    if capture.get("status") == "capturing":
                        with FileLock(str(dossier.directory / ".capture.lock"), timeout=0):
                            self.update_capture(dossier_id, key, status="failed", error_code="capture_interrupted")
                    elif capture.get("status") == "completed" and dossier.metadata.type != "verification":
                        self.start_analysis(dossier_id, key)
            except (DossierError, OSError, Timeout):
                continue

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=True)

    # ============================================================
    # IDEMPOTENT CAPTURE ADMISSION
    # ============================================================

    def start_capture(self, dossier_id: str, phase: str | None, zoom_channels=None) -> None:
        dossier = self.workflows.repository.read_dossier(dossier_id)
        self.validate_phase(dossier, phase)
        capture = dossier.metadata.captures.get(phase or "single", {})
        if capture.get("status") == "capturing":
            lock = FileLock(str(dossier.directory / ".capture.lock"))
            try:
                lock.acquire(timeout=0)
            except Timeout:

                # A repeated click follows the live capture without waiting for photo I/O or creating a new attempt.

                return
            else:
                lock.release()
        self.admit_capture(dossier_id, phase, zoom_channels)

    @serialize_dossier
    def admit_capture(self, dossier_id: str, phase: str | None, zoom_channels=None) -> None:
        if self.settings.tunnel_mode == "disabled" or self.stopping.is_set():
            raise DossierError(503, "tunnel_unavailable", "La capture est désactivée sur le serveur.")
        dossier = self.workflows.repository.read_dossier(dossier_id)
        self.validate_phase(dossier, phase)
        key = phase or "single"
        previous = dossier.metadata.captures.get(key)
        self.validate_expertise_capture(dossier, previous)
        if previous and previous["status"] == "completed" and (
            dossier.metadata.type != "forfait_expertise" or available_photo_records(dossier)
        ):
            self.validate_saved_capture(dossier, previous)
            self.start_analysis(dossier_id, key)
            return
        capture_lock = FileLock(str(dossier.directory / ".capture.lock"), thread_local=False)
        tunnel_lock = FileLock(str(self.workflows.repository.dossiers_dir / ".tunnel.lock"), thread_local=False)
        try:
            capture_lock.acquire(timeout=0)
        except Timeout:
            if previous and previous["status"] == "capturing":
                return
            raise DossierError(409, "capture_in_progress", "A capture is already in progress.")
        try:
            tunnel_lock.acquire(timeout=0)
            with self.workflows.editable_dossier(dossier_id, capture_owned=True) as dossier:
                metadata = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
                previous = metadata.setdefault("captures", {}).get(key)
                if previous and previous["status"] == "completed" and (
                    dossier.metadata.type != "forfait_expertise" or available_photo_records(dossier)
                ):
                    self.validate_saved_capture(dossier, previous)
                    return
                self.validate_phase(dossier, phase)
                self.validate_expertise_capture(dossier, previous)
                before_capture = dossier.metadata.captures.get("before", {})
                if phase == "after" and before_capture and before_capture.get("status") != "completed":
                    raise DossierError(409, "before_photos_incomplete", "Finish the before capture first.")
                if phase == "after" and {photo.side for photo in available_photo_records(dossier) if photo.phase == "before"} != set(SIDES):
                    raise DossierError(409, "before_photos_incomplete", "Complete all four before sides first.")
                if dossier.metadata.evidence_finalized_at or (phase == "before" and dossier.metadata.before_finalized_at):
                    raise DossierError(409, "evidence_finalized", "This capture phase is immutable.")
                capture = prepare_capture_attempt(self, dossier, metadata, previous, phase, zoom_channels)
                metadata["captures"][key] = capture
                self.workflows.save_metadata(dossier, metadata)
            self.executor.submit(self.deliver_photos, dossier_id, key, capture, capture_lock, tunnel_lock)
            capture_lock = tunnel_lock = None
        except Timeout as error:
            raise DossierError(409, "tunnel_busy", "The local tunnel is busy.") from error
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "Capture storage is unavailable.") from error
        except RuntimeError as error:
            self.update_capture(dossier_id, key, status="failed", error_code="capture_interrupted")
            raise DossierError(503, "tunnel_unavailable", "Capture worker is stopping.") from error
        finally:
            if tunnel_lock is not None:
                tunnel_lock.release()
            if capture_lock is not None:
                capture_lock.release()

    def validate_phase(self, dossier, phase):
        if (dossier.metadata.type == "verification") != (phase is not None):
            raise DossierError(422, "invalid_photo_phase", "Verification requires a phase; expertise and quick estimation have none.")

    def validate_expertise_capture(self, dossier, capture):
        if dossier.metadata.type != "forfait_expertise":
            return
        capture_id = capture.get("capture_id") if capture else None

        # Existing evidence belongs to its originating inspection and must never be mixed with a new capture.

        if any(not capture_id or photo.capture_id != capture_id
               for photo in available_photo_records(dossier)):
            raise DossierError(409, "capture_not_allowed", "This expertise dossier already has a photo set.")

    def archive_replaced_photos(self, metadata, capture_id, phase=None):
        photos = [photo for photo in metadata.get("photos", []) if photo.get('phase') == phase]
        if photos:

            # Keep superseded evidence on disk, but exclude it from the new scan's active manifest.

            replaced_at = timestamp()
            metadata.setdefault("photo_history", []).extend({
                **photo, "replaced_at": replaced_at, "replaced_by": capture_id,
                "reason": "capture_restarted",
            } for photo in photos)
        metadata["photos"] = [photo for photo in metadata.get('photos', []) if photo not in photos]

    def reset_failed_analysis(self, dossier):
        # A new photo attempt must not publish the previous run's failure while AI admission is pending.

        _, state = self.workflows.repository.read_analysis_state(dossier)
        if state == 'failed':
            publish_json(dossier.directory / 'analysis/status.json', {'status': 'not_analyzed'})

    # ============================================================
    # REAL BYTE PUBLICATION AND RESTART-SAFE PHOTO IDENTITIES
    # ============================================================

    def deliver_photos(self, dossier_id, key, capture, capture_lock, tunnel_lock):
        completed = False
        code = None
        try:
            if self.lifecycle is not None:
                self.lifecycle.begin_capture(dossier_id, capture, tunnel_lock)
            elif self.lighting is not None:
                self.lighting.begin_capture(capture["capture_id"])
            self.internal_lighting.activate_and_verify()
            self.update_capture(dossier_id, key, stage="lighting_ready")
            if self.internal_lighting.settings.enabled and self.stopping.wait(
                    self.internal_lighting.settings.stabilization_ms / 1000):
                raise DossierError(503, "capture_interrupted", "Capture interrupted.")
            self.update_capture(dossier_id, key, stage="capturing")
            baselines = self.cameras.baseline(capture)
            for sample in capture["samples"]:
                if self.stopping.is_set():
                    raise DossierError(503, "capture_interrupted", "Capture interrupted.")
                self.save_sample(dossier_id, capture, sample, baselines)
                if self.stopping.wait(self.settings.mock_photo_delay_seconds):
                    raise DossierError(503, "capture_interrupted", "Capture interrupted.")
            dossier = self.workflows.repository.read_dossier(dossier_id)
            self.validate_saved_capture(dossier, capture)
            self.update_capture(dossier_id, key, status="completed", error_code=None, stage="completed")
            completed = True
        except Exception as error:
            logger.exception("Capture %s failed", capture["capture_id"])
            code = error.code if isinstance(error, DossierError) else "capture_failed"
            self.update_capture(dossier_id, key, status="failed", error_code=code, stage="failed",
                                error_message=error.message if isinstance(error, DossierError) else 'Capture interrompue.')
        finally:
            # Cleanup completes under tunnel ownership before any newer capture can begin.

            successful = completed and not self.stopping.is_set()
            expects_analysis = completed and dossier.metadata.type != "verification"
            if self.lifecycle is not None:
                self.lifecycle.finish_capture(capture["capture_id"], successful, expects_analysis)
            else:
                self.internal_lighting.cleanup()
                if self.lighting is not None:
                    self.lighting.finish_capture(capture["capture_id"], successful)

            if not completed and code in MESSAGES and self.lighting is not None:
                try:
                    self.lighting.set_effect('warning', repeat=True, brightness=100)
                except Exception:
                    logger.warning("Exterior lighting warning failed", exc_info=True)

            # Hold stable admission across releasing camera ownership and queuing AI.

            try:
                with dossier_admission(self.workflows.repository, dossier_id):
                    capture_lock.release()
                    if completed and not self.stopping.is_set():
                        self.start_analysis(dossier_id, key, retry_failed=True)
            except Exception:
                if self.lifecycle is not None:
                    self.lifecycle.reject_analysis(dossier_id)
                logger.exception("Analysis submission failed", extra={"dossier_id": dossier_id})
            finally:
                capture_lock.release()
                if self.lifecycle is None or not self.lifecycle.holds(capture["capture_id"]):
                    tunnel_lock.release()

    def save_sample(self, dossier_id, capture, sample, baselines=None):
        identity = sample.get('camera_channel', sample['source'])
        photo_id = hashlib.sha256(f"{capture['capture_id']}:{identity}".encode()).hexdigest()[:32]
        frame = None
        if capture.get('capture_source'):
            frame = self.cameras.read(capture, sample, baselines or {}, self.stopping)
        with self.workflows.editable_dossier(dossier_id, capture_owned=True) as dossier:
            metadata = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
            self.validate_expertise_capture(dossier, capture)
            photos = metadata.setdefault("photos", [])
            previous = next((photo for photo in photos if Path(photo["path"]).stem == photo_id), None)
            if previous:
                validate_image(confined_path(dossier.directory, previous["path"]))
                return
            content, extension = ((frame['content'], frame['extension']) if frame
                                  else self.provider.read_photo(sample["source"]))
            phase = capture["phase"]
            relative = f"photos/{phase + '/' if phase else ''}{photo_id}{extension}"
            target = confined_path(dossier.directory, relative)
            record = {"path": relative, "photo_id": photo_id, "phase": phase, "side": sample["side"],
                      "capture_id": capture["capture_id"], "original_filename": Path(sample["source"]).name,
                      "source": sample["source"], "uploaded_at": timestamp()}
            if frame:
                record.update({field: sample[field] for field in ('camera_channel', 'camera_name', 'camera_role')})
                record.update(capture_source=capture['capture_source'], frame_id=frame['frame_id'])
            publish_bytes(target, content)
            photos.append(record)
            try:
                self.workflows.save_metadata(dossier, metadata)
            except Exception:
                target.unlink(missing_ok=True)
                raise

    def validate_saved_capture(self, dossier, capture):
        photos = [photo for photo in dossier.metadata.photos if photo.capture_id == capture["capture_id"]]
        if len(photos) != len(capture["samples"]):
            raise DossierError(422, "capture_incomplete", "Capture has missing photographs.")
        for photo in photos:
            validate_image(confined_path(dossier.directory, photo.path))
        if dossier.metadata.type == "verification" and {photo.side for photo in photos} != set(SIDES):
            raise DossierError(422, "capture_sides_incomplete", "Verification requires all four camera sides.")

    def update_capture(self, dossier_id, key, **changes):
        dossier = self.workflows.repository.read_dossier(dossier_id)
        with FileLock(str(dossier.directory / ".metadata.lock"), timeout=5):
            dossier = self.workflows.repository.read_dossier(dossier_id)
            metadata = read_json_object(dossier.directory / "metadata.json", "invalid_metadata")
            metadata["captures"][key].update(changes, updated_at=timestamp())
            self.workflows.save_metadata(dossier, metadata)

    @serialize_dossier
    def start_analysis(self, dossier_id, key, *, retry_failed=False):
        dossier = self.workflows.repository.read_dossier(dossier_id)
        if dossier.metadata.type == "verification":
            return
        _, state = self.workflows.repository.read_analysis_state(dossier)
        if state != "not_analyzed" and not (
            retry_failed and dossier.metadata.type == "forfait_expertise" and state == "failed"
        ):
            return
        # Expertise fingerprints the complete metadata file; finish bookkeeping before freezing its inputs.

        self.update_capture(dossier_id, key, analysis_error=None)
        try:
            self.workflows.manager.start_scan(dossier_id)
        except DossierError as error:
            if error.code not in ("scan_in_progress", "already_analyzed"):
                self.update_capture(dossier_id, key, analysis_error=error.code)
                if self.lifecycle is not None:
                    self.lifecycle.reject_analysis(dossier_id)

    # ============================================================
    # PUBLIC COUNTS FROM SAVED MANIFEST ENTRIES
    # ============================================================

    def read_status(self, dossier_id, phase, root_path="") -> CaptureStatus:
        dossier = self.workflows.repository.read_dossier(dossier_id)
        self.validate_phase(dossier, phase)
        capture = dossier.metadata.captures.get(phase or "single", {})
        photos = []
        for photo in dossier.metadata.photos:
            if not capture or photo.capture_id != capture.get("capture_id"):
                continue
            try:
                validate_image(confined_path(dossier.directory, photo.path))
            except DossierError:
                continue
            base = f"{root_path.rstrip('/')}/api/dossiers/{dossier_id}"
            path = quote(photo.path, safe="/")
            photos.append(CapturedPhoto(photo_id=Path(photo.path).stem, side=photo.side, phase=photo.phase,
                                       **{field: getattr(photo, field) for field in
                                          ('camera_channel', 'camera_name', 'camera_role', 'capture_source', 'frame_id')},
                                       original_filename=photo.original_filename, uploaded_at=photo.uploaded_at,
                                       original_url=f"{base}/media/{path}", preview_url=f"{base}/preview/{path}.webp"))
        return CaptureStatus(dossier_id=dossier_id, phase=phase, capture_id=capture.get("capture_id"),
                             slow_mode=bool(capture.get("slow_mode", False)),
                             capture_source=capture.get('capture_source'), zoom_channels=capture.get('zoom_channels', []),
                             status=capture.get("status", "not_started"), expected_count=len(capture.get("samples", [])),
                             saved_count=len(photos), photos=photos, error_code=capture.get("error_code"),
                             analysis_error=capture.get("analysis_error"), stage=capture.get("stage"),
                             error_message=capture.get('error_message') or MESSAGES.get(capture.get("error_code")))
