import math

import numpy as np
import pytest

from helpers import OBSERVER, T0_NS, gnomonic_path, sample, small_config
from skynet_edge.adsb import Aircraft
from skynet_edge.filters import FilterBank, FilterContext, great_circle_deviation, wingbeat
from skynet_edge.tracking import CONFIRMED, KalmanCV, Track

FPS = 30.0


def make_track(samples, vel=(0.0, 0.0)):
    kf = KalmanCV(samples[-1].u, samples[-1].v, vel[0], vel[1], 1.0, 1.0, 1.0, 1.0)
    tr = Track("t-test", kf, samples[0].t_ns, history_s=30.0)
    tr.state = CONFIRMED
    for s in samples:
        tr.add_sample(s)
    return tr


def track_along(fn, duration, fps=FPS, **kw):
    n = int(round(duration * fps)) + 1
    return make_track([sample(i / fps, *fn(i / fps), **kw) for i in range(n)])


def bank(extra=None):
    return FilterBank(small_config({"filters": extra or {}}).filters, FPS)


def ctx(t_s, **kw):
    c = FilterContext(T0_NS + int(round(t_s * 1e9)), OBSERVER, **kw)
    c.prepare()
    return c


def test_star_is_slow():
    sidereal = 360.0 / 86164.0905
    tr = track_along(gnomonic_path(120.0, 40.0, lambda t: sidereal * t, lambda t: 0.0), 2.0)
    v = bank().evaluate(tr, ctx(2.0))
    assert v.label == "slow" and "star" in v.reason
    young = track_along(gnomonic_path(120.0, 40.0, lambda t: 0.0, lambda t: 0.0), 0.5)
    assert bank().evaluate(young, ctx(0.5)) is None  # too young to judge
    fast = track_along(gnomonic_path(120.0, 40.0, lambda t: 1.0 * t, lambda t: 0.0), 2.0)
    assert bank().evaluate(fast, ctx(2.0)) is None


def test_diffuse_slow_cloud():
    tr = track_along(gnomonic_path(120.0, 40.0, lambda t: 0.4 * t, lambda t: 0.0), 1.5, eff_area=500.0, snr=6.0,
                     area=900)
    assert bank().evaluate(tr, ctx(1.5)).label == "cloud"
    compact = track_along(gnomonic_path(120.0, 40.0, lambda t: 0.4 * t, lambda t: 0.0), 1.5, eff_area=500.0,
                          snr=40.0, area=900)
    assert bank().evaluate(compact, ctx(1.5)) is None  # high contrast: not cloud texture


class StubFlow:
    def __init__(self, vu, vv):
        self.v = (vu, vv)

    def velocity_at(self, u, v, radius):
        return self.v


def test_fast_diffuse_blob_moving_with_the_wind_is_cloud():
    samples = [sample(i / FPS, 120.0 + 3.0 * i / FPS, 40.0, eff_area=400.0, snr=5.0, area=700, u=100 + 2 * i)
               for i in range(31)]
    tr = make_track(samples, vel=(60.0, 0.0))
    assert bank().evaluate(tr, ctx(1.0)) is None  # 3 deg/s: too fast for the slow-cloud rule
    v = bank().evaluate(tr, ctx(1.0, cloud_flow=StubFlow(58.0, 2.0)))
    assert v.label == "cloud" and "cloud field" in v.reason
    assert bank().evaluate(make_track(samples, vel=(60.0, 0.0)), ctx(1.0, cloud_flow=StubFlow(-60.0, 0.0))) is None


def _aircraft_track(ac, offset_deg=0.0, duration=1.0):
    def fn(t):
        az, el, _ = ac.azel_from(*OBSERVER, T0_NS + int(round(t * 1e9)))
        return az, el + offset_deg

    return track_along(fn, duration)


def test_aircraft_matched_against_adsb():
    ac = Aircraft("a1b2c3", "SKY1", 33.70, -112.30, 9000.0, True, 230.0, 270.0, 0.0, T0_NS)
    v = bank().evaluate(_aircraft_track(ac), ctx(1.0, aircraft=[ac]))
    assert v.label == "aircraft" and "SKY1" in v.reason
    assert v.sticky_until_ns > T0_NS + int(20e9)  # stays explained even if ADS-B drops out briefly
    assert bank().evaluate(_aircraft_track(ac, offset_deg=2.0), ctx(1.0, aircraft=[ac])) is None
    baro = Aircraft("a1b2c3", "SKY1", 33.70, -112.30, 9000.0, False, 230.0, 270.0, 0.0, T0_NS)
    assert bank().evaluate(_aircraft_track(ac, offset_deg=1.5), ctx(1.0, aircraft=[baro])).label == "aircraft"
    assert bank().evaluate(_aircraft_track(ac), ctx(1.0)) is None  # no ADS-B feed


