"""Hardware detection against fake sysfs/dev trees, a fake gpsd and a pty "GPS"."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from skynet_edge.appliance.detect import BNO055_CHIP_ID, Detector, contains_nmea, serial_has_nmea
from skynet_edge.appliance.util import CmdResult

GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
RMC = "$GNRMC,001031.00,A,4404.13993,N,12118.86023,W,0.146,,100117,,,A*7B"


def w(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def no_cmd(argv, timeout=5.0, **_):
    return CmdResult(127, "", "not found")


def build_tree(root):
    sysr, devr, procr, etcr = (str(root / x) for x in ("sys", "dev", "proc", "etc"))
    # CSI camera (tegra camera driver naming) and a UVC camera with its metadata node.
    w(f"{sysr}/class/video4linux/video0/name", "vi-output, imx477 9-001a\n")
    w(f"{sysr}/class/video4linux/video1/name", "HD USB Camera: HD USB Camera\n")
    w(f"{sysr}/class/video4linux/video1/index", "0\n")
    w(f"{sysr}/class/video4linux/video2/name", "HD USB Camera: HD USB Camera\n")
    w(f"{sysr}/class/video4linux/video2/index", "1\n")
    os.makedirs(f"{sysr}/bus/usb/drivers/uvcvideo", exist_ok=True)
    for n in (1, 2):
        os.makedirs(f"{sysr}/class/video4linux/video{n}/device", exist_ok=True)
        os.symlink(f"{sysr}/bus/usb/drivers/uvcvideo", f"{sysr}/class/video4linux/video{n}/device/driver")
    # PPS: the Jetson timer source and the GPIO one.
    w(f"{sysr}/class/pps/pps0/name", "ktimer\n")
    w(f"{sysr}/class/pps/pps1/name", "pps-gpio.0\n")
    w(f"{sysr}/class/pps/pps1/assert", "1790000000.000000123#42\n")
    # USB: an RTL-SDR among other devices.
    w(f"{sysr}/bus/usb/devices/1-1/idVendor", "1d6b\n")
    w(f"{sysr}/bus/usb/devices/1-1/idProduct", "0002\n")
    w(f"{sysr}/bus/usb/devices/1-2/idVendor", "0bda\n")
    w(f"{sysr}/bus/usb/devices/1-2/idProduct", "2838\n")
    # NVMe: a blank one and one with a partition.
    w(f"{sysr}/block/nvme0n1/size", "1953525168\n")
    os.makedirs(f"{sysr}/block/nvme0n1/holders", exist_ok=True)
    w(f"{sysr}/block/nvme1n1/size", "1000\n")
    w(f"{sysr}/block/nvme1n1/nvme1n1p1/partition", "1\n")
    w(f"{procr}/mounts", "/dev/mmcblk0p1 / ext4 rw 0 0\n")
    w(f"{procr}/device-tree/model", "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\x00")
    w(f"{procr}/device-tree/serial-number", "1421224012345\x00")
    w(f"{etcr}/nv_tegra_release", "# R36 (release), REVISION: 4.3\n")
    for n in (0, 1, 7):
        w(f"{devr}/i2c-{n}")
    w(f"{devr}/ttyTHS1")
    w(f"{devr}/ttyUSB0")
    return dict(sys_root=sysr, dev_root=devr, proc_root=procr, etc_root=etcr)


class FakeGpsd:
    def __init__(self, port, devices, mode=3):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(4)
        self.devices, self.mode = devices, mode
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                c.recv(1024)
                lines = [{"class": "VERSION", "release": "3.22"},
                         {"class": "DEVICES", "devices": [{"path": d} for d in self.devices]},
                         {"class": "WATCH", "enable": True},
                         {"class": "TPV", "mode": self.mode, "lat": 33.6, "lon": -112.4, "altHAE": 350.0},
                         {"class": "SKY", "uSat": 9, "nSat": 14}]
                c.sendall(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
                time.sleep(0.05)

    def close(self):
        self.sock.close()


def test_detect_everything(tmp_path):
    roots = build_tree(tmp_path)
    gpsd = FakeGpsd(8311, ["/dev/ttyTHS1"])
    try:
        det = Detector(**roots, runner=no_cmd, gpsd_addr=("127.0.0.1", 8311),
                       i2c_probe=lambda bus, addr: BNO055_CHIP_ID if (bus, addr) == (7, 0x28) else None,
                       querycap=lambda dev: 0x84200001).detect()
    finally:
        gpsd.close()
    assert det["errors"] == {}
    assert det["camera"] == {"kind": "csi", "sensor": "imx477", "sensor_id": 0, "device": roots["dev_root"] + "/video0",
                             "name": "vi-output, imx477 9-001a"}
    assert [c["kind"] for c in det["cameras"]] == ["csi", "uvc"]  # the UVC metadata node is skipped
    assert det["gps"]["kind"] == "gpsd" and det["gps"]["device"] == "/dev/ttyTHS1" and det["gps"]["fix_mode"] == 3
    assert det["pps"]["device"].endswith("/pps1") and det["pps"]["all"][0]["timer"] is True
    assert det["pps"]["all"][1]["assert_seq"] == 42
    assert det["imu"] == {"kind": "bno055", "bus": 7, "address": 0x28, "buses": [0, 1, 7]}
    assert det["adsb"]["rtlsdr"] is True and det["adsb"]["usb"] == "0bda:2838"
    nv = {n["device"].rsplit("/", 1)[-1]: n for n in det["nvme"]}
    assert nv["nvme1n1"]["blank"] is False and nv["nvme1n1"]["partitions"] == ["nvme1n1p1"]
    assert nv["nvme0n1"]["size_bytes"] == 1953525168 * 512
    assert nv["nvme0n1"]["blank"] in (None, False, True)  # depends on root + blkid; never raises
    assert det["platform"]["jetson"] is True and det["platform"]["serial"] == "1421224012345"
    assert det["network"]["manager"] is None


def test_serial_probe_used_when_gpsd_absent(tmp_path):
    roots = build_tree(tmp_path)
    calls = []

    def probe(dev, baud, listen_s):
        calls.append((os.path.basename(dev), baud))
        return dev.endswith("ttyUSB0") and baud == 38400

    det = Detector(**roots, runner=no_cmd, gpsd_addr=("127.0.0.1", 8312), serial_probe=probe,
                   i2c_probe=lambda b, a: None).detect()
    assert det["gps"]["kind"] == "serial" and det["gps"]["baud"] == 38400 and det["gps"]["usb"] is True
    assert ("ttyTHS1", 115200) in calls  # every rate tried on the port without NMEA
    assert det["imu"]["kind"] == "none"


def test_nothing_attached(tmp_path):
    for d in ("sys", "dev", "proc", "etc"):
        (tmp_path / d).mkdir()
    det = Detector(sys_root=str(tmp_path / "sys"), dev_root=str(tmp_path / "dev"), proc_root=str(tmp_path / "proc"),
                   etc_root=str(tmp_path / "etc"), runner=no_cmd, gpsd_addr=("127.0.0.1", 8313)).detect()
    assert det["camera"] == {"kind": "none"} and det["gps"]["kind"] == "none" and det["imu"]["kind"] == "none"
    assert det["pps"]["device"] is None and det["nvme"] == [] and det["adsb"] == {"rtlsdr": False}


def test_a_hung_probe_does_not_hang_detection(tmp_path):
    roots = build_tree(tmp_path)
    release = threading.Event()

    def hung(bus, addr):
        release.wait(30)
        return None

    t0 = time.monotonic()
    det = Detector(**roots, runner=no_cmd, gpsd_addr=("127.0.0.1", 8314), deadline_s=1.0, i2c_probe=hung,
                   serial_probe=lambda d, b, s: False).detect()
    release.set()
    assert time.monotonic() - t0 < 2.5
    assert det["errors"].get("imu") == "timed out"
    assert det["camera"]["kind"] == "csi"  # the other probes still answered


def test_nmea_recognition():
    assert contains_nmea("garbage\r\n" + GGA + "\r\n")
    assert contains_nmea(RMC)
    assert not contains_nmea(GGA[:-2] + "00")  # bad checksum
    assert not contains_nmea("\x00\xff random bytes at the wrong baud rate")


def test_serial_has_nmea_on_a_pty():
    master, slave = os.openpty()
    name = os.ttyname(slave)
    stop = threading.Event()

    def talk():
        while not stop.is_set():
            os.write(master, (GGA + "\r\n").encode())
            time.sleep(0.1)

    t = threading.Thread(target=talk, daemon=True)
    t.start()
    try:
        assert serial_has_nmea(name, 9600, 1.0) is True
    finally:
        stop.set()
        t.join()
        os.close(master)
        os.close(slave)


def test_serial_probe_times_out_quietly():
    master, slave = os.openpty()
    try:
        t0 = time.monotonic()
        assert serial_has_nmea(os.ttyname(slave), 9600, 0.3) is False
        assert time.monotonic() - t0 < 1.0
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("text,expect", [("$GPGGA,,,,,,0,,,,,,,,*66", True), ("$GPXYZ,1*00", False)])
def test_nmea_checksum_edge_cases(text, expect):
    assert contains_nmea(text) is expect
