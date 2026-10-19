import { describe, expect, it } from "vitest";
import { CLASSIFICATIONS, type LiveEvent, type Triangulation } from "../api/types";
import { LIMITS, initialState, reducer } from "../store/reducer";
import { coverageQueryForView } from "../geo/coverageQuery";
import { Simulator, createDemoBackend } from "./simulator";

const EPOCH_MS = Date.UTC(2026, 9, 19, 3, 0, 0);

function run(seconds: number): LiveEvent[] {
  const sim = new Simulator(EPOCH_MS);
  const events: LiveEvent[] = [];
  for (let t = 0; t <= seconds; t += 0.5) events.push(...sim.step(t));
  return events;
}

describe("demo simulator", () => {
  const events = run(240);

  it("emits API-shaped events with unique ids and nanosecond timestamps", () => {
    const ids = new Set(events.map((e) => e.id));
    expect(ids.size).toBe(events.length);
    const types = new Set(events.map((e) => e.type));
    expect(types).toEqual(new Set(["node", "track", "triangulation", "clip"]));
    for (const e of events) expect(String(Math.round(e.t_ns)).length).toBe(19);
  });

  it("triangulates only with at least two rays from real node positions", () => {
    const tris = events.filter((e): e is Extract<LiveEvent, { type: "triangulation" }> => e.type === "triangulation");
    expect(tris.length).toBeGreaterThan(100);
    for (const { data } of tris as { data: Triangulation }[]) {
      expect(data.rays.length).toBeGreaterThanOrEqual(2);
      for (const ray of data.rays) {
        expect(ray.el_deg).toBeGreaterThan(9);
        expect(Math.abs(ray.node_lat - 33.5)).toBeLessThan(1);
      }
    }
  });

  it("produces every classification", () => {
    const seen = new Set(events.flatMap((e) => (e.type === "track" ? [e.data.classification] : [])));
    expect([...seen].sort()).toEqual([...CLASSIFICATIONS].sort());
  });

  it("stays within the store's memory bounds over a long session", () => {
    let state = reducer(initialState, { type: "snapshot", nodes: [], tracks: [] });
    const long = run(3600);
    for (let i = 0; i < long.length; i += 200) {
      state = reducer(state, { type: "events", events: long.slice(i, i + 200), live: true, receivedAt: i });
    }
    expect(Object.keys(state.tracks).length).toBeLessThanOrEqual(LIMITS.tracks);
    for (const t of Object.values(state.tracks)) expect(t.points.length).toBeLessThanOrEqual(LIMITS.pointsPerTrack);
    expect(state.flashes.length).toBeLessThanOrEqual(LIMITS.flashes);
    expect(Object.values(state.nodes).filter((n) => n.online)).toHaveLength(5);
  });

  it("serves coverage with consistent counts and deploy suggestions", async () => {
    const backend = createDemoBackend(() => EPOCH_MS);
    const query = coverageQueryForView({ west: -113.2, south: 33.1, east: -111.9, north: 34.1 }, 10_000);
    const coverage = await backend.getCoverage(query);
    const { blind, mono, stereo } = coverage.summary;
    expect(blind + mono + stereo).toBe(coverage.features.length);
    expect(stereo).toBeGreaterThan(0);
    expect(mono).toBeGreaterThan(0);
    expect(blind).toBeGreaterThan(0);
    expect(coverage.suggestions.length).toBeGreaterThan(0);
    expect(coverage.suggestions.length).toBeLessThanOrEqual(10);
    for (const s of coverage.suggestions) expect(s.gain_cells).toBeGreaterThan(0);
    const ring = coverage.features[0]?.geometry.coordinates[0];
    expect(ring?.[0]).toEqual(ring?.at(-1)); // closed GeoJSON ring
  });
});
