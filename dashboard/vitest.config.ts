import { defineConfig } from "vitest/config";

// Unit tests run in Node: geometry, store, API client and the Cesium-side
// frustum transform (CesiumJS math classes work without a WebGL context).
export default defineConfig({
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
