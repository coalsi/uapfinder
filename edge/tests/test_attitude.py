import struct
import time

import pytest

from skynet_edge.config import parse_config
from skynet_edge.sensors import attitude as att_mod
from skynet_edge.sensors.attitude import (CHIP_ID, MODE_CONFIG, MODE_NDOF, REG_CALIB_STAT, REG_CHIP_ID,
                                          REG_EUL_HEADING_LSB, REG_OPR_MODE, AttitudeSource, Bno055,
                                          bno055_to_camera, make_attitude_source)


class FakeSMBus:
    """Register-level BNO055 stand-in (smbus2.SMBus interface)."""

    def __init__(self, chip_id=CHIP_ID):
        self.regs = {REG_CHIP_ID: chip_id, REG_CALIB_STAT: 0b11111110}
        self.writes = []
        self.set_euler(100.0, -2.0, 30.0)

    def set_euler(self, heading, roll, pitch):
        raw = struct.pack("<hhh", round(heading * 16), round(roll * 16), round(pitch * 16))
        for i, b in enumerate(raw):
            self.regs[REG_EUL_HEADING_LSB + i] = b

    def read_byte_data(self, addr, reg):
        assert addr == 0x28
        return self.regs.get(reg, 0)

    def write_byte_data(self, addr, reg, value):
        self.writes.append((reg, value))
        self.regs[reg] = value

    def read_i2c_block_data(self, addr, reg, n):
        return [self.regs.get(reg + i, 0) for i in range(n)]


def cfg(**bno):
    return parse_config({"attitude": {"source": "bno055", "yaw_deg": 5.0, "pitch_deg": 45.0,
                                      "bno055": {"magnetic_declination_deg": 10.0, **bno}}}).attitude


def test_static_attitude():
    a = parse_config({"attitude": {"yaw_deg": -20.0, "pitch_deg": 60.0, "roll_deg": 1.0}}).attitude
    src = make_attitude_source(a)
    assert type(src) is AttitudeSource
    assert src.attitude().yaw_deg == 340.0 and src.attitude().pitch_deg == 60.0
    assert src.attitude().as_message() == {"pitch_deg": 60.0, "roll_deg": 1.0, "yaw_deg": 340.0}


def test_bno055_initialisation_and_euler_read(monkeypatch):
    monkeypatch.setattr(att_mod.time, "sleep", lambda s: None)
    bus = FakeSMBus()
    imu = Bno055(cfg(), bus=bus)
    imu.initialize()
    assert bus.writes[0] == (REG_OPR_MODE, MODE_CONFIG) and bus.writes[-1] == (REG_OPR_MODE, MODE_NDOF)
    assert imu.read_euler() == pytest.approx((100.0, -2.0, 30.0))
    assert imu.read_calibration() == (3, 3, 3, 2)
    imu.poll()
    a = imu.attitude()
    assert a.yaw_deg == pytest.approx(110.0)  # magnetic heading + declination = true azimuth
    assert a.pitch_deg == pytest.approx(30.0) and a.roll_deg == pytest.approx(-2.0)


def test_heading_average_wraps_through_north(monkeypatch):
    monkeypatch.setattr(att_mod.time, "sleep", lambda s: None)
    bus = FakeSMBus()
    imu = Bno055(cfg(magnetic_declination_deg=0.0), bus=bus)
    for h in (358.0, 2.0, 359.0, 1.0):
        bus.set_euler(h, 0.0, 10.0)
        imu.poll()
    assert min(imu.attitude().yaw_deg, 360 - imu.attitude().yaw_deg) < 0.1


def test_mount_offsets_and_signs():
    c = cfg(yaw_offset_deg=-90.0, pitch_offset_deg=40.0, pitch_sign=-1.0, roll_offset_deg=175.0).bno055
    a = bno055_to_camera(heading=350.0, roll=10.0, pitch=2.0, cfg=c)
    assert a.yaw_deg == pytest.approx((350.0 + 10.0 - 90.0) % 360.0)
    assert a.pitch_deg == pytest.approx(38.0)
    assert a.roll_deg == pytest.approx(-175.0)  # 185 wrapped into [-180, 180)


def test_missing_chip_raises(monkeypatch):
    monkeypatch.setattr(att_mod.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="BNO055 not found"):
        Bno055(cfg(), bus=FakeSMBus(chip_id=0x00)).initialize()


def test_thread_keeps_polling():
    bus = FakeSMBus()
    imu = Bno055(cfg(poll_hz=100.0), bus=bus)
    imu.start()
    bus.set_euler(200.0, 0.0, 20.0)
    deadline = time.monotonic() + 3
    while abs(imu.attitude().yaw_deg - 210.0) > 1.0 and time.monotonic() < deadline:
        time.sleep(0.01)
    imu.stop()
    assert imu.attitude().yaw_deg == pytest.approx(210.0, abs=1.0)
    assert imu.status()["attitude"] == "bno055"
