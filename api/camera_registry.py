"""Define fixed Tiandy channel roles independently of editable upstream labels.

Depends only on the commissioned channel map; Centre is maintenance-only.
"""

from api.errors import DossierError


CAMERAS = {
    4: ("Avant", "essential", "front"),
    13: ("Arrière", "essential", "rear"),
    14: ("gauche", "essential", "left"),
    10: ("droit", "essential", "right"),
    1: ("Arrière_gauche", "zoom", "rear"),
    2: ("Avant_gauche", "zoom", "front"),
    5: ("gauche_avant", "zoom", "left"),
    6: ("Avant_droit", "zoom", "front"),
    7: ("droite_avant", "zoom", "right"),
    8: ("droit_arriere", "zoom", "right"),
    11: ("gauche_arriere", "zoom", "left"),
    12: ("Arrière_droit", "zoom", "rear"),
    3: ("Centre", "diagnostic", None),
}
ESSENTIAL_CHANNELS = (4, 13, 14, 10)
ZOOM_CHANNELS = tuple(ch for ch, (_, role, _) in CAMERAS.items() if role == "zoom")


def validate_zoom_channels(channels):
    if len(channels) > 8 or len(set(channels)) != len(channels) or any(
        type(ch) is not int or ch not in ZOOM_CHANNELS for ch in channels
    ):
        raise DossierError(422, "invalid_zoom_channels", "Choisissez uniquement des caméras zoom distinctes.")
    return list(channels)


def camera_identity(channel, name=None):
    if channel not in CAMERAS:
        raise DossierError(404, "camera_not_found", "Caméra inconnue.")
    fallback, role, side = CAMERAS[channel]
    return {"channel": channel, "name": name or fallback, "role": role, "side": side}
