import { Cartesian3, Matrix4, Transforms } from "cesium";
import type { Node } from "../api/types";
import { PinholeCamera, frustumCornerRaysEnu } from "../geo/camera";

export interface Frustum {
  apex: Cartesian3;
  /** Far corners at the requested range: top-left, top-right, bottom-right, bottom-left. */
  far: Cartesian3[];
}

/** Nodes report FOVs, not resolutions; with a vertical FOV the aspect ratio does not matter, without one 16:9 is assumed. */
const SENSOR_WIDTH = 1920;
const SENSOR_HEIGHT = 1080;

/** Viewing frustum of a node out to `rangeM`, or null until its position and camera attitude are known. */
export function nodeFrustum(node: Node, rangeM: number): Frustum | null {
  const { lat, lon, alt_m, yaw_deg, pitch_deg, roll_deg, fov_h_deg, fov_v_deg } = node;
  if (lat == null || lon == null || alt_m == null || yaw_deg == null || pitch_deg == null || fov_h_deg == null) {
    return null;
  }
  const cam = PinholeCamera.fromFov(SENSOR_WIDTH, SENSOR_HEIGHT, fov_h_deg, fov_v_deg);
  const rays = frustumCornerRaysEnu(cam, { yawDeg: yaw_deg, pitchDeg: pitch_deg, rollDeg: roll_deg ?? 0 });
  const apex = Cartesian3.fromDegrees(lon, lat, alt_m);
  // Cesium derives "up" from the position's ellipsoid gradient, which tilts
  // from the geodetic vertical by ~1e-7 rad per km of height. Building the
  // frame at the foot point and moving it to the apex keeps it identical to
  // skynet_common's geodetic ENU frame.
  const enuToEcef = Transforms.eastNorthUpToFixedFrame(Cartesian3.fromDegrees(lon, lat, 0));
  Matrix4.setTranslation(enuToEcef, apex, enuToEcef);
  const far = rays.map(([e, n, u]) =>
    Matrix4.multiplyByPoint(enuToEcef, new Cartesian3(e * rangeM, n * rangeM, u * rangeM), new Cartesian3()),
  );
  return { apex, far };
}
