"""Sky mask: which pixels may produce detections.

Two independent restrictions, both optional:

* ``min_elevation_deg``: pixels whose line of sight is below this elevation
  (ground, rooftops, trees on the horizon, and the low sky where refraction
  and haze ruin triangulation) are ignored. Computed from the camera model
  and the *current* attitude, so it follows a BNO055-reported attitude.
* ``polygon``: pixel-coordinate polygon to ``include`` (only the inside is
  sky) or ``exclude`` (e.g. a chimney, a street light, a neighbour's
  window). Drawn with ``cv2.fillPoly``.

The mask is rebuilt only when the attitude changes by more than
``REBUILD_DEG``, so a static camera computes it once.
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from .config import SkyMaskConfig
from .geometry import Attitude, CameraModel

REBUILD_DEG = 0.1
_ELEVATION_STEP_PX = 8  # elevation varies smoothly; evaluate on a coarse grid and interpolate


def build_sky_mask(cfg: SkyMaskConfig, cam: CameraModel, att: Attitude) -> Optional[np.ndarray]:
    """Full-resolution bool mask (True = usable sky), or None if nothing is masked."""
    mask: Optional[np.ndarray] = None
    if cfg.min_elevation_deg is not None:
        el = cam.elevation_map(att, step=_ELEVATION_STEP_PX).astype(np.float32)
        el_full = cv2.resize(el, (cam.width, cam.height), interpolation=cv2.INTER_LINEAR)
        mask = el_full >= cfg.min_elevation_deg
    if cfg.polygon is not None:
        poly = np.zeros((cam.height, cam.width), dtype=np.uint8)
        cv2.fillPoly(poly, [np.round(np.asarray(cfg.polygon, dtype=np.float64)).astype(np.int32)], 255)
        inside = poly > 0
        region = inside if cfg.polygon_mode == "include" else ~inside
        mask = region if mask is None else mask & region
    if mask is not None and bool(mask.all()):
        return None
    return mask


class SkyMask:
    """Caches the mask for the latest attitude."""

    def __init__(self, cfg: SkyMaskConfig, cam: CameraModel):
        self.cfg = cfg
        self.cam = cam
        self._att: Optional[Attitude] = None
        self.mask: Optional[np.ndarray] = None
        self.sky_fraction = 1.0

    def update(self, att: Attitude) -> bool:
        """Rebuild if the attitude moved; returns True when the mask changed."""
        if self._att is not None and _attitude_delta(self._att, att) <= REBUILD_DEG:
            return False
        self._att = att
        self.mask = build_sky_mask(self.cfg, self.cam, att)
        self.sky_fraction = 1.0 if self.mask is None else float(self.mask.mean())
        return True


def _attitude_delta(a: Attitude, b: Attitude) -> float:
    dyaw = abs((a.yaw_deg - b.yaw_deg + 180.0) % 360.0 - 180.0)
    droll = abs((a.roll_deg - b.roll_deg + 180.0) % 360.0 - 180.0)
    return max(dyaw, abs(a.pitch_deg - b.pitch_deg), droll)
