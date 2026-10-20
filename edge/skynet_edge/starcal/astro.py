"""Positional astronomy for star calibration: catalog (ICRS/J2000) -> local az/el.

Everything is vectorised with NumPy and needs no network data (no IERS
tables), so it runs offline on the node. The chain for a star is the
classical equinox-based one:

1. proper motion from epoch J2000.0 to the date (linear, on the sphere);
2. annual aberration (Earth's orbital velocity from a low-precision solar
   theory, ~0.05" accuracy in the 20.5" effect);
3. frame bias + IAU 2006 precession (Fukushima-Williams angles, as ERFA
   ``pfw06``/``fw2m``) + nutation (the 18 largest terms of the IAU 1980
   series, ~0.1" truncation error) -> true equator and equinox of date;
4. Earth rotation by Greenwich apparent sidereal time (IAU 2006 GMST from the
   Earth rotation angle + equation of the equinoxes), with UT1 = UTC + dut1;
5. topocentric horizon frame (geodetic latitude, i.e. the ellipsoid normal,
   matching skynet_common's ENU), then atmospheric refraction (two-term
   A tan z + B tan^3 z model above 10 deg, Saemundsson/Bennett near the
   horizon, scaled for pressure and temperature).

Neglected: polar motion (<0.5"), diurnal aberration (<0.33"), gravitational
light deflection (<0.01" at >45 deg from the Sun), parallax of stars (<0.8"),
the IAU 2000 vs 1980 nutation difference (~0.05"), and UT1-UTC when dut1 is
not given (|UT1-UTC| < 0.9 s, i.e. < 0.004 deg of azimuth rotation).
Verified against astropy 8.0 / ERFA in edge/tests/test_starcal_astro.py.

The Sun (Meeus, "Astronomical Algorithms" ch. 25, ~0.01 deg) and the Moon
(main terms of ch. 47, ~0.3 deg, topocentric) are for darkness checks and
glare masking only.
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np

ARCSEC = math.pi / (180.0 * 3600.0)
DEG = math.pi / 180.0
J2000_JD = 2451545.0
UNIX_EPOCH_JD = 2440587.5
# TT - UTC = (TAI - UTC) + 32.184 s. TAI - UTC has been 37 s since
# 2017-01-01 and no leap second is scheduled; an error of a few seconds in TT
# is irrelevant here (it only enters precession/nutation/solar theory).
TT_MINUS_UTC_S = 37.0 + 32.184


# --- time ------------------------------------------------------------------

def jd_utc(t_ns: int) -> float:
    return UNIX_EPOCH_JD + (t_ns / 1e9) / 86400.0


def julian_centuries_tt(t_ns: int) -> float:
    return (jd_utc(t_ns) + TT_MINUS_UTC_S / 86400.0 - J2000_JD) / 36525.0


def earth_rotation_angle(t_ns: int, dut1_s: float = 0.0) -> float:
    """ERA (IAU 2000), radians in [0, 2pi)."""
    # Split to keep precision: days since J2000 UT1.
    du = (t_ns / 1e9 + dut1_s) / 86400.0 + (UNIX_EPOCH_JD - J2000_JD)
    frac = math.fmod(du, 1.0)
    era = 2.0 * math.pi * (frac + 0.7790572732640 + 0.00273781191135448 * du)
    return era % (2.0 * math.pi)


def mean_obliquity(t: float) -> float:
    """IAU 2006 mean obliquity of the ecliptic, radians (t = TT centuries)."""
    return (84381.406 + t * (-46.836769 + t * (-0.0001831 + t * (0.00200340 + t * (-0.000000576
            + t * -0.0000000434))))) * ARCSEC


def gmst(t_ns: int, dut1_s: float = 0.0) -> float:
    """Greenwich mean sidereal time (IAU 2006, ERFA gmst06), radians."""
    t = julian_centuries_tt(t_ns)
    poly = (0.014506 + t * (4612.156534 + t * (1.3915817 + t * (-0.00000044 + t * (-0.000029956
            + t * -0.0000000368))))) * ARCSEC
    return (earth_rotation_angle(t_ns, dut1_s) + poly) % (2.0 * math.pi)


# Largest terms of the IAU 1980 nutation series (Meeus table 22.A),
# multipliers of D, M, M', F, Omega; dpsi = (a + b t) sin(arg), deps = (c + d t) cos(arg),
# in units of 0.0001".
_NUT_TERMS = np.array([
    # D   M  M'  F  Om      a       b      c      d
    [0, 0, 0, 0, 1, -171996, -174.2, 92025, 8.9],
    [-2, 0, 0, 2, 2, -13187, -1.6, 5736, -3.1],
    [0, 0, 0, 2, 2, -2274, -0.2, 977, -0.5],
    [0, 0, 0, 0, 2, 2062, 0.2, -895, 0.5],
    [0, 1, 0, 0, 0, 1426, -3.4, 54, -0.1],
    [0, 0, 1, 0, 0, 712, 0.1, -7, 0.0],
    [-2, 1, 0, 2, 2, -517, 1.2, 224, -0.6],
    [0, 0, 0, 2, 1, -386, -0.4, 200, 0.0],
    [0, 0, 1, 2, 2, -301, 0.0, 129, -0.1],
    [-2, -1, 0, 2, 2, 217, -0.5, -95, 0.3],
    [-2, 0, 1, 0, 0, -158, 0.0, 0, 0.0],
    [-2, 0, 0, 2, 1, 129, 0.1, -70, 0.0],
    [0, 0, -1, 2, 2, 123, 0.0, -53, 0.0],
    [2, 0, 0, 0, 0, 63, 0.0, 0, 0.0],
    [0, 0, 1, 0, 1, 63, 0.1, -33, 0.0],
    [2, 0, -1, 2, 2, -59, 0.0, 26, 0.0],
    [0, 0, -1, 0, 1, -58, -0.1, 32, 0.0],
    [0, 0, 1, 2, 1, -51, 0.0, 27, 0.0],
], dtype=np.float64)


def _fundamental_args(t: float) -> np.ndarray:
    """D, M, M', F, Omega (radians), Meeus eq. 22.x."""
    d = 297.85036 + t * (445267.111480 + t * (-0.0019142 + t / 189474.0))
    m = 357.52772 + t * (35999.050340 + t * (-0.0001603 - t / 300000.0))
    mp = 134.96298 + t * (477198.867398 + t * (0.0086972 + t / 56250.0))
    f = 93.27191 + t * (483202.017538 + t * (-0.0036825 + t / 327270.0))
    om = 125.04452 + t * (-1934.136261 + t * (0.0020708 + t / 450000.0))
    return np.radians(np.array([d, m, mp, f, om]))


