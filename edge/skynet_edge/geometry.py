"""Camera geometry on the edge: distorted pixel <-> line-of-sight az/el.

The shared ``skynet_common.camera`` module defines the attitude convention
(yaw = azimuth of the optical axis from true north, pitch = elevation, roll =
right-side-down) for an ideal pinhole. Real lenses distort, so every
measured centroid is first passed through ``cv2.undistortPoints`` with the
calibrated intrinsics and then converted with ``pixel_to_azel`` from the
shared package, keeping one source of truth for the attitude maths.

Pixel coordinates follow OpenCV: the centre of pixel (0, 0) is at (0.0, 0.0).
That is the convention of a checkerboard calibration and of the detector's
intensity-weighted centroids. When only a field of view is configured the
principal point is therefore placed at ((W-1)/2, (H-1)/2), not W/2, to avoid
a systematic half-pixel bias.

The vectorised helpers (elevation map for the sky mask, synthetic rendering)
re-implement the same rotation with NumPy; tests assert they agree with the
scalar shared functions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from skynet_common.camera import PinholeCamera, camera_basis_enu, pixel_to_azel

from .config import CameraConfig

import cv2

_UNDISTORT_CRITERIA = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 1e-9)


@dataclass(frozen=True)
class Attitude:
    """Camera optical-axis attitude, the ``imu_orientation`` of the protocol."""

    yaw_deg: float
    pitch_deg: float
    roll_deg: float

    def as_message(self) -> dict:
        return {"pitch_deg": round(self.pitch_deg, 4), "roll_deg": round(self.roll_deg, 4),
                "yaw_deg": round(self.yaw_deg % 360.0, 4)}


class CameraModel:
    """Intrinsics + distortion of the sky camera."""

    def __init__(self, width: int, height: int, fx: float, fy: float, cx: float, cy: float,
                 dist: Sequence[float] = ()):
        self.width = width
        self.height = height
        self.pinhole = PinholeCamera(width, height, fx, fy, cx, cy)
        self.K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
        self.dist = np.asarray(list(dist), dtype=np.float64)
        self.has_distortion = bool(self.dist.size) and bool(np.any(self.dist != 0.0))

    @classmethod
    def from_fov(cls, width: int, height: int, fov_h_deg: float, fov_v_deg: Optional[float] = None,
                 dist: Sequence[float] = ()) -> "CameraModel":
        fx = (width / 2.0) / math.tan(math.radians(fov_h_deg) / 2.0)
        fy = fx if fov_v_deg is None else (height / 2.0) / math.tan(math.radians(fov_v_deg) / 2.0)
        return cls(width, height, fx, fy, (width - 1) / 2.0, (height - 1) / 2.0, dist)

    @classmethod
    def from_config(cls, cam: CameraConfig) -> "CameraModel":
        if cam.intrinsics is not None:
            i = cam.intrinsics
            return cls(cam.width, cam.height, i.fx, i.fy, i.cx, i.cy, i.dist)
        return cls.from_fov(cam.width, cam.height, cam.fov_h_deg, cam.fov_v_deg)

    @property
    def fov_h_deg(self) -> float:
        return self.pinhole.fov_h_deg

    @property
    def fov_v_deg(self) -> float:
        return self.pinhole.fov_v_deg

    @property
    def deg_per_px(self) -> float:
        return self.pinhole.deg_per_pixel

    # --- distortion -------------------------------------------------------

    def undistort(self, uv: np.ndarray) -> np.ndarray:
        """Distorted pixel coordinates (N, 2) -> ideal pinhole pixel coordinates."""
        uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2)
        if not self.has_distortion or uv.size == 0:
            return uv
        # undistortPoints' fixed 5 iterations leave ~0.002-0.1 px residuals in
        # the corners of a wide lens; iterate to convergence instead. OpenCV 4.x
        # (JetPack ships 4.8) names this undistortPointsIter; 5.x folded it
        # into undistortPoints(criteria=...).
        src = uv.reshape(-1, 1, 2)
        if hasattr(cv2, "undistortPointsIter"):
            out = cv2.undistortPointsIter(src, self.K, self.dist, None, self.K, _UNDISTORT_CRITERIA)
        else:
            out = cv2.undistortPoints(src, self.K, self.dist, P=self.K, criteria=_UNDISTORT_CRITERIA)
        return out.reshape(-1, 2)

    def distort(self, uv_ideal: np.ndarray) -> np.ndarray:
        """Ideal pinhole pixel coordinates (N, 2) -> distorted (as the sensor sees them)."""
        uv_ideal = np.asarray(uv_ideal, dtype=np.float64).reshape(-1, 2)
        if not self.has_distortion or uv_ideal.size == 0:
            return uv_ideal
        x = (uv_ideal[:, 0] - self.K[0, 2]) / self.K[0, 0]
        y = (uv_ideal[:, 1] - self.K[1, 2]) / self.K[1, 1]
        pts = np.stack([x, y, np.ones_like(x)], axis=1).reshape(-1, 1, 3)
        out, _ = cv2.projectPoints(pts, np.zeros(3), np.zeros(3), self.K, self.dist)
        return out.reshape(-1, 2)

    # --- az/el ------------------------------------------------------------

    def pixel_to_azel(self, u: float, v: float, att: Attitude) -> Tuple[float, float]:
        (ui, vi), = self.undistort(np.array([[u, v]]))
        return pixel_to_azel(self.pinhole, float(ui), float(vi), att.yaw_deg, att.pitch_deg, att.roll_deg)

    def pixels_to_azel(self, uv: np.ndarray, att: Attitude) -> np.ndarray:
        """Vectorised (N, 2) distorted pixels -> (N, 2) [az, el] degrees."""
        ideal = self.undistort(uv)
        enu = self._ideal_to_enu(ideal[:, 0], ideal[:, 1], att)
        return enu_to_azel_array(enu)

    def azel_to_pixels(self, azel: np.ndarray, att: Attitude) -> np.ndarray:
        """Vectorised (N, 2) [az, el] -> (N, 2) distorted pixels; NaN behind the camera."""
        azel = np.asarray(azel, dtype=np.float64).reshape(-1, 2)
        d = azel_to_enu_array(azel[:, 0], azel[:, 1])
        right, down, forward = (np.asarray(b) for b in camera_basis_enu(att.yaw_deg, att.pitch_deg, att.roll_deg))
        x, y, z = d @ right, d @ down, d @ forward
        with np.errstate(divide="ignore", invalid="ignore"):
            u = np.where(z > 1e-9, self.K[0, 0] * x / z + self.K[0, 2], np.nan)
            v = np.where(z > 1e-9, self.K[1, 1] * y / z + self.K[1, 2], np.nan)
        ideal = np.stack([u, v], axis=1)
        ok = np.isfinite(ideal).all(axis=1)
        out = np.full_like(ideal, np.nan)
        out[ok] = self.distort(ideal[ok])
        return out

    def _ideal_to_enu(self, u: np.ndarray, v: np.ndarray, att: Attitude) -> np.ndarray:
        right, down, forward = (np.asarray(b) for b in camera_basis_enu(att.yaw_deg, att.pitch_deg, att.roll_deg))
        x = (np.asarray(u) - self.K[0, 2]) / self.K[0, 0]
        y = (np.asarray(v) - self.K[1, 2]) / self.K[1, 1]
        return x[..., None] * right + y[..., None] * down + forward

    def elevation_map(self, att: Attitude, step: int = 1) -> np.ndarray:
        """Elevation (deg) of the centre of each ``step`` x ``step`` pixel block.

        Used to mask the ground/horizon. Undistortion runs once per call on
        every block centre, so callers cache the result and only recompute
        when the attitude changes.
        """
        hs, ws = self.height // step, self.width // step
        us = np.arange(ws) * step + (step - 1) / 2.0
        vs = np.arange(hs) * step + (step - 1) / 2.0
        uu, vv = np.meshgrid(us, vs)
        ideal = self.undistort(np.stack([uu.ravel(), vv.ravel()], axis=1))
        enu = self._ideal_to_enu(ideal[:, 0], ideal[:, 1], att)
        el = np.degrees(np.arctan2(enu[:, 2], np.hypot(enu[:, 0], enu[:, 1])))
        return el.reshape(hs, ws)


def azel_to_enu_array(az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    az = np.radians(np.asarray(az_deg, dtype=np.float64))
    el = np.radians(np.asarray(el_deg, dtype=np.float64))
    ce = np.cos(el)
    return np.stack([ce * np.sin(az), ce * np.cos(az), np.sin(el)], axis=-1)


def enu_to_azel_array(enu: np.ndarray) -> np.ndarray:
    enu = np.asarray(enu, dtype=np.float64)
    az = np.degrees(np.arctan2(enu[..., 0], enu[..., 1])) % 360.0
    el = np.degrees(np.arctan2(enu[..., 2], np.hypot(enu[..., 0], enu[..., 1])))
    return np.stack([az, el], axis=-1)


def angular_separation_deg(az1: float, el1: float, az2: float, el2: float) -> float:
    """Great-circle angle between two directions (haversine form, stable for tiny angles)."""
    p1, p2 = math.radians(el1), math.radians(el2)
    dl = math.radians(az2 - az1)
    a = math.sin((p2 - p1) / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return math.degrees(2.0 * math.asin(min(1.0, math.sqrt(a))))


def angular_separation_array(az1: np.ndarray, el1: np.ndarray, az2: np.ndarray, el2: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(el1), np.radians(el2)
    dl = np.radians(np.asarray(az2) - np.asarray(az1))
    a = np.sin((p2 - p1) / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return np.degrees(2.0 * np.arcsin(np.minimum(1.0, np.sqrt(a))))
