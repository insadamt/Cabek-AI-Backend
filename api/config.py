"""Load local API, worker, and mock tunnel settings from the environment.

Depends on dotenv and pathlib; camera delivery delay never controls AI execution.
"""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# API CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class ApiSettings:
    dossiers_dir: Path
    cors_origins: tuple[str, ...] = ()
    models_dir: Path = Path(__file__).resolve().parents[1] / "models"
    scan_timeout_seconds: float = 1800
    scan_max_pending: int = 8
    tunnel_mode: str = "disabled"
    mock_photos_dir: Path | None = None
    mock_photo_delay_seconds: float = 0

    @classmethod
    def from_environment(cls) -> "ApiSettings":

        # Load local secrets before workers inherit the environment; deployment values take precedence.

        load_dotenv(PROJECT_ROOT / ".env", override=False)

        # Anchor the default to the repo so starting Uvicorn elsewhere still finds the dossiers.

        default_root = Path(__file__).resolve().parents[1] / "data" / "dossiers"
        return cls(
            dossiers_dir=Path(os.environ.get("CABEK_DOSSIERS_DIR", default_root)),
            models_dir=Path(os.environ.get("CABEK_MODELS_DIR", default_root.parents[1] / "models")),
            scan_timeout_seconds=float(os.environ.get("CABEK_SCAN_TIMEOUT_SECONDS", "1800")),
            scan_max_pending=int(os.environ.get("CABEK_SCAN_MAX_PENDING", "8")),
            tunnel_mode=os.environ.get("CABEK_TUNNEL_MODE", "disabled"),
            mock_photos_dir=Path(os.environ.get("CABEK_MOCK_PHOTOS_DIR") or PROJECT_ROOT / ".local" / "tunnel-photos"),
            mock_photo_delay_seconds=float(os.environ.get("CABEK_MOCK_PHOTO_DELAY_SECONDS", "0")),
            cors_origins=tuple(
                origin.strip()
                for origin in os.environ.get("CABEK_CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
        )
