import { describe, expect, it } from "vitest";
import fixtures from "./__fixtures__/camera.json";
import { type Attitude, PinholeCamera, azelToPixel, cameraBasisEnu, frustumCornerAzel, pixelToAzel } from "./camera";
import { geodeticToEcef, lookAngles } from "./geodesy";

// Fixtures are computed by common/skynet_common via scripts/gen_camera_fixtures.py.

interface CameraCase {
  width: number;
  height: number;
  fov_h: number;
  fov_v: number | null;
  yaw: number;
  pitch: number;
  roll: number;
}

const attitude = (c: { yaw: number; pitch: number; roll: number }): Attitude => ({
  yawDeg: c.yaw,
  pitchDeg: c.pitch,
  rollDeg: c.roll,
});
const camera = (c: CameraCase) => PinholeCamera.fromFov(c.width, c.height, c.fov_h, c.fov_v);

/** Azimuth difference on the circle, so 359.9999 vs 0.0001 counts as close. */
const azDiff = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

function expectVec(actual: readonly number[], expected: readonly number[], digits = 12) {
  expected.forEach((v, i) => expect(actual[i]).toBeCloseTo(v, digits));
}

describe("camera port matches skynet_common.camera", () => {
  it("camera_basis_enu", () => {
    expect(fixtures.basis.length).toBeGreaterThan(10);
    for (const c of fixtures.basis) {
      const [right, down, forward] = cameraBasisEnu(attitude(c));
      expectVec(right, c.right);
      expectVec(down, c.down);
      expectVec(forward, c.forward);
    }
  });

  it("pixel_to_azel", () => {
    for (const c of fixtures.pixel_to_azel) {
      const [az, el] = pixelToAzel(camera(c), c.u, c.v, attitude(c));
      expect(azDiff(az, c.az)).toBeLessThan(1e-9);
      expect(el).toBeCloseTo(c.el, 9);
    }
  });

  it("frustum_corner_azel", () => {
    expect(fixtures.frustum_corner_azel.length).toBeGreaterThan(30);
    for (const c of fixtures.frustum_corner_azel) {
      const corners = frustumCornerAzel(camera(c), attitude(c));
      expect(corners).toHaveLength(4);
      corners.forEach(([az, el], i) => {
        const [eaz, eel] = c.corners[i] as [number, number];
        expect(azDiff(az, eaz)).toBeLessThan(1e-9);
        expect(el).toBeCloseTo(eel, 9);
      });
    }
  });

  it("azel_to_pixel, including directions behind the camera", () => {
    let behind = 0;
    for (const c of fixtures.azel_to_pixel) {
      const uv = azelToPixel(camera(c), c.az, c.el, attitude(c));
      if (c.uv === null) {
        behind++;
        expect(uv).toBeNull();
      } else {
        expect(uv).not.toBeNull();
        expectVec(uv as [number, number], c.uv, 6);
      }
    }
    expect(behind).toBeGreaterThan(0);
  });

  it("blueprint attitude: yaw 0, pitch 45 looks north at 45 degrees", () => {
    const cam = PinholeCamera.fromFov(1920, 1080, 90);
    const [az, el] = pixelToAzel(cam, cam.cx, cam.cy, { yawDeg: 0, pitchDeg: 45, rollDeg: 0 });
    expect(azDiff(az, 0)).toBeLessThan(1e-9);
    expect(el).toBeCloseTo(45, 12);
    expect(cam.fovVDeg).toBeCloseTo(58.7, 1);
  });
});

describe("geodesy port matches skynet_common.geodesy", () => {
  it("geodetic_to_ecef", () => {
    for (const c of fixtures.geodetic_to_ecef) {
      const ecef = geodeticToEcef(c.lat, c.lon, c.h);
      c.ecef.forEach((v, i) => expect(Math.abs((ecef[i] as number) - v)).toBeLessThan(1e-6));
    }
  });

  it("look_angles", () => {
    for (const c of fixtures.look_angles) {
      const [az, el, range] = lookAngles(...(c.obs as [number, number, number]), ...(c.tgt as [number, number, number]));
      expect(azDiff(az, c.az)).toBeLessThan(1e-9);
      expect(el).toBeCloseTo(c.el, 9);
      expect(Math.abs(range - c.range_m)).toBeLessThan(1e-6);
    }
  });
});
