"""Synthetic sky video with exact ground truth, for tests, the bench and demos.

There is no camera here, so the pipeline is validated end to end on rendered
frames from a camera with a known attitude, intrinsics and position. Every
object is defined by its true line of sight (az/el) over time and projected
with the same ``CameraModel`` the node uses, so a reported ``vector_target``
can be compared with the truth to well below a pixel.

The standard scene contains one object of each class the filters must
separate (positions are given in fractions of the field of view so the scene
works at any resolution):

* ``target``: a point source flying straight, making a hard 90 deg turn in
  0.25 s, and flying straight again. No ballistic or aircraft path does this,
  so it must be reported with a high anomaly score.
* ``cloud``: a large, low-contrast diffuse patch that brightens over one
  second and drifts at 0.5 deg/s (filtered as cloud).
* ``bird``: a dark, defocused silhouette close to the lens, flapping at 8 Hz
  (area oscillation) and growing as it approaches (filtered as bird).
* ``aircraft``: an airliner at about 9 km altitude crossing the field, with a
  matching ADS-B record from :meth:`Scene.adsb_json` (filtered as aircraft).

The sky gradient and three planes of Gaussian noise are precomputed; each
frame adds one noise plane at a pseudo-random column offset, so the noise
never repeats in a way the rolling background could learn, and a 1080p
frame renders in about 2 ms. The bench measures the pipeline, not the
renderer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from skynet_common.camera import camera_basis_enu
from skynet_common.geodesy import ecef_to_geodetic, enu_to_azel, enu_to_ecef_vector, geodetic_to_ecef

from .adsb import Aircraft, KT_TO_MPS, FT_TO_M
from .config import EdgeConfig
from .geometry import Attitude, CameraModel

DEFAULT_T0_NS = 1_790_000_000_000_000_000  # 2026-09-21T13:33:20Z
DEFAULT_OBSERVER = (33.6292, -112.3679, 357.2)

AzElFn = Callable[[float], Tuple[float, float]]


def boresight_offset_azel(att: Attitude, x_deg: float, y_deg: float) -> Tuple[float, float]:
    """Az/el of the direction ``x_deg`` right and ``y_deg`` up of the optical axis (gnomonic)."""
    right, down, forward = camera_basis_enu(att.yaw_deg, att.pitch_deg, att.roll_deg)
    tx, ty = math.tan(math.radians(x_deg)), math.tan(math.radians(y_deg))
    d = [forward[i] + tx * right[i] - ty * down[i] for i in range(3)]
    return enu_to_azel(d[0], d[1], d[2])


@dataclass
class SceneObject:
    name: str
    kind: str  # "point", "cloud", "bird", "aircraft"
    azel: AzElFn  # scene time (s) -> true line of sight
    start_s: float = 0.0
    end_s: float = float("inf")
    params: Dict[str, Any] = field(default_factory=dict)

    def visible(self, t: float) -> bool:
        return self.start_s <= t <= self.end_s


class Scene:
    """Renders frames of a list of objects for one camera."""

    _NOISE_PLANES = 3
    _NOISE_MARGIN = 97  # extra noise columns; a prime, so offsets cycle slowly

    def __init__(self, cam: CameraModel, att: Attitude, objects: List[SceneObject], t0_ns: int,
                 fps: float, duration_s: float, observer: Tuple[float, float, float],
                 aircraft: Optional[Aircraft] = None, seed: int = 1, noise_dn: float = 1.5,
                 loop: bool = True):
        self.cam = cam
        self.att = att
        self.objects = objects
        self.t0_ns = t0_ns
        self.fps = fps
        self.duration_s = duration_s
        self.observer = observer
        self.aircraft = aircraft
        self.loop = loop
        w, h = cam.width, cam.height
        self.seed = seed
        rng = np.random.default_rng(seed)
        el = cv2.resize(cam.elevation_map(att, step=8).astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        sky = 75.0 + 45.0 * (1.0 - np.sin(np.radians(np.clip(el, 0.0, 90.0))))  # brighter towards the horizon
        self._sky = np.rint(sky).astype(np.int16)
        self._noise = [np.clip(np.rint(rng.normal(0.0, noise_dn, (h, w + self._NOISE_MARGIN))), -127, 127)
                       .astype(np.int8) for _ in range(self._NOISE_PLANES)]
        self._tmp = np.empty((h, w), dtype=np.int16)
        self._buf = np.full((h * 3 // 2, w), 128, dtype=np.uint8)
        self._cloud_sprites: Dict[str, np.ndarray] = {}

    # --- truth --------------------------------------------------------------

    def scene_time(self, t_ns: int) -> float:
        t = (t_ns - self.t0_ns) / 1e9
        return t % self.duration_s if self.loop else t

    def truth(self, name: str, t_ns: int) -> Optional[Tuple[float, float]]:
        """True az/el of a named object at a frame time, None when not visible."""
        t = self.scene_time(t_ns)
        for obj in self.objects:
            if obj.name == name and obj.visible(t):
                return obj.azel(t)
        return None

    def adsb_json(self, t_ns: Optional[int] = None) -> Dict[str, Any]:
        """A readsb/dump1090 ``aircraft.json`` describing the scene's aircraft at ``t_ns``."""
        t_ns = self.t0_ns if t_ns is None else t_ns
        out: Dict[str, Any] = {"now": t_ns / 1e9, "messages": 0, "aircraft": []}
        ac = self.aircraft
        if ac is not None:
            lat, lon, alt = ac.position_at(t_ns)
            out["aircraft"].append({
                "hex": ac.hex, "flight": ac.flight + " ", "lat": lat, "lon": lon,
                "alt_geom": alt / FT_TO_M, "alt_baro": alt / FT_TO_M - 100.0,
                "gs": (ac.gs_mps or 0.0) / KT_TO_MPS, "track": ac.track_deg, "geom_rate": 0,
                "seen_pos": 0.0, "seen": 0.0, "type": "adsb_icao",
            })
        return out

    # --- rendering ----------------------------------------------------------

    def render(self, i: int, t_ns: int) -> np.ndarray:
        """I420 frame for frame index ``i`` at ``t_ns`` (the returned buffer is reused)."""
        t = self.scene_time(t_ns)
        h, w = self.cam.height, self.cam.width
        y = self._buf[:h]
        pick = (i * 2654435761 + self.seed) & 0xFFFFFFFF  # Knuth multiplicative hash of the frame index
        plane, off = pick % self._NOISE_PLANES, (pick >> 8) % self._NOISE_MARGIN
        cv2.add(self._sky, self._noise[plane][:, off:off + w], dst=self._tmp, dtype=cv2.CV_16S)
        cv2.convertScaleAbs(self._tmp, y)  # sky > 0, so abs() is a no-op; saturates at 255
        for obj in self.objects:
            if not obj.visible(t):
                continue
            az, el = obj.azel(t)
            uv = self.cam.azel_to_pixels(np.array([[az, el]]), self.att)[0]
            if not np.all(np.isfinite(uv)):
                continue
            draw = getattr(self, f"_draw_{obj.kind}")
            draw(y, float(uv[0]), float(uv[1]), t - obj.start_s, obj.params)
        return self._buf

    @staticmethod
    def _add(y: np.ndarray, sprite: np.ndarray, x0: int, y0: int) -> None:
        """Add a signed float sprite at integer offset (x0, y0) with clipping."""
        h, w = y.shape
        sh, sw = sprite.shape
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(w, x0 + sw), min(h, y0 + sh)
        if xa >= xb or ya >= yb:
            return
        roi = y[ya:yb, xa:xb]
        patch = roi.astype(np.float32) + sprite[ya - y0:yb - y0, xa - x0:xb - x0]
        np.copyto(roi, np.clip(np.rint(patch), 0, 255).astype(np.uint8))

    def _draw_point(self, y: np.ndarray, u: float, v: float, age: float, p: Dict[str, Any]) -> None:
        sigma, amp = p.get("psf_sigma_px", 0.8), p.get("amplitude_dn", 40.0)
        r = int(math.ceil(4 * sigma))
        x0, y0 = int(math.floor(u)) - r, int(math.floor(v)) - r
        xs = np.arange(x0, x0 + 2 * r + 2, dtype=np.float32) - u
        ys = np.arange(y0, y0 + 2 * r + 2, dtype=np.float32) - v
        sprite = amp * np.exp(-(ys[:, None] ** 2 + xs[None, :] ** 2) / (2.0 * sigma * sigma))
        self._add(y, sprite.astype(np.float32), x0, y0)

    _draw_aircraft = _draw_point

    def _draw_cloud(self, y: np.ndarray, u: float, v: float, age: float, p: Dict[str, Any]) -> None:
        sigma = p["sigma_px"]
        key = f"{sigma:.2f}"
        sprite = self._cloud_sprites.get(key)
        if sprite is None:
            r = int(3 * sigma)
            g = np.arange(-r, r + 1, dtype=np.float32)
            # Two overlapping lobes give the patch some structure.
            sprite = (np.exp(-(g[:, None] ** 2 + g[None, :] ** 2) / (2 * sigma ** 2))
                      + 0.6 * np.exp(-((g[:, None] - 0.4 * sigma) ** 2 + (g[None, :] + 0.5 * sigma) ** 2)
                                     / (2 * (0.6 * sigma) ** 2))).astype(np.float32)
            sprite /= sprite.max()
            self._cloud_sprites[key] = sprite
        fade = min(1.0, age / p.get("fade_in_s", 1.0))
        r = sprite.shape[0] // 2
        self._add(y, sprite * (p["amplitude_dn"] * fade), int(round(u)) - r, int(round(v)) - r)

    def _draw_bird(self, y: np.ndarray, u: float, v: float, age: float, p: Dict[str, Any]) -> None:
        scale = 1.0 + p.get("growth_per_s", 0.0) * age
        flap = abs(math.sin(math.pi * p.get("wingbeat_hz", 8.0) * age))  # |sin|: wings up and down
        body = p["body_px"] * scale
        span = p["span_px"] * scale * (0.35 + 0.65 * flap)
        size = int(math.ceil(2 * max(span, body) + 12))
        mask = np.zeros((size, size), dtype=np.uint8)
        c = size // 2
        cv2.ellipse(mask, (c, c), (max(1, int(body)), max(1, int(body * 0.4))), 0, 0, 360, 255, -1)
        cv2.ellipse(mask, (c, c - int(body * 0.1)), (max(1, int(span)), max(1, int(body * 0.25))), 0, 0, 360, 255, -1)
        blurred = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), p.get("blur_px", 1.5))
        self._add(y, blurred * -p["contrast_dn"], int(round(u)) - c, int(round(v)) - c)


