"""Prepare four PDF-backed expertise dossiers, then optionally replace the idle dataset.

Depends on PyMuPDF, the local OCR service and the real vehicle catalog when available.
Preparation never touches active dossiers. Activation requires a stopped backend,
archives its exact working data under ignored .local, and rolls back a failed rename.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf
from api.config import ApiSettings
from api.econstat_documents import read_document, first_page_preview
from api.econstat_service import EconstatService
from api.errors import DossierError
from api.repository import DossierMetadata, DossierRepository
from api.service import DossierService
from api.vehicle_catalog import VehicleCatalog
from scans.artifacts import publish_json

CASES = (
    ('892246', 'CONSTAT.pdf', 'A', '84547-H-06', [1, 12]),
    ('892323', 'recap5295.pdf', 'B', '11168-B-72', [2]),
    ('875915', 'recap3651.pdf', 'A', '97131-A-13', [2, 5]),
    ('895671', 'recap6616.pdf', 'A', '90340-E-06', [1, 12]),
)


def selected_vehicle_metadata(source, reference, side, registration):
    with pymupdf.open(source) as pdf:
        page = pdf[0]
        text = page.get_text()
        if reference not in text:
            raise ValueError('Unexpected document reference')
        words = [{'text': w[4], 'x': (w[0]+w[2])/2/page.rect.width,
                  'y': (w[1]+w[3])/2/page.rect.height} for w in page.get_text('words')]
    selected = [w for w in words if (w['x'] < .34 if side == 'A' else w['x'] > .66)]
    if registration not in [w['text'] for w in selected if .21 < w['y'] < .23]:
        raise ValueError('Registration does not belong to selected vehicle')

    def field_at(y):
        line = sorted([w for w in selected if abs(w['y']-y) < .003 and
                       (w['x'] > .11 if side == 'A' else True) and
                       re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]*', w['text'])], key=lambda w: w['x'])
        value = ' '.join(w['text'] for w in line)
        if not value:
            raise ValueError('Missing vehicle field')
        return value

    accident = re.search(r"Date de l'accident, le\s+(\d{4}-\d{2}-\d{2})", text)
    if accident is None:
        raise ValueError('Missing accident date')
    return {'marque': field_at(.204), 'modele': field_at(.1905),
            'matricule': registration, 'date_sinistre': accident.group(1),
            'annee': None, 'date_mise_en_circulation': None, 'marque_id': None, 'model_id': None}


def resolve_catalog_ids(vehicles):
    catalog = VehicleCatalog()
    try:
        brands = catalog.brands()
    except DossierError:
        print('Catalog unavailable: retaining extracted labels and null IDs.')
        return
    models = {}
    for vehicle in vehicles:
        matches = [b for b in brands if b.libelle.casefold().strip() == vehicle['marque'].casefold().strip()]
        if len(matches) != 1:
            continue
        brand = matches[0]
        if brand.id not in models:
            try: models[brand.id] = catalog.models(brand.id)
            except DossierError: models[brand.id] = []
        candidates = [m for m in models[brand.id] if m.libelle.casefold().strip() == vehicle['modele'].casefold().strip()]
        if len(candidates) == 1:
            model = candidates[0]
            vehicle.update(marque_id=brand.id, model_id=model.id, marque=brand.libelle, modele=model.libelle)


def validate_stage(stage, settings):
    repository = DossierRepository(stage)
    service = EconstatService(repository, settings.econstat)
    try:
        expected = {f'DOS-ECONSTAT-{reference}-{side}' for reference, _, side, _, _ in CASES}
        if set(repository.dossier_ids()) != expected:
            raise ValueError('Expected exactly the four approved dossiers')
        for reference, _, side, registration, channels in CASES:
            dossier_id = f'DOS-ECONSTAT-{reference}-{side}'
            dossier = repository.read_dossier(dossier_id)
            detail = DossierService(repository).read_result(dossier_id)
            if (dossier.metadata.photos or dossier.metadata.captures or detail.result is not None
                    or detail.status != 'not_analyzed' or detail.workflow_status != 'awaiting_photos'
                    or detail.vehicle.matricule != registration):
                raise ValueError('Dossier must be empty and awaiting photographs')
            prepared = service.describe(dossier_id)
            if (prepared['status'] != 'ready' or prepared['recommended_zoom_channels'] != channels
                    or prepared['vehicle_reference'] != f'vehicle_{side}'):
                raise ValueError(f'Incorrect OCR result for {dossier_id}')
            first_page_preview(read_document(repository, dossier_id))
    finally:
        service.close()


def prepare(source_directory, settings):
    now = datetime.now(timezone.utc)
    stage = ROOT / '.local' / f'econstat-seed-{now:%Y%m%d-%H%M%S}'
    stage.mkdir(parents=True)
    vehicles = [selected_vehicle_metadata(source_directory/name, ref, side, plate)
                for ref, name, side, plate, _ in CASES]
    resolve_catalog_ids(vehicles)
    for (reference, filename, side, _, _), vehicle in zip(CASES, vehicles):
        dossier_id = f'DOS-ECONSTAT-{reference}-{side}'
        folder = stage/dossier_id
        (folder/'documents').mkdir(parents=True)
        shutil.copy2(source_directory/filename, folder/'documents/econstat.pdf')
        metadata = {'dossier_id': dossier_id, 'type': 'forfait_expertise', 'source': 'external',
                    'created_at': now.isoformat(), 'updated_at': now.isoformat(),
                    'photos': [], 'captures': {}, 'workflow_status': 'awaiting_photos',
                    'econstat': {'path': 'documents/econstat.pdf', 'original_filename': filename}, **vehicle}
        DossierMetadata.model_validate(metadata)
        publish_json(folder/'metadata.json', metadata)
    service = EconstatService(DossierRepository(stage), settings.econstat)
    try:
        for reference, _, side, _, _ in CASES:
            dossier_id = f'DOS-ECONSTAT-{reference}-{side}'
            result = service.start_analysis(dossier_id)
            deadline = time.monotonic() + settings.econstat.timeout + 15
            while result['status'] == 'processing' and time.monotonic() < deadline:
                time.sleep(.25)
                result = service.describe(dossier_id)
            if result['status'] != 'ready':
                raise ValueError(f'Preparation failed for {dossier_id}: {result["message"]}')
            print(dossier_id, result['recommended_zoom_channels'], flush=True)
    finally:
        service.close()
    validate_stage(stage, settings)
    print('Validated staging directory:', stage, flush=True)
    return stage


def activate(stage, settings, backend_port):
    stage = stage.resolve()
    if stage.parent != (ROOT/'.local').resolve() or not stage.name.startswith('econstat-seed-'):
        raise ValueError('Activation requires a staged directory from this script')
    with socket.socket() as connection:
        if connection.connect_ex(('127.0.0.1', backend_port)) == 0:
            raise ValueError('Stop the backend and its capture/scan workers before activation')
    validate_stage(stage, settings)
    active = settings.dossiers_dir.resolve()
    for path in active.glob('*/metadata.json'):
        value = json.loads(path.read_text())
        if any(c.get('status') == 'capturing' for c in value.get('captures', {}).values()):
            raise ValueError('An existing capture has not stopped')
    for path in active.glob('*/analysis/status.json'):
        if json.loads(path.read_text()).get('status') in ('queued', 'processing'):
            raise ValueError('An existing scan has not stopped')
    backup = ROOT/'.local/dossier-backups'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True)
    active.rename(backup/'dossiers')
    try:
        stage.rename(active)
    except OSError:
        (backup/'dossiers').rename(active)
        raise
    print('Archived existing dataset:', backup/'dossiers')
    print('Activated four empty expertise dossiers:', active)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path.home()/'Downloads')
    parser.add_argument('--activate', type=Path, help='Activate a previously validated staging directory')
    parser.add_argument('--backend-port', type=int, default=9000)
    args = parser.parse_args()
    settings = ApiSettings.from_environment()
    if args.activate:
        activate(args.activate, settings, args.backend_port)
    else:
        prepare(args.source_dir, settings)
