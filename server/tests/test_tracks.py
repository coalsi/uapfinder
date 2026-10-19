"""3D track linking, kinematics and classification."""

from __future__ import annotations

import math
import random
from typing import Callable, Optional, Sequence, Tuple

import pytest

from skynet_common.geodesy import ecef_to_geodetic, enu_to_ecef_vector, geodetic_to_ecef
from skynet_server.config import TrackConfig
from skynet_server.models import Match, RayObs, TrackState
from skynet_server.tracks import G0, TrackManager, fit_polynomial

T0 = 1_800_000_000_000_000_000
ORIGIN = (33.5, -112.0, 8000.0)
Vec = Tuple[float, float, float]


def ray(node: str, track: str) -> RayObs:
    return RayObs(node, 1, track, 33.4, -112.1, 300.0, 0.0, 45.0, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))


def match_at(t_ns: int, ecef: Vec, sigma_m: float, rays: Sequence[Tuple[str, str]] = (("a", "t1"), ("b", "t2"))
             ) -> Match:
    lat, lon, alt = ecef_to_geodetic(*ecef)
    return Match(t_ns, [ray(n, t) for n, t in rays], ecef, lat, lon, alt, sigma_m, 5.0, 30.0,
                 [f"{n}:{t_ns}" for n, _ in rays])


def fly(path: Callable[[float], Vec], seconds: float, sigma_m: float = 20.0, hz: float = 10.0, seed: int = 1,
        mgr: Optional[TrackManager] = None) -> TrackState:
    """Feed a track with noisy points of ``path`` (ENU metres at ORIGIN vs time)."""
    rng = random.Random(seed)
    mgr = mgr or TrackManager(TrackConfig())
    links: dict = {}
    tracks: dict = {}
    o = geodetic_to_ecef(*ORIGIN)
    per_axis = sigma_m / math.sqrt(3.0)
    track = None
    for i in range(int(seconds * hz)):
        t = i / hz
        e = enu_to_ecef_vector(ORIGIN[0], ORIGIN[1], path(t))
        p = (o[0] + e[0] + rng.gauss(0, per_axis), o[1] + e[1] + rng.gauss(0, per_axis),
             o[2] + e[2] + rng.gauss(0, per_axis))
        upd = mgr.assign([match_at(T0 + int(t * 1e9), p, sigma_m)], links, tracks, T0)[0]
        assert upd is not None
        track = upd.track
    assert len(tracks) == 1
    return track


def straight(v: Vec) -> Callable[[float], Vec]:
    return lambda t: (v[0] * t, v[1] * t, v[2] * t)


def test_fit_polynomial_recovers_velocity_and_acceleration():
    samples = [(t / 10, 3.0 + 200.0 * t / 10, -50.0 * t / 10, 0.5 * 30.0 * (t / 10) ** 2, 1.0) for t in range(40)]
    line = fit_polynomial(samples[-10:], quadratic=False)
    quad = fit_polynomial(samples, quadratic=True)
    assert quad.velocity[0] == pytest.approx(200.0, abs=1e-6) and quad.accel[2] == pytest.approx(30.0, abs=1e-6)
    assert line.velocity[1] == pytest.approx(-50.0, abs=1e-6)
    # One wild point (a bad association) is rejected instead of faking a manoeuvre.
    samples[30] = (samples[30][0], samples[30][1] + 5000.0, samples[30][2], samples[30][3], 1.0)
    robust = fit_polynomial(samples, quadratic=True)
    assert robust.accel[0] == pytest.approx(0.0, abs=1e-6) and robust.n == len(samples) - 1


def test_cruise_raises_no_flags():
    for seed in range(6):
        track = fly(straight((-120.0, 180.0, 0.0)), 30.0, seed=seed)
        assert track.flags == [] and track.classification == "unknown", (seed, track.max_accel_mps2)
        assert track.speed_mps == pytest.approx(216.3, abs=25.0)


