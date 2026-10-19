import argparse
from pathlib import Path
from typing import Callable

from filelock import FileLock

from api.files import read_json_object
from scans.contracts import ScanRequest


# ============================================================
# CHILD PROCESS ENTRY POINT
# ============================================================

def run_accepted_scan(request_path: Path, run_id: str, analyze: Callable) -> None:

    # This lock survives an API crash while its child is still writing the shared folder.

    with FileLock(str(request_path.parent / ".worker.lock"), timeout=30):
        request = ScanRequest.model_validate_json(request_path.read_bytes())
        status = read_json_object(request_path.parent / "status.json", "invalid_status")
        if request.run_id != run_id or status.get("run_id") != run_id or status.get("status") != "processing":
            raise RuntimeError("This worker no longer owns the accepted scan.")
        analyze(request)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an accepted dossier scan.")
    parser.add_argument("request_path")
    parser.add_argument("run_id")
    arguments = parser.parse_args()

    # Import model dependencies only inside workers; read-only API startup stays lightweight.

    from scans.runner import analyze_dossier

    run_accepted_scan(Path(arguments.request_path), arguments.run_id, analyze_dossier)


if __name__ == "__main__":
    main()
