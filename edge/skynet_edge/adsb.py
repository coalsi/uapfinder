"""ADS-B aircraft feed for the aircraft false-positive filter.

Two formats are read:

* ``dump1090``: readsb / dump1090-fa ``aircraft.json`` (local receiver, by
  file path or URL, e.g. ``http://127.0.0.1/tar1090/data/aircraft.json``).
  ``now`` is Unix seconds and the list is under ``aircraft``.
* ``adsbx_v2``: the ADS-B Exchange v2 JSON shape (list under ``ac``, ``now``
  in milliseconds), also served by readsb's API with ``&jv2`` and by
  compatible aggregators. The URL may contain ``{lat}``, ``{lon}`` and
  ``{dist}`` (nautical miles) placeholders filled from the node position;
  an API key is sent in a configurable header (``api-auth`` for ADSBx).

Per aircraft we use ``lat``/``lon``, ``alt_geom`` (feet above the WGS84
ellipsoid, the same reference as our node altitude) or, failing that,
``alt_baro`` (pressure altitude: off by the geoid separation and the
weather, so the filter uses a wider gate), ``gs`` (knots), ``track``
(degrees true), ``geom_rate``/``baro_rate`` (ft/min) and ``seen_pos``
(seconds before ``now`` the position was received). Positions are
extrapolated along the great circle to each frame time before being turned
into az/el with ``skynet_common.geodesy.look_angles``.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from skynet_common.geodesy import EARTH_MEAN_RADIUS_M, look_angles

from .config import AdsbConfig

log = logging.getLogger(__name__)

FT_TO_M = 0.3048
KT_TO_MPS = 1852.0 / 3600.0
FPM_TO_MPS = FT_TO_M / 60.0


@dataclass(frozen=True)
class Aircraft:
    hex: str
    flight: str
    lat: float
    lon: float
    alt_m: float
    alt_geometric: bool  # True: alt_geom (WGS84 HAE); False: barometric fallback
    gs_mps: Optional[float]
    track_deg: Optional[float]
    vrate_mps: float
    t_pos_ns: int  # time the position was valid

    def position_at(self, t_ns: int) -> Tuple[float, float, float]:
        """Dead-reckoned (lat, lon, alt) at ``t_ns``."""
        dt = (t_ns - self.t_pos_ns) / 1e9
        lat, lon = self.lat, self.lon
        if self.gs_mps and self.track_deg is not None and dt != 0.0:
            lat, lon = destination(lat, lon, self.track_deg, self.gs_mps * dt)
        return lat, lon, self.alt_m + self.vrate_mps * dt

    def azel_from(self, obs_lat: float, obs_lon: float, obs_alt: float, t_ns: int) -> Tuple[float, float, float]:
        lat, lon, alt = self.position_at(t_ns)
        return look_angles(obs_lat, obs_lon, obs_alt, lat, lon, alt)


def destination(lat: float, lon: float, bearing_deg: float, distance_m: float) -> Tuple[float, float]:
    """Great-circle destination on a mean-radius sphere (ample for a few km of dead reckoning)."""
    d = distance_m / EARTH_MEAN_RADIUS_M
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(bearing_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_aircraft_json(data: Dict[str, Any], received_ns: Optional[int] = None) -> List[Aircraft]:
    """Parse readsb/dump1090 ``aircraft.json`` or an ADSBx-v2-style response.

    ``received_ns`` (default: now) stands in for a missing ``now`` field;
    some ADSBx-compatible endpoints omit it.
    """
    items = data.get("ac") if "ac" in data else data.get("aircraft")
    now = _num(data.get("now"))
    if now is None:
        now_s = (time.time_ns() if received_ns is None else received_ns) / 1e9
    else:
        now_s = now / 1000.0 if now > 1e11 else now  # ADSBx v2 uses milliseconds, readsb files seconds
    out: List[Aircraft] = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        lat, lon = _num(a.get("lat")), _num(a.get("lon"))
        if lat is None or lon is None:
            continue
        geom = _num(a.get("alt_geom"))
        baro = _num(a.get("alt_baro"))  # may be the string "ground"
        if geom is None and baro is None:
            continue
        seen_pos = _num(a.get("seen_pos")) or 0.0
        gs, trk = _num(a.get("gs")), _num(a.get("track"))
        rate = _num(a.get("geom_rate")) if geom is not None else None
        rate = rate if rate is not None else _num(a.get("baro_rate"))
        out.append(Aircraft(
            hex=str(a.get("hex", "")).strip().lower(),
            flight=str(a.get("flight", "")).strip(),
            lat=lat, lon=lon,
            alt_m=(geom if geom is not None else baro) * FT_TO_M,
            alt_geometric=geom is not None,
            gs_mps=None if gs is None else gs * KT_TO_MPS,
            track_deg=trk,
            vrate_mps=(rate or 0.0) * FPM_TO_MPS,
            t_pos_ns=int((now_s - seen_pos) * 1e9),
        ))
    return out


PositionFn = Callable[[], Optional[Tuple[float, float, float]]]


class AdsbFeed:
    """Polls the configured source in a background thread and keeps the latest aircraft list."""

    def __init__(self, cfg: AdsbConfig, position: PositionFn, clock: Callable[[], int] = time.time_ns):
        self.cfg = cfg
        self._position = position
        self._clock = clock
        self._lock = threading.Lock()
        self._aircraft: List[Aircraft] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()
        self.errors = 0
        self.last_ok_ns = 0

    @property
    def enabled(self) -> bool:
        return self.cfg.source != "none"

    def start(self) -> None:
        if not self.enabled:
            return
        self.poll_once()
        self._thread = threading.Thread(target=self._run, name="adsb", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.cfg.poll_interval_s):
            self.poll_once()

    def _fetch(self) -> Optional[Dict[str, Any]]:
        cfg = self.cfg
        if cfg.file_path:
            return json.loads(Path(cfg.file_path).read_text(encoding="utf-8"))
        url = str(cfg.url)
        if cfg.source == "adsbx_v2":
            pos = self._position()
            if pos is None:
                return None
            url = url.format(lat=f"{pos[0]:.4f}", lon=f"{pos[1]:.4f}", dist=f"{cfg.radius_nm:.0f}")
        headers = {cfg.api_key_header: cfg.api_key} if cfg.api_key else {}
        resp = self._session.get(url, headers=headers, timeout=cfg.timeout_s)
        resp.raise_for_status()
        return resp.json()

    def poll_once(self) -> None:
        try:
            data = self._fetch()
            if data is None:
                return
            aircraft = parse_aircraft_json(data, self._clock())
        except (OSError, ValueError, requests.RequestException) as exc:
            self.errors += 1
            log.warning("ADS-B poll failed: %s", exc)
            return
        with self._lock:
            self._aircraft = aircraft
        self.last_ok_ns = self._clock()

    def aircraft(self, t_ns: int) -> List[Aircraft]:
        """Aircraft whose last position is recent enough to extrapolate to ``t_ns``."""
        max_age = int(self.cfg.max_position_age_s * 1e9)
        with self._lock:
            return [a for a in self._aircraft if abs(t_ns - a.t_pos_ns) <= max_age]

    def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {}
        with self._lock:
            n = len(self._aircraft)
        age = (self._clock() - self.last_ok_ns) / 1e9 if self.last_ok_ns else None
        return {"adsb_aircraft": n, "adsb_age_s": None if age is None else round(age, 1),
                "adsb_errors": self.errors}
