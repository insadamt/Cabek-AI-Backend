"""Select demo/live cameras and freeze per-inspection camera plans.

Depends on the commissioned registry, private demo storage and the Tiandy client.
"""

from api.camera_registry import CAMERAS, ESSENTIAL_CHANNELS, camera_identity, validate_zoom_channels
from api.demo_cameras import DemoCameras
from api.errors import DossierError
from api.tiandy_client import TiandyClient


class CameraService:
    def __init__(self, captures):
        self.captures = captures
        self.client = TiandyClient(captures.settings.tiandy)
        self.demo = DemoCameras(captures.provider)

    def source(self):
        if self.captures.settings.tunnel_mode == "disabled":
            return "disabled"
        mock = getattr(self.captures, "mock_results", None)
        if mock and mock.read()["enabled"]:
            return "demo" if self.captures.settings.tunnel_mode == "mock" else "tiandy"
        store = self.captures.operation_settings
        demo = store.read()["demo_mode"] if store else self.captures.settings.tunnel_mode == "mock"
        return "demo" if demo else "tiandy"

    def catalog(self, phase=None, root_path=""):
        source = self.source()
        upstream, error = {}, None
        if source == "tiandy":
            try:
                upstream = self.client.cameras()
            except DossierError as failure:
                error = failure.message
        cameras = []
        demo_ready = self.demo.readiness(phase) if source == 'demo' else {}
        for channel in CAMERAS:
            item = camera_identity(channel, upstream.get(channel, {}).get("name"))
            item.update(online=False, error_message=error, resolution=None)
            if source == "demo" and item["role"] != "diagnostic":
                item['online'] = demo_ready.get(item['side'], False)
                if not item['online']:
                    item["error_message"] = "Photo de démonstration indisponible."
            elif source == "tiandy":
                item["online"] = upstream.get(channel, {}).get("online", False)
                item["resolution"] = upstream.get(channel, {}).get("resolution")
            base = f"{root_path.rstrip('/')}/api/cameras/{channel}"
            suffix = f"?phase={phase}" if phase else ""
            item.update(stream_url=base + "/stream" + suffix, latest_image_url=base + "/latest.jpg" + suffix)
            cameras.append(item)
        return {"source": source, "cameras": cameras}

    def plan(self, zoom_channels, phase, source=None):
        channels = [*ESSENTIAL_CHANNELS, *validate_zoom_channels(zoom_channels)]
        source = source or self.source()
        if source == "demo":
            return source, self.demo.plan(channels, phase)
        if source != "tiandy":
            raise DossierError(503, "tunnel_unavailable", "La capture est désactivée.")
        cameras = self.client.cameras()
        samples = []
        for channel in channels:
            item = camera_identity(channel, cameras.get(channel, {}).get("name"))
            if not cameras.get(channel, {}).get("online"):
                raise DossierError(503, "camera_unavailable", f"Caméra indisponible : {item['name']} (CH{channel}).")
            samples.append({"source": f"camera-{channel}.jpg", "side": item["side"],
                            "camera_channel": channel, "camera_name": item["name"], "camera_role": item["role"]})
        return source, samples

    def baseline(self, capture):
        if capture.get("capture_source") != "tiandy":
            return {}
        baselines = {}
        for sample in capture['samples']:
            try:
                baselines[sample['camera_channel']] = self.client.snapshot(sample['camera_channel'])['frame_id']
            except DossierError as error:
                raise DossierError(error.status_code, error.code,
                                   f"{sample['camera_name']} (CH{sample['camera_channel']}) : {error.message}") from None
        return baselines

    def read(self, capture, sample, baselines, stopping):
        if capture["capture_source"] == "demo":
            return self.demo.read(sample)
        try:
            return self.client.fresh_snapshot(sample["camera_channel"], baselines[sample["camera_channel"]], stopping)
        except DossierError as error:
            raise DossierError(error.status_code, error.code,
                               f"{sample['camera_name']} (CH{sample['camera_channel']}) : {error.message}") from None
