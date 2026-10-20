"""Camera model used by the solver, identical to the edge node's.

Attitude convention: skynet_common.camera (yaw = azimuth of the optical axis
clockwise from true north, pitch = its elevation, roll = right side down).
Intrinsics: pinhole fx, fy, cx, cy with pixel centres at integer coordinates
(OpenCV), and OpenCV radial distortion k1, k2 (p1 = p2 = k3 = 0), i.e. the
``dist = [k1, k2, 0, 0, 0]`` that ``skynet_edge.geometry.CameraModel``
feeds to cv2.undistortPoints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import numpy as np


def attitude_matrix(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """3x3 matrix whose rows are the camera right, down, forward axes in ENU.

    ``M @ d_enu`` gives camera-frame (x right, y down, z forward) coordinates.
    Same construction as skynet_common.camera.camera_basis_enu.
    """
    y, p, r = math.radians(yaw_deg), math.radians(pitch_deg), math.radians(roll_deg)
    fwd = np.array([math.cos(p) * math.sin(y), math.cos(p) * math.cos(y), math.sin(p)])
    right0 = np.array([math.cos(y), -math.sin(y), 0.0])
    down0 = np.cross(fwd, right0)
    cr, sr = math.cos(r), math.sin(r)
    right = cr * right0 + sr * down0
    down = cr * down0 - sr * right0
    return np.stack([right, down, fwd])


def matrix_to_attitude(m: np.ndarray, yaw_hint_deg: float = 0.0) -> Tuple[float, float, float]:
    """Inverse of :func:`attitude_matrix`. At pitch = +-90 deg yaw and roll are
    degenerate; ``yaw_hint_deg`` is then kept and the roll absorbs the rest."""
    fwd = m[2] / np.linalg.norm(m[2])
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[2])))))
    horiz = math.hypot(float(fwd[0]), float(fwd[1]))
    yaw = math.degrees(math.atan2(float(fwd[0]), float(fwd[1]))) % 360.0 if horiz > 1e-9 else yaw_hint_deg % 360.0
    y = math.radians(yaw)
    right0 = np.array([math.cos(y), -math.sin(y), 0.0])
    down0 = np.cross(fwd, right0)
    down0 /= np.linalg.norm(down0)
    right0 = np.cross(down0, fwd)
    roll = math.degrees(math.atan2(float(m[0] @ down0), float(m[0] @ right0)))
    return yaw, pitch, roll


def rotvec_matrix(w: np.ndarray) -> np.ndarray:
    """Rodrigues rotation matrix for a rotation vector (radians)."""
    th = float(np.linalg.norm(w))
    if th < 1e-15:
        return np.eye(3)
    k = w / th
    kx = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(th) * kx + (1.0 - math.cos(th)) * (kx @ kx)


@dataclass(frozen=True)
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    k1: float = 0.0
    k2: float = 0.0

    @classmethod
    def from_fov(cls, width: int, height: int, fov_h_deg: float) -> "Intrinsics":
        f = (width / 2.0) / math.tan(math.radians(fov_h_deg) / 2.0)
        return cls(f, f, (width - 1) / 2.0, (height - 1) / 2.0)

    def with_(self, **kw) -> "Intrinsics":
        return replace(self, **kw)

    @property
    def dist(self) -> Tuple[float, float, float, float, float]:
        return (self.k1, self.k2, 0.0, 0.0, 0.0)

    def max_r2(self) -> float:
        """Largest normalised r^2 for which the radial mapping is monotonic."""
        # d/dr [r (1 + k1 r^2 + k2 r^4)] = 1 + 3 k1 r^2 + 5 k2 r^4 > 0
        roots = np.roots([5.0 * self.k2, 3.0 * self.k1, 1.0]) if self.k2 != 0 else (
            np.array([-1.0 / (3.0 * self.k1)]) if self.k1 < 0 else np.array([]))
        pos = [float(r.real) for r in np.atleast_1d(roots) if abs(r.imag) < 1e-12 and r.real > 0]
        return min(pos) if pos else 1e6


def project_cam(xyz: np.ndarray, intr: Intrinsics, r2_max: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Camera-frame directions (N, 3) -> distorted pixels (N, 2) and a validity mask."""
    z = xyz[:, 2]
    ok = z > 1e-6
    zs = np.where(ok, z, 1.0)
    x = xyz[:, 0] / zs
    y = xyz[:, 1] / zs
    r2 = x * x + y * y
    lim = intr.max_r2() if r2_max is None else r2_max
    ok &= r2 < lim
    s = 1.0 + r2 * (intr.k1 + intr.k2 * r2)
    uv = np.stack([intr.fx * x * s + intr.cx, intr.fy * y * s + intr.cy], axis=1)
    return uv, ok


def unproject(uv: np.ndarray, intr: Intrinsics, iterations: int = 20) -> np.ndarray:
    """Distorted pixels (N, 2) -> unit camera-frame rays (N, 3)."""
    xd = (uv[:, 0] - intr.cx) / intr.fx
    yd = (uv[:, 1] - intr.cy) / intr.fy
    x, y = xd.copy(), yd.copy()
    if intr.k1 != 0.0 or intr.k2 != 0.0:
        for _ in range(iterations):
            r2 = x * x + y * y
            s = 1.0 + r2 * (intr.k1 + intr.k2 * r2)
            x = xd / s
            y = yd / s
    v = np.stack([x, y, np.ones_like(x)], axis=1)
    return v / np.linalg.norm(v, axis=1, keepdims=True)
