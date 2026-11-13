"""Configure the optional local lighting owner without changing firmware settings.

Depends on environment configuration; bearer tokens stay on the tunnel host and
are separate from pricing credentials and browser configuration.
"""

from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class LightingSettings:
    enabled: bool = False
    port: str = "auto"
    baud: int = 115200
    token: str = field(default="", repr=False)
    maintenance_token: str = field(default="", repr=False)
    timeout: float = 1.0
    reconnect_seconds: float = 2.0
    health_seconds: float = 5.0

    def __post_init__(self):
        if self.baud <= 0 or not all(0 < value <= 60 for value in (
            self.timeout, self.reconnect_seconds, self.health_seconds,
        )):
            raise ValueError("Lighting baud and bounded time intervals must be positive.")
        if self.enabled and (len(self.token) < 32 or len(self.maintenance_token) < 32
                             or self.token == self.maintenance_token):
            raise ValueError("Lighting requires two distinct bearer tokens of at least 32 characters.")

    @classmethod
    def from_environment(cls):
        return cls(
            enabled=os.getenv("CABEK_LIGHTS_ENABLED", "0") == "1",
            port=os.getenv("CABEK_LIGHTS_PORT", "auto"),
            baud=int(os.getenv("CABEK_LIGHTS_BAUD", "115200")),
            token=os.getenv("CABEK_LIGHTS_TOKEN", ""),
            maintenance_token=os.getenv("CABEK_LIGHTS_MAINTENANCE_TOKEN", ""),
            timeout=float(os.getenv("CABEK_LIGHTS_TIMEOUT", "1")),
            reconnect_seconds=float(os.getenv("CABEK_LIGHTS_RECONNECT_SECONDS", "2")),
            health_seconds=float(os.getenv("CABEK_LIGHTS_HEALTH_SECONDS", "5")),
        )
