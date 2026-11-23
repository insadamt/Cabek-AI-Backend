"""Clone prepared native evidence into independent normal dossier identities.

Depends on native manifests and validation. Immutable template revisions stay
private; every clone rewrites photo/result identities and is atomically published.
"""
import json
import re
from pathlib import Path
import shutil
from uuid import uuid4
from api.errors import DossierError
from api.files import read_json_object
from api.private_demo.bundles import copy_native, validate_native
from api.workflow_service import timestamp
from scans.artifacts import publish_json


def rewrite(value, replacements, field=None):
    if isinstance(value, dict):
        return {key: rewrite(item, replacements, key) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item, replacements) for item in value]
    if isinstance(value, str) and field not in ("side", "phase", "matricule", "type", "source", "marque", "modele"):
        for old, new in replacements:
            value = re.sub(r'(?<![A-Za-z0-9])' + re.escape(old) + r'(?![A-Za-z0-9])', lambda match: new, value)
    return value


def prepare_clone(templates, item, destination, dossier_id, phase=None, existing=None):
    source = templates.directory(item)
    validate_native(source, complete=True)
    from api.private_demo.editor_schemas import AUTHORING_FILE, EditorDocument
    authoring_path = source / AUTHORING_FILE
    authoring = read_json_object(authoring_path, 'invalid_demo_editor') if authoring_path.exists() else None
    copy_native(source, destination)
    # Authoring geometry and imported base layers belong only to private templates.

    (destination / '.demo-editor.json').unlink(missing_ok=True)
    shutil.rmtree(destination / '.editor-assets', ignore_errors=True)
    metadata = read_json_object(destination / 'metadata.json', 'invalid_metadata')
    old_id = metadata['dossier_id']
    replacements = [(old_id, dossier_id)]
    for photo in metadata['photos']:
        stem = Path(photo['path']).stem
        replacements.append((stem, uuid4().hex))
    # Rename longer artifact names before rewriting their JSON references.
    for path in sorted(destination.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if path.is_file():
            name = rewrite(path.name, replacements)
            if name != path.name:
                path.rename(path.with_name(name))
    for path in destination.rglob('*.json'):
        publish_json(path, rewrite(json.loads(path.read_text()), replacements))
    metadata = read_json_object(destination / 'metadata.json', 'invalid_metadata')
    capture_id = uuid4().hex
    key = phase or 'single'
    selected = [p for p in metadata['photos'] if p.get('phase') == phase]
    if not selected:
        raise DossierError(422, 'demo_incomplete', 'Photographies requises pour cette phase.')
    if phase:
        for photo in metadata['photos']:
            if photo not in selected:
                (destination / photo['path']).unlink(missing_ok=True)
        metadata['photos'] = selected
        shutil.rmtree(destination / 'analysis', ignore_errors=True)
    for photo in selected:
        photo.update(capture_id=capture_id, capture_source='demo', original_filename=Path(photo['path']).name, uploaded_at=timestamp())
        photo.pop('photo_id', None)
    metadata.update(dossier_id=dossier_id, demo=True, demo_provenance={'template_id': item['id'], 'version': item['version']},
                    created_at=timestamp(), updated_at=timestamp(), photo_history=[], captures={})
    metadata.pop('before_finalized_at', None); metadata.pop('evidence_finalized_at', None)
    metadata.pop('creation_key', None); metadata.pop('creation_request', None)
    if existing:
        if existing.get('demo_provenance') != metadata['demo_provenance']:
            raise DossierError(409, 'demo_version_changed', 'Le modèle a changé. Recommencez une démonstration indépendante.')
        metadata['photos'] = existing['photos'] + selected
        metadata['captures'] = existing.get('captures', {})
        metadata['created_at'] = existing['created_at']
    metadata['captures'][key] = {'capture_id':capture_id, 'phase':phase, 'capture_source':'demo', 'status':'completed',
        'stage':'prepared_results', 'created_at':timestamp(), 'samples':[{'source':p['path'], 'side':p['side']} for p in selected], 'zoom_channels':[]}
    if phase == 'after':
        metadata.update(before_finalized_at=timestamp(), evidence_finalized_at=timestamp())
    publish_json(destination / 'metadata.json', metadata)
    if not phase:
        publish_json(destination / 'analysis/status.json', {'status':'completed', 'demo':True})
        if authoring:
            from api.private_demo.editor_render import render_manual_report
            document = EditorDocument.model_validate(rewrite(authoring, replacements))
            document.metadata = metadata
            result = read_json_object(destination / 'analysis/result.json', 'invalid_result')
            (destination / 'analysis/report.pdf').write_bytes(render_manual_report(document, result))
    report = destination / 'analysis/report.pdf'
    if report.is_file():
        import pymupdf
        with pymupdf.open(report) as document:
            for page in document:
                page.insert_text((12, page.rect.height - 12), 'DEMONSTRATION - ' + dossier_id,
                                 fontsize=7, color=(0.5, 0.25, 0.1))
            content = document.tobytes(garbage=4, deflate=True)
        report.write_bytes(content)
    return metadata
