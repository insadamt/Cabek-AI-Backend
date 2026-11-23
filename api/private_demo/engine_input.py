"""Freeze structured annotations for engine pricing without carrying manual costs.

Depends on editor DTOs, immutable template revisions and native photo identities;
painted pixels alone cannot supply a part class or damage severity to the engine.
"""
from pathlib import Path
import shutil

from api.errors import DossierError
from api.files import read_json_object
from api.private_demo.catalog import PART_CLASSES, DAMAGE_CLASSES
from api.private_demo.editor_document import load_document
from api.private_demo.editor_schemas import EditorDocument
from api.private_demo.materialize import rewrite
from scans.artifacts import publish_json


def validate_engine_document(document):
    for part in document.parts:
        if not part.damages:
            raise DossierError(422, 'mock_damage_required', 'Associez un dommage à chaque pièce annotée.')
        if part.model_class is None:
            classes = {damage.extra.get('piece_ai_brut') for damage in part.damages}
            if len(classes) == 1 and next(iter(classes)) in dict(PART_CLASSES):
                part.model_class = classes.pop()
        if part.model_class not in dict(PART_CLASSES):
            raise DossierError(422, 'mock_part_class_required', f'Choisissez le type de pièce : {part.name}.')
        for damage in part.damages:
            if damage.type not in dict(DAMAGE_CLASSES):
                raise DossierError(422, 'mock_damage_class_required', 'Choisissez un type de dommage du catalogue.')
    return document


def freeze_engine_inputs(templates, item, stage):
    source = templates.directory(item)
    document = validate_engine_document(load_document(source, item))
    metadata = read_json_object(stage / 'metadata.json', 'invalid_metadata')
    originals = read_json_object(source / 'metadata.json', 'invalid_metadata')['photos']
    replacements = [(document.metadata['dossier_id'], metadata['dossier_id'])]
    replacements += [(Path(old['path'].replace('\\', '/')).stem, Path(new['path']).stem)
                     for old, new in zip(originals, metadata['photos'], strict=True)]
    document = EditorDocument.model_validate(rewrite(document.model_dump(), replacements))
    document.metadata = metadata
    evidence = stage / '.mock-evidence'
    evidence.mkdir()
    publish_json(evidence / 'document.json', document.model_dump())
    shutil.copytree(stage / 'analysis/annotated', evidence / 'annotated')
    shutil.rmtree(stage / 'analysis')