def _straight_with_turn(att: Attitude, fov_h: float, fov_v: float, speed_deg_s: float,
                        t_turn: float, turn_s: float, turn_deg: float) -> AzElFn:
    """Image-plane path: straight, a constant-rate turn, straight again (angles from the boresight)."""
    x0, y0 = -0.32 * fov_h, 0.22 * fov_v
    heading0 = math.radians(-10.0)  # slightly downwards, to the right
    omega = math.radians(turn_deg) / turn_s

    def pos(t: float) -> Tuple[float, float]:
        t1 = min(t, t_turn)
        x = x0 + speed_deg_s * t1 * math.cos(heading0)
        yy = y0 + speed_deg_s * t1 * math.sin(heading0)
        if t > t_turn:
            tau = min(t - t_turn, turn_s)
            # Arc of radius v / omega, then a straight leg on the new heading.
            radius = speed_deg_s / omega
            h1 = heading0 - omega * tau  # turning clockwise in the image (right turn)
            x += radius * (math.sin(heading0) - math.sin(h1))
            yy += radius * (math.cos(h1) - math.cos(heading0))
            if t > t_turn + turn_s:
                rest = t - t_turn - turn_s
                x += speed_deg_s * rest * math.cos(h1)
                yy += speed_deg_s * rest * math.sin(h1)
        return x, yy

    return lambda t: boresight_offset_azel(att, *pos(t))


