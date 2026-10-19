"""Camera attitude: a static calibrated pointing, or a BNO055 IMU.

Recommended: ``static``. The cameras are bolted down, and pointing error
is the dominant term in the triangulation error budget: 1 deg of yaw error
at 50 km range is 50 km x tan(1 deg) = ~870 m of lateral error. A consumer
IMU's magnetometer heading is rarely better than 1-2 deg near buildings, so
the attitude should be solved once from a star field (plate solve a night
frame, see README "Calibration") and entered here.

The BNO055 path is for mobile or temporary deployments. In NDOF fusion mode
the chip outputs Euler angles at 100 Hz in registers 0x1A..0x1F
(heading, roll, pitch; little-endian int16, 16 LSB per degree with the
default UNIT_SEL). Heading is relative to *magnetic* north, so the local
magnetic declination (east positive) is added to get true north, and fixed
mount offsets map the sensor axes onto the camera's yaw/pitch/roll. Those
offsets are first-order (small misalignments only); calibrate them against
a star-field solution. Register map from the Bosch BNO055 datasheet
(as used by the Adafruit CircuitPython driver): CHIP_ID 0x00 = 0xA0,
PAGE_ID 0x07, CALIB_STAT 0x35, UNIT_SEL 0x3B, OPR_MODE 0x3D (CONFIG 0x00,
NDOF 0x0C), PWR_MODE 0x3E, SYS_TRIGGER 0x3F.

The BNO055 driver is NOT hardware-tested; it is exercised against a fake
SMBus in the tests. On the Orin Nano developer kit, header pins 3/5 are
I2C bus 7 (``i2cdetect -y -r 7``).
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from ..config import AttitudeConfig, Bno055Config
from ..geometry import Attitude

log = logging.getLogger(__name__)

REG_CHIP_ID = 0x00
REG_PAGE_ID = 0x07
REG_EUL_HEADING_LSB = 0x1A
REG_CALIB_STAT = 0x35
REG_UNIT_SEL = 0x3B
REG_OPR_MODE = 0x3D
REG_PWR_MODE = 0x3E
REG_SYS_TRIGGER = 0x3F
CHIP_ID = 0xA0
MODE_CONFIG = 0x00
MODE_NDOF = 0x0C
EULER_LSB_PER_DEG = 16.0


class AttitudeSource:
    """Static attitude; subclasses override ``attitude()`` and run a device thread."""

    def __init__(self, cfg: AttitudeConfig):
        self.cfg = cfg
        self._static = Attitude(cfg.yaw_deg % 360.0, cfg.pitch_deg, cfg.roll_deg)

    def start(self) -> None:
        """Static attitude needs no device."""

    def stop(self) -> None:
        """Static attitude needs no device."""

    def attitude(self) -> Attitude:
        return self._static

    def status(self) -> Dict[str, Any]:
        return {"attitude": "static"}


def bno055_to_camera(heading: float, roll: float, pitch: float, cfg: Bno055Config) -> Attitude:
    """Map BNO055 Euler angles (deg) to the protocol's camera attitude."""
    yaw = (heading + cfg.magnetic_declination_deg + cfg.yaw_offset_deg) % 360.0
    cam_pitch = max(-90.0, min(90.0, cfg.pitch_sign * pitch + cfg.pitch_offset_deg))
    cam_roll = ((cfg.roll_sign * roll + cfg.roll_offset_deg + 180.0) % 360.0) - 180.0
    return Attitude(yaw, cam_pitch, cam_roll)


class Bno055(AttitudeSource):
    """BNO055 over I2C via smbus2, polled in a thread and averaged."""

    def __init__(self, cfg: AttitudeConfig, bus: Any = None):
        super().__init__(cfg)
        self.bcfg = cfg.bno055
        self._bus = bus
        self._lock = threading.Lock()
        self._window: Deque[Tuple[float, float, float]] = deque(maxlen=max(1, self.bcfg.smoothing_samples))
        self._latest: Optional[Attitude] = None
        self.calibration = (0, 0, 0, 0)
        self.errors = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _open_bus(self) -> Any:
        if self._bus is None:
            from smbus2 import SMBus  # optional dependency: pip install skynet-edge[imu]

            self._bus = SMBus(self.bcfg.i2c_bus)
        return self._bus

    def initialize(self) -> None:
        bus = self._open_bus()
        addr = self.bcfg.address
        for _ in range(10):  # the chip needs up to ~850 ms after power-on
            try:
                if bus.read_byte_data(addr, REG_CHIP_ID) == CHIP_ID:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"BNO055 not found at 0x{addr:02x} on i2c-{self.bcfg.i2c_bus}")
        bus.write_byte_data(addr, REG_OPR_MODE, MODE_CONFIG)
        time.sleep(0.025)  # any mode -> CONFIG takes 19 ms
        bus.write_byte_data(addr, REG_PAGE_ID, 0x00)
        bus.write_byte_data(addr, REG_PWR_MODE, 0x00)  # normal power
        bus.write_byte_data(addr, REG_UNIT_SEL, 0x00)  # degrees, m/s^2, Windows orientation convention
        bus.write_byte_data(addr, REG_SYS_TRIGGER, 0x00)  # internal oscillator
        bus.write_byte_data(addr, REG_OPR_MODE, MODE_NDOF)
        time.sleep(0.02)  # CONFIG -> fusion mode takes 7 ms

    def read_euler(self) -> Tuple[float, float, float]:
        """(heading, roll, pitch) in degrees."""
        raw = bytes(self._open_bus().read_i2c_block_data(self.bcfg.address, REG_EUL_HEADING_LSB, 6))
        h, r, p = struct.unpack("<hhh", raw)
        return h / EULER_LSB_PER_DEG, r / EULER_LSB_PER_DEG, p / EULER_LSB_PER_DEG

    def read_calibration(self) -> Tuple[int, int, int, int]:
        """(system, gyro, accel, mag) calibration levels, 0..3 each."""
        c = self._open_bus().read_byte_data(self.bcfg.address, REG_CALIB_STAT)
        return (c >> 6) & 3, (c >> 4) & 3, (c >> 2) & 3, c & 3

    def poll(self) -> None:
        heading, roll, pitch = self.read_euler()
        with self._lock:
            self._window.append((heading, roll, pitch))
            hs = [math.radians(w[0]) for w in self._window]
            mean_h = math.degrees(math.atan2(sum(map(math.sin, hs)), sum(map(math.cos, hs)))) % 360.0
            mean_r = sum(w[1] for w in self._window) / len(self._window)
            mean_p = sum(w[2] for w in self._window) / len(self._window)
            self._latest = bno055_to_camera(mean_h, mean_r, mean_p, self.bcfg)

    def start(self) -> None:
        self.initialize()
        self.poll()
        self._thread = threading.Thread(target=self._run, name="bno055", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        period = 1.0 / self.bcfg.poll_hz
        n = 0
        while not self._stop.wait(period):
            try:
                self.poll()
                n += 1
                if n % int(max(1, self.bcfg.poll_hz * 5)) == 0:
                    self.calibration = self.read_calibration()
            except OSError as exc:
                self.errors += 1
                log.warning("BNO055 read failed: %s", exc)

    def attitude(self) -> Attitude:
        with self._lock:
            return self._latest if self._latest is not None else self._static

    def status(self) -> Dict[str, Any]:
        return {"attitude": "bno055", "imu_calib": list(self.calibration), "imu_errors": self.errors}


def make_attitude_source(cfg: AttitudeConfig) -> AttitudeSource:
    return Bno055(cfg) if cfg.source == "bno055" else AttitudeSource(cfg)
