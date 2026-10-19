"""Node position from gpsd, a raw NMEA serial receiver, or a static survey.

Triangulation needs the node position as WGS84 latitude/longitude and
height above the *ellipsoid* (HAE). Receivers report height above mean sea
level and the geoid separation separately, so:

* gpsd (JSON, TCP 2947): TPV ``altHAE`` when present (gpsd >= 3.20).
  Otherwise ``altMSL`` (or the older ``alt``) + ``geoidSep``. A TPV with
  ``mode`` < 2 has no fix and is ignored; a 2-D fix (mode 2) updates
  lat/lon but keeps the last 3-D altitude.
* NMEA: GGA altitude is MSL; HAE = altitude + geoid separation (fields 9
  and 11). RMC supplies the validity flag (A/V). Sentences whose checksum
  fails are dropped; any talker id (GP, GN, GL, GA, BD...) is accepted.

The node is fixed, so a surveyed ``gps.static`` position is also accepted,
both as the only source and as the fallback until the first fix.
The serial and gpsd code paths are tested against recorded sentences and a
fake gpsd server, not against a real receiver.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..config import GpsConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fix:
    latitude: float
    longitude: float
    altitude_m: float  # WGS84 ellipsoid height
    mode: int  # 2 = 2-D, 3 = 3-D, 0 = static/surveyed
    t_ns: int
    source: str

    def as_message(self) -> Dict[str, float]:
        return {"latitude": round(self.latitude, 7), "longitude": round(self.longitude, 7),
                "altitude_m": round(self.altitude_m, 2)}


class PositionSource:
    """Holds the latest fix; subclasses feed it from a device thread."""

    def __init__(self, cfg: GpsConfig, clock: Callable[[], int] = time.time_ns):
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._fix: Optional[Fix] = None
        self._last_alt: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fallback = None
        if cfg.static is not None:
            s = cfg.static
            self.fallback = Fix(s.latitude, s.longitude, s.altitude_m, 0, 0, "static")

    def start(self) -> None:
        if self._thread is None and type(self)._run is not PositionSource._run:
            self._thread = threading.Thread(target=self._run, name=f"gps-{self.cfg.source}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        """Device loop; static sources have none."""

    def update(self, lat: float, lon: float, alt_hae: Optional[float], mode: int, source: str) -> None:
        with self._lock:
            if alt_hae is not None and mode >= 3:
                self._last_alt = alt_hae
            alt = self._last_alt
            if alt is None and self.fallback is not None:
                alt = self.fallback.altitude_m
            if alt is None:
                return  # 2-D fix without any altitude yet: unusable for triangulation
            self._fix = Fix(lat, lon, alt, mode, self._clock(), source)

    def current(self) -> Optional[Fix]:
        """Latest fix younger than ``max_fix_age_s``, else the static fallback, else None."""
        with self._lock:
            fix = self._fix
        if fix is not None and self._clock() - fix.t_ns <= self.cfg.max_fix_age_s * 1e9:
            return fix
        return self.fallback

    def status(self) -> Dict[str, Any]:
        fix = self.current()
        return {"gps": "none" if fix is None else fix.source, "gps_mode": 0 if fix is None else fix.mode}


# --- gpsd --------------------------------------------------------------------


def parse_gpsd_tpv(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract lat/lon/HAE/mode from a gpsd TPV report; None if it has no fix."""
    if obj.get("class") != "TPV":
        return None
    mode = obj.get("mode", 0)
    lat, lon = obj.get("lat"), obj.get("lon")
    if not isinstance(mode, int) or mode < 2 or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    alt = obj.get("altHAE")
    if not isinstance(alt, (int, float)):
        msl = obj.get("altMSL", obj.get("alt"))
        sep = obj.get("geoidSep")
        alt = msl + sep if isinstance(msl, (int, float)) and isinstance(sep, (int, float)) else None
    return {"lat": float(lat), "lon": float(lon), "alt": None if alt is None else float(alt), "mode": mode}


