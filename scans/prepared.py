"""Turn frozen photo annotations into normal engine inputs without YOLO inference.

Depends on the same barèmes and classification keys as the real detector. Manual
prices and repair decisions are discarded; damage rules choose the action afresh.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from api.files import confined_path, read_json_object
from api.private_demo.editor_schemas import EditorDocument
from core.expertise_engine import calculer_cout_dommage, SOLUTIONS
from scans.artifacts import annotation_paths, publish_bytes


def region_mask(region, size):
    mask = Image.new('1', size)
    points = [(round(p.x * (size[0] - 1)), round(p.y * (size[1] - 1))) for p in region.points]
    draw = ImageDraw.Draw(mask)
    if region.shape == 'rectangle':
        x, y = zip(*points)
        draw.rectangle((min(x), min(y), max(x), max(y)), fill=1)
    else:
        draw.polygon(points, fill=1)
    return np.array(mask, dtype=bool)


def critical_overlap(photo, damage_id, size):
    damage_regions = [r for r in photo.regions if r.layer == 'damages' and r.detection_id == damage_id]
    critical_regions = [r for r in photo.regions if r.layer == 'critical']
    overlap = 0.0
    for damage in damage_regions:
        damaged = region_mask(damage, size)
        area = np.count_nonzero(damaged)
        for critical in critical_regions:
            fraction = np.count_nonzero(damaged & region_mask(critical, size)) / max(area, 1)
            overlap = max(overlap, float(fraction))
    return overlap


def load_prepared_instances(request):
    document = EditorDocument.model_validate(read_json_object(
        confined_path(request.dossier_dir, '.mock-evidence/document.json'), 'invalid_mock_document'))
    photos = {photo.id: photo for photo in document.photos}
    if set(photos) != {photo.photo_id for photo in request.photos}:
        raise ValueError('Prepared photo identities do not match this dossier.')
    sizes = {}
    for photo in request.photos:
        with Image.open(confined_path(request.dossier_dir, photo.original_path)) as original:
            sizes[photo.photo_id] = original.size
        for destination in annotation_paths(request.output_dir, photo):
            source = confined_path(request.dossier_dir, '.mock-evidence/annotated/' + destination.name)
            publish_bytes(destination, source.read_bytes())
    instances = []
    for part in document.parts:
        for damage in part.damages:
            references = set(damage.photo_ids) | {
                p.id for p in document.photos for r in p.regions if r.detection_id == damage.id}
            references = references or set(part.photo_ids)
            if not references or not references <= set(photos):
                raise ValueError('Damage evidence is missing from the prepared photos.')
            for identity in sorted(references):
                overlap = critical_overlap(photos[identity], damage.id, sizes[identity])
                critical = overlap >= 0.05
                severity = 'mid' if damage.severity == 'medium' else damage.severity
                severity = 'high' if critical else severity
                detail = calculer_cout_dommage(part.model_class, damage.type, severity)
                name = Path(photos[identity].path).name
                instances.append({
                    'piece_ai_brut': part.model_class, 'piece': part.name,
                    'type_brut': damage.type, 'type': damage.type.removeprefix('d_').replace('_', ' '),
                    'niveau': severity, 'confiance': damage.confidence if damage.confidence is not None else 1.0,
                    'source_image': name, 'images_sources': [name], 'n_vues': 1,
                    'zone_critique_detectee': critical, 'zone_critique_overlap_pct': overlap * 100,
                    'cout': detail.get('total'), 'cout_detail': detail,
                    'remplacement_requis': False, 'chiffrage_bloque': False,
                    'action_experte': '', 'action_ia': 'reparation',
                    'solution': SOLUTIONS.get(damage.type, 'À vérifier par un expert'),
                    'validation_expert': 'a_valider', 'model_version': 'prepared-annotations',
                })
    return instances
