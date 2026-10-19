// TypeScript port of common/skynet_common/camera.py (ideal pinhole camera).
//
// Camera frame follows OpenCV: +x right, +y down, +z forward (optical axis).
// Attitude: yaw = optical-axis azimuth clockwise from true north, pitch =
// elevation above the horizon, roll = rotation about the optical axis with
// positive values putting the right-hand side of the image down.

import { type Vec3, azelToEnu, cross, dot, enuToAzel, normalize, toDeg, toRad } from "./geodesy";

export interface Attitude {
  yawDeg: number;
  pitchDeg: number;
  rollDeg: number;
}

/** ENU unit vectors of the camera's (right, down, forward) axes. */
export function cameraBasisEnu({ yawDeg, pitchDeg, rollDeg }: Attitude): [Vec3, Vec3, Vec3] {
  const y = toRad(yawDeg);
  const p = toRad(pitchDeg);
  const r = toRad(rollDeg);
  const forward: Vec3 = [Math.cos(p) * Math.sin(y), Math.cos(p) * Math.cos(y), Math.sin(p)];
  const right0: Vec3 = [Math.cos(y), -Math.sin(y), 0.0];
  const down0 = cross(forward, right0);
  const cr = Math.cos(r);
  const sr = Math.sin(r);
  const right: Vec3 = [
    cr * right0[0] + sr * down0[0],
    cr * right0[1] + sr * down0[1],
    cr * right0[2] + sr * down0[2],
  ];
  const down: Vec3 = [
    cr * down0[0] - sr * right0[0],
    cr * down0[1] - sr * right0[1],
    cr * down0[2] - sr * right0[2],
  ];
  return [right, down, forward];
}

export class PinholeCamera {
  constructor(
    readonly width: number,
    readonly height: number,
    readonly fx: number,
    readonly fy: number,
    readonly cx: number,
    readonly cy: number,
  ) {}

  /** Ideal camera with the principal point at the image centre; square pixels if `fovVDeg` is omitted. */
  static fromFov(width: number, height: number, fovHDeg: number, fovVDeg?: number | null): PinholeCamera {
    const fx = width / 2 / Math.tan(toRad(fovHDeg) / 2);
    const fy = fovVDeg == null ? fx : height / 2 / Math.tan(toRad(fovVDeg) / 2);
    return new PinholeCamera(width, height, fx, fy, width / 2, height / 2);
  }

  get fovHDeg(): number {
    return toDeg(2 * Math.atan(this.width / 2 / this.fx));
  }

  get fovVDeg(): number {
    return toDeg(2 * Math.atan(this.height / 2 / this.fy));
  }

  pixelToCameraRay(u: number, v: number): Vec3 {
    return normalize([(u - this.cx) / this.fx, (v - this.cy) / this.fy, 1.0]);
  }

  /** Project a camera-frame direction; null if it points behind the camera. */
  cameraRayToPixel(ray: Vec3): [number, number] | null {
    if (ray[2] <= 1e-12) return null;
    return [(this.fx * ray[0]) / ray[2] + this.cx, (this.fy * ray[1]) / ray[2] + this.cy];
  }

  inImage(u: number, v: number, margin = 0): boolean {
    return margin <= u && u <= this.width - margin && margin <= v && v <= this.height - margin;
  }
}

/** Azimuth/elevation (deg) of the line of sight through pixel (u, v). */
export function pixelToAzel(cam: PinholeCamera, u: number, v: number, attitude: Attitude): [number, number] {
  const [right, down, forward] = cameraBasisEnu(attitude);
  const [x, y, z] = cam.pixelToCameraRay(u, v);
  return enuToAzel(
    x * right[0] + y * down[0] + z * forward[0],
    x * right[1] + y * down[1] + z * forward[1],
    x * right[2] + y * down[2] + z * forward[2],
  );
}

/** Pixel position of a direction, or null if it is behind the camera. */
export function azelToPixel(
  cam: PinholeCamera,
  azDeg: number,
  elDeg: number,
  attitude: Attitude,
): [number, number] | null {
  const [right, down, forward] = cameraBasisEnu(attitude);
  const d = azelToEnu(azDeg, elDeg);
  return cam.cameraRayToPixel([dot(d, right), dot(d, down), dot(d, forward)]);
}

/** Az/el of the four image corners: top-left, top-right, bottom-right, bottom-left. */
export function frustumCornerAzel(cam: PinholeCamera, attitude: Attitude): [number, number][] {
  const corners: [number, number][] = [
    [0, 0],
    [cam.width, 0],
    [cam.width, cam.height],
    [0, cam.height],
  ];
  return corners.map(([u, v]) => pixelToAzel(cam, u, v, attitude));
}

/** Unit ENU directions of the four frustum corner rays (same order as frustumCornerAzel). */
export function frustumCornerRaysEnu(cam: PinholeCamera, attitude: Attitude): Vec3[] {
  return frustumCornerAzel(cam, attitude).map(([az, el]) => azelToEnu(az, el));
}
