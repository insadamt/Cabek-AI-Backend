"""Import validated native dossiers additively with conflict detection and rollback.

Depends on native schemas, bounded ZIP extraction, and root/dossier locks. Never
runs the destructive seed script or overwrites an existing dossier silently.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from filelock import FileLock
from api.errors import DossierError
from api.private_demo.bundles import copy_native, extract_zip, validate_native


def digest(directory):
    value = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if path.is_file() and not path.name.endswith('.lock'):
            value.update(path.relative_to(directory).as_posix().encode())
            if path.relative_to(directory).as_posix() == 'metadata.json':
                metadata = json.loads(path.read_text()); metadata.pop('imported_capture_resume', None)
                value.update(json.dumps(metadata, sort_keys=True).encode())
            else:
                value.update(path.read_bytes())
    return value.hexdigest()


def import_dossiers(source, destination, apply=False, identities=None):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cabek-import-') as temporary:
        source_root = Path(temporary) / 'source'
        if source.is_file():
            source_root.mkdir()
            extract_zip(source.read_bytes(), source_root)
        elif source.is_dir() and not source.is_symlink():
            source_root = source
        else:
            raise ValueError('Select an existing native directory or ZIP archive.')
        candidates = [source_root] if (source_root / 'metadata.json').is_file() else sorted(p for p in source_root.iterdir() if p.is_dir() and not p.name.startswith('.'))
        if identities:
            candidates = [path for path in candidates if path.name in identities]
            if {path.name for path in candidates} != set(identities):
                raise ValueError('Some requested dossier IDs are missing from the source.')
        if not candidates:
            raise ValueError('No native dossiers found.')
        prepared = []
        staging = destination / ('.import-' + uuid4().hex)
        staging.mkdir()
        published = []
        try:
            for candidate in candidates:
                stage = staging / candidate.name; stage.mkdir()
                copy_native(candidate, stage)
                metadata = validate_native(stage)
                identity = metadata['dossier_id']
                metadata['imported_capture_resume'] = False
                from scans.artifacts import publish_json
                publish_json(stage / 'metadata.json', metadata)
                if any(item['id'] == identity for item in prepared):
                    raise ValueError('Duplicate dossier IDs in import.')
                existing = destination / identity
                state = 'duplicate' if existing.exists() and digest(existing) == digest(stage) else 'conflict' if existing.exists() else 'new'
                prepared.append({'id':identity, 'state':state, 'stage':stage})
            if any(item['state'] == 'conflict' for item in prepared):
                raise ValueError('Conflicting dossiers: ' + ', '.join(i['id'] for i in prepared if i['state']=='conflict'))
            if apply:
                with FileLock(str(destination / '.import.lock'), timeout=15):
                    for item in prepared:
                        if item['state'] == 'duplicate':
                            continue
                        target = destination / item['id']
                        if target.exists():
                            raise ValueError('Dossier appeared during import; retry safely.')
                        item['stage'].rename(target); published.append(target)
            return {'applied':apply, 'dossiers':[{k:v for k,v in item.items() if k!='stage'} for item in prepared]}
        except Exception:
            for path in reversed(published):
                shutil.rmtree(path)
            raise
        finally:
            shutil.rmtree(staging)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path);parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--ids', nargs='+')
    args=parser.parse_args()
    print(json.dumps(import_dossiers(args.source,args.destination,args.apply,args.ids)))


if __name__ == '__main__':
    main()