def _bird_samples(area_fn, duration=1.5, snr=20.0, fps=FPS):
    n = int(round(duration * fps)) + 1
    return [sample(i / fps, 120.0 + 15.0 * i / fps, 30.0, area=int(area_fn(i / fps)), snr=snr) for i in range(n)]


def test_flapping_bird():
    tr = make_track(_bird_samples(lambda t: 300.0 * (1.0 + 0.4 * abs(math.sin(math.pi * 8.0 * t)))))
    v = bank().evaluate(tr, ctx(1.5))
    assert v.label == "bird" and "wingbeat" in v.reason


def test_approaching_defocused_bird():
    tr = make_track(_bird_samples(lambda t: 250.0 * math.exp(1.2 * t), snr=12.0))
    v = bank().evaluate(tr, ctx(1.5))
    assert v.label == "bird" and "d ln(area)/dt" in v.reason and "defocused" in v.reason


def test_large_soft_steady_object_is_not_called_a_bird():
    tr = make_track(_bird_samples(lambda t: 400.0, snr=10.0))  # big and defocused, but no flapping or approach
    assert bank().evaluate(tr, ctx(1.5)) is None
    small = make_track(_bird_samples(lambda t: 40.0 * (1.0 + 0.4 * abs(math.sin(math.pi * 8.0 * t)))))
    assert bank().evaluate(small, ctx(1.5)) is None  # too small to be near the lens


def test_wingbeat_detector():
    t = np.arange(0, 1.5, 1 / FPS)
    f, power, depth = wingbeat(t, 0.2 * np.sin(2 * np.pi * 6.0 * t), FPS, 2.0, 25.0)
    assert f == pytest.approx(6.0, abs=0.7) and power > 0.8 and depth == pytest.approx(0.2 / math.sqrt(2), rel=0.1)
    assert wingbeat(t, 0.5 * (t - 0.7) ** 2 - 0.1, FPS, 2.0, 25.0) is None  # a smooth trend is not a wingbeat
    assert wingbeat(t[:5], t[:5], FPS, 2.0, 25.0) is None


def test_satellite_and_meteor_labels_when_enabled():
    b = bank({"satellite": {"enabled": True}, "meteor": {"enabled": True}})
    sat = track_along(gnomonic_path(200.0, 60.0, lambda t: 0.8 * t, lambda t: 0.1 * t), 3.0)
    assert great_circle_deviation(list(sat.samples)) < 1e-3
    assert b.evaluate(sat, ctx(3.0)).label == "satellite"
    sat.flags.add("maneuvered")
    sat.verdicts.clear()
    assert b.evaluate(sat, ctx(3.0)) is None  # never hide a track that has turned
    meteor = track_along(gnomonic_path(200.0, 60.0, lambda t: 25.0 * t, lambda t: -5.0 * t), 0.4)
    assert b.evaluate(meteor, ctx(0.4)).label == "meteor"
    assert bank().evaluate(track_along(gnomonic_path(200.0, 60.0, lambda t: 0.8 * t, lambda t: 0.0), 3.0),
                           ctx(3.0)) is None  # disabled by default


def test_verdicts_hold_except_slow():
    b = bank({"hold_s": 3.0})
    cloud = track_along(gnomonic_path(120.0, 40.0, lambda t: 0.4 * t, lambda t: 0.0), 1.5, eff_area=500.0, snr=6.0)
    assert b.evaluate(cloud, ctx(1.5)).label == "cloud"
    for s in cloud.samples:
        s.snr = 40.0  # now compact and bright: the cloud rule no longer fires
    assert b.evaluate(cloud, ctx(2.0)).label == "cloud"  # still held
    assert b.evaluate(cloud, ctx(5.0)) is None  # hold expired
    hover = track_along(gnomonic_path(120.0, 40.0, lambda t: 0.0, lambda t: 0.0), 2.0)
    assert b.evaluate(hover, ctx(2.0)).label == "slow"
    darting = [sample(2.0 + i / FPS, 120.0 + 5.0 * i / FPS, 40.0) for i in range(1, 31)]
    for s in darting:
        hover.add_sample(s)
    assert b.evaluate(hover, ctx(3.0)) is None  # moves again: reported immediately
