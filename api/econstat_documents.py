"""Read confined PDFs and cache only their first-page WebP preview.

Depends on the dossier repository, PyMuPDF and atomic publication. Original PDFs
are private attachments and never enter the photograph manifest.
"""
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
from io import BytesIO
from PIL import Image
from api.errors import DossierError
from api.files import confined_path, normalize_relative_path
from scans.artifacts import publish_bytes
from api.econstat_contracts import normalize_registration

MAX_PDF_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class EconstatDocument:
    dossier_id: str
    filename: str
    registration: str
    version: str
    content: bytes
    directory: object

    @property
    def cache_directory(self):
        return confined_path(self.directory, f'.econstat/{self.version}')


def read_document(repository, dossier_id):
    dossier = repository.read_dossier(dossier_id)
    attachment = dossier.metadata.econstat
    if attachment is None:
        raise DossierError(404, 'econstat_not_found', 'Aucun E-Constat joint à ce dossier.')
    try:
        relative = normalize_relative_path(attachment.path)
        parts = PurePosixPath(relative).parts
        if len(parts) < 2 or parts[0] != 'documents' or PurePosixPath(relative).suffix.lower() != '.pdf':
            raise ValueError('Invalid attachment location')
        path = confined_path(dossier.directory, relative)
        with path.open('rb') as stream:
            content = stream.read(MAX_PDF_BYTES + 1)
        # Some mPDF exports include a short graphics preamble before the PDF header.

        if not content or len(content) > MAX_PDF_BYTES or b'%PDF-' not in content[:1024]:
            raise ValueError('Invalid PDF')
        registration = dossier.metadata.matricule
        version = hashlib.sha256(content + b'\0' + normalize_registration(registration).encode()).hexdigest()
        filename = attachment.original_filename.replace('\\', '/').split('/')[-1]
        return EconstatDocument(dossier_id, filename, registration, version, content, dossier.directory)
    except (OSError, ValueError, RuntimeError):
        raise DossierError(422, 'invalid_econstat', 'E-Constat absent ou illisible. Sélectionnez les caméras manuellement.') from None


def first_page_preview(document):
    target = confined_path(document.directory, f'.econstat/{document.version}/page-1.webp')
    if target.is_file():
        return target
    try:
        import pymupdf
        with pymupdf.open(stream=document.content, filetype='pdf') as pdf:
            if pdf.is_encrypted or not len(pdf):
                raise ValueError('Unreadable PDF')
            page = pdf[0]
            if min(page.rect.width, page.rect.height) <= 0:
                raise ValueError('Invalid page geometry')
            scale = min(3, 2400 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
            image = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
            output = BytesIO()
            image.save(output, 'WEBP', quality=90)
            publish_bytes(target, output.getvalue())
        return target
    except Exception:
        raise DossierError(422, 'invalid_econstat', 'La première page de l’E-Constat ne peut pas être affichée.') from None
