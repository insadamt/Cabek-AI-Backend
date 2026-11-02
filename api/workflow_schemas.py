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
    "capturing", "capture_failed", "awaiting_metadata",
]
DossierSort = Literal["id_desc", "id_asc", "created_at_desc", "created_at_asc", "updated_at_desc", "updated_at_asc"]
RegistrationDate = Annotated[str, Field(description="Complete ISO first-registration date: YYYY-MM-DD.")]


def validate_registration_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("A complete YYYY-MM-DD registration date is required.")
    date.fromisoformat(value)
    return value


def normalize_plate(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[0-9]{1,6}-[A-Z\u0621-\u063a\u0641-\u064a]-[0-9]{1,6}", normalized):
        raise ValueError("Expected a plate such as 14554-B-4578 or 84654-ب-454.")
    return normalized


class CreateDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["chiffrage_rapide", "verification"]
    marque_id: VehicleId | None = None
    model_id: VehicleId | None = None
    matricule: str = Field(default="", max_length=100)
    date_mise_en_circulation: RegistrationDate | None = None

    @field_validator("date_mise_en_circulation")
    @classmethod
    def registration_date(cls, value):
        if value is None:
            raise ValueError("Omit unknown registration dates.")
        return validate_registration_date(value)

    _plate = field_validator("matricule")(normalize_plate)


class PatchDossier(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    marque_id: VehicleId | None = None
    model_id: VehicleId | None = None
    matricule: str | None = Field(default=None, min_length=1, max_length=100)
    date_mise_en_circulation: RegistrationDate | None = None
    annee: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    date_sinistre: str | None = None

    _plate = field_validator("matricule")(lambda value: normalize_plate(value) if value is not None else value)

    @field_validator("date_mise_en_circulation")
    @classmethod
    def registration_date(cls, value):
        return validate_registration_date(value) if value is not None else value

    @field_validator("date_sinistre")
    @classmethod
    def accident_date(cls, value):
        return validate_registration_date(value) if value is not None else value
