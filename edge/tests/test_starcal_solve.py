"""Star calibration: end-to-end solves on synthetic night-sky frames.

The frames are rendered from the bundled catalog with OpenCV's projection
(cv2.projectPoints) and the skynet_common attitude convention, with noise,
hot pixels, a light-pollution gradient, extra faint stars that are not in the
catalog and, per test, clouds, a Moon, planets and ground. The recovered
calibration is checked the way the node uses it: through
skynet_edge.geometry.CameraModel (cv2.undistortPoints) + pixel_to_azel.

This is synthetic validation only; see the module docstring of
skynet_edge.starcal.solve and the coordinator notes for a real-sky test.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from skynet_edge.geometry import Attitude, CameraModel
from skynet_edge.starcal import Observer, Prior, SolveParams, StarCalResult, calibrate, synth
from skynet_edge.starcal.model import attitude_matrix

T_NIGHT = 1_792_533_600_000_000_000          # 2026-10-20 22:00 UTC (Sun at -50 deg in Zurich)
T_DAY = T_NIGHT - 11 * 3600 * 1_000_000_000  # 11:00 UTC
ZURICH = Observer(47.0, 8.0, 500.0)


def render(**kw):
    obs = kw.pop("observer", ZURICH)
    t = kw.pop("t_ns", T_NIGHT)
    s = synth.SynthSky(**kw)
    return s, synth.render(s, t, obs.lat, obs.lon, obs.alt_m)


def rotation_error_deg(r: StarCalResult, s: synth.SynthSky) -> float:
    m1 = attitude_matrix(r.yaw_deg, r.pitch_deg, r.roll_deg)
    m2 = attitude_matrix(s.yaw_deg, s.pitch_deg, s.roll_deg)
    return math.degrees(math.acos(min(1.0, (np.trace(m1 @ m2.T) - 1.0) / 2.0)))


def mapping_error_deg(r: StarCalResult, s: synth.SynthSky, margin: float = 0.02) -> float:
    """Max angle between the true and the calibrated pixel -> direction mapping
    over a grid covering the frame, using the node's own CameraModel."""
    true = CameraModel(s.width, s.height, s.fx, s.fy, s.cx, s.cy, [s.k1, s.k2, 0, 0, 0])
    cal = CameraModel(s.width, s.height, r.fx, r.fy, r.cx, r.cy, r.dist)
    gu, gv = np.meshgrid(np.linspace(margin * s.width, (1 - margin) * s.width, 24),
                         np.linspace(margin * s.height, (1 - margin) * s.height, 14))
    uv = np.stack([gu.ravel(), gv.ravel()], axis=1)
    a = true.pixels_to_azel(uv, Attitude(s.yaw_deg, s.pitch_deg, s.roll_deg))
    b = cal.pixels_to_azel(uv, Attitude(r.yaw_deg, r.pitch_deg, r.roll_deg))
    ea = np.radians(a)
    eb = np.radians(b)
    va = np.stack([np.cos(ea[:, 1]) * np.sin(ea[:, 0]), np.cos(ea[:, 1]) * np.cos(ea[:, 0]), np.sin(ea[:, 1])], 1)
    vb = np.stack([np.cos(eb[:, 1]) * np.sin(eb[:, 0]), np.cos(eb[:, 1]) * np.cos(eb[:, 0]), np.sin(eb[:, 1])], 1)
    return float(np.degrees(np.arccos(np.clip(np.sum(va * vb, axis=1), -1, 1))).max())


def wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


@pytest.fixture(scope="module")
def cluttered_45():
    """45 deg elevation, mild barrel distortion, offset principal point, focal
    length 0.5% off nominal; clouds over ~30% of the frame, a Moon with glare,
    planets, hot pixels, a gradient. Heading unknown."""
    s, img = render(k1=-0.08, k2=0.0, fx=965.0, fy=965.0, cx=963.0, cy=536.0, yaw_deg=130.0, pitch_deg=45.0,
                    roll_deg=3.0, clouds=0.3, moon=(1500, 300), extra_bright_sources=12, seed=11)
    prior = Prior(1920, 1080, 90.0, pitch_deg=44.0, roll_deg=2.0, sigma_deg=2.0)
    return s, img, calibrate(img, T_NIGHT, ZURICH, prior)


