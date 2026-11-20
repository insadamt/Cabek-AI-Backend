"""Read bounded Tiandy responses without disclosing the upstream API key.

Depends on stdlib HTTP and the existing image decoder; no device is contacted at import.
"""

from dataclasses import dataclass, field
from io import BytesIO
import json
import math
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from fastapi import UploadFile
from starlette.datastructures import Headers

from api.errors import DossierError
from api.photo_service import MAX_UPLOAD_BYTES, read_validated_upload


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirect forbidden", headers, fp)


@dataclass(frozen=True)
class TiandySettings:
    url: str = "http://127.0.0.1:8001"
    key: str = field(default="", repr=False)
    timeout: float = 5
    max_age_seconds: float = 5

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Configure a direct Tiandy URL without credentials or query parameters.")
        if not all(math.isfinite(v) and 0 < v <= 30 for v in (self.timeout, self.max_age_seconds)):
            raise ValueError("Tiandy timeouts and frame age must be positive and at most 30 seconds.")

    @classmethod
    def from_environment(cls):
        return cls(os.getenv("CABEK_TIANDY_API_URL", "http://127.0.0.1:8001"),
                   os.getenv("CABEK_TIANDY_API_KEY", ""))


class TiandyClient:
    def __init__(self, settings):
        self.settings = settings
        self.opener = build_opener(ProxyHandler({}), RejectRedirects())

    def open(self, path):
        if not self.settings.key:
            raise DossierError(503, "camera_not_configured", "La connexion Tiandy n’est pas configurée.")
        request = Request(self.settings.url.rstrip("/") + path,
                          headers={"X-API-Key": self.settings.key, "Accept": "*/*"})
        try:
            return self.opener.open(request, timeout=self.settings.timeout)
        except HTTPError as error:
            status = error.code
            error.close()
            code = "camera_auth_failed" if status in (401, 403) else "camera_unavailable"
            raise DossierError(503, code, "Le service caméra Tiandy refuse la requête ou ne fournit aucune image.") from None
        except OSError:
            raise DossierError(503, "camera_unavailable", "Le service caméra Tiandy est injoignable.") from None

    def cameras(self):
        try:
            with self.open("/api/cameras") as response:
                raw = response.read(131073)
            if len(raw) > 131072:
                raise ValueError()
            payload = json.loads(raw)
            result = {}
            for item in payload["cameras"]:
                if (type(item["channel"]) is not int or type(item["online"]) is not bool
                        or not isinstance(item.get("name"), str)):
                    raise ValueError()
                result[item["channel"]] = item
            return result
        except (OSError, ValueError, KeyError, TypeError):
            raise DossierError(502, "camera_response_invalid", "Réponse Tiandy invalide.") from None

    def snapshot(self, channel):
        try:
            with self.open(f"/api/cameras/{channel}/latest.jpg") as response:
                headers = response.headers
                frame_id = headers.get("X-Frame-Id")
                received = headers.get("X-Frame-Received-At")
                age = float(headers.get("X-Frame-Age-Ms", "nan"))
                if (not frame_id or len(frame_id) > 200 or not received
                        or not math.isfinite(age) or not 0 <= age <= self.settings.max_age_seconds * 1000):
                    raise ValueError()
                content = response.read(MAX_UPLOAD_BYTES + 1)
                media_type = headers.get_content_type()
            validated, extension = read_validated_upload(UploadFile(
                file=BytesIO(content), filename="camera.jpg", headers=Headers({"content-type": media_type})))
            return {"content": validated, "extension": extension, "frame_id": frame_id,
                    "received_at": received, "age_ms": age}
        except (OSError, ValueError, TypeError):
            raise DossierError(503, "camera_frame_invalid", "Image caméra absente, périmée ou invalide.") from None

    def fresh_snapshot(self, channel, baseline, stopping):
        deadline = time.monotonic() + self.settings.timeout
        while not stopping.is_set():
            frame = self.snapshot(channel)
            if frame["frame_id"] != baseline:
                return frame
            if time.monotonic() >= deadline:
                break
            stopping.wait(.1)
        raise DossierError(503, "camera_frame_stale", "La caméra ne fournit aucune nouvelle image.")
