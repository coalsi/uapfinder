import type { LiveEvent, Node, Track, TrackClip, TrackDetail, TrackPoint, Triangulation } from "../api/types";

export const LIMITS = {
  /** Most recent points kept per track (older points are dropped from the polyline). */
  pointsPerTrack: 1000,
  /** Tracks kept in memory; the ones with the oldest `last_ns` are evicted first. */
  tracks: 100,
  /** Clip requests remembered per track. */
  clipsPerTrack: 50,
  /** Recent live triangulations kept for the ray flash animation. */
  flashes: 32,
} as const;

export interface TrackState {
  track: Track;
  /** Ascending by `t_ns`, unique `t_ns`, at most `LIMITS.pointsPerTrack`. */
  points: TrackPoint[];
}

export interface RayFlash {
  /** Event id of the triangulation. */
  key: string;
  triangulation: Triangulation;
  /** Client clock (ms) when the event was applied. */
  receivedAt: number;
}

export interface State {
  nodes: Record<string, Node>;
  tracks: Record<string, TrackState>;
  /** track_id -> request_id -> clip */
  clips: Record<string, Record<string, TrackClip>>;
  flashes: RayFlash[];
  /** True once the first node/track snapshot has been applied. */
  loaded: boolean;
}

export type Action =
  | { type: "snapshot"; nodes: Node[]; tracks: Track[] }
  | { type: "trackDetail"; detail: TrackDetail }
  | { type: "events"; events: LiveEvent[]; live: boolean; receivedAt: number };

export const initialState: State = { nodes: {}, tracks: {}, clips: {}, flashes: [], loaded: false };

/** Insert or replace points by `t_ns`, keeping ascending order and the cap. */
export function mergePoints(existing: TrackPoint[], incoming: TrackPoint[]): TrackPoint[] {
  if (incoming.length === 0) return existing;
  const out = existing.slice();
  for (const p of incoming) {
    const last = out[out.length - 1];
    if (!last || p.t_ns > last.t_ns) {
      out.push(p);
      continue;
    }
    let lo = 0;
    let hi = out.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if ((out[mid] as TrackPoint).t_ns < p.t_ns) lo = mid + 1;
      else hi = mid;
    }
    if (out[lo]?.t_ns === p.t_ns) out[lo] = p;
    else out.splice(lo, 0, p);
  }
  return out.length > LIMITS.pointsPerTrack ? out.slice(out.length - LIMITS.pointsPerTrack) : out;
}

function withoutPoints(track: Track): Track {
  if (track.points === undefined) return track;
  const summary = { ...track };
  delete summary.points;
  return summary;
}

function upsertTrack(tracks: Record<string, TrackState>, incoming: Track): Record<string, TrackState> {
  const prev = tracks[incoming.track_id];
  const fresh = incoming.points ?? [];
  const extra = incoming.last_point ? [...fresh, incoming.last_point] : fresh;
  if (!prev) {
    return { ...tracks, [incoming.track_id]: { track: withoutPoints(incoming), points: mergePoints([], extra) } };
  }
  // A summary older than what we have (e.g. from a backfill) only contributes points.
  const track = incoming.last_ns >= prev.track.last_ns ? withoutPoints(incoming) : prev.track;
  return { ...tracks, [incoming.track_id]: { track, points: mergePoints(prev.points, extra) } };
}

function triangulationPoint(t: Triangulation): TrackPoint {
  return {
    t_ns: t.t_ns,
    lat: t.lat,
    lon: t.lon,
    alt_m: t.alt_m,
    sigma_m: t.sigma_m,
    error_margin_m: t.error_margin_m,
    node_ids: t.rays.map((r) => r.node_id),
  };
}

