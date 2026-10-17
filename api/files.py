import json
from math import isfinite
from pathlib import Path, PurePosixPath
import re
import warnings

from PIL import Image
from pypdf import PdfReader

from api.errors import DossierError


# ============================================================
# ALLOWED IDENTIFIERS AND IMAGE FORMATS
# ============================================================

DOSSIER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
IMAGE_TYPES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


# ============================================================
# PATH VALIDATION
# ============================================================

def normalize_relative_path(value: str, *, legacy: bool = False) -> str:
    if legacy:

        # Existing metadata was written on Windows; HTTP paths still require forward slashes.

        value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise DossierError(404, "artifact_not_found", "Artifact not found.")
    return path.as_posix()


def confined_path(directory: Path, relative_path: str) -> Path:
    try:
        target = (directory / relative_path).resolve()

        # Check after resolving symlinks so an allowed filename cannot escape its dossier.

        if not target.is_relative_to(directory.resolve()):
            raise DossierError(404, "artifact_not_found", "Artifact not found.")
        return target
    except (OSError, RuntimeError, ValueError) as error:
        raise DossierError(404, "artifact_not_found", "Artifact not found.") from error


# ============================================================
# JSON VALIDATION
# ============================================================

def read_json_object(path: Path, code: str) -> dict:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError("Expected an object.")
        return value
    except (OSError, ValueError, RecursionError) as error:
        raise DossierError(422, code, "Saved dossier data is invalid or unreadable.") from error


def reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON numbers are unsupported.")


def parse_finite_float(value: str) -> float:
    number = float(value)

    # JSON exponents such as 1e999 can overflow even without an explicit Infinity token.

    if not isfinite(number):
        raise ValueError("Nonfinite JSON numbers are unsupported.")
    return number


# ============================================================
# IMAGE AND REPORT VALIDATION
# ============================================================

def validate_image(path: Path) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:

                # A trusted extension alone does not establish that the saved file is an image.

                if image.format != IMAGE_TYPES.get(path.suffix.lower()):
                    raise ValueError("Unexpected image format.")
                image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise DossierError(422, "invalid_image", "Saved image is invalid or unreadable.") from error


def validate_pdf(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted or not reader.pages:
                raise ValueError("Expected a readable report.")
    except Exception as error:
        raise DossierError(422, "invalid_report", "Saved report is invalid or unreadable.") from error