def nutation(t: float) -> Tuple[float, float]:
    """(dpsi, deps) in radians."""
    args = _NUT_TERMS[:, :5] @ _fundamental_args(t)
    dpsi = np.sum((_NUT_TERMS[:, 5] + _NUT_TERMS[:, 6] * t) * np.sin(args)) * 1e-4 * ARCSEC
    deps = np.sum((_NUT_TERMS[:, 7] + _NUT_TERMS[:, 8] * t) * np.cos(args)) * 1e-4 * ARCSEC
    return float(dpsi), float(deps)


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]])


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


def precession_angles(t: float) -> Tuple[float, float, float, float]:
    """IAU 2006 Fukushima-Williams angles gamma_b, phi_b, psi_b, eps_A (radians)."""
    gamb = (-0.052928 + t * (10.556378 + t * (0.4932044 + t * (-0.00031238 + t * (-0.000002788
            + t * 0.0000000260))))) * ARCSEC
    phib = (84381.412819 + t * (-46.811016 + t * (0.0511268 + t * (0.00053289 + t * (-0.000000440
            + t * -0.0000000176))))) * ARCSEC
    psib = (-0.041775 + t * (5038.481484 + t * (1.5584175 + t * (-0.00018522 + t * (-0.000026452
            + t * -0.0000000148))))) * ARCSEC
    return gamb, phib, psib, mean_obliquity(t)


def bias_precession_matrix(t: float) -> np.ndarray:
    """GCRS -> mean equator and equinox of date (no nutation)."""
    gamb, phib, psib, epsa = precession_angles(t)
    return _rx(-epsa) @ _rz(-psib) @ _rx(phib) @ _rz(gamb)


