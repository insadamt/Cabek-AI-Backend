"""Publish complete artifacts and fingerprint immutable scan inputs.

Depends on confined dossier storage and image/PDF validators. Quick jobs exclude
editable vehicle fields; the pricing gate validates their separate frozen snapshot.
"""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from PIL import Image

from api.files import confined_path, validate_image, validate_pdf
from api.repository import DossierRecord
from api.schemas import StoredAnalysis, StoredPhoto


# ============================================================
# ATOMIC ARTIFACT PUBLICATION
# ============================================================

def publish_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".scan-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        # Readers see the final filename only after the entire file has been closed.

        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_json(path: Path, content: dict) -> None:
    publish_bytes(path, json.dumps(content, ensure_ascii=False, allow_nan=False, indent=2).encode())


def publish_annotation(path: Path, pixels) -> None:
    import io

    with io.BytesIO() as buffer:
        Image.fromarray(pixels).save(buffer, format="PNG")
        publish_bytes(path, buffer.getvalue())


# ============================================================
# INPUT SNAPSHOTS AND EXPECTED OUTPUTS
# ============================================================

def input_signatures(dossier: DossierRecord) -> dict[str, str]:
    paths = ["metadata.json", *(photo.original_path for photo in dossier.photos.values())]
    if confined_path(dossier.directory, "feedback.json").is_file():
        paths.append("feedback.json")
    if dossier.metadata.mock_engine:
        evidence = confined_path(dossier.directory, '.mock-evidence')
        paths.extend(p.relative_to(dossier.directory).as_posix() for p in sorted(evidence.rglob('*')) if p.is_file())
    signatures = {}
    for relative_path in paths:
        path = confined_path(dossier.directory, relative_path)
        if relative_path == "metadata.json" and dossier.metadata.type == "chiffrage_rapide":
            from api.files import read_json_object
            from scans.metadata_gate import VEHICLE_FIELDS, MUTABLE_WORKFLOW_FIELDS

            metadata = read_json_object(path, "invalid_metadata")
            frozen = {key: value for key, value in metadata.items()
                      if key not in VEHICLE_FIELDS | MUTABLE_WORKFLOW_FIELDS}
            signatures[relative_path] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
            continue
        with path.open("rb") as stream:
            signatures[relative_path] = hashlib.file_digest(stream, "sha256").hexdigest()
    return signatures


def annotation_paths(output_dir: Path, photo: StoredPhoto) -> list[Path]:
    return [output_dir / "annotated" / Path(getattr(photo, field)).name
            for field in ("parts_path", "damages_path", "critical_path")]


def validate_outputs(output_dir: Path, dossier: DossierRecord) -> StoredAnalysis:
    result = StoredAnalysis.model_validate_json((output_dir / "result.json").read_bytes())
    actual = {photo.photo_id: photo for photo in result.photos}
    if result.dossier_id != dossier.metadata.dossier_id or actual != dossier.photos:
        raise ValueError("Worker result does not match the accepted dossier.")
    if len(actual) != len(result.photos):
        raise ValueError("Duplicate output photos.")
    for photo in result.photos:
        for path in annotation_paths(output_dir, photo):
            validate_image(path)
    validate_pdf(output_dir / "report.pdf")
    return result


