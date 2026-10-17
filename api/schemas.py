from math import isclose
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScanState = Literal["not_analyzed", "queued", "processing", "completed", "failed"]
Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Vehicle(BaseModel):
    marque: str = ""
    modele: str = ""
    matricule: str = ""
    annee: str | None = None
    date_sinistre: str | None = None


class FinancialBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_labor: Money | None
    paint_labor: Money | None
    paint_materials: Money | None
    replacement_labor: Money | None
    parts_supply: Money | None


class FinancialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "incomplete"]
    currency: Literal["MAD"]
    estimated_total: Money | None
    known_subtotal: Money
    breakdown: FinancialBreakdown
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_totals(self) -> "FinancialResult":
        amounts = list(self.breakdown.model_dump().values())
        subtotal = sum(amount for amount in amounts if amount is not None)
        if not isclose(self.known_subtotal, subtotal, rel_tol=0, abs_tol=0.01):
            raise ValueError("Known subtotal must match the financial breakdown.")
        if self.status == "complete":
            if None in amounts or self.estimated_total is None:
                raise ValueError("Complete finances require every amount.")
            if not isclose(self.estimated_total, subtotal, rel_tol=0, abs_tol=0.01):
                raise ValueError("Estimated total must match the financial breakdown.")
        elif self.estimated_total is not None or not any(w.strip() for w in self.warnings):
            raise ValueError("Incomplete finances require a null total and warnings.")
        return self


class StoredPhoto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photo_id: str
    original_path: str
    parts_path: str
    damages_path: str
    critical_path: str


class StoredAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dossier_id: str
    vehicle: Vehicle
    financial: FinancialResult
    parts: list[dict[str, Any]]
    detections: list[dict[str, Any]]
    photos: list[StoredPhoto] = Field(min_length=1)


class PhotoUrls(BaseModel):
    photo_id: str
    original_url: str
    parts_url: str
    damages_url: str
    critical_url: str


class AnalysisResult(BaseModel):
    schema_version: Literal[1]
    vehicle: Vehicle
    financial: FinancialResult
    parts: list[dict[str, Any]]
    detections: list[dict[str, Any]]
    photos: list[PhotoUrls]
    report_url: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class DossierResult(BaseModel):
    dossier_id: str
    status: ScanState
    progress: None = None
    error: ErrorDetail | None = None
    result: AnalysisResult | None = None


class DossierSummary(BaseModel):
    dossier_id: str
    vehicle: Vehicle
    created_at: str | None = None
    status: ScanState
    photo_count: int


class DossierCounts(BaseModel):
    all: int = 0
    not_analyzed: int = 0
    queued: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0


class DossierWarning(BaseModel):
    dossier_id: str
    code: str
    message: str


class DossierList(BaseModel):
    items: list[DossierSummary]
    total: int
    counts: DossierCounts
    limit: int
    offset: int
    warnings: list[DossierWarning]
