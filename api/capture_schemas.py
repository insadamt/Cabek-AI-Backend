"""Public camera requests and durable progress, separate from AI scan state.

Depends on workflow phase and photo contracts; private sample filesystem paths
are never included in responses.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from api.schemas import WorkflowPhoto
from api.workflow_schemas import PhotoPhase


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: PhotoPhase | None = None
    zoom_channels: list[StrictInt] = Field(default_factory=list, max_length=8)

    @field_validator('zoom_channels')
    @classmethod
    def validate_zooms(cls, value):
        from api.camera_registry import validate_zoom_channels
        return validate_zoom_channels(value)


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
    slow_mode: bool = False
    capture_source: Literal['demo', 'tiandy'] | None = None
    zoom_channels: list[int] = Field(default_factory=list)
    analysis_error: str | None = None
    stage: str | None = None
    error_message: str | None = None