def test_unknown_heading_45deg_with_clouds_moon_planets(cluttered_45):
    s, img, r = cluttered_45
    assert r.ok, r.reason
    assert abs(wrap(r.yaw_deg - s.yaw_deg)) < 0.02
    assert abs(r.pitch_deg - s.pitch_deg) < 0.02
    assert abs(r.roll_deg - s.roll_deg) < 0.02
    assert r.fx == pytest.approx(s.fx, rel=0.002) and r.fy == pytest.approx(s.fy, rel=0.002)
    assert r.k1 == pytest.approx(s.k1, abs=0.005)
    assert abs(r.cx - s.cx) < 3 and abs(r.cy - s.cy) < 3
    assert mapping_error_deg(r, s) < 0.02
    assert r.stars_matched >= 200 and r.rms_deg < 0.03 and r.sigma_deg < 0.01
    assert r.elapsed_s < 30.0


def test_result_fields_and_opencv_dist(cluttered_45):
    _, _, r = cluttered_45
    d = r.to_dict()
    for k in ("ok", "reason", "yaw_deg", "pitch_deg", "roll_deg", "fx", "fy", "cx", "cy", "k1", "k2",
              "stars_matched", "rms_deg", "t_ns"):
        assert k in d
    assert d["t_ns"] == T_NIGHT and r.dist == [r.k1, r.k2, 0.0, 0.0, 0.0]
    assert all(isinstance(d[k], float) for k in ("yaw_deg", "fx", "k1", "rms_deg"))
    assert 0.0 <= r.yaw_deg < 360.0


def test_wide_lens_with_k2_and_heading_prior():
    # the 90-degree-class lens of test_geometry: k1 = -0.20, k2 = 0.05
    s, img = render(k1=-0.20, k2=0.05, fx=1000.0, fy=1000.0, cx=958.0, cy=541.0, yaw_deg=3.0, pitch_deg=50.0,
                    roll_deg=-1.5, seed=12)
    prior = Prior(1920, 1080, 87.7, yaw_deg=1.0, pitch_deg=51.0, roll_deg=0.0, sigma_deg=2.0)
    r = calibrate(img, T_NIGHT, ZURICH, prior)
    assert r.ok, r.reason
    assert rotation_error_deg(r, s) < 0.02
    assert r.fx == pytest.approx(1000.0, rel=0.002)
    assert mapping_error_deg(r, s) < 0.03


def test_zenith_pointing_heading_unknown():
    # Straight up: yaw and roll are the same rotation; compare the attitude as a rotation.
    s, img = render(k1=-0.08, fx=965.0, fy=965.0, yaw_deg=0.0, pitch_deg=90.0, roll_deg=25.0, seed=13)
    r = calibrate(img, T_NIGHT, ZURICH, Prior(1920, 1080, 90.0, pitch_deg=90.0, sigma_deg=3.0))
    assert r.ok, r.reason
    assert rotation_error_deg(r, s) < 0.02
    assert mapping_error_deg(r, s) < 0.02


def test_low_elevation_with_ground_southern_hemisphere():
    obs = Observer(-33.9, 18.4, 30.0)
    t = T_NIGHT + 2 * 3600 * 1_000_000_000   # 02:00 local
    s, img = render(observer=obs, t_ns=t, k1=-0.08, fx=965.0, fy=965.0, yaw_deg=200.0, pitch_deg=20.0,
                    roll_deg=-4.0, seed=14)
    r = calibrate(img, t, obs, Prior(1920, 1080, 90.0, pitch_deg=21.0, roll_deg=-3.0, sigma_deg=2.0))
    assert r.ok, r.reason
    assert abs(wrap(r.yaw_deg - s.yaw_deg)) < 0.03 and abs(r.pitch_deg - s.pitch_deg) < 0.03
    assert abs(r.roll_deg - s.roll_deg) < 0.03
    assert mapping_error_deg(r, s) < 0.03


