"""Exercise process supervision and metadata gating with deterministic test outputs.

Depends on real locks, image files, PDF files, and the production metadata gate;
only detections and the upstream catalog are replaced within this test worker.
"""

import json
from pathlib import Path
import sys
import time

from PIL import Image
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scans.worker import run_accepted_scan
from scans.metadata_gate import await_pricing_metadata


def create_outputs(scan_request):
    request = scan_request.model_dump(mode="json")
    mode = sys.argv[2]
    output = Path(request["output_dir"])
    if mode == "hang":
        time.sleep(30)
        return
    if mode == "fail":
        raise RuntimeError("private /path/token must never reach the API")
    annotated = output / "annotated"
    annotated.mkdir(exist_ok=True)
    for photo in request["photos"]:
        for field in ("parts_path", "damages_path", "critical_path"):
            path = annotated / Path(photo[field]).name
            temporary = path.with_suffix(".tmp")
            Image.new("RGB", (16, 12), "blue").save(temporary, format="PNG")
            temporary.replace(path)
            time.sleep(0.2)
    from api.vehicle_catalog import VehicleCatalog, ResolvedVehicle
    VehicleCatalog.resolve = lambda self, selection: ResolvedVehicle(
        marque_id=selection.marque_id, model_id=selection.model_id, marque="Dacia", modele="Sandero",
    )
    await_pricing_metadata(scan_request)
    request = scan_request.model_dump(mode="json")
    report = PdfWriter()
    report.add_blank_page(width=100, height=100)
    report.write(output / "report.pdf")
    result = {
        "schema_version": 1, "dossier_id": request["dossier_id"],
        "vehicle": {key: request["metadata"].get(key, "") for key in ("marque", "modele", "matricule")},
        "financial": {
            "status": "complete", "currency": "MAD", "estimated_total": 0,
            "known_subtotal": 0, "warnings": [],
            "breakdown": {key: 0 for key in (
                "repair_labor", "paint_labor", "paint_materials", "replacement_labor", "parts_supply",
            )},
        },
        "parts": [], "detections": [], "photos": request["photos"],
    }
    (output / "result.json").write_text(json.dumps(result))
    if mode == "bad_output":
        (output / "report.pdf").write_bytes(b"broken")
    if mode == "late_failure":
        raise RuntimeError("Failed after writing final output")


if __name__ == "__main__":
    run_accepted_scan(Path(sys.argv[1]), sys.argv[3], create_outputs)
