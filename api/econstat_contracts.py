"""Validate E-Constat attachments and OCR recommendations before camera mapping.

Depends on Pydantic and the commissioned registry; recommendations never start capture.
"""
import re
from pydantic import BaseModel, ConfigDict, Field

ZONE_CHANNELS = {
    'Avant_gauche': 2, 'Avant': 4, 'Avant_droit': 6,
    'gauche_avant': 5, 'droite_avant': 7,
    'gauche_arrière': 11, 'droit_arrière': 8,
    'Arrière_gauche': 1, 'Arrière': 13, 'Arrière_droit': 12,
}
ZOOM_ZONE_CHANNELS = {zone: channel for zone, channel in ZONE_CHANNELS.items() if channel not in (4, 13)}


class EconstatAttachment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=240)
    original_filename: str = Field(default='E-Constat.pdf', max_length=240)


def normalize_registration(value):
    return re.sub(r'[^A-Z0-9\u0621-\u063a\u0641-\u064a]', '', str(value or '').upper())


def validate_recommendation(payload, registration):
    """Derive selections from validated zones, never trust supplied channel IDs."""
    if not isinstance(payload, dict) or payload.get('schema_version') != '1.0':
        raise ValueError('Unsupported OCR payload')
    vehicle = payload.get('vehicle', {})
    plate = payload.get('registration', {})
    if (vehicle.get('registration_match') is not True
            or vehicle.get('constat_reference') not in ('vehicle_A', 'vehicle_B')
            or not normalize_registration(registration)
            or normalize_registration(plate.get('value')) != normalize_registration(registration)):
        raise ValueError('Registration mismatch')
    raw = payload.get('zones')
    if not isinstance(raw, list) or len(raw) != 10:
        raise ValueError('Incomplete zones')
    zones = []
    for item in raw:
        if (not isinstance(item, dict) or item.get('id') not in ZONE_CHANNELS
                or item.get('status') not in ('damaged', 'uncertain', 'healthy')):
            raise ValueError('Invalid zone')
        zones.append({'id': item['id'], 'status': item['status']})
    if {zone['id'] for zone in zones} != set(ZONE_CHANNELS):
        raise ValueError('Duplicate zones')
    selected = {zone['id'] for zone in zones if zone['status'] in ('damaged', 'uncertain')}
    inspection = payload.get('inspection', {})
    for key, expected in (
        ('damaged_zone_ids', {z['id'] for z in zones if z['status'] == 'damaged'}),
        ('uncertain_zone_ids', {z['id'] for z in zones if z['status'] == 'uncertain'}),
        ('selected_zone_ids', selected),
    ):
        values = inspection.get(key)
        if not isinstance(values, list) or len(values) != len(expected) or set(values) != expected:
            raise ValueError('Inconsistent inspection')
    return {'zones': zones, 'vehicle_reference': vehicle['constat_reference'],
            'recommended_zoom_channels': sorted(ZOOM_ZONE_CHANNELS[z] for z in selected if z in ZOOM_ZONE_CHANNELS)}
