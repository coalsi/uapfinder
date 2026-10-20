"""Star calibration: astrometry, solar/lunar position, catalog and camera model.

Reference values were computed with astropy 8.0.2 (ERFA) offline - IERS
tables not downloaded, so UT1-UTC and polar motion may differ by up to ~1 s
/ 0.3" from reality; that limits the comparison to about 3". Refraction:
astropy's AltAz(pressure=1000 hPa, temperature=10 C, relative_humidity=0,
obswl=0.55 um).
"""

from __future__ import annotations

import datetime as dt
import math
import os

import cv2
import numpy as np
import pytest

from skynet_common.camera import PinholeCamera, azel_to_pixel, camera_basis_enu, pixel_to_azel
from skynet_edge.starcal import Observer, is_dark_enough, sun_altitude_deg
from skynet_edge.starcal import astro
from skynet_edge.starcal.catalog import CATALOG_PATH, load_catalog
from skynet_edge.starcal.model import Intrinsics, attitude_matrix, matrix_to_attitude, project_cam, unproject

STARS = {  # J2000 ICRS position (deg) and proper motion (mas/yr) as in the bundled catalog
    "Arcturus": (213.915450, 19.182410, -1093.45, -1999.40),
    "Vega": (279.234600, 38.783692, 201.02, 287.46),
    "Polaris": (37.954560, 89.264109, 44.22, -11.74),
    "Sirius": (101.287215, -16.716116, -546.01, -1223.08),
    "Betelgeuse": (88.792935, 7.407063, 27.33, 10.86),
    "Deneb": (310.357970, 45.280339, 1.56, 1.55),
    "Acrux": (186.649560, -63.099093, -35.37, -14.73),
}

ALTAZ_REFS = [  # (iso UTC, lat, lon, h, star, alt without refraction, az, alt with refraction)
    ('2026-10-20T22:00:00', 47.0, 8.0, 500.0, 'Vega', 28.537106, 297.579313, 28.566329),
    ('2026-10-20T22:00:00', 47.0, 8.0, 500.0, 'Polaris', 47.478371, 0.591820, 47.492996),
    ('2026-10-20T22:00:00', 47.0, 8.0, 500.0, 'Betelgeuse', 11.010767, 90.911888, 11.089867),
    ('2026-10-20T22:00:00', 47.0, 8.0, 500.0, 'Deneb', 51.532344, 289.150336, 51.545019),
    ('2024-01-15T03:30:00', 34.05, -118.25, 100.0, 'Polaris', 34.678968, 359.965810, 34.701978),
    ('2024-01-15T03:30:00', 34.05, -118.25, 100.0, 'Sirius', 18.286143, 126.131190, 18.333847),
    ('2024-01-15T03:30:00', 34.05, -118.25, 100.0, 'Betelgeuse', 43.978945, 115.834197, 43.995466),
    ('2024-01-15T03:30:00', 34.05, -118.25, 100.0, 'Deneb', 18.634205, 312.726456, 18.680977),
    ('2026-06-21T23:00:00', -33.9, 18.4, 0.0, 'Arcturus', 12.603889, 303.580097, 12.673335),
    ('2026-06-21T23:00:00', -33.9, 18.4, 0.0, 'Vega', 17.086338, 4.818237, 17.137521),
    ('2026-06-21T23:00:00', -33.9, 18.4, 0.0, 'Acrux', 31.376993, 211.748873, 31.403076),
    ('2026-12-01T02:00:00', 0.0, 100.0, 0.0, 'Arcturus', 66.365319, 35.533170, 66.372304),
    ('2026-12-01T02:00:00', 0.0, 100.0, 0.0, 'Vega', 8.158811, 50.716497, 8.262928),
    ('2026-12-01T02:00:00', 0.0, 100.0, 0.0, 'Acrux', 26.025844, 186.435854, 26.058352),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Arcturus', 21.157823, 88.308058, 21.198694),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Vega', 12.754093, 26.761758, 12.822740),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Polaris', 60.016079, 358.749585, 60.025287),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Sirius', 8.951498, 211.080474, 9.047355),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Betelgeuse', 27.689492, 231.857791, 27.719763),
    ('2025-03-10T20:00:00', 60.0, 25.0, 50.0, 'Deneb', 15.397663, 2.316878, 15.454583),
]

TETE_REFS = [  # geocentric apparent RA/Dec, true equator and equinox of date
    ('2026-10-20T22:00:00', 'Arcturus', 214.218189, 19.043929),
    ('2026-10-20T22:00:00', 'Vega', 279.460175, 38.812711),
    ('2026-10-20T22:00:00', 'Polaris', 47.191239, 89.375273),
    ('2035-01-01T00:00:00', 'Arcturus', 214.310813, 19.001342),
    ('2035-01-01T00:00:00', 'Vega', 279.523418, 38.820493),
    ('2035-01-01T00:00:00', 'Polaris', 50.617776, 89.406847),
]

