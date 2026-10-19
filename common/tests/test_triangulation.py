import math
import random

import numpy as np
import pytest

from skynet_common import geodesy as g
from skynet_common.triangulation import (
    GateConfig,
    Ray,
    calculate_uap_3d_position,
    expected_miss_sigma_m,
    gate,
    position_covariance,
    sigma_from_covariance,
    triangulate_many,
    triangulate_rays,
)

NODE_A = (33.6292, -112.3679, 357.2)  # blueprint example location
NODE_B = (33.4484, -112.0740, 331.0)  # ~33 km to the south-east


def ray_to(obs, tgt, az_noise=0.0, el_noise=0.0):
    az, el, _ = g.look_angles(*obs, *tgt)
    return Ray.from_observation(*obs, az + az_noise, el + el_noise)


def test_exact_intersection_recovers_target():
    target = (33.55, -112.20, 9_144.0)  # 30,000 ft
    a, b = ray_to(NODE_A, target), ray_to(NODE_B, target)
    ix, reason = triangulate_rays(a, b)
    assert reason == ""
    assert ix.lat_deg == pytest.approx(target[0], abs=1e-7)
    assert ix.lon_deg == pytest.approx(target[1], abs=1e-7)
    assert ix.alt_m == pytest.approx(target[2], abs=1e-3)
    assert ix.error_margin_m == pytest.approx(0.0, abs=1e-3)
    assert ix.range_a_m == pytest.approx(g.look_angles(*NODE_A, *target)[2], rel=1e-9)


def test_blueprint_function_agrees_with_fast_path():
    rng = random.Random(7)
    for _ in range(200):
        target = (rng.uniform(33.3, 33.8), rng.uniform(-112.5, -111.9), rng.uniform(500, 30_000))
        a = ray_to(NODE_A, target, rng.gauss(0, 0.05), rng.gauss(0, 0.05))
        b = ray_to(NODE_B, target, rng.gauss(0, 0.05), rng.gauss(0, 0.05))
        ix, reason = triangulate_rays(a, b)
        ref = calculate_uap_3d_position(a.origin, b.origin, a.direction, b.direction)
        if reason:
            assert "error" in ref
            continue
        assert (ref["x"], ref["y"], ref["z"]) == pytest.approx(ix.point, abs=1e-4)
        assert ref["error_margin_m"] == pytest.approx(ix.error_margin_m, abs=1e-4)
        assert ref["alt_m"] == pytest.approx(ix.alt_m, abs=1e-4)


def test_blueprint_function_accepts_unnormalised_vectors():
    target = (33.55, -112.20, 5_000.0)
    a, b = ray_to(NODE_A, target), ray_to(NODE_B, target)
    ref = calculate_uap_3d_position(a.origin, b.origin, tuple(5 * c for c in a.direction), tuple(0.1 * c for c in b.direction))
    assert ref["alt_m"] == pytest.approx(5_000.0, abs=1e-3)