def test_narrow_lens():
    s, img = render(k1=-0.02, fx=2050.0, fy=2050.0, yaw_deg=300.0, pitch_deg=60.0, roll_deg=3.0, seed=15)
    r = calibrate(img, T_NIGHT, ZURICH, Prior(1920, 1080, 50.2, pitch_deg=61.0, roll_deg=1.0, sigma_deg=2.0))
    assert r.ok, r.reason
    assert rotation_error_deg(r, s) < 0.02
    assert r.fx == pytest.approx(2050.0, rel=0.002)
    assert mapping_error_deg(r, s) < 0.02


def test_fixed_intrinsics_attitude_only():
    s, img = render(k1=-0.08, fx=965.0, fy=965.0, yaw_deg=77.0, pitch_deg=35.0, roll_deg=1.0, seed=16)
    prior = Prior(1920, 1080, 90.0, pitch_deg=35.0, roll_deg=0.0, sigma_deg=3.0, fx=965.0, fy=965.0,
                  cx=959.5, cy=539.5, k1=-0.08, k2=0.0, fit_intrinsics=False)
    r = calibrate(img, T_NIGHT, ZURICH, prior)
    assert r.ok, r.reason
    assert rotation_error_deg(r, s) < 0.01
    assert (r.fx, r.k1) == (965.0, -0.08)


# --- failure modes: never a wrong "ok" -------------------------------------

def test_overcast_fails():
    _, img = render(clouds=1.0, pitch_deg=45.0, seed=21)
    r = calibrate(img, T_NIGHT, ZURICH, Prior(1920, 1080, 90.0, pitch_deg=45.0, roll_deg=0.0, sigma_deg=2.0))
    assert not r.ok and "too few stars" in r.reason
    assert r.yaw_deg is None


def test_daylight_fails():
    _, img = render(pitch_deg=45.0, seed=22)
    r = calibrate(img, T_DAY, ZURICH, Prior(1920, 1080, 90.0, pitch_deg=45.0, roll_deg=0.0))
    assert not r.ok and "sun" in r.reason and r.sun_alt_deg > 20


def test_bright_sky_fails():
    # Moonlit haze / twilight: background near saturation, only a handful of stars
    _, img = render(background=60000.0, mag_zero_peak=600.0, read_noise=50.0, seed=23)
    r = calibrate(img, T_NIGHT, ZURICH, Prior(1920, 1080, 90.0, pitch_deg=45.0, roll_deg=0.0, sigma_deg=2.0))
    assert not r.ok


def test_too_few_stars_fails():
    # Only the very brightest stars are above the noise
    _, img = render(mag_zero_peak=900.0, extra_faint_stars=0, seed=24)
    r = calibrate(img, T_NIGHT, ZURICH, Prior(1920, 1080, 90.0, pitch_deg=45.0, roll_deg=0.0, sigma_deg=2.0))
    assert not r.ok
    assert r.stars_matched < SolveParams().min_stars or "too few" in r.reason


@pytest.mark.parametrize("case", ["mirrored", "wrong_site", "wrong_time", "wrong_prior"])
def test_inconsistent_inputs_fail(case):
    s, img = render(k1=-0.08, fx=965.0, fy=965.0, yaw_deg=130.0, pitch_deg=45.0, roll_deg=3.0, seed=25)
    obs, t, prior = ZURICH, T_NIGHT, Prior(1920, 1080, 90.0, pitch_deg=44.0, roll_deg=2.0, sigma_deg=2.0)
    if case == "mirrored":            # no rotation reproduces a mirrored sky
        img = img[:, ::-1].copy()
    elif case == "wrong_site":
        obs = Observer(10.0, 8.0, 500.0)
    elif case == "wrong_time":        # the sky turned 15 deg about the pole; the prior forbids that attitude
        t = T_NIGHT + 3600 * 1_000_000_000
    else:
        prior = Prior(1920, 1080, 90.0, pitch_deg=70.0, roll_deg=2.0, sigma_deg=2.0)
    r = calibrate(img, t, obs, prior)
    assert not r.ok, (case, r)
    assert r.yaw_deg is None


def test_image_size_mismatch():
    r = calibrate(np.zeros((540, 960), np.uint8), T_NIGHT, ZURICH, Prior(1920, 1080, 90.0))
    assert not r.ok and "prior says" in r.reason
