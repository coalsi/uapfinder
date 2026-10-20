"""Star calibration: star detection on synthetic stacked frames."""

from __future__ import annotations

import numpy as np
import pytest

from skynet_edge.starcal import synth
from skynet_edge.starcal.detect import detect_stars

T_NIGHT = 1_792_533_600_000_000_000  # 2026-10-20 22:00 UTC
SITE = (47.0, 8.0, 500.0)


def nearest(d, truth_uv):
    dd = np.hypot(d.u[:, None] - truth_uv[None, :, 0], d.v[:, None] - truth_uv[None, :, 1])
    return dd.argmin(axis=1), dd.min(axis=1)


@pytest.fixture(scope="module")
def clear_frame():
    s = synth.SynthSky(k1=-0.08, yaw_deg=130.0, pitch_deg=45.0, roll_deg=3.0, hot_pixels=500, seed=5)
    img, uv, mag = synth.render(s, T_NIGHT, *SITE, return_truth=True)
    return s, img, uv, mag


def test_centroids_subpixel_and_bright_first(clear_frame):
    s, img, uv, mag = clear_frame
    d = detect_stars(img)
    assert len(d) > 450
    j, dist = nearest(d, uv)
    top = slice(0, 50)
    assert np.all(dist[top] < 1.0)                       # the brightest sources are catalog stars
    assert np.median(mag[j[top]]) < 4.0
    good = dist < 1.5
    err = np.hypot(d.u - uv[j, 0], d.v - uv[j, 1])[good]
    assert np.median(err) < 0.2                          # px; noise + faint blended neighbours
    assert np.sum(good) > 0.75 * len(d)


def test_hot_pixels_rejected(clear_frame):
    s, img, uv, mag = clear_frame
    rng = np.random.default_rng(s.seed)
    # rebuild the hot-pixel positions the renderer used: easier to add our own, far from stars
    img2 = img.copy()
    hy = rng.integers(50, s.height - 50, 300)
    hx = rng.integers(50, s.width - 50, 300)
    img2[hy, hx] = 60000
    d = detect_stars(img2)
    dd = np.hypot(d.u[:, None] - hx[None, :], d.v[:, None] - hy[None, :]).min(axis=0)
    star_near = np.hypot(uv[:, None, 0] - hx[None, :], uv[:, None, 1] - hy[None, :]).min(axis=0) < 6
    assert np.sum((dd < 1.0) & ~star_near) == 0


def test_moon_and_clouds_do_not_make_sources():
    s = synth.SynthSky(k1=-0.08, yaw_deg=130.0, pitch_deg=45.0, roll_deg=3.0, clouds=0.4, moon=(1500, 300), seed=7)
    img, uv, mag = synth.render(s, T_NIGHT, *SITE, return_truth=True)
    d = detect_stars(img)
    assert d.extended and np.hypot(d.extended[0][0] - 1500, d.extended[0][1] - 300) < 3
    # nothing detected on the Moon disc or in its immediate glare
    assert np.all(np.hypot(d.u - 1500, d.v - 300) > 25)
    j, dist = nearest(d, uv)
    assert np.mean(dist[:40] < 1.5) > 0.8                # still mostly real stars at the top


def test_overcast_and_daylight_frames_give_nothing():
    s = synth.SynthSky(clouds=1.0, seed=2)
    assert len(detect_stars(synth.render(s, T_NIGHT, *SITE))) < 10
    flat = np.full((1080, 1920), 50000, np.uint16) + np.random.default_rng(0).integers(0, 200, (1080, 1920)).astype(np.uint16)
    assert len(detect_stars(flat)) < 5
    assert len(detect_stars(np.zeros((1080, 1920), np.uint8))) == 0
