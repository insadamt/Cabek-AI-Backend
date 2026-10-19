import os
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import requests

from api.errors import DossierError


# ============================================================
# VEHICLE SELECTION CONTRACTS
# ============================================================

VehicleId = Annotated[int, Field(strict=True, gt=0)]


class BrandOption(BaseModel):
    id: VehicleId
    libelle: str = Field(min_length=1)


class ModelOption(BrandOption):
    marque_id: VehicleId


class VehicleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marque_id: VehicleId
    model_id: VehicleId


class ResolvedVehicle(VehicleSelection):
    marque: str
    modele: str


# ============================================================
# AUTOESTIMATE REFERENCE DATA
# ============================================================

class VehicleCatalog:
    def read_reference(self, path: str, params: dict | None = None) -> list[dict]:
        base_url = os.environ.get("AUTOESTIMATE_BASE_URL", "").rstrip("/")
        token = os.environ.get("AUTOESTIMATE_TOKEN", "")
        if not base_url or not token:
            raise DossierError(503, "autoestimate_unavailable", "AutoEstimate is not configured.")
        try:
            response = requests.get(
                f"{base_url}/{path}", params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10, allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError("Reference request failed.")
            data = response.json()["data"]
            if not isinstance(data, list):
                raise ValueError("Invalid reference list.")
            return data
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:

            # Upstream responses and exceptions can contain credentials or private service details.

            raise DossierError(503, "autoestimate_unavailable", "AutoEstimate reference data is unavailable.") from error

    def brands(self) -> list[BrandOption]:
        return self.validate_options(self.read_reference("marques"), BrandOption)

    def models(self, marque_id: int) -> list[ModelOption]:
        options = self.validate_options(self.read_reference("models", {"marque_id": marque_id}), ModelOption)
        if any(option.marque_id != marque_id for option in options):
            raise DossierError(503, "autoestimate_unavailable", "AutoEstimate model data is inconsistent.")
        return options

    def validate_options(self, values: list[dict], schema: type[BaseModel]) -> list:
        try:
            options = [schema.model_validate(value) for value in values]
            if len({option.id for option in options}) != len(options):
                raise ValueError("Duplicate reference IDs.")
            return options
        except (ValidationError, ValueError, TypeError) as error:
            raise DossierError(503, "autoestimate_unavailable", "AutoEstimate reference data is invalid.") from error

    def resolve(self, selection: VehicleSelection) -> ResolvedVehicle:
        brand = next((item for item in self.brands() if item.id == selection.marque_id), None)
        if brand is None:
            raise DossierError(422, "invalid_vehicle_selection", "Unknown vehicle brand.")
        model = next((item for item in self.models(selection.marque_id) if item.id == selection.model_id), None)
        if model is None:
            raise DossierError(422, "invalid_vehicle_selection", "The model does not belong to the selected brand.")
        return ResolvedVehicle(**selection.model_dump(), marque=brand.libelle, modele=model.libelle)