SUN_REFS = [  # (iso UTC, lat, lon, geometric altitude, azimuth)
    ('2026-10-20T22:00:00', 47.0, 8.0, -50.4774, 331.1867),
    ('2026-10-20T11:00:00', 47.0, 8.0, 32.4969, 176.2659),
    ('2026-06-21T23:00:00', 59.91, 10.75, -6.5654, 355.6330),   # Oslo, midsummer midnight
    ('2026-12-21T11:00:00', 69.65, 18.96, -3.1444, 184.0870),   # Tromso, polar night noon
    ('2026-03-20T18:00:00', 0.0, 0.0, 1.8380, 270.0533),
    ('2026-09-01T03:00:00', -33.9, 151.2, 45.0944, 337.0253),
    ('2026-10-20T17:15:00', 47.0, 8.0, -8.0570, 263.1827),      # civil/nautical twilight
]

MOON_REFS = [
    ('2026-10-20T22:00:00', 47.0, 8.0, 16.8181, 223.2856),
    ('2024-01-15T03:30:00', 34.05, -118.25, 17.0521, 245.7365),
    ('2026-12-01T02:00:00', 0.0, 100.0, 46.7975, 281.8793),
]


def t_ns(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso + "+00:00").timestamp()) * 1_000_000_000


def sep_arcsec(az1, el1, az2, el2) -> float:
    a = astro.azel_to_enu(az1, el1)
    b = astro.azel_to_enu(az2, el2)
    return math.degrees(math.acos(min(1.0, float(a @ b)))) * 3600.0


@pytest.mark.parametrize("ref", ALTAZ_REFS, ids=lambda r: f"{r[0]}-{r[4]}")
def test_star_altaz_matches_astropy(ref):
    iso, lat, lon, h, name, alt0, az0, alt_refr = ref
    ra, dec, pmra, pmdec = STARS[name]
    az, el = astro.stars_altaz(np.array([ra]), np.array([dec]), np.array([pmra]), np.array([pmdec]),
                               t_ns(iso), lat, lon, pressure_hpa=0.0, temperature_c=10.0)
    assert sep_arcsec(az[0], el[0], az0, alt0) < 3.0           # 0.0008 deg
    _, el_r = astro.stars_altaz(np.array([ra]), np.array([dec]), np.array([pmra]), np.array([pmdec]),
                                t_ns(iso), lat, lon, pressure_hpa=1000.0, temperature_c=10.0)
    # The refraction itself: above 20 deg the same two-term model as ERFA; near
    # the horizon Saemundsson/Bennett, which differs from ERFA's (there
    # inaccurate) two-term model by up to ~0.003 deg at 8 deg elevation.
    tol = 0.0035 if alt0 < 15 else (0.001 if alt0 < 20 else 0.0003)
    assert el_r[0] - el[0] == pytest.approx(alt_refr - alt0, abs=tol)
    assert el_r[0] - el[0] > 0.0


@pytest.mark.parametrize("ref", TETE_REFS, ids=lambda r: f"{r[0]}-{r[1]}")
def test_apparent_place_precession_nutation_aberration(ref):
    iso, name, ra_ref, dec_ref = ref
    ra, dec, pmra, pmdec = STARS[name]
    ra_d, dec_d = astro.apparent_radec_of_date(ra, dec, pmra, pmdec, t_ns(iso))
    d_ra = ((ra_d - ra_ref + 180) % 360 - 180) * math.cos(math.radians(dec_ref)) * 3600
    assert abs(d_ra) < 0.1 and abs(dec_d - dec_ref) * 3600 < 0.1


def test_precession_since_j2000_is_not_ignored():
    # The equinox moves ~50.3"/yr: 26.8 yr after J2000 that is 0.37 deg in longitude.
    ra, dec, pmra, pmdec = STARS["Arcturus"]
    ra_d, _ = astro.apparent_radec_of_date(ra, dec, 0.0, 0.0, t_ns('2026-10-20T22:00:00'))
    assert 0.25 < ra_d - ra < 0.40
    gamb, phib, psib, epsa = astro.precession_angles(0.268)
    assert math.degrees(psib) == pytest.approx(5038.48 * 0.268 / 3600, rel=1e-3)


def test_refraction_values():
    # Standard refraction (Bennett/Saemundsson, 1010 hPa, 10 C): ~29' for a true
    # altitude of 0 (the familiar 34' is for an *apparent* altitude of 0), ~1' at 45 deg.
    assert astro.refraction_deg(0.0) * 60 == pytest.approx(29.0, abs=0.5)
    assert astro.refraction_deg(45.0) * 60 == pytest.approx(1.0, abs=0.05)
    assert astro.refraction_deg(90.0) == pytest.approx(0.0, abs=1e-5)
    # 10% thinner air, 10% less refraction
    assert astro.refraction_deg(20.0, 909.0) == pytest.approx(0.9 * astro.refraction_deg(20.0, 1010.0), rel=2e-3)


@pytest.mark.parametrize("ref", SUN_REFS, ids=lambda r: r[0])
def test_sun_position(ref):
    iso, lat, lon, alt, az = ref
    s_az, s_alt = astro.sun_altaz(t_ns(iso), lat, lon)
    assert sep_arcsec(s_az, s_alt, az, alt) < 0.02 * 3600


