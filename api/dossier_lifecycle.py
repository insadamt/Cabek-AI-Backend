"""Serialize local dossier mutations against permanent deletion.

Depends on filelock and the repository confinement checks. Locks live outside
folders so renaming/deleting a dossier cannot replace its admission lock inode.
"""

from contextlib import contextmanager
from functools import wraps

from filelock import FileLock, Timeout

from api.errors import DossierError


@contextmanager
def dossier_admission(repository, dossier_id, *, timeout=5):
    repository.read_dossier(dossier_id)
    directory = repository.dossiers_dir / ".lifecycle-locks"
    try:
        if directory.is_symlink():
            raise DossierError(503, "storage_unavailable", "Dossier locks are unavailable.")
        directory.mkdir(exist_ok=True)
        path = directory / f"{dossier_id}.lock"
        if path.is_symlink():
            raise DossierError(503, "storage_unavailable", "Dossier locks are unavailable.")

        # Reuse the same thread-local lock for nested capture, workflow and scan calls.

        with FileLock(str(path), is_singleton=True).acquire(timeout=timeout):
            repository.read_dossier(dossier_id)
            yield
    except Timeout as error:
        raise DossierError(409, "scan_in_progress", "Another dossier operation is in progress.") from error
    except OSError as error:
        raise DossierError(503, "storage_unavailable", "Dossier storage is unavailable.") from error


def serialize_dossier(method=None, *, timeout=5):
    if method is None:
        return lambda function: serialize_dossier(function, timeout=timeout)

    @wraps(method)
    def serialized(self, dossier_id, *args, **kwargs):
        repository = self.repository if hasattr(self, "repository") else self.workflows.repository
        with dossier_admission(repository, dossier_id, timeout=timeout):
            return method(self, dossier_id, *args, **kwargs)
    return serialized
