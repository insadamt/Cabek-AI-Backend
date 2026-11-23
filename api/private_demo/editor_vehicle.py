"""Resolve changed vehicle selections before publishing a private revision.

Depends on the existing AutoEstimate catalog; unchanged historical values remain
readable and editable offline, while new IDs always receive canonical labels.
"""
from api.errors import DossierError
from api.vehicle_catalog import VehicleSelection
from pydantic import ValidationError

VEHICLE_FIELDS = ('marque_id', 'model_id', 'marque', 'modele')


def resolve_editor_vehicle(document, previous, catalog):
    current = document.metadata
    if all(current.get(key) == previous.get(key) for key in VEHICLE_FIELDS):
        return
    brand_id, model_id = current.get('marque_id'), current.get('model_id')
    if brand_id is None and model_id is None:
        # Clearing a selection is valid for a draft; new free-form names cannot impersonate catalog data.

        if current.get('marque') or current.get('modele'):
            raise DossierError(422, 'invalid_vehicle_selection', 'Choisissez une marque et un modèle du catalogue.')
        return
    try:
        selection = VehicleSelection(marque_id=brand_id, model_id=model_id)
    except ValidationError:
        raise DossierError(422, 'invalid_vehicle_selection', 'Choisissez une marque et un modèle compatibles.') from None
    resolved = catalog.resolve(selection)
    document.metadata.update(resolved.model_dump())
