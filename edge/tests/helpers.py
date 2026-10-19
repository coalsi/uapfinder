"""Shared test helpers: small configurations and synthetic frames."""

from __future__ import annotations

import copy
import math
from typing import Any, Dict, Optional, Tuple

import numpy as np

from skynet_edge.config import EdgeConfig, parse_config
from skynet_edge.geometry import Attitude
from skynet_edge.tracking import TrackSample

T0_NS = 1_790_000_000_000_000_000
MASTER_KEY = b"m" * 32
OBSERVER = (33.6292, -112.3679, 357.2)
ATTITUDE = Attitude(200.0, 40.0, 2.0)


def merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def small_config(extra: Optional[Dict[str, Any]] = None, width: int = 640, height: int = 360,
                 fps: float = 30.0) -> EdgeConfig:
    """A 640x360@30 camera with a known attitude and surveyed position."""
    base = {
        "node": {"node_id": "node-test"},
        "server": {"transport": "none"},
        "camera": {"source": "synthetic", "width": width, "height": height, "fps": fps, "fov_h_deg": 60.0,
                   "realtime": False},
        "attitude": {"source": "static", "yaw_deg": ATTITUDE.yaw_deg, "pitch_deg": ATTITUDE.pitch_deg,
                     "roll_deg": ATTITUDE.roll_deg},
        "gps": {"source": "static", "static": {"latitude": OBSERVER[0], "longitude": OBSERVER[1],
                                               "altitude_m": OBSERVER[2]}},
        "detection": {"sky_mask": {"min_elevation_deg": None}},
        "ring_buffer": {"seconds": 4.0, "hold_seconds": 2.0, "prefault": False},
        "clips": {"pre_s": 2.0, "post_s": 1.0},
        "health": {"watchdog": False, "chrony_offset": False},
    }
    return parse_config(merge(base, extra or {}))


def sky_frame(width: int, height: int, rng: np.random.Generator, level: float = 80.0,
              noise: float = 1.5) -> np.ndarray:
    return np.clip(rng.normal(level, noise, (height, width)), 0, 255).astype(np.uint8)


def add_gaussian(y: np.ndarray, u: float, v: float, amp: float, sigma: float = 0.8) -> None:
    r = int(math.ceil(4 * sigma))
    x0, y0 = int(math.floor(u)) - r, int(math.floor(v)) - r
    xs = np.arange(x0, x0 + 2 * r + 2) - u
    ys = np.arange(y0, y0 + 2 * r + 2) - v
    g = amp * np.exp(-(ys[:, None] ** 2 + xs[None, :] ** 2) / (2 * sigma * sigma))
    roi = y[y0:y0 + g.shape[0], x0:x0 + g.shape[1]]
    roi[...] = np.clip(roi + g[:roi.shape[0], :roi.shape[1]], 0, 255).astype(np.uint8)


def sample(t_s: float, az: float, el: float, *, u: float = 0.0, v: float = 0.0, area: int = 6,
           peak: float = 30.0, snr: float = 15.0, eff_area: float = 4.0, source: str = "det") -> TrackSample:
    return TrackSample(T0_NS + int(round(t_s * 1e9)), u, v, az, el, area, peak, snr, eff_area, 0.5, 1, source)


def path_samples(fn, duration_s: float, fps: float, **kw: Any):
    """TrackSamples along ``fn(t) -> (az, el)``."""
    n = int(round(duration_s * fps)) + 1
    return [sample(i / fps, *fn(i / fps), **kw) for i in range(n)]


def gnomonic_path(az0: float, el0: float, x_of_t, y_of_t):
    """Direction for tangent-plane offsets (deg) around (az0, el0); x east-ish, y up."""
    a, e = math.radians(az0), math.radians(el0)
    r0 = np.array([math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e)])
    e_az = np.array([math.cos(a), -math.sin(a), 0.0])
    e_el = np.array([-math.sin(e) * math.sin(a), -math.sin(e) * math.cos(a), math.cos(e)])

    def fn(t: float) -> Tuple[float, float]:
        d = r0 + math.tan(math.radians(x_of_t(t))) * e_az + math.tan(math.radians(y_of_t(t))) * e_el
        d /= np.linalg.norm(d)
        return math.degrees(math.atan2(d[0], d[1])) % 360.0, math.degrees(math.asin(d[2]))

    return fn