def npb_matrix(t: float) -> Tuple[np.ndarray, float]:
    """GCRS -> true equator and equinox of date, and the equation of the equinoxes (rad)."""
    gamb, phib, psib, epsa = precession_angles(t)
    dpsi, deps = nutation(t)
    m = _rx(-(epsa + deps)) @ _rz(-(psib + dpsi)) @ _rx(phib) @ _rz(gamb)
    om = _fundamental_args(t)[4]
    eqeq = dpsi * math.cos(epsa) + (0.00264096 * math.sin(om) + 0.00006352 * math.sin(2 * om)) * ARCSEC
    return m, eqeq


def gast(t_ns: int, dut1_s: float = 0.0) -> float:
    _, eqeq = npb_matrix(julian_centuries_tt(t_ns))
    return (gmst(t_ns, dut1_s) + eqeq) % (2.0 * math.pi)


# --- Sun -------------------------------------------------------------------

def _sun_geometric_longitude(t: float) -> Tuple[float, float, float]:
    """True geometric longitude of the Sun (mean equinox of date), its anomaly, radians; and R (au)."""
    l0 = 280.46646 + t * (36000.76983 + 0.0003032 * t)
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    mr = math.radians(m)
    c = ((1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(mr)
         + (0.019993 - 0.000101 * t) * math.sin(2 * mr) + 0.000289 * math.sin(3 * mr))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    nu = mr + math.radians(c)
    r = 1.000001018 * (1 - e * e) / (1 + e * math.cos(nu))
    return math.radians((l0 + c) % 360.0), mr, r


def earth_velocity_over_c(t: float) -> np.ndarray:
    """Earth's orbital velocity / c in the GCRS (J2000 equator), from the
    Sun's longitude with the eccentricity term (Meeus eq. 23.2 in vector form)."""
    kappa = 20.49552 * ARCSEC
    lon, _, _ = _sun_geometric_longitude(t)
    lon -= math.radians(1.396971 * t)  # equinox of date -> J2000 (general precession in longitude)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    pi_ = math.radians(102.93735 + t * (1.71946 + 0.00046 * t)) - math.radians(1.396971 * t)
    v_ecl = kappa * np.array([math.sin(lon) - e * math.sin(pi_), -(math.cos(lon) - e * math.cos(pi_)), 0.0])
    eps0 = 84381.406 * ARCSEC
    ce, se = math.cos(eps0), math.sin(eps0)
    return np.array([v_ecl[0], ce * v_ecl[1] - se * v_ecl[2], se * v_ecl[1] + ce * v_ecl[2]])


def sun_true_of_date(t_ns: int) -> np.ndarray:
    """Apparent direction of the Sun, unit vector in true equator/equinox of date."""
    t = julian_centuries_tt(t_ns)
    lon, _, _ = _sun_geometric_longitude(t)
    om = math.radians(125.04 - 1934.136 * t)
    lam = lon - math.radians(0.00569 + 0.00478 * math.sin(om))  # aberration + nutation in longitude
    eps = mean_obliquity(t) + math.radians(0.00256 * math.cos(om))
    return np.array([math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)])


# --- Moon (low precision, for glare masking) ------------------------------

def moon_true_of_date(t_ns: int) -> Tuple[np.ndarray, float]:
    """Geocentric Moon direction (true equator of date) and distance in km; ~0.3 deg."""
    t = julian_centuries_tt(t_ns)
    lp = 218.3164477 + 481267.88123421 * t
    d = math.radians(297.8501921 + 445267.1114034 * t)
    m = math.radians(357.5291092 + 35999.0502909 * t)
    mp = math.radians(134.9633964 + 477198.8675055 * t)
    f = math.radians(93.2720950 + 483202.0175233 * t)
    lam = math.radians(lp + 6.289 * math.sin(mp) + 1.274 * math.sin(2 * d - mp) + 0.658 * math.sin(2 * d)
                       + 0.214 * math.sin(2 * mp) - 0.186 * math.sin(m) - 0.114 * math.sin(2 * f))
    beta = math.radians(5.128 * math.sin(f) + 0.281 * math.sin(mp + f) + 0.278 * math.sin(mp - f)
                        + 0.173 * math.sin(2 * d - f))
    dist = 385000.56 - 20905.0 * math.cos(mp) - 3699.0 * math.cos(2 * d - mp) - 2956.0 * math.cos(2 * d)
    eps = mean_obliquity(t)
    x = math.cos(beta) * math.cos(lam)
    y = math.cos(beta) * math.sin(lam)
    z = math.sin(beta)
    v = np.array([x, math.cos(eps) * y - math.sin(eps) * z, math.sin(eps) * y + math.cos(eps) * z])
    return v, dist


