import { describe, expect, it } from "vitest";
import type { ClipEvent, LiveEvent, Node, Track, TrackPoint, Triangulation } from "../api/types";
import { LIMITS, type State, initialState, mergePoints, reducer, tracksNewestFirst } from "./reducer";

const S = 1e9; // ns per second

const point = (t: number, extra: Partial<TrackPoint> = {}): TrackPoint => ({
  t_ns: t * S,
  lat: 33.6 + t * 1e-4,
  lon: -112.4,
  alt_m: 9000,
  sigma_m: 40,
  error_margin_m: 12,
  node_ids: ["a", "b"],
  ...extra,
});

const track = (id: string, lastS: number, extra: Partial<Track> = {}): Track => ({
  track_id: id,
  first_ns: 0,
  last_ns: lastS * S,
  node_ids: ["a", "b"],
  point_count: 1,
  last_point: point(lastS),
  speed_mps: 250,
  max_speed_mps: 300,
  max_accel_mps2: 15,
  classification: "unknown",
  flags: [],
  ...extra,
});

const node = (id: string, lastSeenS: number | null, extra: Partial<Node> = {}): Node => ({
  node_id: id,
  label: null,
  lat: 33.63,
  lon: -112.37,
  alt_m: 357,
  yaw_deg: 0,
  pitch_deg: 45,
  roll_deg: 0,
  fov_h_deg: 90,
  fov_v_deg: 58.7,
  last_seen_ns: lastSeenS === null ? null : lastSeenS * S,
  online: true,
  status: { fps: 59.8, mem_used_mb: 3120 },
  software_version: "0.1.0",
  ...extra,
});

const triangulation = (trackId: string, t: number): Triangulation => ({
  event_id: `e-${trackId}-${t}`,
  track_id: trackId,
  t_ns: t * S,
  lat: 33.6,
  lon: -112.4,
  alt_m: 9000,
  sigma_m: 40,
  error_margin_m: 12,
  parallax_deg: 20,
  rays: [
    { node_id: "a", az_deg: 10, el_deg: 40, node_lat: 33.63, node_lon: -112.37, node_alt_m: 357 },
    { node_id: "c", az_deg: 200, el_deg: 35, node_lat: 33.37, node_lon: -112.58, node_alt_m: 255 },
  ],
});

let seq = 0;
const wrap = <T extends LiveEvent["type"]>(type: T, data: Extract<LiveEvent, { type: T }>["data"]): LiveEvent =>
  ({ id: `ev-${++seq}`, type, t_ns: 0, data }) as LiveEvent;

const apply = (state: State, events: LiveEvent[], live = true, receivedAt = 1000) =>
  reducer(state, { type: "events", events, live, receivedAt });

describe("mergePoints", () => {
  it("keeps points sorted and unique by t_ns, replacing duplicates", () => {
    const merged = mergePoints([point(1), point(3)], [point(2), point(3, { alt_m: 1 }), point(0), point(4)]);
    expect(merged.map((p) => p.t_ns / S)).toEqual([0, 1, 2, 3, 4]);
    expect(merged[3]?.alt_m).toBe(1);
  });

  it("keeps only the most recent LIMITS.pointsPerTrack points", () => {
    const many = Array.from({ length: LIMITS.pointsPerTrack + 50 }, (_, i) => point(i));
    const merged = mergePoints([], many);
    expect(merged).toHaveLength(LIMITS.pointsPerTrack);
    expect(merged[0]?.t_ns).toBe(50 * S);
  });

  it("returns the same array when there is nothing to add", () => {
    const existing = [point(1)];
    expect(mergePoints(existing, [])).toBe(existing);
  });
});

