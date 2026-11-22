"""Authenticate private demo administration without committing credentials.

Depends on scrypt, private secret/state files and file locks. Session tokens are
stored only as digests; password rotation invalidates every prior session.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from filelock import FileLock
from api.errors import DossierError
from scans.artifacts import publish_json

COOKIE = 'cabek_demo_admin'


def password_record(password):
    if not 8 <= len(password) <= 256:
        raise DossierError(422, 'invalid_password', 'Utilisez un mot de passe de 8 à 256 caractères.')
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {'salt': salt, 'digest': digest, 'epoch': secrets.token_hex(16)}


def save_secret(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    publish_json(path, record)
    path.chmod(0o600)


class AdministrationAuth:
    def __init__(self, root, secret):
        self.root, self.secret = Path(root), Path(secret)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'sessions.json'

    def record(self):
        try:
            value = json.loads(self.secret.read_text())
            if not all(isinstance(value[k], str) for k in ('salt', 'digest', 'epoch')):
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError):
            raise DossierError(503, 'demo_not_initialized', 'Initialisez le secret privé sur le poste.') from None

    def state(self):
        if not self.path.exists():
            return {'sessions': {}, 'failures': 0, 'blocked_until': 0}
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise DossierError(503, 'demo_auth_unavailable', 'Authentification indisponible.') from None

    def login(self, password):
        record = self.record()
        with FileLock(str(self.root / '.auth.lock'), timeout=10):
            state = self.state()
            if state['blocked_until'] > time.time():
                raise DossierError(429, 'demo_rate_limited', 'Trop de tentatives. Réessayez plus tard.')
            candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record['salt']), n=16384, r=8, p=1).hex()
            if not hmac.compare_digest(candidate, record['digest']):
                state['failures'] += 1
                if state['failures'] >= 5:
                    state['blocked_until'] = time.time() + min(900, 30 * 2 ** min(state['failures'] - 5, 5))
                publish_json(self.path, state)
                raise DossierError(401, 'demo_auth_required', 'Identifiants incorrects.')
            token = secrets.token_urlsafe(48)
            now = time.time()
            state['sessions'] = {key: val for key, val in state['sessions'].items() if val['expires'] > now and val['epoch'] == record['epoch']}
            state['sessions'][hashlib.sha256(token.encode()).hexdigest()] = {'expires': now + 1800, 'epoch': record['epoch']}
            state.update(failures=0, blocked_until=0)
            publish_json(self.path, state)
            return token

    def require(self, request):
        from api.settings_routes import require_local_settings
        require_local_settings(request)
        if request.method not in ('GET', 'HEAD') and request.headers.get('x-demo-admin') != '1':
            raise DossierError(403, 'demo_csrf', 'Requête privée invalide.')
        token = request.cookies.get(COOKIE, '')
        if not token:
            raise DossierError(401, 'demo_auth_required', 'Authentification requise.')
        with FileLock(str(self.root / '.auth.lock'), timeout=10):
            session = self.state()['sessions'].get(hashlib.sha256(token.encode()).hexdigest())
            if not session or session['expires'] <= time.time() or session['epoch'] != self.record()['epoch']:
                raise DossierError(401, 'demo_auth_required', 'Session expirée.')

    def logout(self, request):
        with FileLock(str(self.root / '.auth.lock'), timeout=10):
            state = self.state()
            state['sessions'].pop(hashlib.sha256(request.cookies.get(COOKIE, '').encode()).hexdigest(), None)
            publish_json(self.path, state)
