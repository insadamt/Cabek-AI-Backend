"""Prepare annotated evidence for engine pricing after normal camera capture.

Depends on immutable private templates, dossier admission locks and structured
annotation validation. Camera evidence stays on disk for recovery; only the visible manifest
is replaced, and each dossier retains its own identity and vehicle information.
"""
import shutil
from uuid import uuid4

from api.dossier_lifecycle import dossier_admission
from api.errors import DossierError
from api.files import read_json_object
from api.private_demo.engine_input import freeze_engine_inputs, validate_engine_document
from api.private_demo.editor_document import load_document
from api.private_demo.bundles import validate_native
from api.private_demo.materialize import prepare_clone
from api.workflow_service import timestamp
from scans.artifacts import publish_bytes, publish_json


class MockResults:
    def __init__(self, demo):
        self.demo = demo
        self.path = demo.root / 'mock-results.json'

    def read(self):
        return read_json_object(self.path, 'invalid_mock_settings') if self.path.exists() else {'enabled': False, 'template_id': None, 'version': None}

    def configure(self, enabled, template_id=None):
        with self.demo.templates.locked():
            if enabled:
                item = self.demo.templates.get(template_id or self.read().get('template_id'))
                if item['type'] == 'verification':
                    raise DossierError(422, 'mock_result_required', 'Choisissez des photos annotées pour le chiffrage automatique.')
                validate_native(self.demo.templates.directory(item), complete=True)
                validate_engine_document(load_document(self.demo.templates.directory(item), item))
                template_id = item['id']
            value = {'enabled': enabled, 'template_id': template_id or self.read().get('template_id'),
                     'version': item['version'] if enabled else self.read().get('version')}
            publish_json(self.path, value)
            if enabled:
                self.demo.state.operation_settings.update(demo_mode=False)
            return value

    def apply(self, dossier_id):
        configuration = self.read()
        if not configuration['enabled']:
            return None
        if self.demo.repository.read_dossier(dossier_id).metadata.mock_engine:
            return None
        repository = self.demo.repository
        with dossier_admission(repository, dossier_id), self.demo.templates.locked():
            dossier = repository.read_dossier(dossier_id)
            if dossier.metadata.type == 'verification':
                return None
            if dossier.metadata.demo:
                return self.demo.state.dossier_service.read_result(dossier_id)
            if not dossier.metadata.photos:
                raise DossierError(409, 'mock_photos_required', 'Terminez la capture avant le résultat préparé.')
            if any(c.get('status') == 'capturing' for c in dossier.metadata.captures.values()):
                raise DossierError(409, 'capture_in_progress', 'Terminez la capture avant le résultat préparé.')
            if any(c.get('status') != 'completed' for c in dossier.metadata.captures.values()):
                raise DossierError(409, 'capture_incomplete', 'Terminez la capture avant le chiffrage automatique.')
            _, state = repository.read_analysis_state(dossier)
            if state != 'not_analyzed' and state != 'failed':
                raise DossierError(409, 'already_analyzed', 'Ce dossier possède déjà une analyse.')
            item = {**self.demo.templates.get(configuration['template_id']), 'version': configuration['version']}
            stage = repository.dossiers_dir / ('.mock-stage-' + uuid4().hex)
            stage.mkdir()
            try:
                prepared = prepare_clone(self.demo.templates, item, stage, dossier_id)
                original = read_json_object(dossier.directory / 'metadata.json', 'invalid_metadata')
                metadata = {**original, 'photos': prepared['photos'], 'demo': True, 'mock_engine': True,
                            'demo_provenance': prepared['demo_provenance'], 'updated_at': timestamp()}
                metadata['photo_history'] = original.get('photo_history', []) + [
                    {**photo, 'reason': 'mock_result', 'replaced_at': timestamp()} for photo in original['photos']]
                capture = metadata.get('captures', {}).get('single')
                if capture:
                    for photo in metadata['photos']:
                        photo['capture_id'] = capture['capture_id']
                    capture.update(samples=[{'source': p['path'], 'side': p['side']} for p in metadata['photos']],
                                   stage='prepared_results', analysis_error=None, capture_source='demo')
                freeze_engine_inputs(self.demo.templates, item, stage)
                publish_json(stage / 'analysis/status.json', {'status': 'not_analyzed'})
                self.publish(dossier.directory, stage, metadata)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            return None

    def publish(self, target, stage, metadata):
        # Publish the completed marker last; readers never accept partial result files.

        status = target / 'analysis/status.json'
        backups = {status: status.read_bytes() if status.exists() else None,
                   target / 'metadata.json': (target / 'metadata.json').read_bytes()}
        try:
            # Polling clients must receive valid progress during evidence publication too.

            publish_json(status, {'status': 'processing', 'managed_by': 'cabek_scan_v1',
                'progress': {'stage': 'preparing', 'annotations_ready': 0,
                    'annotations_expected': len(metadata['photos']) * 3, 'photos_completed': 0,
                    'photos_total': len(metadata['photos']), 'elapsed_seconds': 0,
                    'report_ready': False, 'result_ready': False}})
            paths = [p for p in stage.rglob('*') if p.is_file() and p.parts[-1] != 'metadata.json']
            paths.sort(key=lambda p: p.relative_to(stage).as_posix() == 'analysis/status.json')
            for source in paths:
                destination = target / source.relative_to(stage)
                if destination not in backups:
                    backups[destination] = destination.read_bytes() if destination.exists() else None
                if source.relative_to(stage).as_posix() == 'analysis/status.json':
                    publish_json(target / 'metadata.json', metadata)
                publish_bytes(destination, source.read_bytes())
        except Exception:
            for path, content in reversed(list(backups.items())):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    publish_bytes(path, content)
            raise
