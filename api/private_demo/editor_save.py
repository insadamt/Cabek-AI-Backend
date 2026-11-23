"""Build one immutable template revision from a complete manual editor snapshot.

Depends on staged template commits and native validators; all bytes are prepared
before publication, while incomplete drafts intentionally omit completed results.
"""
from io import BytesIO
from pathlib import Path
import shutil
from api.errors import DossierError
from api.files import confined_path, validate_image, read_json_object
from api.private_demo.bundles import allowed_path, validate_attachment_pdf
from api.private_demo.editor_document import document_issues
from api.private_demo.editor_financial import aggregate_financial, part_financial
from api.private_demo.editor_render import render_annotation, render_manual_report
from api.private_demo.annotation_render import annotation_appearances
from api.private_demo.editor_schemas import AUTHORING_FILE
from api.schemas import StoredAnalysis, Vehicle
from api.workflow_service import timestamp
from api.workflow_schemas import normalize_plate
from scans.artifacts import publish_bytes, publish_json


def image_content(directory, photo, uploads):
    if photo.upload_key:
        if photo.upload_key not in uploads:
            raise DossierError(422, 'missing_upload', 'Photographie téléversée absente.')
        content = uploads[photo.upload_key]
        from PIL import Image
        from api.photo_service import read_validated_upload
        from starlette.datastructures import Headers, UploadFile
        extension = Path(photo.upload_key).suffix.lower()
        media_type = 'image/png' if extension == '.png' else 'image/jpeg'
        read_validated_upload(UploadFile(BytesIO(content), filename=photo.upload_key, headers=Headers({'content-type': media_type})))
        with Image.open(BytesIO(content)) as source:
            extension = '.png' if source.format == 'PNG' else '.jpg'
        return content, extension
    path = allowed_path(photo.path)
    if path.parts[0] != 'photos':
        raise DossierError(422, 'invalid_photo', 'Original requis dans photos/.')
    source = confined_path(directory, str(path))
    validate_image(source)
    return source.read_bytes(), path.suffix.lower()


