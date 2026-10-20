"""Synthetic night-sky frames for testing the star calibration.

Stars come from the bundled catalog, placed with the astrometry in
``astro.py`` and projected with **OpenCV** (cv2.projectPoints with
dist = [k1, k2, 0, 0, 0]) and the attitude of skynet_common.camera, so the
solver's own projection code is checked against OpenCV's model. Optional
realism: extra faint stars that are not in the catalog, extinction, a
light-pollution gradient, cloud banks (transmission map + glow), a bright
Moon disc with glare, hot pixels, Poisson/read noise, saturation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

from skynet_common.camera import camera_basis_enu

from . import astro
from .catalog import load_catalog
from .model import Intrinsics


@dataclass
class SynthSky:
    width: int = 1920
    height: int = 1080
    fx: float = 960.0
    fy: float = 960.0
    cx: float = 959.5
    cy: float = 539.5
    k1: float = 0.0
    k2: float = 0.0
    yaw_deg: float = 0.0
    pitch_deg: float = 45.0
    roll_deg: float = 0.0
    psf_sigma: float = 1.1
    background: float = 1000.0
    gradient: float = 600.0          # extra counts at the bottom edge
    read_noise: float = 6.0
    gain: float = 1.0                # counts per photo-electron (Poisson term)
    mag_zero_peak: float = 60000.0   # peak counts of a mag-0 star before extinction
    full_scale: float = 65535.0
    extinction: float = 0.25         # mag per airmass
    hot_pixels: int = 300
    extra_faint_stars: int = 3000    # random mag 6.5..8.5 stars not in the catalog
    extra_bright_sources: int = 0    # planets/satellites not in the catalog (mag -2..1)
    clouds: float = 0.0              # fraction of the frame covered by cloud (0..1)
    cloud_glow: float = 500.0
    moon: Optional[Tuple[float, float]] = None   # (u, v) pixel of a Moon disc
    moon_radius_px: float = 6.0
    seed: int = 1
    dtype: type = np.uint16


def _ground_mask(s: SynthSky) -> np.ndarray:
    """True where the pixel looks above the (refracted) horizon."""
    uu, vv = np.meshgrid(np.arange(s.width, dtype=np.float64), np.arange(s.height, dtype=np.float64))
    pts = np.stack([uu.ravel(), vv.ravel()], axis=1).reshape(-1, 1, 2)
    K = np.array([[s.fx, 0, s.cx], [0, s.fy, s.cy], [0, 0, 1.0]])
    ideal = cv2.undistortPoints(pts, K, np.array([s.k1, s.k2, 0, 0, 0], float)).reshape(-1, 2)
    right, down, fwd = (np.array(a) for a in camera_basis_enu(s.yaw_deg, s.pitch_deg, s.roll_deg))
    up = ideal[:, 0] * right[2] + ideal[:, 1] * down[2] + fwd[2]
    return (up > 0).reshape(s.height, s.width)


def _horizon_in_view(s: SynthSky) -> bool:
    """Conservative: the lowest point of the (generously enlarged) field is below 1 deg."""
    half_diag = math.degrees(math.atan(math.hypot(s.width, s.height) / (2.0 * min(s.fx, s.fy))))
    return s.pitch_deg - 1.3 * half_diag < 1.0


def render(s: SynthSky, t_ns: int, lat_deg: float, lon_deg: float, alt_m: float = 0.0,
           pressure_hpa: Optional[float] = None, temperature_c: float = 10.0, return_truth: bool = False):
    """Render a frame; with ``return_truth`` also return (uv, mag) of the catalog stars drawn."""
    rng = np.random.default_rng(s.seed)
    cat = load_catalog()
    p = astro.standard_pressure_hpa(alt_m) if pressure_hpa is None else pressure_hpa
    az, el = astro.stars_altaz(cat.ra_deg, cat.dec_deg, cat.pmra_masyr, cat.pmdec_masyr, t_ns,
                               lat_deg, lon_deg, p, temperature_c)
    mags = cat.vmag.copy()
    is_cat = np.ones(mags.size, bool)
    if s.extra_faint_stars:
        n = s.extra_faint_stars
        # uniform on the upper hemisphere
        zz = rng.uniform(0.0, 1.0, n)
        az = np.concatenate([az, rng.uniform(0, 360, n)])
        el = np.concatenate([el, np.degrees(np.arcsin(zz))])
        mags = np.concatenate([mags, rng.uniform(6.5, 8.5, n)])
        is_cat = np.concatenate([is_cat, np.zeros(n, bool)])
    if s.extra_bright_sources:
        n = s.extra_bright_sources
        az = np.concatenate([az, rng.uniform(0, 360, n)])
        el = np.concatenate([el, rng.uniform(20, 90, n)])
        mags = np.concatenate([mags, rng.uniform(-2.0, 1.0, n)])
        is_cat = np.concatenate([is_cat, np.zeros(n, bool)])
    vis = el > 0.5
    az, el, mags, is_cat = az[vis], el[vis], mags[vis], is_cat[vis]
    airmass = 1.0 / (np.sin(np.radians(el + 244.0 / (165.0 + 47.0 * el ** 1.1))))
    mags = mags + s.extinction * (airmass - 1.0)

    enu = astro.azel_to_enu(az, el)
    right, down, fwd = (np.array(a) for a in camera_basis_enu(s.yaw_deg, s.pitch_deg, s.roll_deg))
    R = np.stack([right, down, fwd])
    cam = enu @ R.T
    front = cam[:, 2] > 0.05
    cam, mags, is_cat = cam[front], mags[front], is_cat[front]
    K = np.array([[s.fx, 0, s.cx], [0, s.fy, s.cy], [0, 0, 1.0]])
    dist = np.array([s.k1, s.k2, 0.0, 0.0, 0.0])
    # keep within the monotonic part of the distortion
    r2 = (cam[:, 0] ** 2 + cam[:, 1] ** 2) / cam[:, 2] ** 2
    keep = r2 < 0.95 * min(4.0, Intrinsics(s.fx, s.fy, s.cx, s.cy, s.k1, s.k2).max_r2())
    cam, mags, is_cat = cam[keep], mags[keep], is_cat[keep]
    uv, _ = cv2.projectPoints(cam.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K, dist)
    uv = uv.reshape(-1, 2)
    inside = (uv[:, 0] > -10) & (uv[:, 0] < s.width + 10) & (uv[:, 1] > -10) & (uv[:, 1] < s.height + 10)
    uv, mags, is_cat = uv[inside], mags[inside], is_cat[inside]

    h, w = s.height, s.width
    sky = np.zeros((h, w), np.float64)
    peaks = s.mag_zero_peak * 10.0 ** (-0.4 * mags)
    rad = 6
    gy, gx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    for (u, v), pk in zip(uv, peaks):
        iu, iv = int(round(u)), int(round(v))
        x0, x1 = max(iu - rad, 0), min(iu + rad + 1, w)
        y0, y1 = max(iv - rad, 0), min(iv + rad + 1, h)
        if x0 >= x1 or y0 >= y1:
            continue
        sx = gx[y0 - iv + rad:y1 - iv + rad, x0 - iu + rad:x1 - iu + rad] + iu - u
        sy = gy[y0 - iv + rad:y1 - iv + rad, x0 - iu + rad:x1 - iu + rad] + iv - v
        sky[y0:y1, x0:x1] += pk * np.exp(-(sx * sx + sy * sy) / (2 * s.psf_sigma ** 2))

    if s.clouds > 0:
        small = rng.normal(size=(h // 60 + 2, w // 60 + 2))
        field_ = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
        field_ = cv2.GaussianBlur(field_, (0, 0), 40)
        thr = np.quantile(field_, 1.0 - s.clouds)
        trans = np.clip(1.0 - (field_ - thr) / (0.15 * field_.std() + 1e-9), 0.0, 1.0)
        trans = np.where(field_ > thr, trans * 0.02, 1.0) if s.clouds >= 0.999 else trans
        sky *= trans
        sky += s.cloud_glow * (1.0 - trans)

    yy = np.arange(h, dtype=np.float64)[:, None] / h
    xx = np.arange(w, dtype=np.float64)[None, :] / w
    sky += s.background + s.gradient * yy + 0.3 * s.gradient * xx * yy

    if s.moon is not None:
        mu, mv = s.moon
        d = np.hypot(np.arange(w)[None, :] - mu, np.arange(h)[:, None] - mv)
        sky += 30000.0 * np.exp(-np.maximum(d - s.moon_radius_px, 0) / 25.0) + 4000.0 / (1 + (d / 150.0) ** 2)
        sky[d <= s.moon_radius_px] = s.full_scale * 1.5

    ground = ~_ground_mask(s) if _horizon_in_view(s) else np.zeros((h, w), bool)
    sky[ground] = s.background * 0.5 + 200.0 * (np.arange(w)[None, :] / w * np.ones((h, 1)))[ground]

    noisy = rng.poisson(np.clip(sky, 0, 1e7) / s.gain) * s.gain + rng.normal(0.0, s.read_noise, sky.shape)
    if s.hot_pixels:
        hy = rng.integers(0, h, s.hot_pixels)
        hx = rng.integers(0, w, s.hot_pixels)
        noisy[hy, hx] += rng.uniform(2000, 60000, s.hot_pixels)
    noisy = np.clip(noisy, 0, s.full_scale)
    if np.issubdtype(np.dtype(s.dtype), np.integer):
        out = np.rint(noisy).astype(s.dtype)
    else:
        out = noisy.astype(s.dtype)
    if return_truth:
        return out, uv[is_cat], mags[is_cat]
    return out
