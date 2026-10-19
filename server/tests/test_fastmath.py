"""The pure-math hot path agrees with the skynet_common reference implementations."""

from __future__ import annotations

import math
import random

import pytest

from skynet_common.geodesy import azel_to_ecef_unit, geodetic_to_ecef, look_angles
from skynet_common.triangulation import (
    GateConfig,
    Ray,
    gate,
    position_covariance,
    sigma_from_covariance,
    triangulate_many,
    triangulate_rays,
)
from skynet_server.fastmath import fast_gate, position_sigma_m, ray_meets_cone, triangulate_many_fast


def random_rays(rng: random.Random, n: int, noise_deg: float, target=None):
    target = target or (33.5 + rng.uniform(-0.2, 0.2), -112.0 + rng.uniform(-0.2, 0.2), rng.uniform(500, 30000))
    rays = []
    for _ in range(n):
        lat, lon = target[0] + rng.uniform(-0.6, 0.6), target[1] + rng.uniform(-0.6, 0.6)
        az, el, _ = look_angles(lat, lon, 300.0, *target)
        rays.append(Ray(geodetic_to_ecef(lat, lon, 300.0),
                        azel_to_ecef_unit(lat, lon, az + rng.gauss(0, noise_deg), el + rng.gauss(0, noise_deg))))
    return rays


def test_triangulate_many_fast_matches_numpy():
    rng = random.Random(7)
    for _ in range(200):
        rays = random_rays(rng, rng.randint(2, 9), 0.05)
        ref, fast = triangulate_many(rays, 0.05), triangulate_many_fast(rays, 0.05)
        assert (ref is None) == (fast is None)
        if ref is None:
            continue
        assert math.dist(ref.point, fast.point) < 1e-3
        assert fast.alt_m == pytest.approx(ref.alt_m, abs=1e-3)
        assert fast.ranges_m == pytest.approx(ref.ranges_m, rel=1e-9, abs=1e-3)
        assert fast.residuals_m == pytest.approx(ref.residuals_m, abs=1e-3)
        assert fast.rms_residual_m == pytest.approx(ref.rms_residual_m, abs=1e-3)


def test_triangulate_many_fast_rejects_degenerate_input():
    o = geodetic_to_ecef(33.5, -112.0, 300.0)
    d = azel_to_ecef_unit(33.5, -112.0, 30.0, 40.0)
    parallel = [Ray(o, d), Ray((o[0] + 10.0, o[1], o[2]), d)]
    assert triangulate_many(parallel) is None and triangulate_many_fast(parallel) is None
    away = [Ray(o, d), Ray(geodetic_to_ecef(33.6, -112.0, 300.0), azel_to_ecef_unit(33.6, -112.0, 0.0, 30.0))]
    assert triangulate_many_fast(away) is None  # closest approach behind the second camera


def test_position_sigma_matches_covariance():
    rng = random.Random(3)
    for _ in range(200):
        rays = random_rays(rng, rng.randint(2, 8), 0.0)
        ranges = [rng.uniform(1_000, 120_000) for _ in rays]
        dirs = [r.direction for r in rays]
        for sigma_deg in (0.01, 0.05, 1.0):
            ref = sigma_from_covariance(position_covariance(dirs, ranges, sigma_deg))
            assert position_sigma_m(dirs, ranges, sigma_deg) == pytest.approx(ref, rel=1e-6)


def test_fast_gate_agrees_with_reference_gate():
    rng = random.Random(11)
    cfg = GateConfig()
    sigma = math.radians(cfg.angular_sigma_deg)
    accepted = 0
    for i in range(3000):
        # Mostly true pairs with noise, plus pairs looking at different objects.
        if i % 3 == 0:
            a, b = random_rays(rng, 1, 0.05)[0], random_rays(rng, 1, 0.05)[0]
        else:
            a, b = random_rays(rng, 2, rng.choice([0.01, 0.05, 0.3]))
        ix, _ = triangulate_rays(a, b)
        ref = gate(ix, cfg) if ix is not None else (False, math.inf, "")
        fast = fast_gate(a.origin, a.direction, b.origin, b.direction, sigma, cfg)
        assert (fast is not None) == ref[0]
        if fast is not None:
            assert fast == pytest.approx(ref[1], rel=1e-9)
            accepted += 1
    assert accepted > 500  # the test exercises both outcomes


def test_ray_meets_cone_matches_brute_force():
    rng = random.Random(5)

    def unit():
        v = [rng.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in v))
        return tuple(x / n for x in v)

    for _ in range(1500):
        o = tuple(rng.uniform(-1e4, 1e4) for _ in range(3))
        apex = tuple(rng.uniform(-1e4, 1e4) for _ in range(3))
        d, axis = unit(), unit()
        cos_half = math.cos(math.radians(rng.uniform(5, 80)))
        t0, t1 = rng.uniform(0, 1e3), rng.uniform(1e3, 5e4)
        brute = False
        for i in range(2001):
            t = t0 + (t1 - t0) * i / 2000
            x = [o[k] + t * d[k] - apex[k] for k in range(3)]
            r = math.sqrt(sum(v * v for v in x))
            if r > 0 and sum(x[k] * axis[k] for k in range(3)) / r >= cos_half:
                brute = True
                break
        # Sampling can only miss a grazing intersection, never invent one.
        assert ray_meets_cone(o, d, apex, axis, cos_half, t0, t1) or not brute
