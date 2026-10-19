import math
import random

import pytest

from skynet_common import geodesy as g


def test_known_ecef_values():
    # Equator / prime meridian on the ellipsoid surface.
    x, y, z = g.geodetic_to_ecef(0.0, 0.0, 0.0)
    assert x == pytest.approx(g.WGS84_A)
    assert y == pytest.approx(0.0, abs=1e-6)
    assert z == pytest.approx(0.0, abs=1e-6)
    # North pole.
    x, y, z = g.geodetic_to_ecef(90.0, 0.0, 0.0)
    assert z == pytest.approx(g.WGS84_B, abs=1e-6)
    assert math.hypot(x, y) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("seed", range(5))
def test_round_trip(seed):
    rng = random.Random(seed)
    for _ in range(2000):
        lat = rng.uniform(-89.999, 89.999)
        lon = rng.uniform(-180.0, 180.0)
        h = rng.uniform(-1000.0, 400_000.0)
        lat2, lon2, h2 = g.ecef_to_geodetic(*g.geodetic_to_ecef(lat, lon, h))
        assert lat2 == pytest.approx(lat, abs=1e-9)
        assert ((lon2 - lon + 180.0) % 360.0) - 180.0 == pytest.approx(0.0, abs=1e-9)
        assert h2 == pytest.approx(h, abs=1e-4)


def test_poles_round_trip():
    for lat in (90.0, -90.0):
        lat2, _, h2 = g.ecef_to_geodetic(*g.geodetic_to_ecef(lat, 10.0, 1234.5))
        assert lat2 == pytest.approx(lat, abs=1e-9)
        assert h2 == pytest.approx(1234.5, abs=1e-6)


def test_matches_pyproj():
    pyproj = pytest.importorskip("pyproj")
    to_ecef = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    rng = random.Random(42)
    for _ in range(500):
        lat = rng.uniform(-89.0, 89.0)
        lon = rng.uniform(-180.0, 180.0)
        h = rng.uniform(-500.0, 100_000.0)
        ex, ey, ez = to_ecef.transform(lon, lat, h)
        x, y, z = g.geodetic_to_ecef(lat, lon, h)
        assert (x, y, z) == pytest.approx((ex, ey, ez), abs=1e-6)
        lat2, lon2, h2 = g.ecef_to_geodetic(ex, ey, ez)
        rlon, rlat, rh = to_ecef.transform(ex, ey, ez, direction="INVERSE")
        assert lat2 == pytest.approx(rlat, abs=1e-9)
        assert lon2 == pytest.approx(rlon, abs=1e-9)
        assert h2 == pytest.approx(rh, abs=1e-4)


def test_enu_basis_orthonormal():
    e, n, u = g.enu_basis(33.6, -112.4)
    for a in (e, n, u):
        assert g.norm(a) == pytest.approx(1.0)
    assert g.dot(e, n) == pytest.approx(0.0, abs=1e-12)
    assert g.dot(e, u) == pytest.approx(0.0, abs=1e-12)
    # Right-handed: east x north = up.
    assert g.cross(e, n) == pytest.approx(u)


def test_azel_conventions():
    assert g.azel_to_enu(0.0, 0.0) == pytest.approx((0.0, 1.0, 0.0), abs=1e-12)  # north
    assert g.azel_to_enu(90.0, 0.0) == pytest.approx((1.0, 0.0, 0.0), abs=1e-12)  # east
    assert g.azel_to_enu(0.0, 90.0) == pytest.approx((0.0, 0.0, 1.0), abs=1e-12)  # zenith
    az, el = g.enu_to_azel(-1.0, -1.0, 0.0)
    assert az == pytest.approx(225.0)
    assert el == pytest.approx(0.0)


def test_look_angles_overhead():
    az, el, rng = g.look_angles(33.0, -112.0, 300.0, 33.0, -112.0, 10_300.0)
    assert el == pytest.approx(90.0, abs=1e-6)
    assert rng == pytest.approx(10_000.0, abs=1e-6)


def test_look_angles_north_target_below_horizon_far_away():
    # A target on the ellipsoid surface 1 degree north is below the horizon (curvature).
    az, el, _ = g.look_angles(33.0, -112.0, 0.0, 34.0, -112.0, 0.0)
    assert ((az + 180.0) % 360.0) - 180.0 == pytest.approx(0.0, abs=1e-6)
    assert el < 0.0


def test_haversine():
    # One degree of latitude is ~111 km.
    assert g.haversine_m(33.0, -112.0, 34.0, -112.0) == pytest.approx(111_195, rel=1e-3)


def test_angle_between_small_angles():
    a = (1.0, 0.0, 0.0)
    b = (math.cos(1e-7), math.sin(1e-7), 0.0)
    assert g.angle_between_deg(a, b) == pytest.approx(math.degrees(1e-7), rel=1e-6)
