import logging

import cv2
import numpy as np
import pytest

from helpers import add_gaussian, sky_frame, small_config
from skynet_edge.detection import (ArrayDetector, CpuDetector, cuda_array_module, interpolated_median_abs,
                                   make_detector)

W, H, FPS = 320, 240, 30.0


def det_cfg(**kw):
    return small_config({"detection": kw}).detection


def warmed(detector, rng, n=40):
    for _ in range(n):
        y = sky_frame(W, H, rng)
        detector.detect(y)
        detector.update_background(y)
    return detector


def test_robust_noise_estimate():
    rng = np.random.default_rng(0)
    for sigma in (0.8, 1.5, 4.0):
        x = np.rint(rng.normal(0, sigma, (4, 5000)))  # integer DN, like a real |frame - background|
        est = interpolated_median_abs(np.abs(x).astype(np.uint8)[None]) / 0.6745
        assert np.allclose(est, x.std(axis=1), rtol=0.06)  # std of the quantised data, not of the model


def test_point_target_subpixel_centroid():
    rng = np.random.default_rng(1)
    det = warmed(CpuDetector(det_cfg(), W, H, FPS), rng)
    for u, v in ((100.3, 80.7), (201.81, 150.25), (37.5, 200.0)):
        y = sky_frame(W, H, rng)
        add_gaussian(y, u, v, 30.0)
        res = det.detect(y)
        (b,) = res.blobs
        assert abs(b.u - u) < 0.1 and abs(b.v - v) < 0.1
        assert b.polarity == 1 and b.snr > 10 and 2 <= b.area <= 20 and b.eff_area < 10
        assert 0 < b.relative_intensity <= 1


def test_noise_only_frames_give_no_detections():
    rng = np.random.default_rng(2)
    det = warmed(CpuDetector(det_cfg(), W, H, FPS), rng)
    blobs = 0
    for _ in range(60):
        y = sky_frame(W, H, rng)
        blobs += len(det.detect(y).blobs)
        det.update_background(y)
    assert blobs <= 1
    # 1.5 DN sensor noise plus quantisation and the residual noise of the rolling background
    assert 1.45 < det.sigma < 1.95


def test_max_pool_decimation_keeps_single_pixel_targets():
    rng = np.random.default_rng(3)
    det = warmed(CpuDetector(det_cfg(decimation=2), W, H, FPS), rng)
    y = sky_frame(W, H, rng)
    y[101, 151] = np.clip(int(y[101, 151]) + 14, 0, 255)  # one pixel, ~9 sigma
    res = det.detect(y)
    assert any(abs(b.u - 151) < 0.6 and abs(b.v - 101) < 0.6 for b in res.blobs)
    # Area averaging (what a plain resize does) would have diluted it to ~1/4 of its contrast.
    diff = cv2.absdiff(y, det._bg_u8)
    averaged = cv2.resize(diff, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    assert averaged.max() < det.cfg.threshold_sigma * det.sigma


def test_dark_silhouette_has_negative_polarity():
    rng = np.random.default_rng(4)
    det = warmed(CpuDetector(det_cfg(), W, H, FPS), rng)
    y = sky_frame(W, H, rng)
    cv2.circle(y, (160, 120), 6, 40, -1)
    (b,) = det.detect(y).blobs
    assert b.polarity == -1 and abs(b.u - 160) < 0.5 and abs(b.v - 120) < 0.5 and b.area > 80


def test_sky_mask_suppresses_masked_pixels():
    rng = np.random.default_rng(5)
    det = warmed(CpuDetector(det_cfg(), W, H, FPS), rng)
    mask = np.ones((H, W), bool)
    mask[H // 2:] = False  # ground
    det.set_sky_mask(mask)
    y = sky_frame(W, H, rng)
    add_gaussian(y, 50.2, 50.2, 30.0)
    add_gaussian(y, 50.2, 200.2, 30.0)
    (b,) = det.detect(y).blobs
    assert b.v < H / 2


def test_global_disturbance_resets_background():
    rng = np.random.default_rng(6)
    det = warmed(CpuDetector(det_cfg(), W, H, FPS), rng)
    y = sky_frame(W, H, rng, level=130.0)  # auto-exposure jump
    res = det.detect(y)
    assert res.disturbed and not res.blobs
    y2 = sky_frame(W, H, rng, level=130.0)
    add_gaussian(y2, 100.4, 100.4, 30.0)
    det.update_background(y)
    assert len(det.detect(y2).blobs) == 1


def test_protected_boxes_update_slowly():
    rng = np.random.default_rng(7)
    det = warmed(CpuDetector(det_cfg(background_update_every=1), W, H, FPS), rng)
    y = sky_frame(W, H, rng)
    y[100:110, 100:110] = 200
    y[100:110, 200:210] = 200
    det.detect(y)
    before = det._bg.copy()
    det.update_background(y, [(100, 100, 10, 10)])
    fast = float((det._bg - before)[100:110, 200:210].mean())
    slow = float((det._bg - before)[100:110, 100:110].mean())
    assert fast > 0 and slow == pytest.approx(fast * det.cfg.foreground_alpha_scale, rel=0.05)


def test_array_backend_matches_cpu_backend():
    """The CuPy code path, run with NumPy as the array module, finds the same blobs."""
    rng = np.random.default_rng(8)
    cfg = det_cfg()
    cpu, arr = CpuDetector(cfg, W, H, FPS), ArrayDetector(cfg, W, H, FPS, np)
    frames = [sky_frame(W, H, rng) for _ in range(40)]
    for y in frames:
        for d in (cpu, arr):
            d.detect(y)
            d.update_background(y)
    y = sky_frame(W, H, rng)
    add_gaussian(y, 120.4, 60.6, 25.0)
    cv2.circle(y, (250, 180), 5, 50, -1)
    a = sorted(cpu.detect(y).blobs, key=lambda b: b.u)
    b = sorted(arr.detect(y).blobs, key=lambda b: b.u)
    assert len(a) == len(b) == 2
    for x, z in zip(a, b):
        assert abs(x.u - z.u) < 0.05 and abs(x.v - z.v) < 0.05 and x.polarity == z.polarity
    assert arr.sigma == pytest.approx(cpu.sigma, rel=0.05)


def test_backend_selection_falls_back_to_cpu(caplog):
    caplog.set_level(logging.INFO)
    d = make_detector(det_cfg(backend="cuda"), W, H, FPS)
    if cuda_array_module() is None:
        assert isinstance(d, CpuDetector)
        assert "using cpu" in caplog.text
    assert make_detector(det_cfg(backend="cpu"), W, H, FPS).name == "cpu"