def test_parallel_rays_rejected():
    a = Ray((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    b = Ray((0.0, 10.0, 0.0), (1.0, 0.0, 0.0))
    assert triangulate_rays(a, b) == (None, "parallel")
    assert "error" in calculate_uap_3d_position(a.origin, b.origin, a.direction, b.direction)


def test_diverging_rays_rejected():
    # Rays whose closest approach lies behind the observers.
    a = Ray((0.0, 0.0, 0.0), g.normalize((-1.0, 1.0, 0.0)))
    b = Ray((10.0, 0.0, 0.0), g.normalize((1.0, 1.0, 0.0)))
    ix, reason = triangulate_rays(a, b)
    assert ix is None and reason == "behind"
    assert "error" in calculate_uap_3d_position(a.origin, b.origin, a.direction, b.direction)


def test_skew_rays_midpoint_and_miss():
    a = Ray((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    b = Ray((5.0, -5.0, 2.0), (0.0, 1.0, 0.0))
    # Closest points are (5,0,0) and (5,0,2): miss distance 2, midpoint (5,0,1).
    # (ECEF-origin rays so the geodetic conversion is irrelevant here.)
    ix, reason = triangulate_rays(a, b)
    assert reason == ""
    assert ix.point == pytest.approx((5.0, 0.0, 1.0))
    assert ix.error_margin_m == pytest.approx(2.0)
    assert ix.parallax_deg == pytest.approx(90.0)


def test_gate_accepts_consistent_and_rejects_inconsistent():
    cfg = GateConfig(angular_sigma_deg=0.05)
    target = (33.55, -112.20, 9_144.0)
    good, _ = triangulate_rays(ray_to(NODE_A, target, 0.03, -0.02), ray_to(NODE_B, target, -0.02, 0.04))
    ok, norm_miss, reason = gate(good, cfg)
    assert ok, reason
    # A 2-degree pointing error on one node is inconsistent with 0.05 deg noise.
    bad, _ = triangulate_rays(ray_to(NODE_A, target, 2.0, 0.0), ray_to(NODE_B, target))
    ok, _, reason = gate(bad, cfg)
    assert not ok and reason == "miss"


def test_gate_rejects_low_parallax():
    cfg = GateConfig(min_parallax_deg=5.0)
    # ~430 km north: the ~27 km east-west baseline subtends only ~3.6 degrees.
    far = (37.5, -112.2, 20_000.0)
    ix, _ = triangulate_rays(ray_to(NODE_A, far), ray_to(NODE_B, far))
    assert ix.parallax_deg < 5.0
    ok, _, reason = gate(ix, cfg)
    assert not ok and reason == "parallax"


def test_noise_statistics_match_expected_sigma():
    """Monte-Carlo: the miss distance scales with expected_miss_sigma_m."""
    rng = random.Random(3)
    sigma_deg = 0.05
    target = (33.55, -112.20, 9_144.0)
    misses, expected = [], []
    for _ in range(3000):
        a = ray_to(NODE_A, target, rng.gauss(0, sigma_deg) / math.cos(math.radians(30)), rng.gauss(0, sigma_deg))
        b = ray_to(NODE_B, target, rng.gauss(0, sigma_deg) / math.cos(math.radians(30)), rng.gauss(0, sigma_deg))
        ix, _ = triangulate_rays(a, b)
        misses.append(ix.error_margin_m)
        expected.append(expected_miss_sigma_m(ix.range_a_m, ix.range_b_m, sigma_deg))
    # Miss distance is |N(0, s^2)| so its RMS should be ~s (within a loose factor
    # since azimuth noise is not exactly isotropic in the tangent plane).
    rms = math.sqrt(sum(m * m for m in misses) / len(misses))
    assert 0.5 < rms / (sum(expected) / len(expected)) < 1.5


def test_triangulate_many_beats_pairs_and_matches_covariance():
    rng = random.Random(11)
    sigma_deg = 0.05
    target = (33.55, -112.20, 9_144.0)
    nodes = [NODE_A, NODE_B, (33.80, -112.10, 400.0), (33.40, -112.45, 350.0)]
    truth = np.array(g.geodetic_to_ecef(*target))
    errs_many, errs_pair = [], []
    for _ in range(400):
        rays = [ray_to(n, target, rng.gauss(0, sigma_deg), rng.gauss(0, sigma_deg)) for n in nodes]
        sol = triangulate_many(rays, angular_sigma_deg=sigma_deg)
        assert sol is not None
        errs_many.append(np.linalg.norm(np.array(sol.point) - truth))
        ix, _ = triangulate_rays(rays[0], rays[1])
        errs_pair.append(np.linalg.norm(np.array(ix.point) - truth))
    assert np.mean(errs_many) < np.mean(errs_pair)
    ranges = [g.look_angles(*n, *target)[2] for n in nodes]
    dirs = [ray_to(n, target).direction for n in nodes]
    predicted = sigma_from_covariance(position_covariance(dirs, ranges, sigma_deg))
    observed = math.sqrt(np.mean(np.square(errs_many)))
    assert 0.6 < observed / predicted < 1.6


def test_triangulate_many_rejects_single_ray():
    with pytest.raises(ValueError):
        triangulate_many([Ray((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))])
