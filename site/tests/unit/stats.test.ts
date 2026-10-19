import { describe, expect, it } from "vitest";
import { buildStatsView, fetchJson, formatCount, formatRate, parseStats, parseVolunteerTotal, type FetchLike } from "../../src/lib/stats";

const STATS = {
  uptime_s: 3600.0,
  packets_total: 123456,
  packets_per_s: 812.4,
  matches_total: 4567,
  matches_per_s: 31.0,
  active_nodes: 12,
  active_tracks: 2,
};

describe("parsing", () => {
  it("reads the documented /stats shape", () => {
    expect(parseStats(STATS)).toEqual({ activeNodes: 12, activeTracks: 2, packetsPerS: 812.4 });
  });
  it("rejects incomplete or invalid bodies", () => {
    expect(parseStats(null)).toBeNull();
    expect(parseStats({ active_nodes: 1 })).toBeNull();
    expect(parseStats({ ...STATS, active_nodes: -1 })).toBeNull();
    expect(parseStats({ ...STATS, packets_per_s: "fast" })).toBeNull();
  });
  it("reads the volunteer total", () => {
    expect(parseVolunteerTotal({ total: 42, cells: [] })).toBe(42);
    expect(parseVolunteerTotal({ detail: "sign-up unavailable" })).toBeNull();
  });
});

describe("formatting", () => {
  it("formats counts and rates", () => {
    expect(formatCount(1234)).toBe("1,234");
    expect(formatCount(2_500_000)).toBe("2.5M");
    expect(formatRate(812.4)).toBe("812");
    expect(formatRate(31)).toBe("31");
    expect(formatRate(0.25)).toBe("0.3");
  });
});

describe("buildStatsView", () => {
  it("is live when stats are available", () => {
    const v = buildStatsView(parseStats(STATS), 42);
    expect(v.state).toBe("live");
    expect(v.tiles.map((t) => t.value)).toEqual(["12", "2", "812", "42"]);
  });
  it("degrades to launching when the API is unreachable", () => {
    const v = buildStatsView(null, null);
    expect(v.state).toBe("launching");
    expect(v.tiles.every((t) => t.value === "—")).toBe(true);
    expect(v.note).toMatch(/launching/);
  });
  it("keeps the volunteer count when only the stats endpoint fails", () => {
    const v = buildStatsView(null, 7);
    expect(v.state).toBe("launching");
    expect(v.tiles.find((t) => t.key === "volunteers")?.value).toBe("7");
  });
  it("says so when no nodes are online yet", () => {
    const v = buildStatsView({ activeNodes: 0, activeTracks: 0, packetsPerS: 0 }, null);
    expect(v.state).toBe("live");
    expect(v.note).toMatch(/No nodes are online yet/);
    expect(v.tiles.find((t) => t.key === "volunteers")?.value).toBe("—");
  });
});

describe("fetchJson", () => {
  const ok = (body: unknown): FetchLike => async () => ({ ok: true, json: async () => body });
  it("returns the body on success", async () => {
    expect(await fetchJson("/x", 1000, ok({ a: 1 }))).toEqual({ a: 1 });
  });
  it("returns null on HTTP errors, network errors and bad JSON", async () => {
    expect(await fetchJson("/x", 1000, async () => ({ ok: false, json: async () => ({}) }))).toBeNull();
    expect(await fetchJson("/x", 1000, async () => { throw new TypeError("offline"); })).toBeNull();
    expect(await fetchJson("/x", 1000, async () => ({ ok: true, json: async () => { throw new SyntaxError("bad"); } }))).toBeNull();
  });
  it("times out", async () => {
    const hang: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))));
    expect(await fetchJson("/x", 20, hang)).toBeNull();
  });
});
