"""Optical flow where it earns its place.

* Sparse pyramidal Lucas-Kanade on small crops, used by the tracker to
  (a) give a brand-new track an initial velocity by flowing its first
  detection back into the previous frame, so a fast target's second
  detection falls inside the gate, and (b) bridge frames where a confirmed
  target dips below the detection threshold. Every LK result must pass a
  forward-backward consistency check. Crops keep the cost independent of
  the 1080p frame size (no full-frame pyramids).
* Coarse dense Farneback flow on a heavily downscaled frame every few
  frames: the cloud motion field. Clouds move coherently with the wind;
  a large diffuse blob whose image velocity matches the local field is
  almost certainly cloud structure, not an object.
"""

from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np


def lk_flow(prev: np.ndarray, cur: np.ndarray, u: float, v: float, win: int, levels: int,
            fb_tol_px: float = 0.5) -> Optional[Tuple[float, float]]:
    """Follow point (u, v) of ``prev`` into ``cur``; None if unreliable."""
    h, w = prev.shape
    reach = (win // 2 + 2) * (2 ** levels) + win
    x0, y0 = max(0, int(u) - reach), max(0, int(v) - reach)
    x1, y1 = min(w, int(u) + reach + 1), min(h, int(v) + reach + 1)
    if x1 - x0 < win or y1 - y0 < win:
        return None
    a = np.ascontiguousarray(prev[y0:y1, x0:x1])
    b = np.ascontiguousarray(cur[y0:y1, x0:x1])
    p0 = np.array([[[u - x0, v - y0]]], dtype=np.float32)
    params = dict(winSize=(win, win), maxLevel=levels,
                  criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    p1, st, _ = cv2.calcOpticalFlowPyrLK(a, b, p0, None, **params)
    if p1 is None or not st[0, 0]:
        return None
    back, st2, _ = cv2.calcOpticalFlowPyrLK(b, a, p1, None, **params)
    if back is None or not st2[0, 0] or float(np.hypot(*(back[0, 0] - p0[0, 0]))) > fb_tol_px:
        return None
    nu, nv = float(p1[0, 0, 0]) + x0, float(p1[0, 0, 1]) + y0
    if not (0 <= nu < w and 0 <= nv < h):
        return None
    return nu, nv


class CloudFlow:
    """Dense flow field of the sky at low resolution, in full-resolution px/s."""

    def __init__(self, width: int, height: int, every: int, small_width: int):
        self.width = width
        self.height = height
        self.every = max(1, every)
        self.sw = max(16, min(small_width, width))
        self.sh = max(9, int(round(height * self.sw / width)))
        self.scale = width / self.sw
        self._prev: Optional[np.ndarray] = None
        self._prev_t_ns = 0
        self._count = 0
        self.field: Optional[np.ndarray] = None  # (sh, sw, 2) px/s at full resolution
        self.field_t_ns = 0

    def update(self, y: np.ndarray, t_ns: int) -> bool:
        """Feed a frame; recomputes the field every ``every`` frames. Returns True when updated."""
        self._count += 1
        if self._prev is not None and self._count % self.every:
            return False
        small = cv2.resize(y, (self.sw, self.sh), interpolation=cv2.INTER_AREA)
        updated = False
        if self._prev is not None and t_ns > self._prev_t_ns:
            flow = cv2.calcOpticalFlowFarneback(self._prev, small, None, 0.5, 3, 9, 3, 5, 1.1, 0)
            dt = (t_ns - self._prev_t_ns) / 1e9
            self.field = flow * (self.scale / dt)
            self.field_t_ns = t_ns
            updated = True
        self._prev, self._prev_t_ns = small, t_ns
        return updated

    def velocity_at(self, u: float, v: float, radius_px: float) -> Optional[Tuple[float, float]]:
        """Median flow (px/s) in a window around a full-resolution point."""
        if self.field is None:
            return None
        r = max(1, int(round(radius_px / self.scale)))
        cx, cy = int(u / self.scale), int(v / self.scale)
        x0, x1 = max(0, cx - r), min(self.sw, cx + r + 1)
        y0, y1 = max(0, cy - r), min(self.sh, cy + r + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        patch = self.field[y0:y1, x0:x1].reshape(-1, 2)
        return float(np.median(patch[:, 0])), float(np.median(patch[:, 1]))