def write_editor_revision(directory, document, uploads, vehicle_catalog=None):
    previous = read_json_object(directory / 'metadata.json', 'invalid_metadata')
    if vehicle_catalog is not None:
        from api.private_demo.editor_vehicle import resolve_editor_vehicle
        resolve_editor_vehicle(document, previous, vehicle_catalog)
    content = {photo.id: image_content(directory, photo, uploads) for photo in document.photos}
    flattened = {}
    for photo in document.photos:
        for layer, path in photo.flattened.items():
            safe = allowed_path(path)
            if safe.parts[0] not in ('analysis', '.editor-assets'):
                raise DossierError(422, 'invalid_annotation', 'Annotation privée invalide.')
            if layer not in photo.rebuilt_layers and not photo.upload_key:
                flattened[(photo.id, layer)] = confined_path(directory, str(safe)).read_bytes()
    attachment = None
    if document.attachment_upload_key:
        attachment = uploads.get(document.attachment_upload_key)
        if attachment is None:
            raise DossierError(422, 'missing_upload', 'Pièce jointe téléversée absente.')
    elif document.attachment_path:
        path = allowed_path(document.attachment_path)
        if path.parts[0] != 'documents' or path.suffix.lower() != '.pdf':
            raise DossierError(422, 'invalid_econstat', 'PDF requis dans documents/.')
        attachment = confined_path(directory, str(path)).read_bytes()
    for name in ('photos', 'analysis', '.editor-assets', '.econstat'):
        shutil.rmtree(directory / name, ignore_errors=True)
    previous_attachment = previous.get('econstat') or {}
    if previous_attachment.get('path'):
        old_path = allowed_path(previous_attachment['path'])
        if old_path.parts[0] == 'documents':
            confined_path(directory, str(old_path)).unlink(missing_ok=True)
    metadata = {**previous, **document.metadata}
    metadata.update(dossier_id=previous['dossier_id'], type=document.category, matricule=normalize_plate(metadata.get('matricule', '')),
                    photos=[], captures={}, updated_at=timestamp())
    for name in ('before_finalized_at', 'evidence_finalized_at'):
        metadata.pop(name, None)
    for photo in document.photos:
        data, extension = content[photo.id]
        phase = f'{photo.phase}/' if document.category == 'verification' and photo.phase else ''
        photo.path = f'photos/{phase}{photo.id}{extension}'
        publish_bytes(directory / photo.path, data)
        photo.flattened = {}
        for layer in ('parts', 'damages', 'critical'):
            base = flattened.get((photo.id, layer))
            if base:
                path = f'.editor-assets/{photo.id}-{layer}.png'
                publish_bytes(directory / path, base)
                photo.flattened[layer] = path
        photo.upload_key = None
        metadata['photos'].append({
            **photo.extra, 'path': photo.path, 'side': photo.side, 'phase': photo.phase,
            'original_filename': photo.original_filename, 'uploaded_at': photo.extra.get('uploaded_at') or timestamp(),
            'camera_channel': photo.camera_channel, 'camera_name': photo.camera_name or None,
            'camera_role': photo.camera_role, 'frame_id': photo.frame_id or None, 'capture_source': 'demo',
        })
    if attachment is not None:
        document.attachment_path = 'documents/econstat.pdf'
        publish_bytes(directory / document.attachment_path, attachment)
        validate_attachment_pdf(directory / document.attachment_path)
        metadata['econstat'] = {'path': document.attachment_path, 'original_filename': document.attachment_name}
    else:
        metadata.pop('econstat', None)
        document.attachment_path = None
    document.attachment_upload_key = None
    document.metadata = metadata
    publish_json(directory / 'metadata.json', metadata)
    publish_json(directory / AUTHORING_FILE, document.model_dump())
    if document_issues(document) or document.category == 'verification':
        return
    appearances = annotation_appearances(document)
    photos = []
    for photo in document.photos:
        paths = {}
        for layer, suffix in [('parts', 'pieces'), ('damages', 'dommages'), ('critical', 'critical')]:
            path = f'analysis/annotated/{photo.id}_{suffix}.png'
            publish_bytes(directory / path, render_annotation(content[photo.id][0], photo.regions, layer, flattened.get((photo.id, layer)), appearances))
            paths[f'{layer}_path'] = path
        photos.append({'photo_id': photo.id, 'original_path': photo.path, **paths})
    parts, detections = [], []
    for part in document.parts:
        sources = set(part.photo_ids)
        sources |= {photo.id for photo in document.photos for region in photo.regions if region.part_id == part.id}
        for damage in part.damages:
            evidence = set(damage.photo_ids) | {photo.id for photo in document.photos for region in photo.regions if region.detection_id == damage.id}
            sources |= evidence
            detections.append({**damage.extra, 'detection_id': damage.id, 'piece': part.name,
                               'piece_ai_brut': part.model_class or damage.extra.get('piece_ai_brut'),
                               'type': damage.type, 'niveau': damage.severity, 'confiance': damage.confidence,
                               'notes': damage.notes, 'images_sources': sorted(evidence), 'manual': True})
        parts.append({**part.extra, 'piece_ai_brut': part.model_class or part.extra.get('piece_ai_brut'), 'part_id': part.id, 'name': part.name, 'action': part.action,
                      'financial': part_financial(part).model_dump(), 'source_images': sorted(sources),
                      'detection_ids': [damage.id for damage in part.damages]})
    result = StoredAnalysis(schema_version=1, dossier_id=metadata['dossier_id'], vehicle=Vehicle.model_validate(metadata),
                            financial=aggregate_financial(document.parts), parts=parts, detections=detections, photos=photos).model_dump()
    publish_bytes(directory / 'analysis/report.pdf', render_manual_report(document, result))
    publish_json(directory / 'analysis/result.json', result)
