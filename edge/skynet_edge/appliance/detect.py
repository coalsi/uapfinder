"""Hardware detection: ``skynet-edge detect``.

Each probe is independent, has its own timeout, and runs in a thread of a
small pool with an overall deadline, so one hung device (a USB serial port
that never answers, an I2C bus held low) cannot hold up the rest. What each
probe looks at:

* camera: ``/sys/class/video4linux/video*/name``. NVIDIA's tegra camera
  driver names CSI capture nodes ``vi-output, <sensor> <bus>-<addr>`` (e.g.
  ``vi-output, imx477 9-001a``); their order is the Argus ``sensor-id``.
  UVC cameras are nodes bound to the ``uvcvideo`` driver with ``index`` 0
  (the other node of a UVC camera is metadata), confirmed with
  ``VIDIOC_QUERYCAP`` when the device can be opened.
* GPS: gpsd first (``?WATCH`` reports its ``DEVICES``); otherwise
  ``/dev/ttyTHS1`` (header pins 8/10), ``/dev/ttyACM*`` and ``/dev/ttyUSB*``
  are read raw at 9600, 38400 and 115200 baud for checksummed NMEA. Ports
  gpsd already uses are never opened (that would change their speed).
* PPS: ``/sys/class/pps/pps*/name``, skipping the timer-based test source
  (``ktimer``) that Jetson kernels register as ``pps0``.
* orientation: a BNO055 answers chip id 0xA0 at register 0 on address 0x28
  or 0x29 of some ``/dev/i2c-*`` bus (all buses are tried).
* ADS-B: an RTL-SDR (USB ``0bda:2838`` or ``0bda:2832``) in
  ``/sys/bus/usb/devices``.
* NVMe: ``/sys/block/nvme*``; partitions, mounts and (as root) ``blkid -p``
  signatures decide whether a drive is blank.
* network: ``nmcli`` device states when NetworkManager runs.

NOT hardware-tested: the sysfs layouts and names follow the kernel and
NVIDIA documentation and are exercised against fake trees in the tests.
"""

from __future__ import annotations

import concurrent.futures
import errno
import fcntl
import glob
import json
import logging
import os
import re
import select
import socket
import struct
import termios
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..sensors.gps import nmea_checksum_ok
from .nm import NetworkManager
from .util import CmdResult, read_text, run

log = logging.getLogger(__name__)

RTLSDR_IDS = {("0bda", "2838"), ("0bda", "2832")}
BNO055_ADDRESSES = (0x28, 0x29)
BNO055_CHIP_ID = 0xA0
GPS_BAUDS = (9600, 38400, 115200)
VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability), 104 bytes
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
_NMEA_RE = re.compile(r"\$[A-Z]{2}[A-Z]{3},[^\r\n$]*\*[0-9A-Fa-f]{2}")
_BAUD_CONST = {9600: termios.B9600, 38400: termios.B38400, 115200: termios.B115200}


def _read(path: str) -> Optional[str]:
    text = read_text(path, 4096)
    return None if text is None else text.strip().strip("\x00").strip()


