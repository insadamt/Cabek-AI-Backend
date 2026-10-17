from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class ApiSettings:
    dossiers_dir: Path
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "ApiSettings":
        default_root = Path(__file__).resolve().parents[1] / "data" / "dossiers"
        return cls(
            dossiers_dir=Path(os.environ.get("CABEK_DOSSIERS_DIR", default_root)),
            cors_origins=tuple(
                origin.strip()
                for origin in os.environ.get("CABEK_CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
        )