# --- horizon frame ----------------------------------------------------------

def true_of_date_to_enu_matrix(t_ns: int, lat_deg: float, lon_deg: float, dut1_s: float = 0.0) -> np.ndarray:
    """Rotation: true equator/equinox of date -> local ENU."""
    g = gast(t_ns, dut1_s)
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lon), math.cos(lon)
    sp, cp = math.sin(lat), math.cos(lat)
    ecef_to_enu = np.array([
        [-sl, cl, 0.0],
        [-sp * cl, -sp * sl, cp],
        [cp * cl, cp * sl, sp],
    ])
    return ecef_to_enu @ _rz(g)


def gcrs_to_enu_matrix(t_ns: int, lat_deg: float, lon_deg: float, dut1_s: float = 0.0) -> np.ndarray:
    npb, _ = npb_matrix(julian_centuries_tt(t_ns))
    return true_of_date_to_enu_matrix(t_ns, lat_deg, lon_deg, dut1_s) @ npb


def radec_to_unit(ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    ra = np.radians(ra_deg)
    dec = np.radians(dec_deg)
    cd = np.cos(dec)
    return np.stack([cd * np.cos(ra), cd * np.sin(ra), np.sin(dec)], axis=-1)


def apply_proper_motion(ra_deg, dec_deg, pmra_masyr, pmdec_masyr, years: float):
    """Linear proper motion along the sphere; pmra is mu_alpha* (includes cos dec)."""
    p = radec_to_unit(ra_deg, dec_deg)
    ra = np.radians(ra_deg)
    dec = np.radians(dec_deg)
    e_ra = np.stack([-np.sin(ra), np.cos(ra), np.zeros_like(ra)], axis=-1)
    e_dec = np.stack([-np.sin(dec) * np.cos(ra), -np.sin(dec) * np.sin(ra), np.cos(dec)], axis=-1)
    k = years * 1e-3 * ARCSEC
    p = p + (np.asarray(pmra_masyr)[..., None] * k) * e_ra + (np.asarray(pmdec_masyr)[..., None] * k) * e_dec
    return p / np.linalg.norm(p, axis=-1, keepdims=True)


def aberrate(p: np.ndarray, v_over_c: np.ndarray) -> np.ndarray:
    """First-order (plus normalisation) stellar aberration; error ~1e-8 rad."""
    q = p + v_over_c - (p @ v_over_c)[..., None] * p
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def enu_to_azel(enu: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    e, n, u = enu[..., 0], enu[..., 1], enu[..., 2]
    az = np.degrees(np.arctan2(e, n)) % 360.0
    el = np.degrees(np.arctan2(u, np.hypot(e, n)))
    return az, el


def azel_to_enu(az_deg, el_deg) -> np.ndarray:
    az = np.radians(az_deg)
    el = np.radians(el_deg)
    ce = np.cos(el)
    return np.stack([ce * np.sin(az), ce * np.cos(az), np.sin(el)], axis=-1)


def standard_pressure_hpa(alt_m: float) -> float:
    """ISA-like pressure at height (ellipsoidal ~ MSL is fine here), hPa."""
    return 1013.25 * math.exp(-max(alt_m, -500.0) / 8434.5)


def refraction_deg(true_el_deg, pressure_hpa: float = 1010.0, temperature_c: float = 10.0):
    """Refraction to add to a true (airless) elevation to get the apparent one, degrees.

    Above 10 deg: the two-term model R = A tan z + B tan^3 z (z = observed
    zenith distance) with A = 58.07", B = -0.0650" at 1010 hPa / 10 C (dry
    air, 0.55 um: the values ERFA ``refco`` gives), scaled with air density.
    Below 5 deg: Saemundsson (1986), the inverse of Bennett's (1982) formula,
    good to ~0.1' down to the horizon, where the two-term model fails. Linear
    blend in between. Clamped at -1 deg. Real-weather uncertainty (unknown
    temperature/pressure: ~3% of R) dominates: ~0.004 deg at 10 deg elevation.
    """
    h = np.maximum(np.asarray(true_el_deg, dtype=np.float64), -1.0)
    scale = (pressure_hpa / 1010.0) * (283.15 / (273.15 + temperature_c))
    r_lo = 1.02 / np.tan(np.radians(h + 10.3 / (h + 5.11))) + 0.0019279   # arcmin; 0 at the zenith
    r_lo = np.maximum(r_lo, 0.0) / 60.0 * (pressure_hpa / 1010.0) * (283.0 / (273.0 + temperature_c))
    a, b = 58.07 / 3600.0 * scale, -0.0650 / 3600.0 * scale
    r_hi = np.zeros_like(h)
    hz = np.maximum(h, 5.0)
    for _ in range(3):  # R is a function of the observed zenith distance
        tz = np.tan(np.radians(90.0 - (hz + r_hi)))
        r_hi = a * tz + b * tz ** 3
    w = np.clip((h - 5.0) / 5.0, 0.0, 1.0)
    return w * r_hi + (1.0 - w) * r_lo


def apparent_radec_of_date(ra_deg, dec_deg, pmra_masyr, pmdec_masyr, t_ns: int,
                           epoch_jyear: float = 2000.0) -> Tuple[np.ndarray, np.ndarray]:
    """Geocentric apparent RA/Dec (true equator and equinox of date), degrees."""
    t = julian_centuries_tt(t_ns)
    years = 2000.0 + t * 100.0 - epoch_jyear
    p = apply_proper_motion(np.asarray(ra_deg, float), np.asarray(dec_deg, float), pmra_masyr, pmdec_masyr, years)
    p = aberrate(p, earth_velocity_over_c(t)) @ npb_matrix(t)[0].T
    ra = np.degrees(np.arctan2(p[..., 1], p[..., 0])) % 360.0
    dec = np.degrees(np.arcsin(np.clip(p[..., 2], -1.0, 1.0)))
    return ra, dec


def stars_altaz(ra_deg, dec_deg, pmra_masyr, pmdec_masyr, t_ns: int, lat_deg: float, lon_deg: float,
                pressure_hpa: float, temperature_c: float, dut1_s: float = 0.0,
                epoch_jyear: float = 2000.0) -> Tuple[np.ndarray, np.ndarray]:
    """Apparent (refracted) azimuth/elevation in degrees of catalog stars."""
    t = julian_centuries_tt(t_ns)
    years = 2000.0 + t * 100.0 - epoch_jyear
    p = apply_proper_motion(np.asarray(ra_deg, float), np.asarray(dec_deg, float), pmra_masyr, pmdec_masyr, years)
    p = aberrate(p, earth_velocity_over_c(t))
    enu = p @ gcrs_to_enu_matrix(t_ns, lat_deg, lon_deg, dut1_s).T
    az, el = enu_to_azel(enu)
    if pressure_hpa > 0:
        el = el + refraction_deg(el, pressure_hpa, temperature_c)
    return az, el


def sun_altaz(t_ns: int, lat_deg: float, lon_deg: float) -> Tuple[float, float]:
    """Unrefracted (geometric) apparent azimuth/elevation of the Sun, degrees."""
    enu = true_of_date_to_enu_matrix(t_ns, lat_deg, lon_deg) @ sun_true_of_date(t_ns)
    az, el = enu_to_azel(enu)
    return float(az), float(el)


def moon_altaz(t_ns: int, lat_deg: float, lon_deg: float, alt_m: float = 0.0) -> Tuple[float, float]:
    """Topocentric unrefracted azimuth/elevation of the Moon, degrees (~0.3 deg)."""
    v, dist = moon_true_of_date(t_ns)
    enu = true_of_date_to_enu_matrix(t_ns, lat_deg, lon_deg) @ v * dist
    enu = enu - np.array([0.0, 0.0, 6371.0 + alt_m / 1000.0])
    az, el = enu_to_azel(enu)
    return float(az), float(el)
