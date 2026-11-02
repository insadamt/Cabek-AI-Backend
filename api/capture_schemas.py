"""Public mock-camera requests and progress, separate from AI scan state.

Depends on workflow phase and photo contracts; private sample filesystem paths
are never included in responses.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas import WorkflowPhoto
from api.workflow_schemas import PhotoPhase


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: PhotoPhase | None = None


class CapturedPhoto(WorkflowPhoto):
    status: Literal["saved"] = "saved"
    original_filename: str


class CaptureStatus(BaseModel):
    dossier_id: str
    phase: PhotoPhase | None = None
    capture_id: str | None = None
    status: Literal["not_started", "capturing", "completed", "failed"]
    expected_count: int = Field(ge=0)
    saved_count: int = Field(ge=0)
    photos: list[CapturedPhoto] = Field(default_factory=list)
    error_code: str | None = None
    analysis_error: str | None = None
