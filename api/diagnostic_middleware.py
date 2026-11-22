"""Record request outcomes and correlation IDs without headers, query strings or bodies.

Depends on the app-scoped diagnostic store; settings log polling is omitted unless
it fails so opening the viewer does not continually generate its own history.
"""
import re
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware


class DiagnosticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        store = request.app.state.diagnostic_logs
        path = request.url.path
        context = {'request_id': request_id}
        match = re.match(r'/api/dossiers/([^/]+)', path)
        if match:
            context['dossier_id'] = match[1]
        try:
            response = await call_next(request)
        except Exception as error:
            store.record('ERROR', 'api.http', type(error).__name__, code='unhandled_exception', **context)
            raise
        response.headers['X-Request-ID'] = request_id
        if path.startswith('/api/private-demo'):
            response.headers['Cache-Control'] = 'no-store'
        if response.status_code >= 400 or not path.startswith('/api/settings'):
            level = 'ERROR' if response.status_code >= 500 else 'WARNING' if response.status_code >= 400 else 'INFO'
            store.record(level, 'api.http', '%s %s · HTTP %s · %s ms' % (
                request.method, '/api/private-demo' if path.startswith('/api/private-demo') else path, response.status_code, round((time.monotonic() - started) * 1000)), **context)
        return response