def _linear_path(att: Attitude, x0: float, y0: float, vx: float, vy: float) -> AzElFn:
    return lambda t: boresight_offset_azel(att, x0 + vx * t, y0 + vy * t)


def make_aircraft(observer: Tuple[float, float, float], att: Attitude, t0_ns: int, t_mid_s: float,
                  x_deg: float, y_deg: float, alt_m: float = 9000.0, gs_mps: float = 230.0) -> Aircraft:
    """An aircraft crossing the field of view, seen at offset (x, y) from the boresight at ``t_mid_s``."""
    lat, lon, h = observer
    az, el = boresight_offset_azel(att, x_deg, y_deg)
    rng_m = (alt_m - h) / math.sin(math.radians(max(el, 5.0)))
    e = math.cos(math.radians(el)) * math.sin(math.radians(az))
    n = math.cos(math.radians(el)) * math.cos(math.radians(az))
    u = math.sin(math.radians(el))
    o = geodetic_to_ecef(lat, lon, h)
    d = enu_to_ecef_vector(lat, lon, (e * rng_m, n * rng_m, u * rng_m))
    mlat, mlon, malt = ecef_to_geodetic(o[0] + d[0], o[1] + d[1], o[2] + d[2])
    track = (att.yaw_deg + 90.0) % 360.0  # crossing the view left to right
    mid = Aircraft("a1b2c3", "SKY123", mlat, mlon, malt, True, gs_mps, track, 0.0, t0_ns + int(t_mid_s * 1e9))
    lat0, lon0, alt0 = mid.position_at(t0_ns)
    return Aircraft(mid.hex, mid.flight, lat0, lon0, alt0, True, gs_mps, track, 0.0, t0_ns)


