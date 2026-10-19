import random

import pytest

from skynet_common import geodesy as g
from skynet_common.camera import (
    PinholeCamera,
    azel_to_pixel,
    camera_basis_enu,
    frustum_corner_azel,
    pixel_to_azel,
)


def test_basis_is_right_handed_orthonormal():
    rng = random.Random(0)
    for _ in range(200):
        r, d, f = camera_basis_enu(rng.uniform(0, 360), rng.uniform(-89, 89), rng.uniform(-180, 180))
        for v in (r, d, f):
            assert g.norm(v) == pytest.approx(1.0)
        assert g.dot(r, d) == pytest.approx(0.0, abs=1e-12)
        assert g.dot(r, f) == pytest.approx(0.0, abs=1e-12)
        # OpenCV camera frame: x (right) cross y (down) = z (forward).
        assert g.cross(r, d) == pytest.approx(f, abs=1e-12)


def test_blueprint_orientation_points_north_45():
    cam = PinholeCamera.from_fov(1920, 1080, 90.0)
    az, el = pixel_to_azel(cam, cam.cx, cam.cy, yaw_deg=0.0, pitch_deg=45.0, roll_deg=0.0)
    assert az == pytest.approx(0.0, abs=1e-9)
    assert el == pytest.approx(45.0)


def test_image_axes_directions():
    cam = PinholeCamera.from_fov(1920, 1080, 60.0)
    # Facing north at the horizon: right of centre is towards the east, up in
    # the image is higher elevation.
    az_r, el_r = pixel_to_azel(cam, cam.cx + 100, cam.cy, 0.0, 0.0, 0.0)
    assert 0.0 < az_r < 90.0 and el_r == pytest.approx(0.0, abs=1e-9)
    _, el_up = pixel_to_azel(cam, cam.cx, cam.cy - 100, 0.0, 0.0, 0.0)
    assert el_up > 0.0
    # Positive roll puts the right side of the image down.
    _, el_roll = pixel_to_azel(cam, cam.cx + 100, cam.cy, 0.0, 0.0, 10.0)
    assert el_roll < 0.0


def test_pixel_round_trip():
    rng = random.Random(5)
    cam = PinholeCamera.from_fov(1920, 1080, 70.0)
    for _ in range(500):
        yaw, pitch, roll = rng.uniform(0, 360), rng.uniform(-30, 89), rng.uniform(-20, 20)
        u, v = rng.uniform(0, 1920), rng.uniform(0, 1080)
        az, el = pixel_to_azel(cam, u, v, yaw, pitch, roll)
        uv = azel_to_pixel(cam, az, el, yaw, pitch, roll)
        assert uv == pytest.approx((u, v), abs=1e-6)


def test_fov_properties():
    cam = PinholeCamera.from_fov(1920, 1080, 90.0)
    assert cam.fov_h_deg == pytest.approx(90.0)
    assert cam.fov_v_deg == pytest.approx(58.7, abs=0.1)  # square pixels
    corners = frustum_corner_azel(cam, 0.0, 45.0, 0.0)
    assert len(corners) == 4
    # Top corners are higher than bottom corners.
    assert corners[0][1] > corners[3][1]


def test_behind_camera_returns_none():
    cam = PinholeCamera.from_fov(640, 480, 60.0)
    assert azel_to_pixel(cam, 180.0, 0.0, 0.0, 0.0, 0.0) is None
