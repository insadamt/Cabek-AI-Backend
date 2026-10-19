from pathlib import Path

import numpy as np
from PIL import Image

from core.expertise_engine import (
    analyser, appliquer_feedback_aux_instances, appliquer_priorite_remplacement,
    calculer_agregats, dedupliquer_instances,
)
from scans.artifacts import annotation_paths, publish_annotation, publish_bytes, publish_json
from scans.backend import MODEL_FILENAMES
from scans.contracts import ScanRequest, ScanSummary
from scans.pricing import enrichir_prix_pieces
from scans.report import generate_report
from scans.results import build_financial, build_result, prepare_invoice_instances


# ============================================================
# REAL DOSSIER ANALYSIS COPIED FROM THE STREAMLIT FLOW
# ============================================================

def load_models(models_dir: Path):
    from ultralytics import YOLO

    return tuple(YOLO(str(models_dir / name)) for name in MODEL_FILENAMES)


def analyze_dossier(request: ScanRequest) -> ScanSummary:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    if (request.output_dir / "result.json").exists():
        raise FileExistsError("The output directory already has a final result.")
    models = load_models(request.models_dir)
    instances = []
    for photo in request.photos:
        with Image.open(request.dossier_dir / photo.original_path) as image:
            pixels = np.array(image.convert("RGB"))
        result = analyser(
            models[0], models[1], pixels,
            conf_pieces=0.50, conf_dommages=0.50, iou_min=0.05,
            marque=request.metadata.get("marque", ""),
            nom_image=Path(photo.original_path).name,
            modele_zone_critique=models[2], conf_zone_critique=0.25,
            seuil_zone_critique=0.05,
        )
        for path, key in zip(annotation_paths(request.output_dir, photo),
                             ("img_pieces", "img_dommages", "img_zones_critiques")):
            if result[key] is None:
                raise ValueError("A required annotation was not generated.")
            publish_annotation(path, result[key])
        instances.extend(result["instances"])

    # Keep the existing order so saved feedback still matches deterministic detection IDs.

    instances = dedupliquer_instances(instances)
    if request.feedback:
        instances = appliquer_feedback_aux_instances(instances, request.feedback)
    instances = appliquer_priorite_remplacement(instances)
    instances = prepare_invoice_instances(instances)
    year = str(request.metadata.get("annee") or "")
    instances = enrichir_prix_pieces(
        instances, request.metadata.get("marque_id"), request.metadata.get("model_id"),
        int(year) if year.isdigit() else None, {},
    )
    aggregates = calculer_agregats(instances)
    financial = build_financial(instances)
    result = build_result(request, instances, financial)
    report = generate_report(request.metadata, instances, aggregates, financial.model_dump())
    publish_bytes(request.output_dir / "report.pdf", report)
    publish_json(request.output_dir / "result.json", result.model_dump(mode="json"))
    return ScanSummary(
        dossier_id=request.dossier_id, output_dir=request.output_dir,
        photo_count=len(request.photos), financial_status=financial.status,
    )
