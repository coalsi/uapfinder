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

from typing import List, Optional, Tuple

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
    """Dense flow field of the sky at low resolution, in full-resolution px/s.

    ``update`` only keeps a downscaled copy of every ``every``-th frame
    (cheap). The Farneback field between the last two copies is computed on
    the first ``velocity_at`` call after a new copy arrived, so it costs
    nothing unless the cloud filter has a diffuse candidate to check.
    """

    def __init__(self, width: int, height: int, every: int, small_width: int):
        self.width = width
        self.height = height
        self.every = max(1, every)
        self.sw = max(16, min(small_width, width))
        self.sh = max(9, int(round(height * self.sw / width)))
        self.scale = width / self.sw
        self._frames: List[Tuple[np.ndarray, int]] = []  # last two (small frame, t_ns)
        self._count = 0
        self._field: Optional[np.ndarray] = None  # (sh, sw, 2) px/s at full resolution
        self._field_t_ns = 0
        self.computed = 0

    def update(self, y: np.ndarray, t_ns: int) -> None:
        """Feed a frame; keeps a small copy every ``every`` frames."""
        self._count += 1
        if self._frames and self._count % self.every:
            return
        small = cv2.resize(y, (self.sw, self.sh), interpolation=cv2.INTER_AREA)
        self._frames = (self._frames + [(small, t_ns)])[-2:]

    def field(self) -> Optional[np.ndarray]:
        if len(self._frames) < 2:
            return None
        (a, ta), (b, tb) = self._frames
        if tb <= ta:
            return None
        if self._field_t_ns != tb:
            flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 2, 9, 3, 5, 1.1, 0)
            self._field = flow * (self.scale / ((tb - ta) / 1e9))
            self._field_t_ns = tb
            self.computed += 1
        return self._field

    def velocity_at(self, u: float, v: float, radius_px: float) -> Optional[Tuple[float, float]]:
        """Median flow (px/s) in a window around a full-resolution point."""
        field = self.field()
        if field is None:
            return None
        r = max(1, int(round(radius_px / self.scale)))
        cx, cy = int(u / self.scale), int(v / self.scale)
        x0, x1 = max(0, cx - r), min(self.sw, cx + r + 1)
        y0, y1 = max(0, cy - r), min(self.sh, cy + r + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        patch = field[y0:y1, x0:x1].reshape(-1, 2)
        return float(np.median(patch[:, 0])), float(np.median(patch[:, 1]))
