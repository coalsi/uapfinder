"""Generate src/geo/__fixtures__/camera.json from the reference Python geometry.

The dashboard's TypeScript port of skynet_common.camera / geodesy is tested
against these values, so the frustums drawn on the globe use exactly the same
attitude convention as the edge nodes and the server. Re-run after changing
common/skynet_common: `npm run fixtures` (needs only the Python stdlib).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep common/ free of __pycache__
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "common"))

from skynet_common import geodesy as g  # noqa: E402
from skynet_common.camera import (  # noqa: E402
    PinholeCamera,
    azel_to_pixel,
    camera_basis_enu,
    frustum_corner_azel,
    pixel_to_azel,
)

OUT = HERE.parent / "src" / "geo" / "__fixtures__" / "camera.json"

# Attitudes that exercise the convention's edge cases: the blueprint default,
# zenith-pointing, wrap-around yaw, strong roll both ways, below the horizon.
FIXED_ATTITUDES = [
    (0.0, 45.0, 0.0),
    (0.0, 90.0, 0.0),
    (359.5, 10.0, 0.0),
    (90.0, 30.0, 25.0),
    (225.0, 60.0, -40.0),
    (135.0, -5.0, 5.0),
]

CAMERAS = [
    (1920, 1080, 90.0, 58.7),
    (1920, 1080, 70.0, None),
    (1280, 960, 120.0, 100.0),
]


def main() -> None:
    rng = random.Random(20261019)
    attitudes = FIXED_ATTITUDES + [
        (rng.uniform(0, 360), rng.uniform(-30, 89), rng.uniform(-60, 60)) for _ in range(12)
    ]

    basis = []
    for yaw, pitch, roll in attitudes:
        right, down, forward = camera_basis_enu(yaw, pitch, roll)
        basis.append({"yaw": yaw, "pitch": pitch, "roll": roll, "right": right, "down": down, "forward": forward})

    pixels = []
    frustums = []
    projections = []
    for width, height, fov_h, fov_v in CAMERAS:
        cam = PinholeCamera.from_fov(width, height, fov_h, fov_v)
        camera = {"width": width, "height": height, "fov_h": fov_h, "fov_v": fov_v}
        for yaw, pitch, roll in attitudes:
            attitude = {"yaw": yaw, "pitch": pitch, "roll": roll}
            corners = frustum_corner_azel(cam, yaw, pitch, roll)
            frustums.append({**camera, **attitude, "corners": [list(c) for c in corners]})
            for _ in range(3):
                u, v = rng.uniform(0, width), rng.uniform(0, height)
                az, el = pixel_to_azel(cam, u, v, yaw, pitch, roll)
                pixels.append({**camera, **attitude, "u": u, "v": v, "az": az, "el": el})
            for _ in range(3):
                az, el = rng.uniform(0, 360), rng.uniform(-20, 90)
                uv = azel_to_pixel(cam, az, el, yaw, pitch, roll)
                projections.append({**camera, **attitude, "az": az, "el": el, "uv": list(uv) if uv else None})

    geodetic = []
    for _ in range(12):
        lat, lon, h = rng.uniform(-80, 80), rng.uniform(-180, 180), rng.uniform(-100, 40000)
        geodetic.append({"lat": lat, "lon": lon, "h": h, "ecef": g.geodetic_to_ecef(lat, lon, h)})

    # Far corners of a node's viewing frustum in ECEF: what the globe draws.
    frustum_ecef = []
    for _ in range(10):
        lat, lon, h = 33.63 + rng.uniform(-2, 2), -112.37 + rng.uniform(-2, 2), rng.uniform(0, 2500)
        yaw, pitch, roll = rng.uniform(0, 360), rng.uniform(0, 90), rng.uniform(-30, 30)
        fov_h, fov_v = rng.choice([(90.0, 58.7), (70.0, 40.2), (120.0, 100.0)])
        range_m = rng.choice([5000.0, 25000.0, 80000.0])
        apex = g.geodetic_to_ecef(lat, lon, h)
        cam = PinholeCamera.from_fov(1920, 1080, fov_h, fov_v)
        far = []
        for az, el in frustum_corner_azel(cam, yaw, pitch, roll):
            d = g.azel_to_ecef_unit(lat, lon, az, el)
            far.append(g.add(apex, g.scale(d, range_m)))
        frustum_ecef.append(
            {"lat": lat, "lon": lon, "alt_m": h, "yaw": yaw, "pitch": pitch, "roll": roll,
             "fov_h": fov_h, "fov_v": fov_v, "range_m": range_m, "apex": apex, "far": far}
        )

    look = []
    for _ in range(12):
        o = (33.63 + rng.uniform(-1, 1), -112.37 + rng.uniform(-1, 1), rng.uniform(0, 1500))
        t = (33.63 + rng.uniform(-1.5, 1.5), -112.37 + rng.uniform(-1.5, 1.5), rng.uniform(500, 30000))
        az, el, rng_m = g.look_angles(*o, *t)
        look.append({"obs": o, "tgt": t, "az": az, "el": el, "range_m": rng_m})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "source": "common/skynet_common (camera.py, geodesy.py) via dashboard/scripts/gen_camera_fixtures.py",
                "basis": basis,
                "pixel_to_azel": pixels,
                "frustum_corner_azel": frustums,
                "azel_to_pixel": projections,
                "geodetic_to_ecef": geodetic,
                "look_angles": look,
                "frustum_ecef": frustum_ecef,
            },
            indent=1,
        )
        + "\n"
    )
    print(f"wrote {OUT.relative_to(HERE.parent)}: {len(frustums)} frustums, {len(pixels)} pixels")


if __name__ == "__main__":
    main()
