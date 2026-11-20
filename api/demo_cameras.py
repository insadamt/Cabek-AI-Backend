"""Create repeatable camera samples from private side photographs.

Depends on the legacy confined sample reader and Pillow; zooms never upscale images.
"""

from io import BytesIO
import hashlib

from PIL import Image

from api.camera_registry import CAMERAS, ZOOM_CHANNELS
from api.errors import DossierError


class DemoCameras:
    def __init__(self, samples):
        self.samples = samples

    def readiness(self, phase):
        available = {}
        try:
            samples = self.samples.plan_capture(phase)
        except (DossierError, OSError):
            return available
        for sample in samples:
            if sample['side'] in available:
                continue
            try:
                self.samples.read_photo(sample['source'])
                available[sample['side']] = True
            except (DossierError, OSError):
                available[sample['side']] = False
        return available

    def plan(self, channels, phase):
        by_side = {}
        for sample in self.samples.plan_capture(phase):
            by_side.setdefault(sample["side"], sample["source"])
        result = []
        for channel in channels:
            name, role, side = CAMERAS[channel]
            if side not in by_side:
                raise DossierError(503, "demo_camera_unavailable", f"Photo de démonstration manquante : {name}.")
            result.append({"source": by_side[side], "side": side, "camera_channel": channel,
                           "camera_name": name, "camera_role": role})
        return result

    def read(self, sample):
        content, extension = self.samples.read_photo(sample["source"])
        if sample["camera_role"] == "zoom":
            peers = [ch for ch in ZOOM_CHANNELS if CAMERAS[ch][2] == sample["side"]]
            with Image.open(BytesIO(content)) as image:
                middle = max(1, image.width // 2)
                box = ((0, 0, middle, image.height) if sample["camera_channel"] == peers[0]
                       else (image.width - middle, 0, image.width, image.height))
                output = BytesIO()
                image.crop(box).convert("RGB").save(output, format="JPEG", quality=95)
                content, extension = output.getvalue(), ".jpg"
        return {"content": content, "extension": extension,
                "frame_id": "demo-" + hashlib.sha256(content).hexdigest()}
