"""Validate and safely transfer native dossier bundles, never executing embedded files.

Depends on ZIP, native metadata/result validators and bounded file inspection.
Symbolic links, active worker state, traversal and duplicate archive paths fail closed.
"""
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile
from api.errors import DossierError
from api.files import DOSSIER_ID_PATTERN, validate_image, validate_pdf, read_json_object
from api.repository import DossierRepository
from pydantic import ValidationError

MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 5000


def allowed_path(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(p in ('', '.', '..') for p in name.split('/'))):
        raise DossierError(422, 'unsafe_bundle', 'Chemin de fichier invalide.')
    if path.suffix.lower() not in ('.json', '.png', '.jpg', '.jpeg', '.pdf', '.webp', '.log', '.txt', '.csv'):
        raise DossierError(422, 'unsafe_bundle', 'Type de fichier non autorisé.')
    return path


def extract_zip(content, destination):
    total = 0
    seen = set()
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if len(archive.infolist()) > MAX_FILES:
                raise ValueError()
            for item in archive.infolist():
                if item.is_dir():
                    continue
                path = allowed_path(item.filename)
                mode = item.external_attr >> 16
                total += item.file_size
                if (str(path) in seen or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                        or total > MAX_BYTES or item.flag_bits & 1):
                    raise ValueError()
                seen.add(str(path))
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError):
        raise DossierError(422, 'invalid_bundle', 'Archive invalide, trop volumineuse ou non sûre.') from None


def copy_native(source, destination):
    total = 0
    count = 0
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise DossierError(422, 'unsafe_bundle', 'Les liens symboliques sont interdits.')
        if path.is_dir():
            continue
        if path.name.endswith('.lock'):
            continue
        allowed_path(relative.as_posix())
        if not path.is_file():
            raise DossierError(422, 'unsafe_bundle', 'Fichier spécial interdit.')
        total += path.stat().st_size
        count += 1
        if total > MAX_BYTES or count > MAX_FILES:
            raise DossierError(413, 'bundle_too_large', 'Dossier trop volumineux.')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def validate_attachment_pdf(path):
    # Native E-Constat exports can contain a short mPDF graphics preamble.
    import pymupdf
    from api.econstat_documents import MAX_PDF_BYTES
    try:
        with path.open('rb') as stream:
            content = stream.read(MAX_PDF_BYTES + 1)
        if len(content) > MAX_PDF_BYTES or b'%PDF-' not in content[:1024]:
            raise ValueError()
        with pymupdf.open(stream=content, filetype='pdf') as document:
            if document.is_encrypted or not len(document):
                raise ValueError()
    except Exception:
        raise DossierError(422, 'invalid_econstat', 'E-Constat absent, chiffré ou illisible.') from None


def validate_native(directory, complete=False):
    metadata = read_json_object(directory / 'metadata.json', 'invalid_metadata')
    if any(c.get('status') in ('capturing', 'queued', 'processing') for c in metadata.get('captures', {}).values()):
        raise DossierError(409, 'active_bundle', 'Arrêtez les captures avant l’import.')
    dossier_id = metadata.get('dossier_id', '')
    if not isinstance(dossier_id, str) or not DOSSIER_ID_PATTERN.fullmatch(dossier_id):
        raise DossierError(422, 'invalid_dossier_id', 'Identifiant dossier invalide.')
    repository = DossierRepository(directory.parent)
    from api.repository import DossierMetadata, DossierRecord
    try:
        model = DossierMetadata.model_validate(metadata)
    except ValidationError:
        raise DossierError(422, 'invalid_metadata', 'Métadonnées natives invalides.') from None
    try:
        dossier = DossierRecord(directory, model, repository.photo_manifest(directory, model))
    except ValueError:
        raise DossierError(422, 'invalid_photo_manifest', 'Utilisez des identifiants photo uniques et des chemins natifs.') from None
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise DossierError(422, 'unsafe_bundle', 'Lien symbolique interdit.')
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix in ('.jpg', '.jpeg', '.png'):
                from fastapi import UploadFile
                from starlette.datastructures import Headers
                from api.files import IMAGE_MEDIA_TYPES
                from api.photo_service import read_validated_upload
                with path.open('rb') as stream:
                    read_validated_upload(UploadFile(file=stream, filename=path.name,
                        headers=Headers({'content-type': IMAGE_MEDIA_TYPES[suffix]})))
            elif suffix == '.pdf':
                if path.relative_to(directory).parts[0] == 'documents':
                    validate_attachment_pdf(path)
                else:
                    validate_pdf(path)
            elif suffix == '.json':
                value = json.loads(path.read_text())
                if path.name == 'status.json' and isinstance(value, dict) and value.get('status') in ('capturing', 'queued', 'processing'):
                    raise DossierError(409, 'active_bundle', 'Arrêtez les travaux avant l’import.')
    for photo in model.photos:
        from api.files import confined_path
        validate_image(confined_path(directory, photo.path))
    if model.econstat:
        from api.files import confined_path
        attachment = allowed_path(model.econstat.path)
        if attachment.parts[0] != 'documents' or attachment.suffix.lower() != '.pdf':
            raise DossierError(422, 'invalid_econstat', 'Placez le PDF dans documents/.')
        validate_attachment_pdf(confined_path(directory, model.econstat.path))
    if model.type == 'verification':
        if (directory / 'analysis/result.json').exists():
            raise DossierError(422, 'verification_ai_forbidden', 'La vérification ne contient aucun résultat IA.')
        if complete:
            for phase in ('before', 'after'):
                if {p.side for p in model.photos if p.phase == phase} != {'front', 'rear', 'left', 'right'}:
                    raise DossierError(422, 'demo_incomplete', 'Quatre côtés sont requis avant et après.')
    elif (directory / 'analysis/result.json').exists() or complete:
        analysis = repository.read_analysis(dossier)
        if analysis is None:
            raise DossierError(422, 'demo_incomplete', 'Résultats natifs et annotations requis.')
        from PIL import Image
        from api.files import confined_path
        for photo in analysis.photos:
            with Image.open(confined_path(directory, photo.original_path)) as original:
                size = original.size
            for name in ('parts_path', 'damages_path', 'critical_path'):
                with Image.open(confined_path(directory, getattr(photo, name))) as annotation:
                    if annotation.size != size:
                        raise DossierError(422, 'annotation_alignment', 'Les annotations doivent avoir les dimensions de l’original.')
    if complete and not model.photos:
        raise DossierError(422, 'demo_incomplete', 'Photographies requises.')
    return metadata


def export_zip(directory):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in directory.rglob('*'):
            if path.is_file() and not path.name.endswith('.lock'):
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()