describe("reducer", () => {
  it("applies a snapshot of nodes and tracks (with their points) and marks the store loaded", () => {
    const s = reducer(initialState, {
      type: "snapshot",
      nodes: [node("a", 10), node("b", 12)],
      tracks: [track("T1", 5, { points: [point(3), point(4)] })],
    });
    expect(s.loaded).toBe(true);
    expect(Object.keys(s.nodes)).toEqual(["a", "b"]);
    expect(s.tracks.T1?.points.map((p) => p.t_ns / S)).toEqual([3, 4, 5]);
    expect(s.tracks.T1?.track.points).toBeUndefined();
  });

  it("does not mutate the previous state", () => {
    const before = reducer(initialState, { type: "snapshot", nodes: [node("a", 10)], tracks: [track("T1", 5)] });
    const frozen = structuredClone(before);
    apply(before, [wrap("triangulation", triangulation("T1", 6)), wrap("node", node("a", 20))]);
    expect(before).toEqual(frozen);
  });

  it("starts a provisional track from a triangulation that arrives before its track event", () => {
    let s = apply(initialState, [wrap("triangulation", triangulation("T9", 100))]);
    expect(s.tracks.T9?.track.classification).toBe("unknown");
    expect(s.tracks.T9?.track.node_ids).toEqual(["a", "c"]);
    expect(s.tracks.T9?.points).toHaveLength(1);

    s = apply(s, [wrap("track", track("T9", 100, { classification: "hypersonic", speed_mps: 2200, flags: ["hypersonic"] }))]);
    expect(s.tracks.T9?.track.classification).toBe("hypersonic");
    expect(s.tracks.T9?.points).toHaveLength(1); // same t_ns as the triangulation: merged, not duplicated
  });

  it("extends a track with later triangulations and ignores stale summaries", () => {
    let s = apply(initialState, [wrap("track", track("T1", 10, { classification: "high_g" }))]);
    s = apply(s, [wrap("triangulation", triangulation("T1", 11))]);
    expect(s.tracks.T1?.track.last_ns).toBe(11 * S);
    expect(s.tracks.T1?.track.node_ids).toEqual(["a", "b", "c"]);
    expect(s.tracks.T1?.track.classification).toBe("high_g");

    // An older summary (e.g. replayed by a backfill) only contributes its point.
    s = apply(s, [wrap("track", track("T1", 9, { classification: "unknown" }))]);
    expect(s.tracks.T1?.track.classification).toBe("high_g");
    expect(s.tracks.T1?.points.map((p) => p.t_ns / S)).toEqual([9, 10, 11]);

    // A late, out-of-order triangulation is inserted in time order.
    s = apply(s, [wrap("triangulation", triangulation("T1", 9.5))]);
    expect(s.tracks.T1?.points.map((p) => p.t_ns / S)).toEqual([9, 9.5, 10, 11]);
    expect(s.tracks.T1?.track.last_ns).toBe(11 * S);
  });

  it("is idempotent for replayed events", () => {
    const events = [wrap("track", track("T1", 10)), wrap("triangulation", triangulation("T1", 11))];
    const once = apply(initialState, events, false);
    const twice = apply(once, events, false);
    expect(twice.tracks).toEqual(once.tracks);
  });

  it("records ray flashes only for live triangulations, bounded", () => {
    const history = apply(initialState, [wrap("triangulation", triangulation("T1", 1))], false);
    expect(history.flashes).toHaveLength(0);

    const burst = Array.from({ length: LIMITS.flashes + 10 }, (_, i) => wrap("triangulation", triangulation("T1", 2 + i)));
    const live = apply(history, burst, true, 4242);
    expect(live.flashes).toHaveLength(LIMITS.flashes);
    expect(live.flashes.at(-1)?.triangulation.t_ns).toBe((2 + LIMITS.flashes + 9) * S);
    expect(live.flashes[0]?.receivedAt).toBe(4242);
  });

  it("keeps the newest node state and ignores older heartbeats", () => {
    let s = apply(initialState, [wrap("node", node("a", 100, { status: { fps: 30 } }))]);
    s = apply(s, [wrap("node", node("a", 90, { status: { fps: 10 } }))]);
    expect(s.nodes.a?.status?.fps).toBe(30);
    // Same last_seen but the server now reports it offline: applied.
    s = apply(s, [wrap("node", node("a", 100, { online: false }))]);
    expect(s.nodes.a?.online).toBe(false);
    s = apply(s, [wrap("node", node("a", 110, { online: true }))]);
    expect(s.nodes.a?.online).toBe(true);
  });

  it("collects clip status per track and merges GET /tracks/{id} details", () => {
    const clip = (status: ClipEvent["status"], url: string | null = null): ClipEvent => ({
      request_id: "c1",
      event_id: "e1",
      track_id: "T1",
      node_id: "a",
      status,
      download_url: url,
    });
    let s = apply(initialState, [wrap("track", track("T1", 10)), wrap("clip", clip("requested"))]);
    expect(s.clips.T1?.c1?.status).toBe("requested");
    s = apply(s, [wrap("clip", clip("uploaded", "/api/v1/clips/c1/file"))]);
    expect(s.clips.T1?.c1).toEqual({ request_id: "c1", node_id: "a", status: "uploaded", download_url: "/api/v1/clips/c1/file" });

    s = reducer(s, {
      type: "trackDetail",
      detail: {
        ...track("T1", 12),
        points: [point(1), point(2), point(12)],
        clips: [{ request_id: "c2", node_id: "b", status: "failed", download_url: null }],
      },
    });
    expect(Object.keys(s.clips.T1 ?? {})).toEqual(["c1", "c2"]);
    expect(s.tracks.T1?.points.map((p) => p.t_ns / S)).toEqual([1, 2, 10, 12]);
    expect(s.tracks.T1?.track.last_ns).toBe(12 * S);
  });

  it("caps clips per track", () => {
    const events = Array.from({ length: LIMITS.clipsPerTrack + 5 }, (_, i) =>
      wrap("clip", { request_id: `c${i}`, event_id: "e", track_id: "T1", node_id: "a", status: "requested", download_url: null }),
    );
    const s = apply(initialState, events);
    const ids = Object.keys(s.clips.T1 ?? {});
    expect(ids).toHaveLength(LIMITS.clipsPerTrack);
    expect(ids[0]).toBe("c5");
  });

  it("evicts the least recently active tracks, and their clips, beyond LIMITS.tracks", () => {
    const events: LiveEvent[] = [];
    for (let i = 0; i < LIMITS.tracks + 20; i++) {
      events.push(wrap("track", track(`T${i}`, 1000 + i)));
      events.push(wrap("clip", { request_id: `c${i}`, event_id: "e", track_id: `T${i}`, node_id: "a", status: "requested", download_url: null }));
    }
    const s = apply(initialState, events);
    expect(Object.keys(s.tracks)).toHaveLength(LIMITS.tracks);
    expect(s.tracks.T0).toBeUndefined();
    expect(s.clips.T0).toBeUndefined();
    expect(s.tracks[`T${LIMITS.tracks + 19}`]).toBeDefined();
  });

  it("ignores event types it does not know", () => {
    const s = apply(initialState, [{ id: "x", type: "future", t_ns: 0, data: {} } as unknown as LiveEvent]);
    expect(s.tracks).toEqual({});
    expect(s.nodes).toEqual({});
  });

  it("orders tracks newest activity first", () => {
    const s = apply(initialState, [wrap("track", track("old", 1)), wrap("track", track("new", 3)), wrap("track", track("mid", 2))]);
    expect(tracksNewestFirst(s.tracks).map((t) => t.track.track_id)).toEqual(["new", "mid", "old"]);
  });
});