def test_hypersonic():
    track = fly(straight((2000.0, 300.0, 0.0)), 15.0, sigma_m=30.0)
    assert "hypersonic" in track.flags and track.classification == "hypersonic"
    assert "high_g" not in track.flags and "ballistic" not in track.flags
    assert track.max_speed_mps == pytest.approx(2022.0, abs=60.0)
    slow = fly(straight((1500.0, 0.0, 0.0)), 15.0, sigma_m=30.0)
    assert "hypersonic" not in slow.flags


def test_high_g_manoeuvre():
    a = (-150.0, 120.0, 40.0)  # 196 m/s^2, 20 g
    track = fly(lambda t: (0.5 * a[0] * t * t, 0.5 * a[1] * t * t, 0.5 * a[2] * t * t), 8.0)
    assert "high_g" in track.flags
    assert 120.0 < track.max_accel_mps2 < 196.3 + 20.0  # a conservative lower bound of the true 196 m/s^2
    gentle = fly(lambda t: (0.5 * 40.0 * t * t, 0.0, 0.0), 8.0)  # 4 g
    assert "high_g" not in gentle.flags


def test_ballistic_free_fall_but_not_constant_velocity():
    track = fly(lambda t: (300.0 * t, 0.0, 200.0 * t - 0.5 * G0 * t * t), 20.0, sigma_m=5.0)
    assert "ballistic" in track.flags
    level = fly(straight((300.0, 0.0, 0.0)), 20.0, sigma_m=5.0)
    assert "ballistic" not in level.flags


def test_stationary():
    track = fly(lambda t: (0.0, 0.0, 0.0), 35.0, hz=5.0)
    assert track.classification == "stationary"
    drifting = fly(straight((5.0, 0.0, 0.0)), 35.0, hz=5.0)
    assert "stationary" not in drifting.flags


def test_linking_new_nodes_and_ghost_veto():
    mgr = TrackManager(TrackConfig())
    links: dict = {}
    tracks: dict = {}
    o = geodetic_to_ecef(*ORIGIN)
    first = mgr.assign([match_at(T0, o, 20.0)], links, tracks, T0)[0]
    assert first.created and first.new_nodes == ["a", "b"] and links == {"a|t1": first.track.track_id,
                                                                            "b|t2": first.track.track_id}
    # Linked through a's local track; c joins.
    p = (o[0] + 20.0, o[1], o[2])
    second = mgr.assign([match_at(T0 + 100_000_000, p, 20.0, [("a", "t1"), ("c", "t5")])], links, tracks, T0)[0]
    assert not second.created and second.track is first.track and second.new_nodes == ["c"]
    # A two-ray match linked to the track but 50 km away is a ghost pairing.
    far = (o[0] + 50_000.0, o[1], o[2])
    assert mgr.assign([match_at(T0 + 200_000_000, far, 20.0, [("a", "t1"), ("d", "t9")])], links, tracks,
                      T0) == [None]
    # A different object with unlinked local tracks gets its own track.
    other = mgr.assign([match_at(T0 + 200_000_000, far, 20.0, [("e", "x"), ("f", "y")])], links, tracks, T0)[0]
    assert other.created and other.track.track_id != first.track.track_id


def test_same_frame_refinements_are_thinned():
    mgr = TrackManager(TrackConfig())
    links: dict = {}
    tracks: dict = {}
    o = geodetic_to_ecef(*ORIGIN)
    first = mgr.assign([match_at(T0, o, 20.0)], links, tracks, T0)[0]
    t1 = T0 + 3_000_000
    small = mgr.assign([match_at(t1, o, 18.0, [("a", "t1"), ("c", "t3")])], links, tracks, T0)[0]
    assert not small.recorded and small.new_nodes == ["c"]  # node joins, but no new point
    big = mgr.assign([match_at(t1, o, 12.0, [("a", "t1"), ("d", "t4")])], links, tracks, T0)[0]
    assert big.recorded and first.track.point_count == 2
    next_frame = mgr.assign([match_at(T0 + 100_000_000, o, 20.0)], links, tracks, T0)[0]
    assert next_frame.recorded
