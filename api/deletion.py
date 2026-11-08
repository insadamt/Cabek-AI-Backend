"""Permanently remove idle, locally owned dossiers under their stable admission lock.

Depends on capture/scan/metadata locks and confined repository paths. A hidden
rename withdraws the folder before removal; failed cleanup never reports success.
"""

from contextlib import ExitStack
import shutil
from uuid import uuid4

from filelock import FileLock, Timeout

from api.dossier_lifecycle import dossier_admission
from api.errors import DossierError
from api.files import read_json_object


def delete_dossier(manager, dossier_id):
    repository = manager.repository
    with dossier_admission(repository, dossier_id, timeout=0):
        dossier = repository.read_dossier(dossier_id)
        if dossier.metadata.source != "cabek_ai" or dossier.metadata.type not in ("chiffrage_rapide", "verification"):
            raise DossierError(409, "externally_owned_dossier", "Only locally owned dossiers can be deleted.")
        if (repository.dossiers_dir / dossier_id).is_symlink():
            raise DossierError(422, "invalid_metadata", "Dossier deletion cannot follow a symlink.")
        for name in (".capture.lock", ".metadata.lock"):
            if (dossier.directory / name).is_symlink():
                raise DossierError(422, "invalid_metadata", "Dossier locks cannot use symlinks.")
        analysis = manager.prepare_analysis_directory(dossier.directory)
        conflict = "capture_in_progress"
        try:
            with ExitStack() as locks:
                locks.enter_context(FileLock(str(dossier.directory / ".capture.lock"), timeout=0))
                conflict = "scan_in_progress"
                locks.enter_context(FileLock(str(analysis / ".scan.lock"), timeout=0))
                locks.enter_context(FileLock(str(analysis / ".worker.lock"), timeout=0))
                locks.enter_context(FileLock(str(dossier.directory / ".metadata.lock"), timeout=0))
                dossier = repository.read_dossier(dossier_id)
                if any(capture.get("status") == "capturing" for capture in dossier.metadata.captures.values()):
                    raise DossierError(409, "capture_in_progress", "Wait for photograph capture to finish.")
                status_path = analysis / "status.json"
                record = read_json_object(status_path, "invalid_status") if status_path.exists() else {}
                state = record.get("status", "not_analyzed")
                if state not in ("not_analyzed", "queued", "processing", "completed", "failed"):
                    raise DossierError(422, "invalid_status", "Saved scan state is invalid.")
                if state in ("queued", "processing"):
                    raise DossierError(409, "scan_in_progress", "Wait for analysis to finish.")
                hidden = repository.dossiers_dir / f".delete-{uuid4().hex}"
                dossier.directory.rename(hidden)
                try:
                    shutil.rmtree(hidden)
                except OSError:

                    # Keep remaining files reachable for inspection/retry if removal failed.

                    hidden.rename(dossier.directory)
                    raise
        except Timeout as error:
            raise DossierError(409, conflict, "Wait for active dossier work to finish.") from error
        except OSError as error:
            raise DossierError(503, "storage_unavailable", "The dossier could not be completely removed.") from error
