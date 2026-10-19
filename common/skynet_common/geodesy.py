"""WGS84 geodesy helpers shared by the edge nodes and the server.

Conventions used everywhere in SKY-NET (see docs/MATH.md):

* Geodetic coordinates are WGS84 latitude/longitude in degrees and height in
  metres above the WGS84 *ellipsoid* (HAE), not above mean sea level.
* ECEF is the WGS84 Earth-Centred Earth-Fixed frame, metres.
* ENU is the local East-North-Up tangent frame at an observer, with "Up" along
  the ellipsoid normal (geodetic vertical).
* Azimuth is degrees clockwise from true north in [0, 360).
* Elevation is degrees above the local geodetic horizon in [-90, 90].

The hot paths (server matchmaking) call these functions thousands of times a
second on single 3-vectors, so they use the ``math`` module and plain tuples.
For tiny vectors that is several times faster than NumPy.
"""

from __future__ import annotations

import math
from typing import Tuple

Vec3 = Tuple[float, float, float]

# WGS84 defining constants.
WGS84_A = 6378137.0  # semi-major axis, m
WGS84_F = 1.0 / 298.257223563  # flattening
WGS84_B = WGS84_A * (1.0 - WGS84_F)  # semi-minor axis, m
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)  # first eccentricity squared
WGS84_EP2 = WGS84_E2 / (1.0 - WGS84_E2)  # second eccentricity squared

# Mean Earth radius, used only for great-circle distance approximations.
EARTH_MEAN_RADIUS_M = 6371008.8

METERS_PER_MILE = 1609.344


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> Vec3:
    """Convert WGS84 geodetic coordinates to ECEF metres."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h_m) * cos_lat * math.cos(lon)
    y = (n + h_m) * cos_lat * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h_m) * sin_lat
    return (x, y, z)


def ecef_to_geodetic(x: float, y: float, z: float) -> Vec3:
    """Convert ECEF metres to WGS84 (lat_deg, lon_deg, h_m).

    Uses Bowring's parametric-latitude initial guess followed by Newton-style
    refinement of the geodetic latitude. Two refinement passes give sub-
    millimetre accuracy for every point from the Earth's centre region out to
    beyond geostationary orbit, which is far more than the triangulation
    error budget needs.
    """
    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    if p < 1e-9:
        # On the polar axis: latitude is +/-90 and height is |z| - b.
        lat = math.copysign(math.pi / 2.0, z) if z != 0.0 else math.pi / 2.0
        return (math.degrees(lat), math.degrees(lon), abs(z) - WGS84_B)

    # Bowring's initial estimate via the parametric (reduced) latitude.
    beta = math.atan2(z * WGS84_A, p * WGS84_B)
    lat = math.atan2(
        z + WGS84_EP2 * WGS84_B * math.sin(beta) ** 3,
        p - WGS84_E2 * WGS84_A * math.cos(beta) ** 3,
    )
    for _ in range(2):
        beta = math.atan2((1.0 - WGS84_F) * math.sin(lat), math.cos(lat))
        lat = math.atan2(
            z + WGS84_EP2 * WGS84_B * math.sin(beta) ** 3,
            p - WGS84_E2 * WGS84_A * math.cos(beta) ** 3,
        )

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    # This form of the height equation is well conditioned at all latitudes.
    h = p * cos_lat + z * sin_lat - WGS84_A * WGS84_A / n
    return (math.degrees(lat), math.degrees(lon), h)


def enu_basis(lat_deg: float, lon_deg: float) -> Tuple[Vec3, Vec3, Vec3]:
    """Return the ECEF unit vectors (east, north, up) at a geodetic location."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    east = (-so, co, 0.0)
    north = (-sl * co, -sl * so, cl)
    up = (cl * co, cl * so, sl)
    return east, north, up


def azel_to_enu(az_deg: float, el_deg: float) -> Vec3:
    """Unit line-of-sight vector in the local ENU frame for an azimuth/elevation."""
    az = math.radians(az_deg)
    el = math.radians(el_deg)
    ce = math.cos(el)
    return (ce * math.sin(az), ce * math.cos(az), math.sin(el))


def enu_to_azel(e: float, n: float, u: float) -> Tuple[float, float]:
    """Azimuth/elevation in degrees of an ENU vector (need not be normalised)."""
    horiz = math.hypot(e, n)
    az = math.degrees(math.atan2(e, n)) % 360.0
    el = math.degrees(math.atan2(u, horiz))
    return az, el


def enu_to_ecef_vector(lat_deg: float, lon_deg: float, enu: Vec3) -> Vec3:
    """Rotate an ENU direction at a location into the ECEF frame."""
    east, north, up = enu_basis(lat_deg, lon_deg)
    e, n, u = enu
    return (
        e * east[0] + n * north[0] + u * up[0],
        e * east[1] + n * north[1] + u * up[1],
        e * east[2] + n * north[2] + u * up[2],
    )


def ecef_to_enu_vector(lat_deg: float, lon_deg: float, v: Vec3) -> Vec3:
    """Rotate an ECEF direction into the ENU frame at a location."""
    east, north, up = enu_basis(lat_deg, lon_deg)
    return (dot(v, east), dot(v, north), dot(v, up))


def azel_to_ecef_unit(lat_deg: float, lon_deg: float, az_deg: float, el_deg: float) -> Vec3:
    """ECEF unit line-of-sight vector for an observer's azimuth/elevation."""
    return enu_to_ecef_vector(lat_deg, lon_deg, azel_to_enu(az_deg, el_deg))


def look_angles(
    obs_lat: float, obs_lon: float, obs_h: float, tgt_lat: float, tgt_lon: float, tgt_h: float
) -> Tuple[float, float, float]:
    """Azimuth (deg), elevation (deg) and slant range (m) from observer to target."""
    o = geodetic_to_ecef(obs_lat, obs_lon, obs_h)
    t = geodetic_to_ecef(tgt_lat, tgt_lon, tgt_h)
    d = sub(t, o)
    e, n, u = ecef_to_enu_vector(obs_lat, obs_lon, d)
    az, el = enu_to_azel(e, n, u)
    return az, el, norm(d)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle surface distance in metres on a mean-radius sphere."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def angle_between_deg(a: Vec3, b: Vec3) -> float:
    """Angle in degrees between two (not necessarily unit) vectors.

    Uses atan2(|a x b|, a . b), which stays accurate for tiny and near-180
    degree angles where acos() loses precision.
    """
    return math.degrees(math.atan2(norm(cross(a, b)), dot(a, b)))


# --- tiny 3-vector algebra -------------------------------------------------


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def normalize(a: Vec3) -> Vec3:
    n = norm(a)
    if n == 0.0:
        raise ValueError("cannot normalise a zero vector")
    return (a[0] / n, a[1] / n, a[2] / n)