class Detector:
    """Runs all probes. The filesystem roots and the command runner are injectable for tests."""

    def __init__(self, sys_root: str = "/sys", dev_root: str = "/dev", proc_root: str = "/proc",
                 etc_root: str = "/etc", runner: Callable[..., CmdResult] = run,
                 gpsd_addr: Tuple[str, int] = ("127.0.0.1", 2947), deadline_s: float = 8.0,
                 i2c_probe: Optional[Callable[[int, int], Optional[int]]] = None,
                 serial_probe: Optional[Callable[[str, int, float], bool]] = None,
                 querycap: Optional[Callable[[str], Optional[int]]] = None):
        self.sys = sys_root
        self.dev = dev_root
        self.proc = proc_root
        self.etc = etc_root
        self._run = runner
        self.gpsd_addr = gpsd_addr
        self.deadline_s = deadline_s
        self._i2c_probe = i2c_probe or i2c_read_chip_id
        self._serial_probe = serial_probe or serial_has_nmea
        self._querycap = querycap or v4l2_device_caps

    # --- orchestration -----------------------------------------------------

    def detect(self) -> Dict[str, Any]:
        t0 = time.monotonic()
        probes: Dict[str, Callable[[], Any]] = {
            "platform": self.platform, "cameras": self.cameras, "gps": self.gps, "pps": self.pps,
            "imu": self.imu, "adsb": self.adsb, "nvme": self.nvme, "network": self.network,
        }
        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="detect")
        futures = {name: pool.submit(fn) for name, fn in probes.items()}
        end = t0 + self.deadline_s
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=max(0.0, end - time.monotonic()))
            except concurrent.futures.TimeoutError:
                errors[name] = "timed out"
                results[name] = None
            except Exception as exc:  # noqa: BLE001 - a broken probe must not break detection
                log.warning("detect %s failed: %s", name, exc)
                errors[name] = str(exc)
                results[name] = None
        pool.shutdown(wait=False)  # a hung probe thread is left behind; it is a daemon-like worker
        cams = results.get("cameras") or []
        out = {
            "version": 1,
            "t_ns": time.time_ns(),
            "duration_s": round(time.monotonic() - t0, 2),
            "platform": results.get("platform") or {},
            "camera": cams[0] if cams else {"kind": "none"},
            "cameras": cams,
            "gps": results.get("gps") or {"kind": "none"},
            "pps": results.get("pps") or {"device": None, "all": []},
            "imu": results.get("imu") or {"kind": "none"},
            "adsb": results.get("adsb") or {"rtlsdr": False},
            "nvme": results.get("nvme") or [],
            "network": results.get("network") or {"manager": None, "devices": [], "connected": None},
            "errors": errors,
        }
        return out

    # --- platform ----------------------------------------------------------

    def platform(self) -> Dict[str, Any]:
        model = _read(f"{self.proc}/device-tree/model") or _read(f"{self.sys}/firmware/devicetree/base/model")
        serial = (_read(f"{self.proc}/device-tree/serial-number")
                  or _read(f"{self.sys}/firmware/devicetree/base/serial-number"))
        l4t = _read(f"{self.etc}/nv_tegra_release")
        return {"model": model, "serial": serial, "jetson": l4t is not None,
                "l4t": l4t.splitlines()[0] if l4t else None}

    # --- cameras -----------------------------------------------------------

    def cameras(self) -> List[Dict[str, Any]]:
        csi: List[Dict[str, Any]] = []
        uvc: List[Dict[str, Any]] = []
        nodes = sorted(glob.glob(f"{self.sys}/class/video4linux/video*"),
                       key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
        for node in nodes:
            name = _read(f"{node}/name") or ""
            dev = f"{self.dev}/{os.path.basename(node)}"
            driver = os.path.basename(os.path.realpath(f"{node}/device/driver")) if os.path.exists(
                f"{node}/device/driver") else ""
            if name.startswith("vi-output"):
                m = re.match(r"vi-output,\s*([A-Za-z0-9_]+)", name)
                sensor = m.group(1).lower() if m else "unknown"
                csi.append({"kind": "csi", "sensor": sensor, "sensor_id": len(csi), "device": dev, "name": name})
            elif driver == "uvcvideo" or "uvc" in driver:
                index = _read(f"{node}/index")
                if index not in (None, "0"):
                    continue
                caps = self._querycap(dev)
                if caps is not None and not caps & V4L2_CAP_VIDEO_CAPTURE:
                    continue
                uvc.append({"kind": "uvc", "device": dev, "name": name})
        return csi + uvc

    # --- GPS -----------------------------------------------------------------

    def gpsd_devices(self, timeout_s: float = 2.0) -> Optional[Dict[str, Any]]:
        """gpsd's devices and current fix mode, or None if gpsd is not reachable."""
        try:
            sock = socket.create_connection(self.gpsd_addr, timeout=0.5)
        except OSError:
            return None
        devices: List[str] = []
        mode = 0
        sats_used: Optional[int] = None
        try:
            sock.settimeout(0.3)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            end = time.monotonic() + timeout_s
            got_devices = False
            while time.monotonic() < end:
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    if got_devices and mode:
                        break
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cls = obj.get("class") if isinstance(obj, dict) else None
                    if cls == "DEVICES":
                        got_devices = True
                        devices = [d.get("path") for d in obj.get("devices", []) if isinstance(d, dict)
                                   and isinstance(d.get("path"), str)]
                    elif cls == "TPV" and isinstance(obj.get("mode"), int):
                        mode = max(mode, obj["mode"])
                    elif cls == "SKY" and isinstance(obj.get("uSat"), int):
                        sats_used = obj["uSat"]
                if got_devices and (mode >= 2 or not devices):
                    break
        finally:
            sock.close()
        return {"devices": devices, "mode": mode, "satellites_used": sats_used}

    def gps_candidates(self) -> List[str]:
        out = []
        for pattern in ("ttyTHS1", "ttyACM*", "ttyUSB*"):
            out.extend(sorted(glob.glob(f"{self.dev}/{pattern}")))
        return out

    def gps(self) -> Dict[str, Any]:
        gpsd = self.gpsd_devices()
        if gpsd is not None and gpsd["devices"]:
            dev = gpsd["devices"][0]
            return {"kind": "gpsd", "device": dev, "devices": gpsd["devices"], "gpsd": True,
                    "fix_mode": gpsd["mode"], "satellites_used": gpsd["satellites_used"],
                    "usb": "/ttyACM" in dev or "/ttyUSB" in dev}
        busy = {os.path.realpath(d) for d in (gpsd or {}).get("devices", [])}
        candidates = [c for c in self.gps_candidates() if os.path.realpath(c) not in busy]
        found: Optional[Tuple[str, int]] = None
        if candidates:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix="gps-probe")
            futs = {pool.submit(self._probe_port, c): c for c in candidates}
            try:
                for fut in concurrent.futures.as_completed(futs, timeout=6.0):
                    baud = fut.result()
                    if baud and (found is None or candidates.index(futs[fut]) < candidates.index(found[0])):
                        found = (futs[fut], baud)
            except concurrent.futures.TimeoutError:
                log.warning("GPS serial probe timed out on some ports")
            finally:
                pool.shutdown(wait=False)
        if found is None:
            return {"kind": "none", "gpsd": gpsd is not None, "probed": candidates}
        dev, baud = found
        return {"kind": "serial", "device": dev, "baud": baud, "gpsd": gpsd is not None,
                "usb": "/ttyACM" in dev or "/ttyUSB" in dev}

    def _probe_port(self, dev: str) -> Optional[int]:
        for baud in GPS_BAUDS:
            try:
                if self._serial_probe(dev, baud, 1.2):
                    return baud
            except OSError as exc:
                log.debug("serial probe %s @%d: %s", dev, baud, exc)
                if exc.errno in (errno.ENOENT, errno.EACCES, errno.EBUSY, errno.ENODEV):
                    return None
        return None

    # --- PPS -------------------------------------------------------------------

    def pps(self) -> Dict[str, Any]:
        found = []
        for d in sorted(glob.glob(f"{self.sys}/class/pps/pps*")):
            name = _read(f"{d}/name") or ""
            assert_ = _read(f"{d}/assert") or ""
            seq = None
            m = re.search(r"#(\d+)", assert_)
            if m:
                seq = int(m.group(1))
            found.append({"device": f"{self.dev}/{os.path.basename(d)}", "name": name, "timer": name == "ktimer",
                          "assert_seq": seq})
        real = [p for p in found if not p["timer"]]
        return {"device": real[0]["device"] if real else None, "name": real[0]["name"] if real else None,
                "all": found}

    # --- I2C orientation sensor ------------------------------------------------

    def i2c_buses(self) -> List[int]:
        buses = []
        for p in glob.glob(f"{self.dev}/i2c-*"):
            m = re.search(r"i2c-(\d+)$", p)
            if m:
                buses.append(int(m.group(1)))
        return sorted(buses)

    def imu(self) -> Dict[str, Any]:
        buses = self.i2c_buses()
        if not buses:
            return {"kind": "none", "buses": []}
        jobs = [(b, a) for b in buses for a in BNO055_ADDRESSES]
        hits: List[Tuple[int, int]] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs)), thread_name_prefix="i2c-probe")
        futs = {pool.submit(self._i2c_probe, b, a): (b, a) for b, a in jobs}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=3.0):
                try:
                    if fut.result() == BNO055_CHIP_ID:
                        hits.append(futs[fut])
                except OSError:
                    pass
        except concurrent.futures.TimeoutError:
            log.warning("I2C probe timed out on some buses")
        finally:
            pool.shutdown(wait=False)
        if not hits:
            return {"kind": "none", "buses": buses}
        bus, addr = sorted(hits)[0]
        return {"kind": "bno055", "bus": bus, "address": addr, "buses": buses}

    # --- ADS-B receiver ----------------------------------------------------------

    def adsb(self) -> Dict[str, Any]:
        for d in sorted(glob.glob(f"{self.sys}/bus/usb/devices/*")):
            vid, pid = _read(f"{d}/idVendor"), _read(f"{d}/idProduct")
            if vid and pid and (vid.lower(), pid.lower()) in RTLSDR_IDS:
                return {"rtlsdr": True, "usb": f"{vid}:{pid}", "product": _read(f"{d}/product")}
        return {"rtlsdr": False}

    # --- NVMe ------------------------------------------------------------------------

    def mounts(self) -> List[Tuple[str, str]]:
        text = read_text(f"{self.proc}/mounts", 1 << 20) or ""
        out = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1].replace("\\040", " ")))
        return out

    def nvme(self) -> List[Dict[str, Any]]:
        out = []
        mounts = self.mounts()
        for blk in sorted(glob.glob(f"{self.sys}/block/nvme*")):
            name = os.path.basename(blk)
            if not re.fullmatch(r"nvme\d+n\d+", name):
                continue
            size = _read(f"{blk}/size")
            parts = sorted(os.path.basename(p) for p in glob.glob(f"{blk}/{name}p*"))
            dev = f"{self.dev}/{name}"
            names = [dev] + [f"{self.dev}/{p}" for p in parts]
            mounted = [m for src, m in mounts if src in names]
            holders = os.listdir(f"{blk}/holders") if os.path.isdir(f"{blk}/holders") else []
            sig = self.signatures(dev) if not parts else None
            blank: Optional[bool]
            if parts or mounted or holders or sig:
                blank = False
            elif sig is None:
                blank = None  # unknown (blkid needs root)
            else:
                blank = True
            out.append({"device": dev, "size_bytes": int(size) * 512 if size and size.isdigit() else None,
                        "partitions": parts, "mounted": mounted, "signatures": sig, "blank": blank})
        return out

    def signatures(self, dev: str) -> Optional[List[str]]:
        """Partition-table / file-system signatures blkid finds; None if it cannot tell."""
        if os.geteuid() != 0:
            return None
        res = self._run(["blkid", "-p", "-o", "export", dev], timeout=5.0)
        if res.returncode == 2:
            return []
        if res.returncode == 0:
            return [line for line in res.stdout.splitlines() if line.startswith(("TYPE=", "PTTYPE="))] or ["unknown"]
        return None

    # --- network ------------------------------------------------------------------------

    def network(self) -> Dict[str, Any]:
        nm = NetworkManager(self._run)
        if not nm.available():
            return {"manager": None, "devices": [], "connected": None}
        devs = nm.devices()
        return {"manager": "networkmanager",
                "devices": [{"device": d.device, "type": d.type, "state": d.state, "connection": d.connection}
                            for d in devs if d.type in ("ethernet", "wifi")],
                "connected": nm.is_connected(devs), "hotspot": nm.hotspot_active(devs)}


