from pathlib import Path

from pydantic import BaseModel

from api.schemas import StoredPhoto


# ============================================================
# INTERNAL WORKER INPUT
# ============================================================

class ScanRequest(BaseModel):
    run_id: str
    dossier_id: str
    dossier_dir: Path
    output_dir: Path
    models_dir: Path
    metadata: dict
    photos: list[StoredPhoto]
    feedback: list[dict]
    input_signatures: dict[str, str]


class ScanSummary(BaseModel):
    dossier_id: str
    output_dir: Path
    photo_count: int
    financial_status: str
