import { describe, expect, it } from "vitest";
import type { Node } from "../api/types";
import fixtures from "../geo/__fixtures__/camera.json";
import { nodeFrustum } from "./frustum";

const baseNode: Node = {
  node_id: "n",
  label: null,
  lat: null,
  lon: null,
  alt_m: null,
  yaw_deg: null,
  pitch_deg: null,
  roll_deg: null,
  fov_h_deg: null,
  fov_v_deg: null,
  last_seen_ns: null,
  online: true,
  status: null,
  software_version: null,
};

describe("nodeFrustum (Cesium ENU -> ECEF)", () => {
  it("matches the Python reference far corners to the millimetre", () => {
    expect(fixtures.frustum_ecef.length).toBeGreaterThan(5);
    for (const c of fixtures.frustum_ecef) {
      const f = nodeFrustum(
        {
          ...baseNode,
          lat: c.lat,
          lon: c.lon,
          alt_m: c.alt_m,
          yaw_deg: c.yaw,
          pitch_deg: c.pitch,
          roll_deg: c.roll,
          fov_h_deg: c.fov_h,
          fov_v_deg: c.fov_v,
        },
        c.range_m,
      );
      expect(f).not.toBeNull();
      const { apex, far } = f as NonNullable<typeof f>;
      expect(Math.hypot(apex.x - c.apex[0]!, apex.y - c.apex[1]!, apex.z - c.apex[2]!)).toBeLessThan(1e-3);
      far.forEach((p, i) => {
        const e = c.far[i] as number[];
        expect(Math.hypot(p.x - e[0]!, p.y - e[1]!, p.z - e[2]!)).toBeLessThan(1e-3);
      });
    }
  });

  it("is null until the first heartbeat reports the camera", () => {
    expect(nodeFrustum({ ...baseNode, lat: 33.63, lon: -112.37, alt_m: 357 }, 25_000)).toBeNull();
  });
});
