"""Expose AutoEstimate vehicle options and validated dossier vehicle selection.

Depends on the catalog and vehicle service; local quick edits honor the pricing
snapshot, while external expertise keeps its original scan locks.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from api.schemas import ErrorResponse
from api.vehicle_catalog import BrandOption, ModelOption, ResolvedVehicle, VehicleSelection
from api.vehicle_service import VehicleSelectionService


# ============================================================
# VEHICLE REFERENCE AND SELECTION ENDPOINTS
# ============================================================

router = APIRouter(tags=["Vehicles"], responses={
    422: {"model": ErrorResponse, "description": "Invalid IDs or vehicle selection"},
    503: {"model": ErrorResponse, "description": "AutoEstimate or storage unavailable"},
})


@router.get("/api/vehicles/marques", response_model=list[BrandOption], summary="List AutoEstimate brands",
            description="Returns brand IDs and labels for selection. The backend supplies the AutoEstimate token.")
def list_brands(request: Request):
    return request.app.state.vehicle_catalog.brands()


@router.get("/api/vehicles/models", response_model=list[ModelOption], summary="List models for a brand",
            description="Requires a positive marque_id. Returns that brand's model IDs and labels; an empty list means no models.")
def list_models(request: Request, marque_id: Annotated[int, Query(gt=0)]):
    return request.app.state.vehicle_catalog.models(marque_id)


@router.put("/api/dossiers/{dossier_id}/vehicle", response_model=ResolvedVehicle,
            summary="Save validated vehicle IDs and resolved names",
            description="Requires an existing uncompleted dossier and JSON marque_id/model_id. Validates the relationship "
                        "against AutoEstimate and saves canonical names while preserving other metadata. Quick vehicle "
                        "edits remain available until pricing; expertise active scans and finalized dossiers reject edits.",
            responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def update_vehicle(request: Request, dossier_id: Annotated[str, Path()], selection: VehicleSelection):
    service = VehicleSelectionService(request.app.state.scan_manager, request.app.state.vehicle_catalog)
    return service.update_vehicle(dossier_id, selection)
