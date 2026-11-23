"""Hydrate old native templates and inspect private authoring documents.

Depends on editor DTOs and confined paths; unknown native business fields survive
round trips and flattened annotations remain available until explicitly rebuilt.
"""
from pathlib import Path
from urllib.parse import quote
from api.files import read_json_object
from api.private_demo.editor_schemas import (
    AUTHORING_FILE, CATEGORIES, LABOR_CATEGORIES, EditorDocument,
)


def document_issues(document):
    issues = []
    photo_ids = {photo.id for photo in document.photos}
    parts = {part.id: part for part in document.parts}
    damages = {damage.id: (part.id, damage) for part in document.parts for damage in part.damages}
    if not photo_ids:
        issues.append('Ajoutez au moins une photographie.')
    if document.category == 'verification':
        if document.parts:
            issues.append('La vérification accepte uniquement les photographies avant / après.')
        for phase, label in [('before', 'avant'), ('after', 'après')]:
            present = {photo.side for photo in document.photos if photo.phase == phase}
            if present != {'front', 'rear', 'left', 'right'}:
                issues.append(f'Complétez les quatre côtés {label}.')
    for photo in document.photos:
        if (document.category == 'verification') != (photo.phase is not None):
            issues.append(f'Phase incompatible : {photo.original_filename or photo.id}.')
        for region in photo.regions:
            if region.part_id and region.part_id not in parts:
                issues.append('Une zone référence une pièce supprimée.')
            if region.detection_id and (region.detection_id not in damages or damages[region.detection_id][0] != region.part_id):
                issues.append('Une zone référence un dommage incompatible.')
            if region.layer == 'damages' and not region.detection_id:
                issues.append('Associez chaque zone de dommage à un dommage de pièce.')
            if region.layer == 'parts' and not region.part_id:
                issues.append('Associez chaque zone de pièce à une pièce.')
    for part in document.parts:
        if not part.name.strip():
            issues.append('Renseignez le nom de chaque pièce.')
        evidence = set(part.photo_ids) | {p.id for p in document.photos for r in p.regions if r.part_id == part.id}
        evidence |= {identity for damage in part.damages for identity in damage.photo_ids}
        if not evidence:
            issues.append(f'Associez une photo à la pièce {part.name or "sans nom"}.')
        if not evidence <= photo_ids:
            issues.append(f'Une photo de la pièce {part.name} a été supprimée.')
    return list(dict.fromkeys(issues))


def source_ids(references, photos):
    output = []
    for reference in references or []:
        name = Path(str(reference).replace('\\', '/')).name
        matches = [p['id'] for p in photos if reference == p['id'] or name == Path(p['path']).name or Path(name).stem == p['id']]
        if len(matches) == 1 and matches[0] not in output:
            output.append(matches[0])
    return output


def load_document(directory, item):
    path = directory / AUTHORING_FILE
    if path.exists():
        document = EditorDocument.model_validate(read_json_object(path, 'invalid_demo_editor'))
        document.title = item['title']
        return document
    metadata = read_json_object(directory / 'metadata.json', 'invalid_metadata')
    result_path = directory / 'analysis/result.json'
    result = read_json_object(result_path, 'invalid_result') if result_path.exists() else {}
    annotations = {p['photo_id']: p for p in result.get('photos', [])}
    photos = []
    for photo in metadata.get('photos', []):
        identity = Path(photo['path'].replace('\\', '/')).stem
        stored = annotations.get(identity, {})
        photos.append({
            'id': identity, 'path': photo['path'].replace('\\', '/'),
            'original_filename': photo.get('original_filename') or Path(photo['path']).name,
            'side': photo.get('side') or 'front', 'phase': photo.get('phase'),
            'camera_channel': photo.get('camera_channel'), 'camera_name': photo.get('camera_name') or '',
            'camera_role': photo.get('camera_role'), 'frame_id': photo.get('frame_id') or '',
            'flattened': {layer: stored[f'{layer}_path'] for layer in ('parts', 'damages', 'critical') if stored.get(f'{layer}_path')},
            'extra': photo,
        })
    detections = result.get('detections', [])
    parts = []
    for index, part in enumerate(result.get('parts', [])):
        damages = []
        for damage_index, damage in enumerate(detections):
            if damage.get('detection_id') not in part.get('detection_ids', []):
                continue
            damages.append({
                'id': str(damage.get('detection_id') or f'damage-{index}-{damage_index}'),
                'type': damage.get('type') or 'd_rayure', 'severity': damage.get('niveau') or 'medium',
                'confidence': damage.get('confiance') if isinstance(damage.get('confiance'), (float, int)) and 0 <= damage['confiance'] <= 1 else None,
                'notes': damage.get('notes') or '',
                'photo_ids': source_ids(damage.get('images_sources'), photos), 'extra': damage,
            })
        financial = part.get('financial') or {}
        from api.private_demo.catalog import PART_CLASSES
        model_class = part.get('piece_ai_brut')
        parts.append({
            'model_class': model_class if model_class in dict(PART_CLASSES) else None,
            'id': str(part.get('part_id') or f'part-{index}'), 'name': part.get('name') or '',
            'action': part.get('action') or 'repair', 'damages': damages,
            'photo_ids': source_ids(part.get('source_images'), photos),
            'amounts': {key: financial.get('breakdown', {}).get(key) for key in CATEGORIES},
            'hours': {key: financial.get('labor_hours', {}).get(key) for key in LABOR_CATEGORIES}, 'extra': part,
        })
    attachment = metadata.get('econstat') or {}
    return EditorDocument.model_validate({
        'title': item['title'], 'category': metadata.get('type', 'forfait_expertise'),
        'metadata': metadata, 'photos': photos, 'parts': parts,
        'attachment_path': attachment.get('path'), 'attachment_name': attachment.get('original_filename', 'E-Constat.pdf'),
    })


def inspect_editor(store, item):
    directory = store.directory(item)
    document = load_document(directory, item)
    paths = {photo.path for photo in document.photos}
    paths |= {path for photo in document.photos for path in photo.flattened.values()}
    if document.attachment_path:
        paths.add(document.attachment_path)
    result_path = directory / 'analysis/result.json'
    result = read_json_object(result_path, 'invalid_result') if result_path.exists() else None
    if result:
        paths.add('analysis/report.pdf')
        paths |= {p[key] for p in result['photos'] for key in ('parts_path', 'damages_path', 'critical_path')}
    prefix = f'/api/private-demo/templates/{item["id"]}/media/'
    urls = {path: prefix + quote(path, safe='/') + '?version=' + item['version'] for path in paths if (directory / path).is_file()}
    return {'editor': document.model_dump(), 'analysis': result, 'issues': document_issues(document), 'media': urls}
