from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image

from api.errors import DossierError


# ============================================================
# BOUNDED WEBP PREVIEW CACHE
# ============================================================

PREVIEW_MAX_DIMENSION = 1600
PREVIEW_QUALITY = 82


def generate_webp_preview(source: Path) -> tuple[bytes, str]:
    try:
        stat = source.stat()

        # Include file identity and change times so replaced scan outputs cannot reuse stale previews.

        revision = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return encode_cached_preview(source, revision)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise DossierError(422, "invalid_image", "Saved image cannot be previewed.") from error


@lru_cache(maxsize=16)
def encode_cached_preview(source: Path, revision: tuple[int, ...]) -> tuple[bytes, str]:
    with Image.open(source) as image:

        # Preserve the stored pixel orientation so originals align with model annotations.

        preview = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        preview.thumbnail((PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION), Image.Resampling.LANCZOS)
        with BytesIO() as buffer:
            preview.save(buffer, format="WEBP", quality=PREVIEW_QUALITY, method=4)
            content = buffer.getvalue()
    return content, f'"{sha256(content).hexdigest()}"'
