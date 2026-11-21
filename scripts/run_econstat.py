"""Launch the unchanged sibling E-Constat OCR service with backend-owned secrets.

Depends on the OCR repository's virtualenv and uvicorn. Reads the Cabek .env,
keeps models under ignored .local, and binds only to the local host.
"""
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / '.env', override=False)
    token = os.environ.get('CABEK_ECONSTAT_API_TOKEN', '')
    if not token:
        raise SystemExit('Configure CABEK_ECONSTAT_API_TOKEN in the backend .env first.')
    source = ROOT.parent / 'Cabek-EConstat-OCR'
    interpreter = source / '.venv/bin/python'
    if not interpreter.is_file():
        raise SystemExit('Install the sibling OCR requirements into its .venv first (CPU torch recommended).')
    endpoint = urlsplit(os.environ.get('CABEK_ECONSTAT_API_URL', 'http://127.0.0.1:8002'))
    if endpoint.hostname not in ('127.0.0.1', 'localhost') or endpoint.scheme != 'http':
        raise SystemExit('This launcher supports local HTTP OCR only; launch remote deployments separately.')
    environment = dict(os.environ, ECONSTAT_API_TOKEN=token,
                       EASYOCR_MODULE_PATH=str(ROOT / '.local/easyocr'),
                       OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4')
    result = subprocess.run([str(interpreter), '-m', 'uvicorn', 'api:app', '--host', '127.0.0.1',
                             '--port', str(endpoint.port or 8002)], cwd=source, env=environment)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
