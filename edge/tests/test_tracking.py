import itertools

import numpy as np
import pytest

from helpers import T0_NS, add_gaussian, sky_frame, small_config
from skynet_edge.detection import Blob
from skynet_edge.tracking import CONFIRMED, TENTATIVE, Tracker, gated_assignment, hungarian

FPS = 30.0
DT = int(1e9 / FPS)


def blob(u, v, area=6):
    return Blob(u=u, v=v, area=area, bbox=(int(u) - 2, int(v) - 2, 5, 5), peak=30.0, mean=15.0, flux=60.0,
                eff_area=4.0, snr=15.0, sigma=2.0, polarity=1, relative_intensity=0.3)


def fake_azel(uv):
    return np.asarray(uv, dtype=float) * 0.1  # az, el = u/10, v/10 degrees


def tracker(**kw):
    return Tracker(small_config({"tracking": kw}).tracking, FPS, id_prefix="t")


def test_hungarian_matches_brute_force():
    rng = np.random.default_rng(0)
    for n, m in [(1, 1), (3, 3), (4, 6), (6, 4), (5, 5)]:
        for _ in range(20):
            c = rng.random((n, m))
            pairs = hungarian(c)
            assert len(pairs) == min(n, m)
            assert len({i for i, _ in pairs}) == len({j for _, j in pairs}) == len(pairs)
            k = min(n, m)
            best = min(sum(c[i, j] for i, j in zip(rows, cols))
                       for rows in itertools.permutations(range(n), k)
                       for cols in [range(m)] if n > m) if n > m else \
                min(sum(c[i, j] for i, j in zip(range(n), cols)) for cols in itertools.permutations(range(m), k))
            assert sum(c[i, j] for i, j in pairs) == pytest.approx(best)


def test_gated_assignment_respects_the_gate():
    cost = np.array([[1.0, 9.0, 0.0], [2.0, 1.0, 0.0], [0.0, 0.0, 5.0]])
    feasible = np.array([[True, True, False], [True, True, False], [False, False, True]])
    assert sorted(gated_assignment(cost, feasible)) == [(0, 0), (1, 1), (2, 2)]
    assert gated_assignment(cost, np.zeros_like(feasible)) == []


def test_lifecycle_tentative_confirmed_lost():
    tr = tracker(confirm_hits=5, confirm_window=8, max_coast_s=0.2)
    out = None
    for i in range(10):
        out = tr.update(T0_NS + i * DT, [blob(100 + 2 * i, 50 + i)], fake_azel)
        if i < 4:
            assert tr.tracks[0].state == TENTATIVE and not out.active
    assert [t.state for t in tr.tracks] == [CONFIRMED]
    track = tr.tracks[0]
    assert track.id == "t-000001" and len(track.samples) == 10
    s = track.last
    assert (s.az, s.el) == pytest.approx((s.u * 0.1, s.v * 0.1))
    ended = []
    for i in range(10, 20):
        ended += tr.update(T0_NS + i * DT, [], fake_azel).ended
    assert ended == [track] and not tr.tracks


def test_clutter_never_confirms():
    tr = tracker()
    rng = np.random.default_rng(1)
    confirmed = 0
    for i in range(60):
        out = tr.update(T0_NS + i * DT, [blob(*rng.uniform(0, 600, 2))], fake_azel)
        confirmed += len(out.confirmed_now)
    assert confirmed == 0


def test_crossing_targets_keep_their_identities():
    tr = tracker()
    for i in range(40):
        a = blob(100 + 5 * i, 100 + 5 * i)
        b = blob(100 + 5 * i, 300 - 5 * i)  # paths cross at i = 20
        tr.update(T0_NS + i * DT, [a, b] if i % 2 else [b, a], fake_azel)
    assert len(tr.tracks) == 2
    for t in tr.tracks:
        us = np.array([s.u for s in t.samples])
        vs = np.array([s.v for s in t.samples])
        slope = np.polyfit(us, vs, 1)[0]
        assert abs(abs(slope) - 1.0) < 0.01  # each track stayed on one straight line
        assert len(t.samples) == 40


def frames_with_target(n, u0, v0, du, dv, w=320, h=240, seed=2):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        y = sky_frame(w, h, rng)
        add_gaussian(y, u0 + du * i, v0 + dv * i, 40.0, sigma=1.2)
        out.append(y)
    return out


@pytest.mark.parametrize("du, dv", [(4.0, 1.0), (12.0, -3.0)])
def test_optical_flow_bridges_missed_detections(du, dv):
    """LK, seeded with the Kalman prediction, follows the target through missed detections."""
    n = 16
    frames = frames_with_target(n, 40.0, 100.0, du, dv)
    tr = tracker()
    prev = None
    for i in range(n):
        blobs = [] if i in (10, 11) else [blob(40.0 + du * i, 100.0 + dv * i)]
        tr.update(T0_NS + i * DT, blobs, fake_azel, prev, frames[i])
        prev = frames[i]
    (t,) = tr.tracks
    lk = [s for s in t.samples if s.source == "lk"]
    assert [s.t_ns for s in lk] == [T0_NS + 10 * DT, T0_NS + 11 * DT]
    for s in lk:
        i = (s.t_ns - T0_NS) // DT
        assert abs(s.u - (40.0 + du * i)) < 0.5 and abs(s.v - (100.0 + dv * i)) < 0.5
    assert t.state == CONFIRMED and len(t.samples) == n
