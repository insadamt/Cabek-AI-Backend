"""Expose the deployed AI vocabulary without loading inference dependencies.

Depends on the class names in best_pieces.pt, best.pt and best_zone_critique.pt;
record identities remain independent of these classification keys.
"""
from pydantic import BaseModel


class CatalogOption(BaseModel):
    key: str
    label: str


class EditorCatalog(BaseModel):
    parts: list[CatalogOption]
    damages: list[CatalogOption]
    critical: list[CatalogOption]


PART_CLASSES = (
    ('p_Aile arriere droite', 'Aile arrière droite'),
    ('p_Aile arriere gauche', 'Aile arrière gauche'),
    ('p_Aile avant droite', 'Aile avant droite'),
    ('p_Aile avant gauche', 'Aile avant gauche'),
    ('p_Calandre', 'Calandre'), ('p_Capot', 'Capot'), ('p_Coffre', 'Coffre'),
    ('p_Feu arriere droit', 'Feu arrière droit'), ('p_Feu arriere gauche', 'Feu arrière gauche'),
    ('p_Pare-brise arriere', 'Pare-brise arrière'), ('p_Pare-brise avant', 'Pare-brise avant'),
    ('p_Pare-chocs arriere', 'Pare-chocs arrière'), ('p_Pare-chocs avant', 'Pare-chocs avant'),
    ('p_Phare avant droit', 'Phare avant droit'), ('p_Phare avant gauche', 'Phare avant gauche'),
    ('p_Pneu', 'Pneu'), ('p_Porte arriere droite', 'Porte arrière droite'),
    ('p_Porte arriere gauche', 'Porte arrière gauche'), ('p_Porte avant droite', 'Porte avant droite'),
    ('p_Porte avant gauche', 'Porte avant gauche'), ('p_Retroviseur droit', 'Rétroviseur droit'),
    ('p_Retroviseur gauche', 'Rétroviseur gauche'), ('p_Roue', 'Roue'),
    ('p_bas de caisse', 'Bas de caisse'), ('p_vitre', 'Vitre'),
)
DAMAGE_CLASSES = (
    ('d_bosse', 'Bosse'), ('d_brise', 'Bris de vitrage'), ('d_casse', 'Casse'),
    ('d_crevaison', 'Crevaison'), ('d_fissure', 'Fissure'), ('d_lampe_casse', 'Optique cassée'),
    ('d_piece_manquante', 'Pièce manquante'), ('d_rayure', 'Rayure'),
)
CRITICAL_CLASSES = (('zone_critique', 'Zone critique'),)


def editor_catalog():
    return EditorCatalog(**{
        name: [CatalogOption(key=key, label=label) for key, label in values]
        for name, values in [('parts', PART_CLASSES), ('damages', DAMAGE_CLASSES), ('critical', CRITICAL_CLASSES)]
    })