@pytest.mark.parametrize("ref", MOON_REFS, ids=lambda r: r[0])
def test_moon_position_good_enough_for_glare_masking(ref):
    iso, lat, lon, alt, az = ref
    m_az, m_alt = astro.moon_altaz(t_ns(iso), lat, lon)
    assert sep_arcsec(m_az, m_alt, az, alt) < 0.5 * 3600


def test_is_dark_enough_known_places_and_dates():
    zurich = Observer(47.0, 8.0, 500.0)
    assert is_dark_enough(t_ns('2026-10-20T22:00:00'), zurich)          # sun -50 deg
    assert not is_dark_enough(t_ns('2026-10-20T11:00:00'), zurich)      # noon
    assert not is_dark_enough(t_ns('2026-10-20T17:15:00'), zurich)      # sun -8 deg: twilight
    # Oslo never gets astronomically dark around midsummer (sun -6.6 deg at midnight)
    assert not is_dark_enough(t_ns('2026-06-21T23:00:00'), Observer(59.91, 10.75, 20.0))
    # Tromso in the polar night: the sun stays below the horizon but above -12 deg at noon
    assert not is_dark_enough(t_ns('2026-12-21T11:00:00'), Observer(69.65, 18.96, 10.0))
    assert is_dark_enough(t_ns('2026-12-21T22:00:00'), Observer(69.65, 18.96, 10.0))
    assert sun_altitude_deg(t_ns('2026-12-21T11:00:00'), Observer(69.65, 18.96)) == pytest.approx(-3.1444, abs=0.02)


def test_catalog_bundle():
    cat = load_catalog()
    assert 8000 < len(cat) < 10000
    assert os.path.getsize(CATALOG_PATH) < 1_000_000
    assert os.path.exists(os.path.join(os.path.dirname(CATALOG_PATH), "NOTICE.md"))
    assert cat.vmag.max() <= 6.5
    i = int(np.argmin(cat.vmag))
    assert cat.hip[i] == 32349  # Sirius
    assert cat.ra_deg[i] == pytest.approx(101.2872, abs=1e-3)
    assert np.all((cat.ra_deg >= 0) & (cat.ra_deg < 360)) and np.all(np.abs(cat.dec_deg) <= 90)


def test_attitude_matrix_matches_shared_camera_convention():
    for ypr in [(0.0, 45.0, 0.0), (237.5, 12.0, -7.0), (359.0, 89.0, 30.0), (90.0, -5.0, 170.0)]:
        m = attitude_matrix(*ypr)
        ref = np.array(camera_basis_enu(*ypr))
        assert np.allclose(m, ref, atol=1e-12)
        back = matrix_to_attitude(m)
        assert np.allclose(attitude_matrix(*back), m, atol=1e-9)
    # exactly at the zenith yaw is degenerate: the hint is kept, roll absorbs the rest
    m = attitude_matrix(30.0, 90.0, 10.0)
    y, p, r = matrix_to_attitude(m, yaw_hint_deg=30.0)
    assert (y, p) == pytest.approx((30.0, 90.0)) and r == pytest.approx(10.0)


def test_projection_matches_opencv_and_pixel_to_azel():
    intr = Intrinsics(1000.0, 1002.0, 958.0, 541.0, -0.2, 0.05)
    K = np.array([[intr.fx, 0, intr.cx], [0, intr.fy, intr.cy], [0, 0, 1.0]])
    rng = np.random.default_rng(3)
    xyz = np.column_stack([rng.uniform(-0.9, 0.9, 200), rng.uniform(-0.5, 0.5, 200), np.ones(200)])
    uv, ok = project_cam(xyz, intr)
    ref, _ = cv2.projectPoints(xyz.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K, np.array(intr.dist))
    assert ok.all() and np.abs(uv - ref.reshape(-1, 2)).max() < 1e-6
    rays = unproject(uv, intr)
    assert np.abs(rays - xyz / np.linalg.norm(xyz, axis=1, keepdims=True)).max() < 1e-9
    # without distortion, pixel -> ENU agrees with skynet_common.camera
    cam = PinholeCamera(1920, 1080, 1000.0, 1002.0, 958.0, 541.0)
    ip = intr.with_(k1=0.0, k2=0.0)
    m = attitude_matrix(123.0, 33.0, 4.0)
    for u, v in [(10.0, 20.0), (958.0, 541.0), (1900.0, 1000.0)]:
        az, el = pixel_to_azel(cam, u, v, 123.0, 33.0, 4.0)
        a2, e2 = astro.enu_to_azel(unproject(np.array([[u, v]]), ip) @ m)
        assert sep_arcsec(az, el, a2[0], e2[0]) < 1e-6
        pu, pv = azel_to_pixel(cam, az, el, 123.0, 33.0, 4.0)
        assert (pu, pv) == pytest.approx((u, v), abs=1e-7)
