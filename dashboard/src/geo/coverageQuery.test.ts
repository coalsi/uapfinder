import { describe, expect, it } from "vitest";
import { type GeoRect, SERVER_MAX_CELLS, TARGET_CELLS, cellCount, cellSizeFor, coverageQueryForView, coverageQueryKey } from "./coverageQuery";

const rect = (west: number, south: number, east: number, north: number): GeoRect => ({ west, south, east, north });
const asRect = (q: ReturnType<typeof coverageQueryForView>): GeoRect => rect(q.min_lon, q.min_lat, q.max_lon, q.max_lat);

describe("coverageQueryForView", () => {
  it("uses small cells for a city-sized view and larger cells as the view grows", () => {
    const city = coverageQueryForView(rect(-112.6, 33.5, -112.3, 33.7), 3000);
    const region = coverageQueryForView(rect(-114, 32.5, -111, 34.5), 3000);
    const state = coverageQueryForView(rect(-115, 31, -109, 37), 3000);
    expect(city.cell_km).toBe(2);
    expect(region.cell_km).toBeGreaterThan(city.cell_km);
    expect(state.cell_km).toBeGreaterThan(region.cell_km);
  });

  it("stays well under the server's 10 000-cell cap for any view", () => {
    const views = [
      rect(-180, -85, 180, 85), // whole globe
      rect(-125, 24, -66, 50), // continental US
      rect(-112.5, 33.6, -112.49, 33.61), // tiny
      rect(170, -50, -170, -30), // across the antimeridian
      rect(-10, 80, 10, 89), // near the pole
    ];
    for (const view of views) {
      const q = coverageQueryForView(view, 10_000);
      const cells = cellCount(asRect(q), q.cell_km);
      expect(cells).toBeLessThanOrEqual(SERVER_MAX_CELLS);
      expect(q.min_lat).toBeLessThan(q.max_lat);
      expect(q.min_lon).toBeLessThan(q.max_lon);
      expect(q.min_lat).toBeGreaterThanOrEqual(-85);
      expect(q.max_lat).toBeLessThanOrEqual(85);
      expect(q.min_lon).toBeGreaterThanOrEqual(-180);
      expect(q.max_lon).toBeLessThanOrEqual(180);
    }
  });

  it("covers the visible area and snaps outwards to the cell grid, so small pans reuse the same request", () => {
    const view = rect(-112.61, 33.42, -112.13, 33.81);
    const q = coverageQueryForView(view, 3000);
    expect(q.min_lon).toBeLessThanOrEqual(view.west);
    expect(q.max_lon).toBeGreaterThanOrEqual(view.east);
    expect(q.min_lat).toBeLessThanOrEqual(view.south);
    expect(q.max_lat).toBeGreaterThanOrEqual(view.north);
    const nudged = coverageQueryForView(rect(-112.6095, 33.4205, -112.1305, 33.8095), 3000);
    expect(coverageQueryKey(nudged)).toBe(coverageQueryKey(q));
  });

  it("passes the altitude slice through and keys on it", () => {
    const view = rect(-112.6, 33.5, -112.3, 33.7);
    const a = coverageQueryForView(view, 3000);
    const b = coverageQueryForView(view, 10_000);
    expect(a.ref_alt_m).toBe(3000);
    expect(coverageQueryKey(a)).not.toBe(coverageQueryKey(b));
  });

  it("picks the smallest nice cell size that keeps the grid near the target", () => {
    const r = rect(-113, 33, -112, 34);
    const km = cellSizeFor(r);
    expect(cellCount(r, km)).toBeLessThanOrEqual(TARGET_CELLS);
  });
});