class GpsdClient(PositionSource):
    WATCH = b'?WATCH={"enable":true,"json":true}\n'

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.cfg.gpsd_host, self.cfg.gpsd_port), timeout=5.0) as sock:
                    sock.settimeout(5.0)
                    sock.sendall(self.WATCH)
                    backoff = 1.0
                    self._read_stream(sock)
            except OSError as exc:
                log.warning("gpsd %s:%d: %s", self.cfg.gpsd_host, self.cfg.gpsd_port, exc)
            self._stop.wait(backoff)
            backoff = min(backoff * 2.0, 30.0)

    def _read_stream(self, sock: socket.socket) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.handle_line(line)

    def handle_line(self, line: bytes) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        tpv = parse_gpsd_tpv(obj) if isinstance(obj, dict) else None
        if tpv is not None:
            self.update(tpv["lat"], tpv["lon"], tpv["alt"], tpv["mode"], "gpsd")


# --- raw NMEA ------------------------------------------------------------------


def nmea_checksum_ok(sentence: str) -> bool:
    s = sentence.strip()
    if not s.startswith("$") or "*" not in s:
        return False
    body, _, given = s[1:].rpartition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value: str, hemi: str) -> Optional[float]:
    if not value or len(hemi) != 1 or hemi not in "NSEW":
        return None
    dot = value.find(".")
    deg_len = (dot if dot >= 0 else len(value)) - 2
    try:
        deg = float(value[:deg_len]) + float(value[deg_len:]) / 60.0
    except ValueError:
        return None
    return -deg if hemi in "SW" else deg


class NmeaParser:
    """Stateful GGA + RMC parser producing fixes."""

    def __init__(self) -> None:
        self.rmc_valid = True  # until an RMC says otherwise

    def feed(self, sentence: str) -> Optional[Dict[str, Any]]:
        if not nmea_checksum_ok(sentence):
            return None
        fields = sentence.strip()[1:].split("*")[0].split(",")
        kind = fields[0][2:]
        if kind == "RMC" and len(fields) > 6:
            self.rmc_valid = fields[2] == "A"
            return None
        if kind != "GGA" or len(fields) < 12 or not self.rmc_valid:
            return None
        try:
            quality = int(fields[6] or 0)
        except ValueError:
            return None
        lat, lon = _coord(fields[2], fields[3]), _coord(fields[4], fields[5])
        if quality == 0 or lat is None or lon is None:
            return None
        alt = None
        try:
            alt = float(fields[9]) + float(fields[11])  # MSL altitude + geoid separation = HAE
        except ValueError:
            pass
        return {"lat": lat, "lon": lon, "alt": alt, "mode": 3 if alt is not None else 2}


class NmeaSerialGps(PositionSource):
    """Reads NMEA directly from a serial port (pyserial). Not hardware-tested."""

    def _run(self) -> None:
        import serial  # optional dependency: pip install skynet-edge[serial]

        parser = NmeaParser()
        while not self._stop.is_set():
            try:
                with serial.Serial(self.cfg.nmea_device, self.cfg.nmea_baud, timeout=1.0) as port:
                    while not self._stop.is_set():
                        raw = port.readline()
                        if raw:
                            self.feed(parser, raw.decode("ascii", errors="replace"))
            except (OSError, serial.SerialException) as exc:
                log.warning("NMEA %s: %s", self.cfg.nmea_device, exc)
                self._stop.wait(5.0)

    def feed(self, parser: NmeaParser, sentence: str) -> None:
        got = parser.feed(sentence)
        if got is not None:
            self.update(got["lat"], got["lon"], got["alt"], got["mode"], "nmea")


def make_position_source(cfg: GpsConfig, clock: Callable[[], int] = time.time_ns) -> PositionSource:
    if cfg.source == "gpsd":
        return GpsdClient(cfg, clock)
    if cfg.source == "nmea":
        return NmeaSerialGps(cfg, clock)
    return PositionSource(cfg, clock)
