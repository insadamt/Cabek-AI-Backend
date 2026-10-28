"""Dossier workflow contracts for local creation, edits, and photo comparisons.

Depends on Pydantic and the existing vehicle IDs; no upstream calls occur here.
"""

from datetime import date
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.vehicle_catalog import VehicleId


# ============================================================
# CATEGORY, PHASE, AND REQUEST CONTRACTS
# ============================================================

DossierType = Literal["forfait_expertise", "chiffrage_rapide", "verification"]
PhotoSide = Literal["front", "rear", "left", "right"]
PhotoPhase = Literal["before", "after"]
WorkflowStatus = Literal[
    "not_analyzed", "queued", "processing", "completed", "failed",
    "draft", "awaiting_photos", "ready_to_scan", "analyzing", "estimated",
    "before_incomplete", "awaiting_after", "after_incomplete", "comparison_ready",
]
DossierSort = Literal["id_desc", "id_asc", "created_at_desc", "created_at_asc", "updated_at_desc", "updated_at_asc"]
RegistrationDate = Annotated[str, Field(description="Complete ISO first-registration date: YYYY-MM-DD.")]


def validate_registration_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("A complete YYYY-MM-DD registration date is required.")
    date.fromisoformat(value)
    return value


class CreateDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["chiffrage_rapide", "verification"]
    marque_id: VehicleId
    model_id: VehicleId
    matricule: str = Field(min_length=1, max_length=100)
    date_mise_en_circulation: RegistrationDate

    _registration_date = field_validator("date_mise_en_circulation")(validate_registration_date)


class PatchDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    marque_id: VehicleId | None = None
    model_id: VehicleId | None = None
    matricule: str | None = Field(default=None, min_length=1, max_length=100)
    date_mise_en_circulation: RegistrationDate | None = None

    @field_validator("date_mise_en_circulation")
    @classmethod
    def registration_date(cls, value):
        return validate_registration_date(value) if value is not None else value