function applyTriangulation(tracks: Record<string, TrackState>, t: Triangulation): Record<string, TrackState> {
  const point = triangulationPoint(t);
  const prev = tracks[t.track_id];
  if (!prev) {
    // The `track` event for a new track may arrive after its first point.
    const track: Track = {
      track_id: t.track_id,
      first_ns: t.t_ns,
      last_ns: t.t_ns,
      node_ids: point.node_ids,
      point_count: 1,
      last_point: point,
      speed_mps: 0,
      max_speed_mps: 0,
      max_accel_mps2: 0,
      classification: "unknown",
      flags: [],
    };
    return { ...tracks, [t.track_id]: { track, points: [point] } };
  }
  const points = mergePoints(prev.points, [point]);
  let track = prev.track;
  if (t.t_ns >= track.last_ns) {
    const nodeIds = Array.from(new Set([...track.node_ids, ...point.node_ids]));
    track = {
      ...track,
      last_ns: t.t_ns,
      last_point: point,
      node_ids: nodeIds,
      point_count: Math.max(track.point_count, points.length),
    };
  }
  return { ...tracks, [t.track_id]: { track, points } };
}

function applyNode(nodes: Record<string, Node>, node: Node): Record<string, Node> {
  const prev = nodes[node.node_id];
  if (prev?.last_seen_ns != null && node.last_seen_ns != null && node.last_seen_ns < prev.last_seen_ns) return nodes;
  return { ...nodes, [node.node_id]: node };
}

function applyClip(
  clips: State["clips"],
  trackId: string,
  clip: TrackClip,
): State["clips"] {
  const forTrack = { ...clips[trackId], [clip.request_id]: clip };
  const ids = Object.keys(forTrack);
  for (const id of ids.slice(0, Math.max(0, ids.length - LIMITS.clipsPerTrack))) delete forTrack[id];
  return { ...clips, [trackId]: forTrack };
}

/** Drop the tracks with the oldest activity beyond the cap, and their clips. */
function evict(state: State): State {
  const ids = Object.keys(state.tracks);
  if (ids.length <= LIMITS.tracks) return state;
  const byAge = ids.sort((a, b) => (state.tracks[a]?.track.last_ns ?? 0) - (state.tracks[b]?.track.last_ns ?? 0));
  const tracks = { ...state.tracks };
  const clips = { ...state.clips };
  for (const id of byAge.slice(0, ids.length - LIMITS.tracks)) {
    delete tracks[id];
    delete clips[id];
  }
  return { ...state, tracks, clips };
}

function applyEvents(state: State, events: LiveEvent[], live: boolean, receivedAt: number): State {
  let { nodes, tracks, clips, flashes } = state;
  for (const event of events) {
    switch (event.type) {
      case "node":
        nodes = applyNode(nodes, event.data);
        break;
      case "track":
        tracks = upsertTrack(tracks, event.data);
        break;
      case "triangulation":
        tracks = applyTriangulation(tracks, event.data);
        if (live) flashes = [...flashes, { key: event.id, triangulation: event.data, receivedAt }];
        break;
      case "clip": {
        const { request_id, node_id, status, download_url, track_id } = event.data;
        clips = applyClip(clips, track_id, { request_id, node_id, status, download_url });
        break;
      }
      default:
        // Unknown event types from a newer server are ignored.
        break;
    }
  }
  if (flashes.length > LIMITS.flashes) flashes = flashes.slice(flashes.length - LIMITS.flashes);
  return evict({ ...state, nodes, tracks, clips, flashes });
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "snapshot": {
      let { nodes, tracks } = state;
      for (const n of action.nodes) nodes = applyNode(nodes, n);
      for (const t of action.tracks) tracks = upsertTrack(tracks, t);
      return evict({ ...state, nodes, tracks, loaded: true });
    }
    case "trackDetail": {
      const { clips: detailClips, ...track } = action.detail;
      let clips = state.clips;
      for (const clip of detailClips) clips = applyClip(clips, track.track_id, clip);
      return evict({ ...state, tracks: upsertTrack(state.tracks, track), clips });
    }
    case "events":
      return applyEvents(state, action.events, action.live, action.receivedAt);
  }
}

/** Tracks ordered newest activity first. */
export function tracksNewestFirst(tracks: Record<string, TrackState>): TrackState[] {
  return Object.values(tracks).sort((a, b) => b.track.last_ns - a.track.last_ns);
}
