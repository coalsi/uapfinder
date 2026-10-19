"""Matchmaker behaviour on synthetic rays computed from real WGS84 geometry."""

from __future__ import annotations

import math
from typing import Dict, Iterable, Optional, Sequence

import pytest

from skynet_common.geodesy import azel_to_ecef_unit, enu_to_ecef_vector, geodetic_to_ecef, look_angles
from skynet_server.config import MatchConfig
from skynet_server.matchmaker import Matchmaker, NodeSamples, Plan, TrackSamples
from skynet_server.models import Detection

T0 = 1_800_000_000_000_000_000
TARGET = (33.55, -112.20, 9000.0)
NODES = {  # ~20-40 km around the target
    "a": (33.40, -112.35), "b": (33.72, -112.05), "c": (33.70, -112.40), "d": (33.38, -111.98),
    "e": (33.55, -111.85),
}


def observe(node: str, target, t_ns: int, track: Optional[str] = "t", seq: int = 1, d_az: float = 0.0,
            d_el: float = 0.0) -> Detection:
    lat, lon = NODES[node]
    az, el, _ = look_angles(lat, lon, 300.0, *target)
    az, el = az + d_az, el + d_el
    return Detection(node, t_ns, seq, track, lat, lon, 300.0, az, el, geodetic_to_ecef(lat, lon, 300.0),
                     azel_to_ecef_unit(lat, lon, az, el))


class Source:
    def __init__(self, dets: Iterable[Detection] = ()):
        self.nodes: Dict[str, NodeSamples] = {}
        for d in dets:
            self.add(d)

    def add(self, d: Detection) -> None:
        self.nodes.setdefault(d.node_id, NodeSamples()).add(d, 0)

    def local_track(self, node_id: str, track_id: str) -> Optional[TrackSamples]:
        ns = self.nodes.get(node_id)
        return ns.tracks.get(track_id) if ns else None

    def local_tracks(self, node_id: str) -> Iterable[TrackSamples]:
        ns = self.nodes.get(node_id)
        return ns.tracks.values() if ns else ()

    def untracked(self, node_id: str, lo: int, hi: int) -> Sequence[Detection]:
        ns = self.nodes.get(node_id)
        return ns.untracked(lo, hi) if ns else ()


def moving_target(t_ns: int, speed_east: float = 2000.0):
    """A target moving east at ``speed_east`` m/s through TARGET at T0."""
    dt = (t_ns - T0) / 1e9
    o = geodetic_to_ecef(*TARGET)
    d = enu_to_ecef_vector(TARGET[0], TARGET[1], (speed_east * dt, 0.0, 0.0))
    from skynet_common.geodesy import ecef_to_geodetic
    return ecef_to_geodetic(o[0] + d[0], o[1] + d[1], o[2] + d[2])


def error_m(match, target) -> float:
    return math.dist(match.point, geodetic_to_ecef(*target))


def test_pair_triangulates_the_target():
    mm = Matchmaker(MatchConfig())
    src = Source([observe("b", TARGET, T0)])
    m = mm.match(Plan(observe("a", TARGET, T0), ["b"]), src)
    assert m is not None
    assert error_m(m, TARGET) < 1.0
    assert m.alt_m == pytest.approx(TARGET[2], abs=1.0)
    assert [r.node_id for r in m.rays] == ["a", "b"]
    assert m.pair_keys == ["a:1|b:1"] and m.error_margin_m < 1.0 and 0 < m.sigma_m < 100


def test_window_and_mismatch_are_rejected():
    mm = Matchmaker(MatchConfig())
    late = Source([observe("b", TARGET, T0 + 60_000_000)])  # outside the 50 ms window, no bracket
    assert mm.match(Plan(observe("a", TARGET, T0), ["b"]), late) is None
    elsewhere = Source([observe("b", (33.9, -111.6, 9000.0), T0)])  # another object
    assert mm.match(Plan(observe("a", TARGET, T0, track="x"), ["b"]), elsewhere) is None
    # Small angular noise still passes the gate; a 1 degree error does not.
    noisy = Source([observe("b", TARGET, T0, d_az=0.05)])
    assert mm.match(Plan(observe("a", TARGET, T0, track="y"), ["b"]), noisy) is not None
    off = Source([observe("b", TARGET, T0, d_az=1.0)])
    assert mm.match(Plan(observe("a", TARGET, T0, track="z"), ["b"]), off) is None


def test_bracketing_samples_are_interpolated():
    t = T0
    before, after = t - 60_000_000, t + 40_000_000  # 2000 m/s: the object moves 80-120 m in that time
    src = Source([observe("b", moving_target(before), before, seq=1), observe("b", moving_target(after), after, seq=2)])
    m = Matchmaker(MatchConfig()).match(Plan(observe("a", moving_target(t), t, seq=7), ["b"]), src)
    assert m is not None and m.rays[1].interpolated
    assert m.rays[1].seq == 2  # w = 0.6: the later sample is the nearer one
    assert error_m(m, moving_target(t)) < 15.0
    # Without the later sample only the nearest one within 50 ms could be used, and none is.
    only_before = Source([observe("b", moving_target(before), before)])
    assert Matchmaker(MatchConfig()).match(Plan(observe("a", moving_target(t), t), ["b"]), only_before) is None


def test_interpolation_beats_nearest_sample():
    t = T0
    near, far = t - 30_000_000, t + 70_000_000
    nearest_only = Source([observe("b", moving_target(near), near)])
    bracketed = Source([observe("b", moving_target(near), near), observe("b", moving_target(far), far, seq=2)])
    me = observe("a", moving_target(t), t)
    m_nearest = Matchmaker(MatchConfig()).match(Plan(me, ["b"]), nearest_only)
    m_interp = Matchmaker(MatchConfig()).match(Plan(me, ["b"]), bracketed)
    assert m_nearest is not None and m_interp is not None
    assert error_m(m_interp, moving_target(t)) < error_m(m_nearest, moving_target(t)) / 5