def standard_scene(cfg: EdgeConfig, t0_ns: int = DEFAULT_T0_NS, seed: int = 1, duration_s: float = 8.0,
                   loop: bool = True) -> Scene:
    """The four-object scene described in the module docstring, for ``cfg``'s camera."""
    cam = CameraModel.from_config(cfg.camera)
    a = cfg.attitude
    att = Attitude(a.yaw_deg % 360.0, a.pitch_deg, a.roll_deg)
    observer = DEFAULT_OBSERVER
    if cfg.gps.static is not None:
        observer = (cfg.gps.static.latitude, cfg.gps.static.longitude, cfg.gps.static.altitude_m)
    fov_h, fov_v = cam.fov_h_deg, cam.fov_v_deg
    aircraft = make_aircraft(observer, att, t0_ns, duration_s / 2, -0.05 * fov_h, -0.30 * fov_v)

    def aircraft_azel(t: float) -> Tuple[float, float]:
        az, el, _ = aircraft.azel_from(*observer, t0_ns + int(round(t * 1e9)))
        return az, el

    objects = [
        SceneObject("cloud", "cloud", _linear_path(att, 0.22 * fov_h, 0.18 * fov_v, -0.5, 0.0), start_s=0.5,
                    params={"sigma_px": 0.045 * cam.width, "amplitude_dn": 30.0, "fade_in_s": 1.0}),
        SceneObject("aircraft", "aircraft", aircraft_azel, start_s=0.0,
                    params={"psf_sigma_px": 0.9, "amplitude_dn": 45.0}),
        SceneObject("target", "point", _straight_with_turn(att, fov_h, fov_v, 0.06 * fov_h, 2.5, 0.25, 90.0),
                    start_s=0.3, end_s=6.5, params={"psf_sigma_px": 0.8, "amplitude_dn": 35.0}),
        SceneObject("bird", "bird", _linear_path(att, -0.40 * fov_h, -0.15 * fov_v, 0.18 * fov_h, 0.02 * fov_v),
                    start_s=1.0, end_s=5.0,
                    params={"body_px": 0.016 * cam.width, "span_px": 0.04 * cam.width,
                            "wingbeat_hz": 8.0, "growth_per_s": 0.12, "contrast_dn": 45.0, "blur_px": 1.5}),
    ]
    return Scene(cam, att, objects, t0_ns, cfg.camera.fps, duration_s, observer, aircraft, seed=seed, loop=loop)
