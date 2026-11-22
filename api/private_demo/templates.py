"""Version private templates independently of ordinary dossiers.

Depends on native bundle validation, atomic registry publication and a shared
file lock. Published revisions are immutable; deactivation never removes clones.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from uuid import uuid4
from filelock import FileLock
from api.errors import DossierError
from api.files import read_json_object
from api.workflow_schemas import normalize_plate
from scans.artifacts import publish_json, publish_bytes
from api.private_demo.bundles import allowed_path, validate_native, extract_zip, export_zip


class TemplateStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    @contextmanager
    def locked(self):
        with FileLock(str(self.root / '.registry.lock'), timeout=15):
            yield

    def registry(self):
        path = self.root / 'registry.json'
        return read_json_object(path, 'demo_registry_invalid') if path.exists() else {'templates': {}}

    def list(self):
        return list(self.registry()['templates'].values())

    def get(self, identity):
        value = self.registry()['templates'].get(identity)
        if not value:
            raise DossierError(404, 'demo_template_not_found', 'Modèle introuvable.')
        return value

    def directory(self, item):
        return self.root / 'templates' / item['id'] / item['version']

    def match(self, matricule, category):
        try:
            matricule = normalize_plate(matricule)
        except ValueError:
            raise DossierError(409, 'demo_registration_required', 'Saisissez une immatriculation valide avant la démonstration.') from None
        matches = [item for item in self.list() if item['active'] and item['matricule'] == matricule and item['type'] == category]
        if len(matches) != 1:
            raise DossierError(409, 'demo_template_unmatched', 'Aucun modèle actif pour cette immatriculation et ce parcours. Aucune capture réelle ne sera lancée.')
        validate_native(self.directory(matches[0]), complete=True)
        return matches[0]

    def commit(self, metadata=None, identity=None, asset=None, archive=None, active=None, title=None):
        with self.locked():
            registry = self.registry()
            previous = registry['templates'].get(identity) if identity else None
            if identity and not previous:
                raise DossierError(404, 'demo_template_not_found', 'Modèle introuvable.')
            identity = identity or uuid4().hex
            version = uuid4().hex
            target = self.root / 'templates' / identity / version
            stage = self.root / ('.stage-' + uuid4().hex)
            stage.mkdir()
            try:
                if previous:
                    shutil.copytree(self.directory(previous), stage, dirs_exist_ok=True)
                if archive is not None:
                    shutil.rmtree(stage); stage.mkdir()
                    extract_zip(archive, stage)
                if metadata is not None:
                    publish_json(stage / 'metadata.json', metadata)
                if asset:
                    name, content = asset
                    path = allowed_path(name)
                    if path.parts[0] not in ('photos', 'analysis', 'documents', '.econstat'):
                        raise DossierError(422, 'invalid_asset', 'Emplacement non autorisé.')
                    publish_bytes(stage / path, content)
                saved = read_json_object(stage / 'metadata.json', 'invalid_metadata')
                category = saved.get('type', 'forfait_expertise')
                matricule = normalize_plate(saved.get('matricule', ''))
                enabled = active if active is not None else previous['active'] if previous else False
                result_path = stage / 'analysis/result.json'
                report_path = stage / 'analysis/report.pdf'
                if result_path.exists() and not report_path.exists() and category != 'verification':
                    from api.schemas import StoredAnalysis
                    from scans.report import generate_report
                    result = StoredAnalysis.model_validate(read_json_object(result_path, 'invalid_result'))
                    publish_bytes(report_path, generate_report(saved, [], {}, result.financial.model_dump()))
                validate_native(stage, complete=enabled)
                if enabled and any(item['id'] != identity and item['active'] and item['type'] == category and item['matricule'] == matricule for item in registry['templates'].values()):
                    raise DossierError(409, 'demo_mapping_conflict', 'Une correspondance active existe déjà.')
                item = {'id': identity, 'version': version, 'title': (title or (previous or {}).get('title') or matricule)[:120],
                        'type': category, 'matricule': matricule, 'active': enabled}
                target.parent.mkdir(parents=True, exist_ok=True)
                stage.rename(target)
                registry['templates'][identity] = item
                publish_json(self.root / 'registry.json', registry)
                return item
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def duplicate(self, identity):
        original = self.get(identity)
        return self.commit(archive=export_zip(self.directory(original)), active=False, title=original['title'] + ' · copie')

    def delete(self, identity):
        with self.locked():
            registry = self.registry()
            item = self.get(identity)
            if item['active']:
                raise DossierError(409, 'demo_active_template', 'Désactivez le modèle avant sa suppression.')
            registry['templates'].pop(identity)
            publish_json(self.root / 'registry.json', registry)
            # Retain immutable revisions for audit and recovery outside public listings.

    def inspect(self, identity):
        item = self.get(identity)
        directory = self.directory(item)
        return {**item, 'metadata': read_json_object(directory / 'metadata.json', 'invalid_metadata'),
                'artifacts': sorted(p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file())}