def test_three_or_more_nodes_are_solved_together():
    src = Source([observe(n, TARGET, T0, d_az=0.02 * i) for i, n in enumerate("bcd")])
    m = Matchmaker(MatchConfig()).match(Plan(observe("a", TARGET, T0), ["b", "c", "d"]), src)
    assert m is not None and sorted(r.node_id for r in m.rays) == ["a", "b", "c", "d"]
    assert len(m.pair_keys) == 3 and error_m(m, TARGET) < 30.0
    pair = Matchmaker(MatchConfig()).match(Plan(observe("a", TARGET, T0), ["b"]), Source([observe("b", TARGET, T0)]))
    assert m.sigma_m < pair.sigma_m


def test_inconsistent_ray_is_dropped():
    # Node e reports a different object that lies on a's line of sight, 12 km
    # further away, so the a-e pair alone passes the gate but disagrees with
    # the a-b-c solution.
    a = observe("a", TARGET, T0)
    far_point = tuple(a.origin[i] + a.direction[i] * (math.dist(a.origin, geodetic_to_ecef(*TARGET)) + 12_000.0)
                      for i in range(3))
    from skynet_common.geodesy import ecef_to_geodetic
    decoy = ecef_to_geodetic(*far_point)
    src = Source([observe("b", TARGET, T0), observe("c", TARGET, T0), observe("e", decoy, T0, track="other")])
    m = Matchmaker(MatchConfig()).match(Plan(a, ["b", "c", "e"]), src)
    assert m is not None and sorted(r.node_id for r in m.rays) == ["a", "b", "c"]
    assert error_m(m, TARGET) < 5.0


def test_ray_cap_keeps_the_best_neighbours():
    src = Source([observe(n, TARGET, T0, d_az=0.01 * i) for i, n in enumerate("bcde")])
    m = Matchmaker(MatchConfig(max_rays=3)).match(Plan(observe("a", TARGET, T0), list("bcde")), src)
    assert m is not None and len(m.rays) == 3


def test_search_plans_guided_full_and_backoff():
    mm = Matchmaker(MatchConfig(), full_search_interval_ns=2_000_000_000, miss_backoff_ns=300_000_000)
    a0 = observe("a", TARGET, T0)
    assert mm.plan(a0, ["b", "c"]).guide is None  # nothing known yet: full search
    src = Source([observe("b", TARGET, T0)])
    assert mm.match(mm.plan(a0, ["b", "c"]), src) is not None
    assert mm.plan(observe("a", TARGET, T0 + 100_000_000, seq=2), ["b", "c"]).guide == {"b": "t"}
    # b learnt a as a partner too, but still runs its own first full search
    # (a's match says nothing about nodes only b might see).
    assert "a" in mm._partners[("b", "t")].by_node
    assert mm.plan(observe("b", TARGET, T0 + 100_000_000, seq=2), ["a", "c"]).guide is None
    # The periodic full search comes back after the interval.
    assert mm.plan(observe("a", TARGET, T0 + 2_500_000_000, seq=3), ["b", "c"]).guide is None
    # A local track whose full search found nothing is not searched again for a while.
    lone = observe("c", (34.2, -111.0, 5000.0), T0, track="lonely")
    assert mm.match(Plan(lone, ["a", "b"]), src) is None
    assert mm.plan(observe("c", (34.2, -111.0, 5000.0), T0 + 100_000_000, track="lonely"), ["a", "b"]) is None
    assert mm.plan(observe("c", (34.2, -111.0, 5000.0), T0 + 400_000_000, track="lonely"), ["a", "b"]) is not None
    assert mm.plan(a0, []) is None  # no neighbours


def test_guided_failure_requests_a_full_search_only_when_useful():
    mm = Matchmaker(MatchConfig())
    a0 = observe("a", TARGET, T0)
    assert mm.match(mm.plan(a0, ["b"]), Source([observe("b", TARGET, T0)])) is not None
    plan = mm.plan(observe("a", TARGET, T0 + 100_000_000, seq=2), ["b"])
    assert plan.guide == {"b": "t"}
    # b reported this track recently but not yet for this frame: no point in a full search.
    assert mm.match(plan, Source([observe("b", TARGET, T0)])) is None and not plan.retry
    # b went silent on that track: a full search might find it under a new id.
    plan = mm.plan(observe("a", TARGET, T0 + 200_000_000, seq=3), ["b"])
    assert mm.match(plan, Source()) is None and plan.retry


def test_full_search_skips_neighbours_that_cannot_see_the_ray():
    from skynet_common.camera import camera_basis_enu
    from skynet_common.geodesy import enu_to_ecef_vector

    def cone(node: str, yaw: float, pitch: float):
        lat, lon = NODES[node]
        _, _, forward = camera_basis_enu(yaw, pitch, 0.0)
        return (geodetic_to_ecef(lat, lon, 300.0), enu_to_ecef_vector(lat, lon, forward), math.cos(math.radians(60)))

    az_b, el_b, _ = look_angles(*NODES["b"], 300.0, *TARGET)
    cones = {"b": cone("b", az_b, el_b), "c": cone("c", 270.0, 10.0)}  # c looks west, away from a's ray
    mm = Matchmaker(MatchConfig(), cones=cones)
    plan = mm.full_plan(observe("a", TARGET, T0), ["b", "c", "d"])
    assert plan.neighbours == ["b", "d"] and mm.cone_skipped == 1  # d has no known camera: kept
    src = Source([observe("b", TARGET, T0)])
    assert mm.match(plan, src) is not None
