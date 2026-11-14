"""Existing scan and dashboard responses, extended with independent workflow fields.

Depends on Pydantic and shared workflow contracts; saved financial semantics stay unchanged.
"""

from math import isclose
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from api.workflow_schemas import DossierType, PhotoPhase, PhotoSide, WorkflowStatus


# ============================================================
# SHARED STATUS AND MONEY TYPES
# ============================================================

ScanState = Literal["not_analyzed", "queued", "processing", "completed", "failed"]
Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]


# ============================================================
# VEHICLE AND FINANCIAL CONTRACTS
# ============================================================

class Vehicle(BaseModel):
    marque_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    model_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    marque: str = ""
    modele: str = ""
    matricule: str = ""
    annee: str | None = None
    date_mise_en_circulation: str | None = None
    date_sinistre: str | None = None


class FinancialBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_labor: Money | None
    paint_labor: Money | None
    paint_materials: Money | None
    replacement_labor: Money | None
    parts_supply: Money | None


class LaborHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_labor: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    paint_labor: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    replacement_labor: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class FinancialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "incomplete"]
    currency: Literal["MAD"]
    estimated_total: Money | None = Field(description="Full estimate in MAD; null when finances are incomplete.")
    known_subtotal: Money = Field(description="Sum of known financial breakdown amounts in MAD.")
    breakdown: FinancialBreakdown
    labor_hours: LaborHours = Field(
        default_factory=LaborHours,
        description="Total working hours per labor category; null when any contributing duration is unknown.",
    )
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_totals(self) -> "FinancialResult":
        amounts = list(self.breakdown.model_dump().values())

        # Unknown costs stay null; only known amounts contribute to the partial subtotal.

        subtotal = sum(amount for amount in amounts if amount is not None)
        if not isclose(self.known_subtotal, subtotal, rel_tol=0, abs_tol=0.01):
            raise ValueError("Known subtotal must match the financial breakdown.")

        # A numeric partial subtotal must never be presented as a complete estimate.

        if self.status == "complete":
            if None in amounts or self.estimated_total is None:
                raise ValueError("Complete finances require every amount.")
            if not isclose(self.estimated_total, subtotal, rel_tol=0, abs_tol=0.01):
                raise ValueError("Estimated total must match the financial breakdown.")
        elif self.estimated_total is not None or not any(w.strip() for w in self.warnings):
            raise ValueError("Incomplete finances require a null total and warnings.")
        return self


# ============================================================
# RUNNER OUTPUT STORED ON DISK
# ============================================================

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


# ============================================================
# FRONTEND RESULT CONTRACT
# ============================================================

class OriginalPhoto(BaseModel):
    photo_id: str
    original_url: str
    preview_url: str


class WorkflowPhoto(OriginalPhoto):
    side: PhotoSide
    phase: PhotoPhase | None = None
    uploaded_at: str | None = None


class ComparisonSlot(BaseModel):
    side: PhotoSide
    before: WorkflowPhoto | None = None
    after: WorkflowPhoto | None = None


class VerificationComparison(BaseModel):
    slots: list[ComparisonSlot]
    missing_before: list[PhotoSide]
    missing_after: list[PhotoSide]
    before_locked: bool = False
    finalized: bool = False


class PhotoUrls(BaseModel):
    photo_id: str
    original_url: str
    parts_url: str
    damages_url: str
    critical_url: str
    original_preview_url: str
    parts_preview_url: str
    damages_preview_url: str
    critical_preview_url: str


class AnalysisResult(BaseModel):
    schema_version: Literal[1]
    vehicle: Vehicle
    financial: FinancialResult
    parts: list[dict[str, Any]] = Field(description="Part IDs, names, actions, finances, detection IDs, and source images.")
    detections: list[dict[str, Any]] = Field(description="Saved engine detections, severity, confidence, feedback, and costs.")
    photos: list[PhotoUrls]
    report_url: str = Field(description="URL for downloading the saved PDF.")


# ============================================================
# ERROR AND DOSSIER RESPONSES
# ============================================================

class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ============================================================
# OBSERVED SCAN OUTPUT READINESS
# ============================================================

class ScanProgress(BaseModel):
    stage: Literal["queued", "preparing", "analyzing_photos", "awaiting_metadata", "finalizing", "completed", "failed"]
    annotations_ready: int = Field(ge=0)
    annotations_expected: int = Field(ge=0)
    photos_completed: int = Field(ge=0)
    photos_total: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    eta_seconds: float | None = Field(default=None, description="Unknown until a reliable estimate is available.")
    report_ready: bool
    result_ready: bool


class DossierResult(BaseModel):
    dossier_id: str
    type: DossierType = "forfait_expertise"
    source: str = "external"
    created_at: str | None = None
    updated_at: str | None = None
    workflow_status: WorkflowStatus = "not_analyzed"
    comparison: VerificationComparison | None = None
    vehicle: Vehicle = Field(description="Current saved metadata vehicle, available in every scan state.")
    status: ScanState
    photos: list[WorkflowPhoto | OriginalPhoto] = Field(
        default_factory=list, description="Existing original photos, available before analysis."
    )

    # Unscanned dossiers have no progress; managed scans expose observed file counts.

    progress: ScanProgress | None = None
    error: ErrorDetail | None = None
    result: AnalysisResult | None = Field(default=None, description="Saved analysis; null without valid final output.")


# ============================================================
# DASHBOARD SUMMARIES AND COUNTS
# ============================================================

class DossierSummary(BaseModel):
    dossier_id: str
    type: DossierType = "forfait_expertise"
    source: str = "external"
    workflow_status: WorkflowStatus = "not_analyzed"
    updated_at: str | None = None
    vehicle: Vehicle
    created_at: str | None = None
    status: ScanState
    photo_count: int = Field(description="Number of existing declared original photos.")


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
    total: int = Field(description="Matches after search and status filtering, before pagination.")
    counts: DossierCounts = Field(description="Counts after search, before status filtering and pagination.")
    type_counts: dict[str, int] = Field(default_factory=dict)
    workflow_counts: dict[str, int] = Field(default_factory=dict)
    limit: int
    offset: int
    warnings: list[DossierWarning] = Field(description="Folders skipped because their saved data is unavailable or invalid.")
