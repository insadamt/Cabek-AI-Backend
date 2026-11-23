"""Describe editable demo evidence independently of the public scan contract.

Depends on native money/workflow types; bounded geometry uses original-relative
coordinates so browser zoom never changes saved markings.
"""
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from api.schemas import Money
from api.workflow_schemas import DossierType, PhotoPhase, PhotoSide

AUTHORING_FILE = '.demo-editor.json'
CATEGORY_LABELS = {
    'repair_labor': 'Tôlerie et réparation', 'paint_labor': 'Main-d’œuvre peinture',
    'paint_materials': 'Produits peinture', 'replacement_labor': 'Main-d’œuvre échange',
    'parts_supply': 'Fourniture des pièces',
}
CATEGORIES = tuple(CATEGORY_LABELS)
LABOR_CATEGORIES = ('repair_labor', 'paint_labor', 'replacement_labor')
Identity = Annotated[str, Field(min_length=1, max_length=120, pattern=r'^[^/\\:.]+$')]
Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class EditorModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Point(EditorModel):
    x: Coordinate
    y: Coordinate


class Region(EditorModel):
    id: Identity
    layer: Literal['parts', 'damages', 'critical']
    shape: Literal['rectangle', 'polygon']
    points: list[Point] = Field(min_length=2, max_length=512)
    part_id: str | None = None
    detection_id: str | None = None

    @model_validator(mode='after')
    def validate_shape(self):
        if self.shape == 'rectangle' and len(self.points) != 2:
            raise ValueError('Un rectangle possède deux coins.')
        if self.shape == 'polygon' and len(self.points) < 3:
            raise ValueError('Un polygone possède au moins trois sommets.')
        if len({(p.x, p.y) for p in self.points}) < (2 if self.shape == 'rectangle' else 3):
            raise ValueError('Une zone doit avoir une surface.')
        if self.shape == 'rectangle' and (self.points[0].x == self.points[1].x or self.points[0].y == self.points[1].y):
            raise ValueError('Une zone doit avoir une surface.')
        if self.shape == 'polygon':
            area = sum(first.x * second.y - second.x * first.y for first, second in zip(self.points, self.points[1:] + self.points[:1]))
            if abs(area) < 1e-8:
                raise ValueError('Un polygone doit avoir une surface.')
        return self


class EditorPhoto(EditorModel):
    id: Identity
    path: str = Field(default='', max_length=240)
    upload_key: str | None = Field(default=None, max_length=240)
    original_filename: str = Field(default='', max_length=240)
    side: PhotoSide = 'front'
    phase: PhotoPhase | None = None
    camera_channel: Annotated[int, Field(ge=1, le=9999)] | None = None
    camera_name: str = Field(default='', max_length=120)
    camera_role: Literal['essential', 'zoom', 'diagnostic'] | None = None
    frame_id: str = Field(default='', max_length=240)
    regions: list[Region] = Field(default_factory=list, max_length=1000)
    flattened: dict[Literal['parts', 'damages', 'critical'], str] = Field(default_factory=dict)
    rebuilt_layers: list[Literal['parts', 'damages', 'critical']] = Field(default_factory=list, max_length=3)
    extra: dict[str, Any] = Field(default_factory=dict)


class EditorDamage(EditorModel):
    id: Identity
    type: str = Field(default='d_rayure', max_length=120)
    severity: str = Field(default='medium', max_length=120)
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    notes: str = Field(default='', max_length=10000)
    photo_ids: list[str] = Field(default_factory=list, max_length=500)
    extra: dict[str, Any] = Field(default_factory=dict)


class EditorPart(EditorModel):
    id: Identity
    name: str = Field(default='', max_length=240)
    model_class: str | None = Field(default=None, max_length=120)
    action: Literal['repair', 'replace'] = 'repair'
    photo_ids: list[str] = Field(default_factory=list, max_length=500)
    damages: list[EditorDamage] = Field(default_factory=list, max_length=1000)
    amounts: dict[str, Money | None] = Field(default_factory=lambda: {key: None for key in CATEGORIES})
    hours: dict[str, Money | None] = Field(default_factory=lambda: {key: None for key in LABOR_CATEGORIES})
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_categories(self):
        if self.model_class is not None:
            from api.private_demo.catalog import PART_CLASSES
            if self.model_class not in dict(PART_CLASSES):
                raise ValueError('Classe de pièce inconnue.')
        if set(self.amounts) != set(CATEGORIES) or set(self.hours) != set(LABOR_CATEGORIES):
            raise ValueError('Catégories de chiffrage invalides.')
        return self


class EditorDocument(EditorModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=120)
    category: DossierType
    metadata: dict[str, Any]
    photos: list[EditorPhoto] = Field(default_factory=list, max_length=500)
    parts: list[EditorPart] = Field(default_factory=list, max_length=500)
    attachment_path: str | None = Field(default=None, max_length=240)
    attachment_upload_key: str | None = Field(default=None, max_length=240)
    attachment_name: str = Field(default='E-Constat.pdf', max_length=240)

    @model_validator(mode='after')
    def validate_unique_ids(self):
        groups = [self.photos, self.parts, [damage for part in self.parts for damage in part.damages],
                  [region for photo in self.photos for region in photo.regions]]
        if any(len({item.id for item in group}) != len(group) for group in groups):
            raise ValueError('Identifiants en double.')
        return self


class EditorSave(EditorModel):
    expected_version: str = Field(min_length=1, max_length=120)
    document: EditorDocument
