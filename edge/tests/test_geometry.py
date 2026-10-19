import math

import numpy as np
import pytest

from skynet_common.camera import PinholeCamera, pixel_to_azel
from skynet_common.geodesy import angle_between_deg, azel_to_enu
from skynet_edge.config import SkyMaskConfig
from skynet_edge.geometry import Attitude, CameraModel, angular_separation_array, angular_separation_deg
from skynet_edge.skymask import SkyMask, build_sky_mask

# A typical wide-angle (~90 deg) calibration. (Much stronger coefficients put the
# image corners outside the range of the distortion polynomial, where no
# undistorted point exists.)
DIST = [-0.20, 0.05, 0.0005, -0.0003, 0.0]


def test_from_fov_uses_pixel_centre_convention():
    cam = CameraModel.from_fov(1920, 1080, 90.0)
    assert cam.K[0, 2] == 959.5 and cam.K[1, 2] == 539.5
    assert cam.fov_h_deg == pytest.approx(90.0)
    assert cam.deg_per_px == pytest.approx(0.0597, abs=1e-3)


@pytest.mark.parametrize("dist", [(), DIST])
def test_vectorised_azel_matches_shared_scalar_code(dist):
    cam = CameraModel(1920, 1080, 1000.0, 1002.0, 958.0, 541.0, dist)
    rng = np.random.default_rng(0)
    uv = rng.uniform([0, 0], [1920, 1080], (50, 2))
    for att in (Attitude(0.0, 45.0, 0.0), Attitude(237.5, 12.0, -7.0), Attitude(359.0, 89.0, 30.0)):
        vec = cam.pixels_to_azel(uv, att)
        for (u, v), (az, el) in zip(uv, vec):
            az2, el2 = cam.pixel_to_azel(u, v, att)
            assert angular_separation_deg(az, el, az2, el2) < 1e-9
    if not dist:  # without distortion the edge model is exactly the shared pinhole
        pin = PinholeCamera(1920, 1080, 1000.0, 1002.0, 958.0, 541.0)
        att = Attitude(237.5, 12.0, -7.0)
        az, el = cam.pixel_to_azel(100.0, 200.0, att)
        assert (az, el) == pytest.approx(pixel_to_azel(pin, 100.0, 200.0, 237.5, 12.0, -7.0))


def test_round_trip_through_distortion_including_corners():
    cam = CameraModel(1920, 1080, 1000.0, 1000.0, 959.5, 539.5, DIST)
    att = Attitude(80.0, 35.0, 3.0)
    uv = np.array([[0.0, 0.0], [1919.0, 1079.0], [959.5, 539.5], [300.0, 900.0]])
    back = cam.azel_to_pixels(cam.pixels_to_azel(uv, att), att)
    assert np.abs(back - uv).max() < 1e-4
    assert np.isnan(cam.azel_to_pixels(np.array([[260.0, -35.0]]), att)).all()  # behind the camera


def test_optical_axis_and_roll_convention():
    cam = CameraModel.from_fov(1920, 1080, 90.0)
    att = Attitude(120.0, 30.0, 10.0)
    az, el = cam.pixel_to_azel(959.5, 539.5, att)
    assert (az, el) == pytest.approx((120.0, 30.0))
    # Positive roll puts the right side of the image down: a point right of centre is lower.
    _, el_right = cam.pixel_to_azel(1500.0, 539.5, att)
    assert el_right < 30.0


def test_angular_separation():
    for a1, e1, a2, e2 in [(10, 20, 10.001, 20), (350, 0, 10, 0), (0, 89.9, 180, 89.9), (5, 5, 5, 5)]:
        ref = angle_between_deg(azel_to_enu(a1, e1), azel_to_enu(a2, e2))
        assert angular_separation_deg(a1, e1, a2, e2) == pytest.approx(ref, abs=1e-9)
    arr = angular_separation_array(np.array([10.0, 350.0]), np.array([20.0, 0.0]), np.array([10.001, 10.0]),
                                   np.array([20.0, 0.0]))
    assert arr == pytest.approx([0.001 * math.cos(math.radians(20)), 20.0], rel=1e-6)


def test_elevation_map_and_min_elevation_mask():
    cam = CameraModel.from_fov(640, 360, 90.0)
    att = Attitude(0.0, 10.0, 0.0)  # horizon in the lower part of the frame
    el = cam.elevation_map(att, step=1)
    assert el[180, 320] == pytest.approx(10.0, abs=0.2)
    mask = build_sky_mask(SkyMaskConfig(min_elevation_deg=5.0), cam, att)
    assert mask[0].all() and not mask[-1].any()
    rows = np.nonzero(~mask[:, 320])[0]
    assert el[rows[0], 320] < 5.0 + 0.3 and el[rows[0] - 1, 320] >= 5.0 - 0.3
    assert build_sky_mask(SkyMaskConfig(min_elevation_deg=-80.0), cam, att) is None  # nothing masked


def test_polygon_include_and_exclude():
    cam = CameraModel.from_fov(200, 100, 60.0)
    att = Attitude(0.0, 60.0, 0.0)
    poly = [[50, 20], [150, 20], [150, 80], [50, 80]]
    inc = build_sky_mask(SkyMaskConfig(min_elevation_deg=None, polygon=poly, polygon_mode="include"), cam, att)
    exc = build_sky_mask(SkyMaskConfig(min_elevation_deg=None, polygon=poly, polygon_mode="exclude"), cam, att)
    assert inc[50, 100] and not inc[5, 5] and not exc[50, 100] and exc[5, 5]


def test_sky_mask_rebuilds_only_when_attitude_moves():
    cam = CameraModel.from_fov(160, 90, 90.0)
    sky = SkyMask(SkyMaskConfig(min_elevation_deg=5.0), cam)
    assert sky.update(Attitude(0.0, 10.0, 0.0))
    assert not sky.update(Attitude(0.05, 10.05, 0.0))
    assert sky.update(Attitude(0.0, 40.0, 0.0))
    assert sky.sky_fraction == 1.0 and sky.mask is None  # bottom edge at ~10.6 deg: nothing to mask
