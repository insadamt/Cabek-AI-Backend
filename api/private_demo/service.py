"""Admit matched demonstrations before hardware or worker invocation.

Depends on private template revisions, existing dossier locks, and normal capture
status DTOs. Prepared evidence is published as native data, never as a real AI job.
"""
import os
from pathlib import Path
import shutil
from uuid import uuid4
from filelock import FileLock
from api.dossier_lifecycle import dossier_admission
from api.errors import DossierError
from api.files import read_json_object, confined_path
from api.private_demo.auth import AdministrationAuth
from api.private_demo.templates import TemplateStore
from api.private_demo.materialize import prepare_clone
from scans.artifacts import publish_json, publish_bytes


class PrivateDemoService:
    def __init__(self, application, settings):
        self.state = application.state
        self.root = Path(os.environ.get('CABEK_DEMO_DIR', settings.runtime_root / 'private-demo'))
        self.templates = TemplateStore(self.root)
        secret = os.environ.get('CABEK_DEMO_SECRET_FILE', self.root / 'secret.json')
        self.auth = AdministrationAuth(self.root / 'authentication', secret)
        self.repository = self.state.workflow_service.repository
        if self.root.resolve().is_relative_to(self.repository.dossiers_dir):
            raise ValueError('Private templates must be outside public dossier storage.')

    def managed(self):
        return bool(os.environ.get('CABEK_DEMO_DIR')) or self.auth.secret.exists()

    def enabled(self):
        return self.state.operation_settings.read()['demo_mode']

    def capture(self, dossier_id, phase, request_key=None):
        dossier = self.repository.read_dossier(dossier_id)
        if not self.managed() and not dossier.metadata.demo:
            return None
        if not self.enabled():
            if dossier.metadata.demo:
                raise DossierError(409, 'demo_disabled', 'La démonstration est désactivée. Ce dossier simulé ne peut pas utiliser le matériel réel.')
            return None
        self.state.capture_service.validate_phase(dossier, phase)
        item = self.templates.match(dossier.metadata.matricule, dossier.metadata.type)
        with dossier_admission(self.repository, dossier_id), self.templates.locked():
            dossier = self.repository.read_dossier(dossier_id)
            metadata = read_json_object(dossier.directory / 'metadata.json', 'invalid_metadata')
            if any(c.get('status') == 'capturing' for c in metadata.get('captures', {}).values()):
                raise DossierError(409, 'capture_in_progress', 'Une capture est déjà active.')
            if phase == 'after':
                if not metadata.get('demo') or {p['side'] for p in metadata['photos'] if p.get('phase') == 'before'} != {'front','rear','left','right'}:
                    raise DossierError(409, 'before_photos_incomplete', 'Terminez les quatre côtés avant.')
            if metadata.get('demo') and metadata.get('captures', {}).get(phase or 'single', {}).get('status') == 'completed':
                return dossier_id
            # Imported expertise remains externally owned; publish an independent clone.
            target_id = 'DOS-DEMO-' + uuid4().hex if dossier.metadata.type == 'forfait_expertise' else dossier_id
            target = self.repository.dossiers_dir / target_id
            stage = self.repository.dossiers_dir / ('.demo-stage-' + uuid4().hex)
            stage.mkdir()
            created_files = []
            metadata_published = False
            try:
                prepared = prepare_clone(self.templates, item, stage, target_id, phase, metadata if phase == 'after' else None)
                if target_id != dossier_id:
                    stage.rename(target)
                else:
                    # Preserve the draft identity and vehicle ownership; existing production evidence is never replaced.
                    if metadata.get('photos') and not (phase == 'after' and metadata.get('demo')):
                        raise DossierError(409, 'demo_existing_evidence', 'Utilisez un nouveau dossier pour cette démonstration.')
                    for path in stage.rglob('*'):
                        if path.is_file() and path.relative_to(stage).as_posix() not in ('metadata.json', 'analysis/result.json', 'analysis/status.json'):
                            relative = path.relative_to(stage)
                            destination = confined_path(target, relative.as_posix())
                            if destination.exists() and destination.read_bytes() != path.read_bytes():
                                raise DossierError(409, 'demo_artifact_conflict', 'Un fichier existant doit être préservé.')
                            if not destination.exists():
                                created_files.append(destination)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(path, destination)
                    for field in ('creation_key', 'creation_request'):
                        if field in metadata:
                            prepared[field] = metadata[field]
                    publish_json(target / 'metadata.json', prepared)
                    metadata_published = True
                    for name in ('analysis/result.json', 'analysis/status.json'):
                        source = stage / name
                        if source.exists():
                            destination = target / name
                            if not destination.exists():
                                created_files.append(destination)
                            publish_bytes(destination, source.read_bytes())
                return target_id
            except Exception:
                if target_id == dossier_id:
                    if metadata_published:
                        publish_json(target / 'metadata.json', metadata)
                    for path in reversed(created_files):
                        path.unlink(missing_ok=True)
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def guard_scan(self, dossier_id):
        dossier = self.repository.read_dossier(dossier_id)
        if dossier.metadata.demo:
            if dossier.metadata.type == 'verification':
                raise DossierError(409, 'scan_disabled_for_type', 'Aucune analyse IA pour une vérification.')
            result = self.state.dossier_service.read_result(dossier_id)
            if result.status != 'completed':
                raise DossierError(409, 'demo_incomplete', 'Le résultat préparé est indisponible ; aucune inférence réelle autorisée.')
            return result
        if self.managed() and self.enabled():
            raise DossierError(409, 'demo_capture_required', 'Préparez la démonstration par le parcours de capture. Aucune IA réelle ne sera lancée.')
        return None
