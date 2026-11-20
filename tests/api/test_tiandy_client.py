"""Check upstream validation and preview resource cleanup without network access.

Depends on in-memory JPEG responses and fake asynchronous HTTP streams.
"""

import asyncio
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from api.camera_routes import stream
from api.errors import DossierError
from api.tiandy_client import TiandyClient, TiandySettings


class ImageResponse(BytesIO):
    def __init__(self, content, *, age='0', frame_id='frame-1', media_type='image/jpeg'):
        super().__init__(content)
        self.headers = Message()
        self.headers['Content-Type'] = media_type
        self.headers['X-Frame-Id'] = frame_id
        self.headers['X-Frame-Age-Ms'] = age
        self.headers['X-Frame-Received-At'] = '2026-11-20T10:00:00+00:00'


def jpeg():
    output = BytesIO()
    Image.new('RGB', (12, 10), 'red').save(output, format='JPEG')
    return output.getvalue()


@pytest.mark.parametrize('options', [{'age': '6000'}, {'age': 'nan'}, {'frame_id': ''}, {'media_type': 'text/html'}])
def test_snapshot_rejects_unusable_frames(options):
    camera = TiandyClient(TiandySettings(key='test-private-key'))
    camera.open = lambda path: ImageResponse(jpeg(), **options)
    with pytest.raises(DossierError):
        camera.snapshot(4)


def test_snapshot_decodes_bytes_and_closes_upstream():
    camera = TiandyClient(TiandySettings(key='test-private-key'))
    response = ImageResponse(jpeg())
    camera.open = lambda path: response
    frame = camera.snapshot(4)
    assert frame['content'] == jpeg() and frame['frame_id'] == 'frame-1'
    assert response.closed


def test_preview_cancellation_closes_both_http_resources(monkeypatch):
    closed = []

    class Upstream:
        status_code = 200
        headers = {'content-type': 'multipart/x-mixed-replace; boundary=frame'}

        async def aiter_raw(self):
            yield b'frame-one'
            yield b'frame-two'

        async def aclose(self):
            closed.append('upstream')

    class HttpClient:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False

        def build_request(self, method, url, headers):
            assert headers == {'X-API-Key': 'private-stream-key'}

        async def send(self, request, stream):
            assert stream
            return Upstream()

        async def aclose(self):
            closed.append('client')

    monkeypatch.setattr('api.camera_routes.httpx.AsyncClient', HttpClient)
    service = SimpleNamespace(source=lambda: 'tiandy', client=TiandyClient(TiandySettings(key='private-stream-key')))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(capture_service=SimpleNamespace(cameras=service))),
                              is_disconnected=AsyncMock(return_value=False))

    async def cancel_preview():
        response = await stream(4, request)
        assert response.headers['content-type'] == 'multipart/x-mixed-replace; boundary=frame'
        assert 'private-stream-key' not in str(response.headers)
        assert await anext(response.body_iterator) == b'frame-one'
        await response.body_iterator.aclose()

    asyncio.run(cancel_preview())
    assert closed == ['upstream', 'client']
