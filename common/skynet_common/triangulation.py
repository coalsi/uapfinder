"""Stereo-triangulation engine.

Two entry points:

* :func:`calculate_uap_3d_position` is the reference implementation from the
  architecture blueprint (section 4), kept call-compatible: it takes ECEF
  node positions and line-of-sight direction vectors and returns the midpoint
  of the shortest segment between the two lines. It is extended to reject
  intersections behind either camera (the inputs are rays, not infinite
  lines) and to report geodetic altitude, ranges and parallax.

* :func:`triangulate_rays` is the allocation-free equivalent used on the
  server hot path, and :func:`triangulate_many` is the N-ray weighted least
  squares solution used once three or more nodes see the same object.

See docs/MATH.md for the derivation and the error model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from .geodesy import (
    Vec3,
    add,
    angle_between_deg,
    azel_to_ecef_unit,
    cross,
    dot,
    ecef_to_geodetic,
    geodetic_to_ecef,
    norm,
    scale,
    sub,
)

# |v1 x v2|^2 below this is treated as parallel (about 0.0057 degrees between
# unit vectors). Same threshold as the blueprint.
PARALLEL_EPS = 1e-8


def calculate_uap_3d_position(node_a_gps, node_b_gps, vector_a, vector_b):
    """
    Calculates the 3D coordinate of a target using two intersecting vectors.

    Parameters:
    - node_a_gps/node_b_gps: tuple (x, y, z) Earth-Centred Earth-Fixed coordinates, metres.
    - vector_a/vector_b: tuple (dx, dy, dz) directional vectors in the same ECEF frame,
      pointing from each node towards the target. They are normalised here.

    Returns:
    - dict: ECEF coordinates, geodetic latitude/longitude/altitude (WGS84, height
      above ellipsoid), ranges from each node, parallax angle and the error
      margin (length of the shortest segment between the two rays), or
      {"error": ...} when the rays cannot be triangulated.
    """
    p1 = np.asarray(node_a_gps, dtype=float)
    p2 = np.asarray(node_b_gps, dtype=float)
    v1 = np.asarray(vector_a, dtype=float)
    v2 = np.asarray(vector_b, dtype=float)
    v1 = v1 / np.linalg.norm(v1)
    v2 = v2 / np.linalg.norm(v2)

    # Lines are defined as: L1 = p1 + t1*v1, L2 = p2 + t2*v2
    cross_v = np.cross(v1, v2)
    denom = np.linalg.norm(cross_v) ** 2

    if denom < PARALLEL_EPS:
        return {"error": "Vectors are perfectly parallel, triangulation mathematically impossible."}

    t = p2 - p1
    t1 = np.linalg.det([t, v2, cross_v]) / denom
    t2 = np.linalg.det([t, v1, cross_v]) / denom

    if t1 <= 0.0 or t2 <= 0.0:
        return {"error": "Closest approach lies behind at least one observer; the rays do not converge."}

    point_on_l1 = p1 + t1 * v1
    point_on_l2 = p2 + t2 * v2

    # Midpoint of the shortest distance line segment between the two skewed rays
    intersection_point = (point_on_l1 + point_on_l2) / 2.0
    error_margin_meters = float(np.linalg.norm(point_on_l1 - point_on_l2))

    lat, lon, alt = ecef_to_geodetic(*intersection_point)
    return {
        "x": float(intersection_point[0]),
        "y": float(intersection_point[1]),
        "z": float(intersection_point[2]),
        "lat_deg": lat,
        "lon_deg": lon,
        "alt_m": alt,
        "range_a_m": float(t1),
        "range_b_m": float(t2),
        "parallax_deg": angle_between_deg(tuple(v1), tuple(v2)),
        "error_margin_m": error_margin_meters,
    }


@dataclass(frozen=True)
class Ray:
    """A line of sight from an observer, in ECEF."""

    origin: Vec3
    direction: Vec3  # unit vector

    @classmethod
    def from_observation(
        cls, lat_deg: float, lon_deg: float, h_m: float, az_deg: float, el_deg: float
    ) -> "Ray":
        return cls(geodetic_to_ecef(lat_deg, lon_deg, h_m), azel_to_ecef_unit(lat_deg, lon_deg, az_deg, el_deg))


@dataclass(frozen=True)
class RayIntersection:
    point: Vec3  # ECEF midpoint of the closest-approach segment
    lat_deg: float
    lon_deg: float
    alt_m: float  # height above the WGS84 ellipsoid
    range_a_m: float
    range_b_m: float
    parallax_deg: float
    error_margin_m: float  # closest-approach distance between the rays


def triangulate_rays(a: Ray, b: Ray) -> Tuple[Optional[RayIntersection], str]:
    """Closest-approach triangulation of two rays.

    Returns ``(intersection, "")`` on success or ``(None, reason)`` where reason
    is ``"parallel"`` or ``"behind"``.
    """
    v1, v2 = a.direction, b.direction
    c = cross(v1, v2)
    denom = dot(c, c)
    if denom < PARALLEL_EPS:
        return None, "parallel"
    t = sub(b.origin, a.origin)
    # det([t, v, c]) is the scalar triple product t . (v x c).
    t1 = dot(t, cross(v2, c)) / denom
    t2 = dot(t, cross(v1, c)) / denom
    if t1 <= 0.0 or t2 <= 0.0:
        return None, "behind"
    q1 = add(a.origin, scale(v1, t1))
    q2 = add(b.origin, scale(v2, t2))
    mid = scale(add(q1, q2), 0.5)
    lat, lon, alt = ecef_to_geodetic(*mid)
    return (
        RayIntersection(
            point=mid,
            lat_deg=lat,
            lon_deg=lon,
            alt_m=alt,
            range_a_m=t1,
            range_b_m=t2,
            parallax_deg=math.degrees(math.atan2(math.sqrt(denom), dot(v1, v2))),
            error_margin_m=norm(sub(q1, q2)),
        ),
        "",
    )


def expected_miss_sigma_m(range_a_m: float, range_b_m: float, angular_sigma_deg: float) -> float:
    """1-sigma expected closest-approach distance for two rays with the given
    per-axis angular noise.

    Each ray's lateral error at its range r is r * sigma. The miss distance is
    the projection of both lateral errors onto the common perpendicular, so it
    has standard deviation sqrt(sa^2 + sb^2).
    """
    s = math.radians(angular_sigma_deg)
    return math.hypot(range_a_m * s, range_b_m * s)


def position_covariance(
    directions: Sequence[Vec3], ranges_m: Sequence[float], angular_sigma_deg: float
) -> np.ndarray:
    """Linearised 3x3 ECEF covariance (m^2) of a multi-ray intersection.

    Each ray constrains the position in the two axes perpendicular to it with
    standard deviation r_i * sigma. The information matrix is
    sum_i (I - v_i v_i^T) / (r_i sigma)^2 and the covariance is its inverse.
    """
    s = math.radians(angular_sigma_deg)
    info = np.zeros((3, 3))
    eye = np.eye(3)
    for v, r in zip(directions, ranges_m):
        vv = np.asarray(v, dtype=float)
        lateral = max(r * s, 1e-3)
        info += (eye - np.outer(vv, vv)) / (lateral * lateral)
    return np.linalg.inv(info)


def sigma_from_covariance(cov: np.ndarray) -> float:
    """3D RMS position uncertainty in metres, sqrt(trace(cov))."""
    return float(math.sqrt(max(float(np.trace(cov)), 0.0)))


@dataclass(frozen=True)
class MultiRaySolution:
    point: Vec3
    lat_deg: float
    lon_deg: float
    alt_m: float
    ranges_m: Tuple[float, ...]
    residuals_m: Tuple[float, ...]  # perpendicular distance from each ray
    rms_residual_m: float


def triangulate_many(rays: Sequence[Ray], angular_sigma_deg: float = 0.05, iterations: int = 2) -> Optional[MultiRaySolution]:
    """Weighted least-squares intersection of two or more rays.

    Minimises sum_i w_i * |(I - v_i v_i^T)(x - p_i)|^2. The weights are
    1 / (r_i * sigma)^2 so that distant observers, whose lateral error is
    larger, count for less; ranges come from the previous iterate, starting
    from equal weights. Returns None if the system is singular (all rays
    parallel) or the solution lies behind any observer.
    """
    if len(rays) < 2:
        raise ValueError("need at least two rays")
    origins = np.array([r.origin for r in rays], dtype=float)
    dirs = np.array([r.direction for r in rays], dtype=float)
    eye = np.eye(3)
    projectors = eye[None, :, :] - dirs[:, :, None] * dirs[:, None, :]
    weights = np.ones(len(rays))
    s = math.radians(angular_sigma_deg)
    x = None
    for _ in range(iterations + 1):
        a_mat = np.einsum("i,ijk->jk", weights, projectors)
        b_vec = np.einsum("i,ijk,ik->j", weights, projectors, origins)
        if np.linalg.cond(a_mat) > 1e12:
            return None
        x = np.linalg.solve(a_mat, b_vec)
        ranges = np.einsum("ij,ij->i", x[None, :] - origins, dirs)
        if np.any(ranges <= 0.0):
            return None
        weights = 1.0 / np.maximum(ranges * s, 1e-3) ** 2
        weights = weights / weights.max()
    assert x is not None
    perp = np.einsum("ijk,ik->ij", projectors, x[None, :] - origins)
    residuals = np.linalg.norm(perp, axis=1)
    lat, lon, alt = ecef_to_geodetic(*x)
    return MultiRaySolution(
        point=(float(x[0]), float(x[1]), float(x[2])),
        lat_deg=lat,
        lon_deg=lon,
        alt_m=alt,
        ranges_m=tuple(float(r) for r in ranges),
        residuals_m=tuple(float(r) for r in residuals),
        rms_residual_m=float(math.sqrt(np.mean(residuals**2))),
    )


@dataclass(frozen=True)
class GateConfig:
    """Acceptance test for a candidate two-ray match.

    A pair of detections from different nodes is accepted as the same object
    when the rays converge in front of both cameras, the parallax angle is
    large enough for the depth to be meaningful, the ranges and altitude are
    physically plausible, and the miss distance is consistent with the
    angular noise (k-sigma) plus a floor for timing and position error.
    """

    angular_sigma_deg: float = 0.05
    k_sigma: float = 3.0
    miss_floor_m: float = 30.0
    min_parallax_deg: float = 1.0
    min_range_m: float = 150.0
    max_range_m: float = 400_000.0
    min_alt_m: float = -500.0
    max_alt_m: float = 150_000.0


def gate(ix: RayIntersection, cfg: GateConfig) -> Tuple[bool, float, str]:
    """Return (accepted, normalised_miss, reason).

    normalised_miss is miss distance divided by its expected 1-sigma value,
    which is what the matchmaker uses to rank competing candidates.
    """
    if ix.parallax_deg < cfg.min_parallax_deg:
        return False, math.inf, "parallax"
    if not (cfg.min_range_m <= ix.range_a_m <= cfg.max_range_m and cfg.min_range_m <= ix.range_b_m <= cfg.max_range_m):
        return False, math.inf, "range"
    if not (cfg.min_alt_m <= ix.alt_m <= cfg.max_alt_m):
        return False, math.inf, "altitude"
    sigma = expected_miss_sigma_m(ix.range_a_m, ix.range_b_m, cfg.angular_sigma_deg)
    normalised = ix.error_margin_m / max(sigma, 1e-6)
    if ix.error_margin_m > cfg.k_sigma * sigma + cfg.miss_floor_m:
        return False, normalised, "miss"
    return True, normalised, ""