# --- low-level probes (real hardware; not exercised by the unit tests) ------------------


def v4l2_device_caps(dev: str) -> Optional[int]:
    """``device_caps`` (or ``capabilities``) from VIDIOC_QUERYCAP; None if the device cannot be queried."""
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
        caps, device_caps = struct.unpack_from("<II", buf, 84)
        return device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps
    except OSError:
        return None
    finally:
        os.close(fd)


def i2c_read_chip_id(bus: int, addr: int) -> Optional[int]:
    """Register 0 of ``addr`` on ``/dev/i2c-<bus>`` via the I2C_SLAVE ioctl (no smbus2 needed)."""
    i2c_slave = 0x0703
    fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
    try:
        fcntl.ioctl(fd, i2c_slave, addr)
        os.write(fd, b"\x00")
        data = os.read(fd, 1)
        return data[0] if data else None
    except OSError as exc:
        if exc.errno in (errno.EREMOTEIO, errno.ENXIO, errno.EIO, errno.ETIMEDOUT, errno.EBUSY):
            return None
        raise
    finally:
        os.close(fd)


def contains_nmea(text: str) -> bool:
    return any(nmea_checksum_ok(m.group(0)) for m in _NMEA_RE.finditer(text))


def serial_has_nmea(dev: str, baud: int, listen_s: float) -> bool:
    """Open a tty raw at ``baud`` for ``listen_s`` and look for one checksummed NMEA sentence."""
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    old = None
    try:
        old = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        attrs[3] = 0  # lflag: raw
        attrs[4] = attrs[5] = _BAUD_CONST[baud]
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
        buf = b""
        end = time.monotonic() + listen_s
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
            if not r:
                break
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buf += chunk
            if contains_nmea(buf.decode("ascii", errors="replace")):
                return True
            buf = buf[-512:]
        return False
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except termios.error:
                pass
        os.close(fd)


def detect(**kwargs: Any) -> Dict[str, Any]:
    return Detector(**kwargs).detect()
