"""Maintain one exclusive serial session using the unchanged cabek-dmx-v1 protocol.

Depends on optional pyserial and the existing ASCII OK/STATUS responses. Only the
lighting worker calls this transport; acknowledgment cannot prove visible LEDs.
"""

from glob import glob
import time


COLORS = {"OFF": ("0", (0, 0, 0)), "RED": ("1", (255, 0, 0)),
          "BLUE": ("2", (0, 0, 255)), "GREEN": ("3", (0, 255, 0)),
          "WHITE": ("4", (255, 255, 255))}


class SerialLightingLink:
    def __init__(self, settings, serial_factory=None):
        self.settings = settings
        self.serial_factory = serial_factory
        self.port = None
        self.buffer = bytearray()
        self.previous_status = None

    def connect(self):
        if self.settings.port == "auto":
            candidates = sorted(glob("/dev/serial/by-id/*"))
            if len(candidates) != 1:
                raise OSError("Configure CABEK_LIGHTS_PORT: auto requires exactly one by-id device.")
            device = candidates[0]
        else:
            device = self.settings.port
        factory = self.serial_factory
        if factory is None:
            import serial
            factory = serial.Serial
        self.port = factory(port=None, baudrate=self.settings.baud, timeout=0.05,
                            write_timeout=self.settings.timeout, exclusive=True)

        # Avoid the usual open-time DTR/RTS reset of an already transmitting ESP32.

        self.port.dtr = False
        self.port.rts = False
        self.port.port = device
        self.port.open()
        self.buffer.clear()
        self.previous_status = None
        self.port.reset_input_buffer()
        return self.query_status(startup=True)

    def close(self):
        if self.port is not None:
            try:
                self.port.close()
            finally:
                self.port = None
        self.buffer.clear()
        self.previous_status = None

    def read_until(self, prefix, *, startup=False):
        deadline = time.monotonic() + self.settings.timeout
        while time.monotonic() < deadline:
            if b"\n" in self.buffer:
                raw, _, remainder = self.buffer.partition(b"\n")
                self.buffer = bytearray(remainder)
                if any(byte not in (9, 13) and not 32 <= byte <= 126 for byte in raw):
                    raise OSError("Corrupt serial response.")
                line = raw.decode("ascii").strip()
                if line.startswith(prefix):
                    return line
                if startup and line.startswith(("BOOT ", "READY ")):
                    continue
                raise OSError("Unexpected serial response or ESP32 restart.")
            self.buffer.extend(self.port.read(512))
            if len(self.buffer) > 8192:
                raise OSError("Serial response exceeded its limit.")
        raise TimeoutError("ESP32 response timed out.")

    def write(self, command):
        if self.port.write(command.encode("ascii")) != 1:
            raise OSError("Incomplete serial write.")

    def query_status(self, *, startup=False):
        self.write("?")
        line = self.read_until("STATUS ", startup=startup)
        try:
            status = dict(item.split("=", 1) for item in line.split()[1:])
            expected = next(rgb for command, rgb in COLORS.values() if command == status["command"])
            valid = (status["firmware"] == "cabek-dmx-v1" and status["mode"] == "dmx_tx"
                     and status["uart2_baud"] == "250000" and int(status["errors"]) == 0
                     and abs(int(status["uart0_baud"]) - self.settings.baud) <= 12
                     and tuple(map(int, status["rgb"].split(","))) == expected)
            if not valid:
                raise ValueError("Unhealthy firmware or DMX status.")
            if self.previous_status:
                elapsed = int(status["uptime_ms"]) - int(self.previous_status["uptime_ms"])
                frames = int(status["frames"]) - int(self.previous_status["frames"])
                if elapsed < 0 or frames < 0 or (elapsed >= 100 and frames == 0):
                    raise ValueError("ESP32 restarted or DMX refresh stalled.")
        except (KeyError, ValueError, StopIteration) as error:
            raise OSError("Invalid or unhealthy ESP32 status.") from error
        self.previous_status = status
        return status

    def set_color(self, color):
        command, rgb = COLORS[color]
        self.write(command)
        expected = f"OK {command} {color} {' '.join(map(str, rgb))}"
        if self.read_until("OK ") != expected:
            raise OSError("ESP32 acknowledgment does not match the requested color.")
        status = self.query_status()
        if status["command"] != command:
            raise OSError("ESP32 did not retain the acknowledged color.")
        return status
