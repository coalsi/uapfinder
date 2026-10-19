"""Angular kinematics of a track on the celestial sphere, and the anomaly score.

A single camera only measures directions, so everything here is angular:
speed (deg/s), acceleration (deg/s^2) and turn rate (deg/s of heading
change). True speed and acceleration need two nodes and are computed by the
server after triangulation; ``anomaly_score`` is an edge-side hint only.

Method: the recent samples are projected gnomonically onto the plane
tangent to the sphere at the latest direction. A gnomonic projection maps
great circles to straight lines, so unaccelerated great-circle motion
(satellites, distant straight-flying aircraft) has zero normal acceleration
in that plane and the fitted curvature is exactly the deviation from
great-circle motion. x(t) and y(t) are fitted with quadratics by
iteratively re-weighted least squares (Huber weights), which suppresses
centroid outliers. The fit's own covariance gives the acceleration noise,
and only acceleration in excess of 3 sigma counts towards the score, so
faint, noisy tracks do not look like manoeuvres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .config import AnomalyConfig
from .geometry import angular_separation_deg, azel_to_enu_array
from .tracking import TrackSample


@dataclass
class Kinematics:
    t_ns: int
    n: int
    speed_deg_s: float
    accel_deg_s2: float  # significant (noise-corrected) angular acceleration magnitude
    normal_accel_deg_s2: float  # significant acceleration perpendicular to the motion
    turn_rate_deg_s: float  # significant heading change rate
    accel_sigma_deg_s2: float  # 1-sigma noise of the fitted acceleration
    residual_deg: float  # robust rms position residual of the fit


def fit_kinematics(samples: Sequence[TrackSample], min_samples: int = 6,
                   min_speed_for_turn_deg_s: float = 0.2) -> Optional[Kinematics]:
    if len(samples) < max(3, min_samples):
        return None
    az = np.array([s.az for s in samples])
    el = np.array([s.el for s in samples])
    t = np.array([(s.t_ns - samples[-1].t_ns) / 1e9 for s in samples])
    if t[0] > -1e-6:
        return None
    p = azel_to_enu_array(az, el)
    a0, e0 = math.radians(az[-1]), math.radians(el[-1])
    r0 = p[-1]
    e_az = np.array([math.cos(a0), -math.sin(a0), 0.0])
    e_el = np.array([-math.sin(e0) * math.sin(a0), -math.sin(e0) * math.cos(a0), math.cos(e0)])
    denom = p @ r0
    if np.any(denom <= 0.1):
        return None
    xy = np.stack([(p @ e_az) / denom, (p @ e_el) / denom], axis=1)

    A = np.stack([np.ones_like(t), t, t * t], axis=1)
    w = np.ones_like(t)
    coef = np.zeros((3, 2))
    sigma = 0.0
    try:
        for _ in range(3):
            # Weighted normal equations: a 3x3 solve, far cheaper than an SVD per iteration.
            aw = A * w[:, None]
            coef = np.linalg.solve(aw.T @ A, aw.T @ xy)
            res = np.sqrt(np.sum((xy - A @ coef) ** 2, axis=1))
            sigma = max(1.4826 * float(np.median(res)), 1e-7)
            k = 1.5 * sigma
            w = np.where(res <= k, 1.0, k / np.maximum(res, 1e-12))
        normal = (A * w[:, None]).T @ A
        cov_unit = np.linalg.inv(normal)
    except np.linalg.LinAlgError:  # degenerate timing (duplicate timestamps)
        return None
    dof = max(1, len(t) - 3)
    resid2 = np.sum((xy - A @ coef) ** 2, axis=1)
    var = float(np.sum(w * resid2) / dof) / 2.0  # per-axis variance
    accel_sigma = 2.0 * math.sqrt(max(cov_unit[2, 2] * var, 0.0))

    vel = coef[1]
    acc = 2.0 * coef[2]
    speed = float(np.hypot(*vel))
    acc_mag = float(np.hypot(*acc))
    sig_acc = max(0.0, acc_mag - 3.0 * accel_sigma)
    turn = a_normal = 0.0
    if math.degrees(speed) >= min_speed_for_turn_deg_s:
        vhat = vel / speed
        a_n = abs(float(vhat[0] * acc[1] - vhat[1] * acc[0]))
        a_normal = max(0.0, a_n - 3.0 * accel_sigma)
        turn = a_normal / speed
    deg = math.degrees
    return Kinematics(samples[-1].t_ns, len(samples), deg(speed), deg(sig_acc), deg(a_normal), deg(turn),
                      deg(accel_sigma), deg(sigma))


def mean_angular_speed(samples: Sequence[TrackSample]) -> Optional[float]:
    """End-to-end angular speed over a set of samples (robust to jitter for slow movers)."""
    if len(samples) < 2:
        return None
    dt = (samples[-1].t_ns - samples[0].t_ns) / 1e9
    if dt <= 0:
        return None
    k = max(1, len(samples) // 5)  # average a few samples at each end
    a = samples[:k]
    b = samples[-k:]
    az1, el1 = _mean_dir(a)
    az2, el2 = _mean_dir(b)
    span = ((b[-1].t_ns + b[0].t_ns) - (a[-1].t_ns + a[0].t_ns)) / 2e9
    return angular_separation_deg(az1, el1, az2, el2) / max(span, 1e-9) if span > 0 else None


def _mean_dir(samples: Sequence[TrackSample]):
    p = azel_to_enu_array(np.array([s.az for s in samples]), np.array([s.el for s in samples])).mean(axis=0)
    return (math.degrees(math.atan2(p[0], p[1])) % 360.0, math.degrees(math.atan2(p[2], math.hypot(p[0], p[1]))))


def anomaly_score(k: Kinematics, cfg: AnomalyConfig) -> float:
    """0..1: how far the track departs from smooth, straight (great-circle) motion."""
    s_turn = 1.0 - math.exp(-max(0.0, k.turn_rate_deg_s - cfg.turn_rate_floor_deg_s) / cfg.turn_rate_scale_deg_s)
    s_acc = 1.0 - math.exp(-max(0.0, k.accel_deg_s2 - cfg.accel_floor_deg_s2) / cfg.accel_scale_deg_s2)
    return 1.0 - (1.0 - s_turn) * (1.0 - s_acc)
