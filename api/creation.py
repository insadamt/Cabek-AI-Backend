"""Deduplicate local draft creation across retries and API restarts.

Depends on a root file lock and an idempotency digest stored with the dossier;
the dossier itself is the durable index, avoiding a second commit boundary.
"""

import hashlib

from filelock import FileLock, Timeout

from api.errors import DossierError
from api.files import read_json_object


def create_once(workflows, request, key: str | None) -> str:
    if key is None:
        return workflows.publish_dossier(request)
    if not key.strip() or len(key) > 200:
        raise DossierError(422, "invalid_idempotency_key", "Use an idempotency key of 1–200 characters.")
    digest = hashlib.sha256(key.encode()).hexdigest()
    root = workflows.repository.dossiers_dir
    workflows.repository.validate_root()
    try:
        with FileLock(str(root / ".creation.lock"), timeout=5):
            for dossier_id in workflows.repository.dossier_ids():
                try:
                    metadata = read_json_object(root / dossier_id / "metadata.json", "invalid_metadata")
                except DossierError:
                    continue
                if metadata.get("creation_key") == digest:
                    if metadata.get("creation_request") != request.model_dump():
                        raise DossierError(409, "idempotency_conflict", "This key belongs to a different creation request.")
                    return dossier_id
            return workflows.publish_dossier(request, digest)
    except Timeout as error:
        raise DossierError(409, "creation_in_progress", "Another dossier is being created.") from error
