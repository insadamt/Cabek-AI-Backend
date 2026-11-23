"""Bound multipart request bodies before Starlette parses or spools uploaded files.

Depends on ASGI and Starlette responses. Per-image limits remain in PhotoService;
this additional cap includes multipart overhead and applies without Content-Length.
"""

from starlette.responses import JSONResponse


MAX_MULTIPART_BYTES = 11 * 1024 * 1024


class UploadLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        if scope["type"] != "http" or not headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data"):
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        private_bundle = path == '/api/private-demo/import' or (path.startswith('/api/private-demo/templates/') and path.endswith('/editor'))
        limit = 129 * 1024 * 1024 if private_bundle else MAX_MULTIPART_BYTES
        content = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            if len(content) + len(body) > limit:
                await JSONResponse(status_code=413, content={"error": {
                    "code": "upload_too_large", "message": "Multipart request exceeds the allowed size.",
                }})(scope, receive, send)
                return
            content.extend(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(content), "more_body": False}

        await self.app(scope, replay, send)
