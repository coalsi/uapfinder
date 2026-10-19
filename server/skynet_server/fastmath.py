"""Pure-``math`` equivalents of the NumPy triangulation helpers for the hot path.

``skynet_common.triangulation.triangulate_many`` and ``position_covariance``
are the reference implementations, but on 3x3 systems NumPy's per-call
overhead dominates: ``triangulate_many`` costs ~170 us (mostly
``np.linalg.cond``'s SVD) and ``position_covariance`` ~15 us, against a budget
of ~100 us per packet at 10k msg/s on one core. The functions here implement
the same algorithms with closed-form 3x3 algebra and are tested against the
reference to sub-millimetre agreement (tests/test_fastmath.py).
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from skynet_common.geodesy import Vec3, ecef_to_geodetic
from skynet_common.triangulation import PARALLEL_EPS, MultiRaySolution, Ray

_TWO_PI_3 = 2.0 * math.pi / 3.0
_COND_LIMIT = 1e12  # same singularity threshold as triangulate_many


def _sym3_eig_extremes(a11: float, a12: float, a13: float, a22: float, a23: float, a33: float) -> Tuple[float, float]:
    """(smallest, largest) eigenvalue of a symmetric 3x3 matrix (Smith, 1961)."""
    p1 = a12 * a12 + a13 * a13 + a23 * a23
    q = (a11 + a22 + a33) / 3.0
    b11, b22, b33 = a11 - q, a22 - q, a33 - q
    p2 = b11 * b11 + b22 * b22 + b33 * b33 + 2.0 * p1
    if p2 <= 0.0:
        return q, q
    p = math.sqrt(p2 / 6.0)
    det_b = (
        b11 * (b22 * b33 - a23 * a23) - a12 * (a12 * b33 - a23 * a13) + a13 * (a12 * a23 - b22 * a13)
    ) / (p * p * p)
    r = max(-1.0, min(1.0, det_b / 2.0))
    phi = math.acos(r) / 3.0
    largest = q + 2.0 * p * math.cos(phi)
    smallest = q + 2.0 * p * math.cos(phi + _TWO_PI_3)
    return smallest, largest


def triangulate_many_fast(
    rays: Sequence[Ray], angular_sigma_deg: float = 0.05, iterations: int = 2
) -> Optional[MultiRaySolution]:
    """Same algorithm and result as ``triangulate_many`` (weighted least squares
    with range-dependent re-weighting), without NumPy.

    The system is solved relative to the first ray's origin: ECEF coordinates
    are ~6.4e6 m, and centring keeps Cramer's rule well conditioned.
    """
    if len(rays) < 2:
        raise ValueError("need at least two rays")
    ref = rays[0].origin
    rel = [(r.origin[0] - ref[0], r.origin[1] - ref[1], r.origin[2] - ref[2]) for r in rays]
    dirs = [r.direction for r in rays]
    s = math.radians(angular_sigma_deg)
    weights = [1.0] * len(rays)
    x = (0.0, 0.0, 0.0)
    ranges = [0.0] * len(rays)
    for _ in range(iterations + 1):
        a11 = a12 = a13 = a22 = a23 = a33 = 0.0
        b1 = b2 = b3 = 0.0
        for w, (ox, oy, oz), (vx, vy, vz) in zip(weights, rel, dirs):
            # w * (I - v v^T) and w * (I - v v^T) o
            p11, p22, p33 = 1.0 - vx * vx, 1.0 - vy * vy, 1.0 - vz * vz
            p12, p13, p23 = -vx * vy, -vx * vz, -vy * vz
            a11 += w * p11
            a12 += w * p12
            a13 += w * p13
            a22 += w * p22
            a23 += w * p23
            a33 += w * p33
            b1 += w * (p11 * ox + p12 * oy + p13 * oz)
            b2 += w * (p12 * ox + p22 * oy + p23 * oz)
            b3 += w * (p13 * ox + p23 * oy + p33 * oz)
        lo, hi = _sym3_eig_extremes(a11, a12, a13, a22, a23, a33)
        if lo <= 0.0 or hi / lo > _COND_LIMIT:
            return None
        c11 = a22 * a33 - a23 * a23
        c12 = a13 * a23 - a12 * a33
        c13 = a12 * a23 - a13 * a22
        c22 = a11 * a33 - a13 * a13
        c23 = a12 * a13 - a11 * a23
        c33 = a11 * a22 - a12 * a12
        det = a11 * c11 + a12 * c12 + a13 * c13
        x = (
            (c11 * b1 + c12 * b2 + c13 * b3) / det,
            (c12 * b1 + c22 * b2 + c23 * b3) / det,
            (c13 * b1 + c23 * b2 + c33 * b3) / det,
        )
        for i, ((ox, oy, oz), (vx, vy, vz)) in enumerate(zip(rel, dirs)):
            ranges[i] = (x[0] - ox) * vx + (x[1] - oy) * vy + (x[2] - oz) * vz
        if min(ranges) <= 0.0:
            return None
        weights = [1.0 / max(r * s, 1e-3) ** 2 for r in ranges]
        wmax = max(weights)
        weights = [w / wmax for w in weights]
    residuals = []
    for r, (ox, oy, oz), (vx, vy, vz) in zip(ranges, rel, dirs):
        dx, dy, dz = x[0] - ox - r * vx, x[1] - oy - r * vy, x[2] - oz - r * vz
        residuals.append(math.sqrt(dx * dx + dy * dy + dz * dz))
    point = (x[0] + ref[0], x[1] + ref[1], x[2] + ref[2])
    lat, lon, alt = ecef_to_geodetic(*point)
    return MultiRaySolution(
        point=point,
        lat_deg=lat,
        lon_deg=lon,
        alt_m=alt,
        ranges_m=tuple(ranges),
        residuals_m=tuple(residuals),
        rms_residual_m=math.sqrt(sum(r * r for r in residuals) / len(residuals)),
    )


def position_sigma_m(directions: Sequence[Vec3], ranges_m: Sequence[float], angular_sigma_deg: float) -> float:
    """``sigma_from_covariance(position_covariance(...))`` without NumPy.

    sqrt(trace(inv(info))) where info = sum_i (I - v_i v_i^T) / (r_i sigma)^2;
    the trace of a 3x3 inverse is the sum of the diagonal cofactors over the
    determinant.
    """
    s = math.radians(angular_sigma_deg)
    a11 = a12 = a13 = a22 = a23 = a33 = 0.0
    for (vx, vy, vz), r in zip(directions, ranges_m):
        lateral = max(r * s, 1e-3)
        w = 1.0 / (lateral * lateral)
        a11 += w * (1.0 - vx * vx)
        a22 += w * (1.0 - vy * vy)
        a33 += w * (1.0 - vz * vz)
        a12 -= w * vx * vy
        a13 -= w * vx * vz
        a23 -= w * vy * vz
    c11 = a22 * a33 - a23 * a23
    c22 = a11 * a33 - a13 * a13
    c33 = a11 * a22 - a12 * a12
    det = a11 * c11 + a12 * (a13 * a23 - a12 * a33) + a13 * (a12 * a23 - a13 * a22)
    if det <= 0.0:
        return math.inf
    return math.sqrt(max((c11 + c22 + c33) / det, 0.0))


def miss_prefilter(
    o1: Vec3, d1: Vec3, o2: Vec3, d2: Vec3, sigma_rad: float, k_sigma: float, miss_floor_m: float,
    min_range_m: float, max_range_m: float,
) -> bool:
    """Cheap necessary condition for ``gate(triangulate_rays(a, b))`` to accept.

    Computes the closest-approach parameters without the geodetic conversion
    and rejects pairs that are parallel, behind, out of range or whose miss
    distance exceeds the k-sigma bound. Survivors still go through the full
    ``triangulate_rays`` + ``gate``; this only skips the expensive work for
    the large majority of candidate pairs that belong to different objects.
    """
    cx = d1[1] * d2[2] - d1[2] * d2[1]
    cy = d1[2] * d2[0] - d1[0] * d2[2]
    cz = d1[0] * d2[1] - d1[1] * d2[0]
    denom = cx * cx + cy * cy + cz * cz
    if denom < PARALLEL_EPS:
        return False
    tx, ty, tz = o2[0] - o1[0], o2[1] - o1[1], o2[2] - o1[2]
    # t1 = t . (d2 x c) / denom, t2 = t . (d1 x c) / denom
    t1 = (tx * (d2[1] * cz - d2[2] * cy) + ty * (d2[2] * cx - d2[0] * cz) + tz * (d2[0] * cy - d2[1] * cx)) / denom
    if not (min_range_m <= t1 <= max_range_m):
        return False
    t2 = (tx * (d1[1] * cz - d1[2] * cy) + ty * (d1[2] * cx - d1[0] * cz) + tz * (d1[0] * cy - d1[1] * cx)) / denom
    if not (min_range_m <= t2 <= max_range_m):
        return False
    # The closest-approach distance is the projection of t onto the common normal.
    miss = abs(tx * cx + ty * cy + tz * cz) / math.sqrt(denom)
    return miss <= k_sigma * math.hypot(t1 * sigma_rad, t2 * sigma_rad) + miss_floor_m
