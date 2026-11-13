"""Expose authenticated lighting status/control and separate maintenance testing.

Depends on the app-owned LightingService and existing structured error handlers;
no lighting credential is shipped to React or accepted in a URL.
"""

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from api.errors import DossierError
from api.schemas import ErrorResponse
from api.lighting_schemas import LightingStatus


class StateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["BOOTING", "INITIALIZING", "READY", "SCANNING", "COMPLETED", "OFF"]


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: Literal["OFF", "RED", "BLUE", "GREEN", "WHITE"]


bearer = HTTPBearer(auto_error=False)


def authorize(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    settings = request.app.state.lighting.settings
    if not settings.enabled:
        raise DossierError(503, "lighting_disabled", "Lighting control is disabled.")
    supplied = credentials.credentials.encode("utf-8") if credentials else b""
    control = secrets.compare_digest(supplied, settings.token.encode("utf-8"))
    maintenance = secrets.compare_digest(supplied, settings.maintenance_token.encode("utf-8"))
    if not (control or maintenance):
        raise DossierError(401, "lighting_unauthorized", "A valid lighting bearer token is required.")
    return maintenance


router = APIRouter(prefix="/api/v1/lights", tags=["Local lighting"],
                   dependencies=[Depends(authorize)], responses={
                       code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 503)
                   })


@router.get("/status", response_model=LightingStatus)
def status(request: Request):
    return request.app.state.lighting.status()


@router.post("/state", status_code=202, response_model=LightingStatus)
def set_state(payload: StateRequest, request: Request):
    request.app.state.lighting.state.request_state(payload.state)
    return request.app.state.lighting.status()


@router.post("/test", status_code=202, response_model=LightingStatus)
def test_color(payload: TestRequest, request: Request, maintenance: bool = Depends(authorize)):
    if not maintenance:
        raise DossierError(403, "lighting_maintenance_required", "A maintenance bearer token is required.")
    request.app.state.lighting.state.start_test(payload.color)
    return request.app.state.lighting.status()
