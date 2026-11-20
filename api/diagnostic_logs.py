"""Keep bounded, redacted local diagnostics without retaining request bodies.

Depends on rotating stdlib logging and confined dossier reads. Disk failures fall
back to memory so diagnostics never replace the application's original error.
"""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from threading import RLock
from uuid import uuid4

from api.files import confined_path


def redact(value):
    text = str(value)
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')) and len(secret) >= 4:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s\"\',;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:token|password|secret|api[_-]?key|authorization)[\"\']?\s*[:=]\s*[\"\']?)[^\s\"\'&,;]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', text)
    return text[:4096]


class CaptureLogHandler(logging.Handler):
    def __init__(self, store):
        super().__init__(logging.INFO)
        self.store = store

    def emit(self, record):
        details = {key: getattr(record, key) for key in
                   ('code', 'request_id', 'dossier_id', 'capture_id', 'run_id') if hasattr(record, key)}
        message = record.getMessage()
        if record.exc_info and record.exc_info[0]:
            message += ' · ' + record.exc_info[0].__name__
        self.store.record(record.levelname, record.name, message, **details)


class DiagnosticFileHandler(RotatingFileHandler):
    def __init__(self, path, store, max_bytes):
        super().__init__(path, maxBytes=max_bytes, backupCount=4, encoding='utf-8')
        self.store = store

    def handleError(self, record):
        # Logging errors must not recurse or mask the application's original failure.

        self.store.disk_available = False


class DiagnosticLogStore:
    def __init__(self, root, repository, max_bytes=5 * 1024 * 1024):
        self.root = root / 'logs'
        self.repository = repository
        self.lock = RLock()
        self.memory = deque(maxlen=1000)
        self.disk_available = True
        self.writer = None
        self.capture_handler = CaptureLogHandler(self)
        self.previous_levels = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.writer = DiagnosticFileHandler(self.root / 'application.jsonl', self, max_bytes)
            self.writer.setFormatter(logging.Formatter('%(message)s'))
        except OSError:
            self.disk_available = False

    def start(self):
        for name in ('api', 'scans', 'uvicorn.error'):
            logger = logging.getLogger(name)
            self.previous_levels[name] = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(self.capture_handler)
        self.record('INFO', 'application', 'Application démarrée')

    def close(self):
        self.record('INFO', 'application', 'Application arrêtée')
        for name, level in self.previous_levels.items():
            logger = logging.getLogger(name)
            logger.removeHandler(self.capture_handler)
            logger.setLevel(level)
        if self.writer:
            self.writer.close()

    def record(self, level, component, message, **details):
        entry = {'id': uuid4().hex, 'timestamp': datetime.now(timezone.utc).isoformat(),
                 'level': level, 'source': 'backend', 'component': component,
                 'message': redact(message), **{key: redact(value) for key, value in details.items()}}
        with self.lock:
            self.memory.append(entry)
            if self.writer:
                try:
                    self.writer.handle(logging.LogRecord('diagnostics', logging.INFO, '', 0,
                                                        json.dumps(entry, ensure_ascii=False), (), None))
                except OSError:
                    self.disk_available = False

    def read(self, *, limit=100, cursor=None, level=None, component=None, query=None,
             dossier_id=None, run_id=None, since=None, until=None):
        with self.lock:
            entries = []
            seen = set()
            candidates = list(reversed(self.memory))
            for suffix in ('', '.1', '.2', '.3', '.4'):
                path = self.root / ('application.jsonl' + suffix)
                try:
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size - 5 * 1024 * 1024))
                        lines = stream.read(5 * 1024 * 1024).decode('utf-8', errors='replace').splitlines()
                    for line in reversed(lines):
                        try:
                            value = json.loads(line)
                            if isinstance(value, dict) and 'id' in value:
                                candidates.append(value)
                        except ValueError:
                            continue
                except OSError:
                    continue
            passed_cursor = cursor is None
            for entry in candidates:
                if entry['id'] in seen:
                    continue
                seen.add(entry['id'])
                if not passed_cursor:
                    passed_cursor = entry['id'] == cursor
                    continue
                if level and entry.get('level') != level:
                    continue
                if component and component not in entry.get('component', ''):
                    continue
                if dossier_id and entry.get('dossier_id') != dossier_id:
                    continue
                if run_id and entry.get('run_id') != run_id:
                    continue
                entry_time = datetime.fromisoformat(entry['timestamp'])
                if since and entry_time < since or until and entry_time > until:
                    continue
                if query and query.casefold() not in json.dumps(entry, ensure_ascii=False).casefold():
                    continue
                entries.append({key: redact(value) if isinstance(value, str) else value for key, value in entry.items()})
                if len(entries) > limit:
                    break
            return {'entries': entries[:limit], 'next_cursor': entries[limit - 1]['id'] if len(entries) > limit else None,
                    'disk_available': self.disk_available}

    def read_worker(self, dossier_id):
        dossier = self.repository.read_dossier(dossier_id)
        path = confined_path(dossier.directory, 'analysis/.worker.log')
        try:
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                content = stream.read(65536).decode('utf-8', errors='replace')
            return {'entries': [{'id': f'worker-{index}', 'timestamp': datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc).isoformat(), 'source': 'worker', 'level': 'INFO',
                'component': 'scans.worker', 'dossier_id': dossier_id, 'message': redact(line)}
                for index, line in enumerate(content.splitlines()[-500:])], 'next_cursor': None,
                'disk_available': self.disk_available}
        except FileNotFoundError:
            return {'entries': [], 'next_cursor': None, 'disk_available': self.disk_available}
