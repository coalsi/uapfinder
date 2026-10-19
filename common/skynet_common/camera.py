"""Pinhole camera geometry: pixel <-> local line-of-sight direction.

Camera frame follows OpenCV: +x right, +y down, +z forward (optical axis).
The camera attitude is given as the optical axis azimuth (``yaw_deg``,
clockwise from true north), elevation (``pitch_deg``, above the horizon) and
``roll_deg``, a rotation about the optical axis with positive values putting
the right-hand side of the image down. With zero roll the image x axis is
horizontal. This is the attitude carried in ``imu_orientation``.

Lens distortion is removed on the edge node (cv2.undistortPoints) before
these functions are used, so they model an ideal pinhole.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from .geodesy import Vec3, cross, dot, enu_to_azel, normalize, azel_to_enu


def camera_basis_enu(yaw_deg: float, pitch_deg: float, roll_deg: float) -> Tuple[Vec3, Vec3, Vec3]:
    """ENU unit vectors of the camera's (right, down, forward) axes."""
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    r = math.radians(roll_deg)
    forward = (math.cos(p) * math.sin(y), math.cos(p) * math.cos(y), math.sin(p))
    right0 = (math.cos(y), -math.sin(y), 0.0)
    down0 = cross(forward, right0)
    cr, sr = math.cos(r), math.sin(r)
    right = (
        cr * right0[0] + sr * down0[0],
        cr * right0[1] + sr * down0[1],
        cr * right0[2] + sr * down0[2],
    )
    down = (
        cr * down0[0] - sr * right0[0],
        cr * down0[1] - sr * right0[1],
        cr * down0[2] - sr * right0[2],
    )
    return right, down, forward


@dataclass(frozen=True)
class PinholeCamera:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def from_fov(cls, width: int, height: int, fov_h_deg: float, fov_v_deg: Optional[float] = None) -> "PinholeCamera":
        """Ideal camera with the principal point at the image centre.

        If ``fov_v_deg`` is omitted, square pixels are assumed.
        """
        fx = (width / 2.0) / math.tan(math.radians(fov_h_deg) / 2.0)
        fy = fx if fov_v_deg is None else (height / 2.0) / math.tan(math.radians(fov_v_deg) / 2.0)
        return cls(width, height, fx, fy, width / 2.0, height / 2.0)

    @property
    def fov_h_deg(self) -> float:
        return math.degrees(2.0 * math.atan((self.width / 2.0) / self.fx))

    @property
    def fov_v_deg(self) -> float:
        return math.degrees(2.0 * math.atan((self.height / 2.0) / self.fy))

    @property
    def deg_per_pixel(self) -> float:
        """Approximate angular size of one pixel at the image centre."""
        return math.degrees(math.atan(1.0 / self.fx))

    def pixel_to_camera_ray(self, u: float, v: float) -> Vec3:
        return normalize(((u - self.cx) / self.fx, (v - self.cy) / self.fy, 1.0))

    def camera_ray_to_pixel(self, ray: Vec3) -> Optional[Tuple[float, float]]:
        """Project a camera-frame direction; None if it points behind the camera."""
        if ray[2] <= 1e-12:
            return None
        return (self.fx * ray[0] / ray[2] + self.cx, self.fy * ray[1] / ray[2] + self.cy)

    def in_image(self, u: float, v: float, margin: float = 0.0) -> bool:
        return margin <= u <= self.width - margin and margin <= v <= self.height - margin


def pixel_to_azel(
    cam: PinholeCamera, u: float, v: float, yaw_deg: float, pitch_deg: float, roll_deg: float
) -> Tuple[float, float]:
    """Azimuth/elevation (deg) of the line of sight through pixel (u, v)."""
    right, down, forward = camera_basis_enu(yaw_deg, pitch_deg, roll_deg)
    x, y, z = cam.pixel_to_camera_ray(u, v)
    e = x * right[0] + y * down[0] + z * forward[0]
    n = x * right[1] + y * down[1] + z * forward[1]
    up = x * right[2] + y * down[2] + z * forward[2]
    return enu_to_azel(e, n, up)


def azel_to_pixel(
    cam: PinholeCamera, az_deg: float, el_deg: float, yaw_deg: float, pitch_deg: float, roll_deg: float
) -> Optional[Tuple[float, float]]:
    """Pixel position of a direction, or None if it is behind the camera."""
    right, down, forward = camera_basis_enu(yaw_deg, pitch_deg, roll_deg)
    d = azel_to_enu(az_deg, el_deg)
    return cam.camera_ray_to_pixel((dot(d, right), dot(d, down), dot(d, forward)))


def frustum_corner_azel(
    cam: PinholeCamera, yaw_deg: float, pitch_deg: float, roll_deg: float
) -> Tuple[Tuple[float, float], ...]:
    """Az/el of the four image corners (top-left, top-right, bottom-right, bottom-left)."""
    corners = ((0.0, 0.0), (cam.width, 0.0), (cam.width, cam.height), (0.0, cam.height))
    return tuple(pixel_to_azel(cam, u, v, yaw_deg, pitch_deg, roll_deg) for u, v in corners)
