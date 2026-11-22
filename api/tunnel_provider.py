"""Read real sample photographs without modifying their source files.

Depends on configured local storage and the upload decoder. Side directories or
filename prefixes describe camera position; optional phase directories describe time.
"""

from pathlib import Path
import re

from fastapi import UploadFile
from starlette.datastructures import Headers

from api.errors import DossierError
from api.files import IMAGE_MEDIA_TYPES, IMAGE_TYPES, normalize_relative_path
from api.photo_service import read_validated_upload


SIDE_ALIASES = {"front": "front", "avant": "front", "rear": "rear", "arriere": "rear",
                "left": "left", "gauche": "left", "right": "right", "droite": "right"}


class MockTunnelProvider:
    def __init__(self, root: Path | None):
        self.root = root.resolve() if root else None

    def plan_capture(self, phase: str | None) -> list[dict]:
        if self.root is None or not self.root.is_dir():
            raise DossierError(503, "mock_photos_unavailable", "Configure CABEK_MOCK_PHOTOS_DIR et son volume persistant avec des photos front, rear, left et right.")
        source = self.root / phase if phase and (self.root / phase).is_dir() else self.root
        if phase is None and (self.root / 'before').is_dir():
            source = self.root / 'before'
        if phase and not (self.root / phase).is_dir() and any((self.root / name).is_dir() for name in ('before', 'after')):
            raise DossierError(422, 'mock_phase_missing', 'Photographies manquantes pour la phase ' + phase + '.')
        samples = []
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_symlink():
                raise DossierError(422, "unsafe_sample_path", "Sample storage must not contain symlinks.")
            if not path.is_file() or path.suffix.lower() not in IMAGE_TYPES:
                continue
            if len(relative.parts) > 2:
                raise DossierError(422, "invalid_sample_layout", "Use side folders or side-prefixed filenames.")
            token = relative.parts[0].lower() if len(relative.parts) == 2 else re.split(r"[_\- .]", path.stem.lower())[0]
            side = SIDE_ALIASES.get(token)
            if side is None:
                raise DossierError(422, "invalid_sample_side", "Every sample must identify front, rear, left, or right.")
            name = path.relative_to(self.root).as_posix()
            self.validate_source_path(name)
            samples.append({"source": name, "side": side})
        if not samples:
            raise DossierError(422, "no_sample_photos", "No JPEG/PNG sample photographs were found.")
        return samples

    def read_photo(self, source: str) -> tuple[bytes, str]:
        self.validate_source_path(source)
        if self.root is None:
            raise DossierError(503, "mock_photos_unavailable", "Configure CABEK_MOCK_PHOTOS_DIR et son volume persistant avec des photos front, rear, left et right.")
        path = self.root / source
        if not path.resolve().is_relative_to(self.root) or any(parent.is_symlink() for parent in (path, *path.parents) if parent != self.root.parent):
            raise DossierError(422, "unsafe_sample_path", "Sample paths must remain inside their configured directory.")
        with path.open("rb") as stream:
            return read_validated_upload(UploadFile(
                file=stream, filename=path.name,
                headers=Headers({"content-type": IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "")}),
            ))

    def validate_source_path(self, source: str) -> None:
        try:
            normalize_relative_path(source)
        except DossierError as error:
            raise DossierError(422, "unsafe_sample_path", "Use plain confined sample photograph paths.") from error
