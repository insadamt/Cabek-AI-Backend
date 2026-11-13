"""Run the persistent serial bridge and resynchronize desired state after failures.

Depends on LightingState, SerialLightingLink and an injected application readiness
probe. Hardware I/O runs on one daemon worker and never blocks capture/HTTP work.
"""

import logging
from threading import Event, Thread
import time

from api.lighting_protocol import SerialLightingLink
from api.lighting_state import LightingState, timestamp


logger = logging.getLogger(__name__)


class LightingService:
    def __init__(self, settings, link=None, clock=time.monotonic):
        self.settings = settings
        self.state = LightingState(clock)
        self.link = link or SerialLightingLink(settings)
        self.clock = clock
        self.stop_event = Event()
        self.thread = None
        self.readiness_thread = None
        self.connected = False
        self.firmware = None
        self.acknowledged_color = None
        self.last_successful_command = None
        self.last_error = None
        self.last_seen_at = None
        self.next_connect = 0
        self.next_health = 0
        self.readiness_probe = None
        self.next_readiness = 0

    def start(self):
        if not self.settings.enabled or self.thread is not None:
            return
        with self.state.lock:
            self.state.transition("BOOTING")
        self.thread = Thread(target=self.run, name="cabek-lighting", daemon=True)
        self.thread.start()
        self.readiness_thread = Thread(target=self.monitor_readiness, name="cabek-lighting-health", daemon=True)
        self.readiness_thread.start()

    def initialize(self):
        with self.state.lock:
            if self.settings.enabled:
                self.state.transition("INITIALIZING")

    def set_readiness_probe(self, readiness_probe):
        with self.state.lock:
            self.readiness_probe = readiness_probe
            self.next_readiness = 0

    def run(self):
        try:
            while not self.stop_event.is_set():
                self.synchronize()
                self.stop_event.wait(0.025)
        finally:

            # Shutdown OFF is best effort; unplugged hardware must not hold up exit.

            try:
                if self.connected:
                    self.link.set_color("OFF")
            except Exception:
                logger.warning("Could not acknowledge shutdown OFF")
            self.disconnect()

    def monitor_readiness(self):

        # Image/storage checks must never delay a capture's serial color command.

        while not self.stop_event.is_set():
            self.check_readiness()
            self.stop_event.wait(0.1)

    def check_readiness(self):
        if self.readiness_probe is None or self.clock() < self.next_readiness:
            return
        self.next_readiness = self.clock() + self.settings.health_seconds
        try:
            self.readiness_probe()
        except Exception as error:
            if self.state.readiness_error != "Required application services are unavailable; inspect server logs/configuration.":
                logger.warning("Lighting readiness check failed: %s", type(error).__name__)
            self.state.set_readiness("Required application services are unavailable; inspect server logs/configuration.")
        else:
            if not self.state.ready:
                logger.info("Required capture services passed lighting readiness checks")
            self.state.set_readiness(None)

    def synchronize(self):
        now = self.clock()
        self.state.desired_color()
        if not self.connected and now < self.next_connect:
            return
        try:
            if not self.connected:
                status = self.link.connect()
                self.record_status(status)
                with self.state.lock:
                    self.connected = True
                    self.acknowledged_color = None
                logger.info("Lighting ESP32 connected")
            color = self.state.desired_color()
            if color != self.acknowledged_color:
                status = self.link.set_color(color)
                with self.state.lock:
                    self.acknowledged_color = color
                    self.last_successful_command = {
                        "command": status["command"], "color": color, "acknowledged_at": timestamp(),
                    }
                self.record_status(status)
                self.next_health = self.clock() + self.settings.health_seconds
            elif now >= self.next_health:
                status = self.link.query_status()
                self.record_status(status)
                expected_command = self.last_successful_command["command"]
                if status["command"] != expected_command:

                    # A reboot or external console command must never silently replace the desired state.

                    self.acknowledged_color = None
                self.next_health = self.clock() + self.settings.health_seconds
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            with self.state.lock:
                changed = self.last_error != message
                self.last_error = message
            if changed:
                logger.warning("Lighting unavailable: %s", message)
            self.disconnect()
            self.next_connect = self.clock() + self.settings.reconnect_seconds

    def record_status(self, status):
        with self.state.lock:
            self.firmware = status.copy()
            self.last_seen_at = timestamp()
            self.last_error = None

    def disconnect(self):
        try:
            self.link.close()
        except Exception:
            logger.warning("Could not close lighting serial port")
        with self.state.lock:
            self.connected = False
            self.acknowledged_color = None

    def close(self):
        with self.state.lock:
            self.state.capture_id = None
            self.state.transition("OFF")
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.settings.timeout * 3 + 2)
        if self.readiness_thread is not None:
            self.readiness_thread.join(timeout=1)

    def status(self):
        with self.state.lock:
            snapshot = self.state.snapshot()
            return {**snapshot, "enabled": self.settings.enabled,
                    "connection_status": "connected" if self.connected else (
                        "disconnected" if self.settings.enabled else "disabled"),
                    "esp32_available": self.connected,
                    "synchronized": self.connected and self.acknowledged_color == snapshot["color"],
                    "acknowledged_color": self.acknowledged_color,
                    "firmware": self.firmware, "last_successful_command": self.last_successful_command,
                    "last_error": self.last_error, "last_seen_at": self.last_seen_at}
