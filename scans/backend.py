"""Launch supervised inference or annotation-pricing workers with scoped dependencies.

Depends on the existing worker process contract; prepared annotations require the
pricing engine libraries but do not require YOLO models or Torch.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol, TextIO

from api.errors import DossierError
from scans.contracts import ScanRequest


MODEL_FILENAMES = ("best_pieces.pt", "best.pt", "best_zone_critique.pt")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# WORKER PROCESS BOUNDARY
# ============================================================

class ScanBackend(Protocol):
    def ensure_available(self, models_dir: Path) -> None: ...

    def launch(self, request_path: Path, log: TextIO) -> subprocess.Popen: ...


class ModelProcessBackend:
    def ensure_available(self, models_dir: Path) -> None:
        dependencies = ("ultralytics", "torch", "cv2", "numpy", "reportlab", "requests")
        if any(importlib.util.find_spec(name) is None for name in dependencies):
            raise DossierError(503, "worker_unavailable", "Install requirements-scan.txt before scanning.")
        if any(not (models_dir / name).is_file() for name in MODEL_FILENAMES):
            raise DossierError(503, "worker_unavailable", "Required model files are unavailable.")

    def ensure_pricing_available(self, models_dir: Path) -> None:
        dependencies = ('cv2', 'numpy', 'reportlab', 'requests')
        if any(importlib.util.find_spec(name) is None for name in dependencies):
            raise DossierError(503, 'worker_unavailable', 'Install the engine pricing dependencies.')

    def launch(self, request_path: Path, log: TextIO) -> subprocess.Popen:

        # A child process keeps inference off HTTP threads and can be stopped on timeout.

        request = ScanRequest.model_validate_json(request_path.read_bytes())
        return subprocess.Popen(
            [sys.executable, "-m", "scans.worker", str(request_path), request.run_id],
            cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ,
                 "YOLO_CONFIG_DIR": str(request_path.parent / ".runtime"),
                 "MPLCONFIGDIR": str(request_path.parent / ".runtime" / "matplotlib")},
        )
