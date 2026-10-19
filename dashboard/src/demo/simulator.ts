// Client-side SKY-NET simulator for demo mode (?demo=1 or VITE_DEMO=1). It
// produces API-shaped nodes, events, track details, stats and coverage, and
// feeds them through the same Backend interface and store as the real server.

import type {
  Classification,
  ClipEvent,
  Coverage,
  CoverageQuery,
  LiveEvent,
  Node,
  Ray,
  Stats,
  Track,
  TrackClip,
  TrackDetail,
  TrackPoint,
  Triangulation,
} from "../api/types";
import { type Vec3, geodeticToEcef, lookAngles, norm, sub } from "../geo/geodesy";
import type { Backend, FeedHandlers } from "../data/backend";
import { computeCoverage, parallaxDeg, sees, type Viewer, viewerFromNode } from "./coverageModel";

const TICK_S = 0.5;
const HISTORY_S = 90;
const G = 9.80665;
const NS_PER_MS = 1e6;
const MAX_POINTS = 2000;

/** Small deterministic PRNG so demo screenshots are reproducible. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function demoNode(
  node_id: string,
  label: string,
  lat: number,
  lon: number,
  alt_m: number,
  attitude: [yaw: number, pitch: number, roll: number],
  fov: [h: number, v: number],
  online = true,
): Node {
  return {
    node_id,
    label,
    lat,
    lon,
    alt_m,
    yaw_deg: attitude[0],
    pitch_deg: attitude[1],
    roll_deg: attitude[2],
    fov_h_deg: fov[0],
    fov_v_deg: fov[1],
    last_seen_ns: null,
    online,
    status: null,
    software_version: online ? "0.1.0" : "0.0.9",
  };
}

// Public positions are rounded to 0.01 degrees, as the server does.
const NODES: Node[] = [
  demoNode("node-surprise", "Surprise AZ rooftop", 33.63, -112.37, 357.2, [205, 30, 0], [90, 58.7]),
  demoNode("node-buckeye", "Buckeye ranch", 33.37, -112.58, 255.0, [20, 32, 0], [90, 58.7]),
  demoNode("node-wittmann", "Wittmann desert", 33.78, -112.53, 470.5, [165, 30, -3], [90, 58.7]),
  demoNode("node-litchfield", "Litchfield Park", 33.49, -112.36, 280.4, [330, 28, 4], [70, 40.2]),
  demoNode("node-whitetank", "White Tank foothills (zenith)", 33.58, -112.62, 410.0, [0, 90, 0], [120, 100]),
  demoNode("node-tonopah", "Tonopah", 33.49, -112.94, 330.0, [75, 28, 0], [90, 58.7], false),
];

interface Sample {
  id: string;
  lat: number;
  lon: number;
  alt: number;
}

interface TrackSpec {
  classification: Classification;
  sample(t: number): Sample | null;
}

const M_PER_DEG = 111_320;

function offset(lat0: number, lon0: number, east: number, north: number): { lat: number; lon: number } {
  return { lat: lat0 + north / M_PER_DEG, lon: lon0 + east / (M_PER_DEG * Math.cos((lat0 * Math.PI) / 180)) };
}

function passId(prefix: string, pass: number): string {
  return `T-${prefix}${(((pass + 1) * 2654435761) >>> 0).toString(16).padStart(8, "0").slice(0, 4)}`;
}

/** Straight passes along `headingDeg`, one new track id per pass. */
function passes(
  prefix: string,
  start: { lat: number; lon: number },
  headingDeg: number,
  periodS: number,
  durationS: number,
  shape: (local: number) => { along: number; across: number; alt: number },
): (t: number) => Sample | null {
  const h = (headingDeg * Math.PI) / 180;
  return (t) => {
    const pass = Math.floor(t / periodS);
    const local = t - pass * periodS;
    if (local > durationS) return null;
    const { along, across, alt } = shape(local);
    const east = along * Math.sin(h) + across * Math.cos(h);
    const north = along * Math.cos(h) - across * Math.sin(h);
    return { id: passId(prefix, pass), ...offset(start.lat, start.lon, east, north), alt };
  };
}

const TRACKS: TrackSpec[] = [
  {
    // Mach ~6.5 at 24 km, west-south-west to east-north-east.
    classification: "hypersonic",
    sample: passes("h", { lat: 33.4, lon: -113.15 }, 68, 100, 75, (s) => ({ along: 2200 * s, across: 0, alt: 24_000 })),
  },
  {
    // Slow, loitering object at ~4.5 km.
    classification: "unknown",
    sample: (t) => ({
      id: "T-u5e0d2",
      ...offset(33.64, -112.46, 9000 * Math.sin((2 * Math.PI * t) / 420), 6000 * Math.sin((2 * Math.PI * t) / 300 + 1)),
      alt: 4500 + 300 * Math.sin(t / 37),
    }),
  },
  {
    // Weaving at 9 km with ~10.5 g lateral acceleration.
    classification: "high_g",
    sample: passes("g", { lat: 33.56, lon: -112.72 }, 95, 200, 160, (s) => ({
      along: 250 * s,
      across: 1500 * Math.sin((2 * Math.PI * s) / 24),
      alt: 9000,
    })),
  },
  {
    // Unpowered arc, apex ~28 km.
    classification: "ballistic",
    sample: passes("b", { lat: 33.36, lon: -112.9 }, 40, 220, 150, (s) => ({
      along: 500 * s,
      across: 0,
      alt: 1000 + 735 * s - 0.5 * G * s * s,
    })),
  },
  {
    // High-altitude balloon, drifting at 0.3 m/s.
    classification: "stationary",
    sample: (t) => ({ id: "T-s1b7a4", ...offset(33.7, -112.33, 0.3 * t, 0), alt: 18_500 }),
  },
];

function velocity(a: Sample | null, b: Sample | null): Vec3 | null {
  if (!a || !b || a.id !== b.id) return null;
  const d = sub(geodeticToEcef(a.lat, a.lon, a.alt), geodeticToEcef(b.lat, b.lon, b.alt));
  return [d[0] / TICK_S, d[1] / TICK_S, d[2] / TICK_S];
}

interface SimTrack {
  track: Track;
  points: TrackPoint[];
  clips: Map<string, TrackClip>;
  slowSinceS: number | null;
}

function classify(spec: TrackSpec, speed: number, maxAccel: number, slowForS: number): [Classification, string[]] {
  const flags: string[] = [];
  if (speed > 1715) flags.push("hypersonic");
  if (maxAccel > 98) flags.push("high_g");
  if (spec.classification === "ballistic") flags.push("ballistic");
  if (slowForS >= 30) flags.push("stationary");
  const order: Classification[] = ["hypersonic", "high_g", "ballistic", "stationary"];
  const classification = order.find((c) => flags.includes(c)) ?? "unknown";
  return [classification, flags];
}

const DEMO_CLIP_URL = `data:text/plain;charset=utf-8,${encodeURIComponent(
  "SKY-NET demo mode: placeholder for a node's HD clip (no real video in demo data).\n",
)}`;

export class Simulator {
  private readonly random = mulberry32(0x5eed);
  private readonly nodes = new Map<string, Node>();
  private readonly tracks = new Map<string, SimTrack>();
  private readonly pendingClips: { dueS: number; clip: ClipEvent }[] = [];
  private readonly observers: { node: Node; viewer: Viewer }[] = [];
  private seq = 0;
  private triangulationsLastTick = 0;

  /** `epochMs` is the wall-clock time of simulation time 0. */
  constructor(private readonly epochMs: number) {
    for (const n of NODES) {
      const node = { ...n };
      this.nodes.set(node.node_id, node);
      const viewer = viewerFromNode(node);
      if (viewer) this.observers.push({ node, viewer });
    }
  }

  private ns(simS: number): number {
    return (this.epochMs + simS * 1000) * NS_PER_MS;
  }

  private event<T extends LiveEvent["type"]>(type: T, simS: number, data: Extract<LiveEvent, { type: T }>["data"]): LiveEvent {
    this.seq += 1;
    return { id: `${Math.round(this.epochMs + simS * 1000)}-${this.seq}`, type, t_ns: this.ns(simS), data } as LiveEvent;
  }

  /** Copies, so the store never holds objects the simulator later mutates. */
  nodeList(): Node[] {
    return [...this.nodes.values()].map((n) => ({ ...n, status: n.status ? { ...n.status } : null }));
  }

  /** Advance to simulation time `t` (seconds) and return the events produced. */
  step(t: number): LiveEvent[] {
    const events: LiveEvent[] = [];
    this.triangulationsLastTick = 0;
    for (const spec of TRACKS) {
      const now = spec.sample(t);
      if (now) this.stepTrack(spec, now, t, events);
    }
    while (this.pendingClips.length > 0 && (this.pendingClips[0] as { dueS: number }).dueS <= t) {
      const { clip } = this.pendingClips.shift() as { dueS: number; clip: ClipEvent };
      this.tracks.get(clip.track_id)?.clips.set(clip.request_id, clip);
      events.push(this.event("clip", t, clip));
    }
    this.heartbeats(t, events);
    return events;
  }

  private heartbeats(t: number, events: LiveEvent[]): void {
    let i = 0;
    for (const node of this.nodes.values()) {
      i += 1;
      // Each online node heartbeats every 10 s, staggered.
      const phase = (t + i * 1.7) % 10;
      if (!node.online) {
        if (node.last_seen_ns === null) {
          node.last_seen_ns = this.ns(t - 7200);
          events.push(this.event("node", t, { ...node }));
        }
        continue;
      }
      if (node.last_seen_ns !== null && phase >= TICK_S) continue;
      node.last_seen_ns = this.ns(t);
      node.status = {
        fps: Math.round((59.5 + this.random() * 0.5) * 10) / 10,
        mem_used_mb: Math.round(3000 + this.random() * 400),
        gpu_temp_c: Math.round((48 + this.random() * 8) * 10) / 10,
        tracks_active: 0,
        clock_offset_ms: Math.round(this.random() * 50) / 1000,
      };
      events.push(this.event("node", t, { ...node, status: { ...node.status } }));
    }
  }

  private stepTrack(spec: TrackSpec, now: Sample, t: number, events: LiveEvent[]): void {
    const seers = this.observers.filter((o) => sees(o.viewer, now.lat, now.lon, now.alt, 10)).slice(0, 4);
    if (seers.length < 2) return;

    const p = geodeticToEcef(now.lat, now.lon, now.alt);
    const prev = spec.sample(t - TICK_S);
    const prev2 = spec.sample(t - 2 * TICK_S);
    const v1 = velocity(now, prev);
    const v0 = velocity(prev, prev2);
    const speed = v1 ? norm(v1) : 0;
    const accel = v1 && v0 ? norm(sub(v1, v0)) / TICK_S : 0;

    const jitter = () => (this.random() - 0.5) * 0.004;
    const rays: Ray[] = seers.map(({ node, viewer: v }) => {
      const [az, el] = lookAngles(v.lat, v.lon, v.alt, now.lat, now.lon, now.alt);
      return {
        node_id: node.node_id,
        az_deg: az + jitter(),
        el_deg: el + jitter(),
        node_lat: v.lat,
        node_lon: v.lon,
        node_alt_m: v.alt,
      };
    });
    const used = seers.map((o) => o.viewer);
    let parallax = 0;
    for (let i = 0; i < used.length; i++) {
      for (let j = i + 1; j < used.length; j++) {
        parallax = Math.max(parallax, parallaxDeg((used[i] as Viewer).ecef, (used[j] as Viewer).ecef, p));
      }
    }
    const meanRange = used.reduce((s, v) => s + norm(sub(p, v.ecef)), 0) / used.length;
    const sigma = Math.round((4 + meanRange * 0.0012 / Math.max(0.2, Math.sin((parallax * Math.PI) / 180))) * 10) / 10;
    const point: TrackPoint = {
      t_ns: this.ns(t),
      lat: now.lat,
      lon: now.lon,
      alt_m: Math.round(now.alt * 10) / 10,
      sigma_m: sigma,
      error_margin_m: Math.round(sigma * (0.2 + 0.4 * this.random()) * 10) / 10,
      node_ids: rays.map((r) => r.node_id),
    };

    let sim = this.tracks.get(now.id);
    const isNew = !sim;
    if (!sim) {
      sim = {
        track: {
          track_id: now.id,
          first_ns: point.t_ns,
          last_ns: point.t_ns,
          node_ids: [],
          point_count: 0,
          last_point: null,
          speed_mps: 0,
          max_speed_mps: 0,
          max_accel_mps2: 0,
          classification: "unknown",
          flags: [],
        },
        points: [],
        clips: new Map(),
        slowSinceS: null,
      };
      this.tracks.set(now.id, sim);
    }
    sim.points.push(point);
    if (sim.points.length > MAX_POINTS) sim.points.shift();
    sim.slowSinceS = speed < 2 ? (sim.slowSinceS ?? t) : null;
    const maxSpeed = Math.max(sim.track.max_speed_mps, speed);
    const maxAccel = Math.max(sim.track.max_accel_mps2, accel);
    const [classification, flags] = classify(spec, speed, maxAccel, sim.slowSinceS === null ? 0 : t - sim.slowSinceS);
    sim.track = {
      ...sim.track,
      last_ns: point.t_ns,
      node_ids: Array.from(new Set([...sim.track.node_ids, ...point.node_ids])),
      point_count: sim.track.point_count + 1,
      last_point: point,
      speed_mps: Math.round(speed * 10) / 10,
      max_speed_mps: Math.round(maxSpeed * 10) / 10,
      max_accel_mps2: Math.round(maxAccel * 10) / 10,
      classification,
      flags,
    };

    const triangulation: Triangulation = {
      event_id: `e-${this.seq + 1}`,
      track_id: now.id,
      t_ns: point.t_ns,
      lat: point.lat,
      lon: point.lon,
      alt_m: point.alt_m,
      sigma_m: point.sigma_m,
      error_margin_m: point.error_margin_m,
      parallax_deg: Math.round(parallax * 100) / 100,
      rays,
    };
    events.push(this.event("triangulation", t, triangulation));
    this.triangulationsLastTick += 1;
    if (isNew || Math.round(t / TICK_S) % 2 === 0) events.push(this.event("track", t, { ...sim.track }));
    if (isNew) this.requestClips(sim, triangulation, t, events);
  }

  private requestClips(sim: SimTrack, tri: Triangulation, t: number, events: LiveEvent[]): void {
    for (const ray of tri.rays) {
      const request_id = `c-${tri.track_id}-${ray.node_id}`;
      const requested: ClipEvent = {
        request_id,
        event_id: tri.event_id,
        track_id: tri.track_id,
        node_id: ray.node_id,
        status: "requested",
        download_url: null,
      };
      sim.clips.set(request_id, requested);
      events.push(this.event("clip", t, requested));
      const failed = this.random() < 0.15;
      this.pendingClips.push({
        dueS: t + 3 + this.random() * 5,
        clip: { ...requested, status: failed ? "failed" : "uploaded", download_url: failed ? null : DEMO_CLIP_URL },
      });
    }
    this.pendingClips.sort((a, b) => a.dueS - b.dueS);
  }

  trackDetail(trackId: string): TrackDetail | null {
    const sim = this.tracks.get(trackId);
    if (!sim) return null;
    return {
      ...sim.track,
      points: sim.points.slice(),
      clips: [...sim.clips.values()].map(({ request_id, node_id, status, download_url }) => ({
        request_id,
        node_id,
        status,
        download_url,
      })),
    };
  }

  stats(t: number): Stats {
    const online = this.observers.length;
    const activeTracks = [...this.tracks.values()].filter((s) => this.ns(t) - s.track.last_ns < 60e9).length;
    const r = this.random;
    const matches = this.triangulationsLastTick / TICK_S;
    return {
      uptime_s: Math.round(t + 86_400),
      packets_total: Math.round(9_000_000 + t * 800),
      packets_per_s: Math.round((online * 10 * Math.max(1, activeTracks) + online * 0.1 + r() * 20) * 10) / 10,
      rejected_total: { bad_signature: 3, stale: 12, replay: 1 },
      matches_total: Math.round(450_000 + t * matches),
      matches_per_s: Math.round(matches * 10) / 10,
      active_nodes: online,
      active_tracks: activeTracks,
      processing_latency_ms: { p50: 0.08 + r() * 0.02, p95: 0.2, p99: 0.35 + r() * 0.1, max: 2.9 },
      backsignal_latency_ms: { p50: 0.28 + r() * 0.06, p95: 0.6, p99: 1.0 + r() * 0.25, max: 4.0 },
    };
  }
}

/** Demo backend: runs the simulator in real time, with 90 s of history at start-up. */
export function createDemoBackend(now: () => number = Date.now): Backend {
  // Simulation time at start-up: a little into the cycle, so a hypersonic
  // pass is under way when the page loads.
  const startS = HISTORY_S + 30;
  const epochMs = now() - startS * 1000;
  const sim = new Simulator(epochMs);
  let simT = startS - HISTORY_S;
  const advanceTo = (t: number): LiveEvent[] => {
    const events: LiveEvent[] = [];
    for (; simT <= t; simT += TICK_S) events.push(...sim.step(simT));
    return events;
  };
  const clock = (wallMs: number) => (wallMs - epochMs) / 1000;

  return {
    kind: "demo",
    async getTrack(id) {
      const detail = sim.trackDetail(id);
      if (!detail) throw new Error(`unknown track ${id}`);
      return detail;
    },
    async getStats() {
      return sim.stats(simT);
    },
    async getCoverage(query: CoverageQuery): Promise<Coverage> {
      return computeCoverage(sim.nodeList(), query);
    },
    // Demo links are generated locally (data: URLs), so they pass through.
    link: (url) => url,
    connect(handlers: FeedHandlers) {
      handlers.onState("demo");
      const history = advanceTo(clock(now()) - TICK_S);
      handlers.onEvents(history, false);
      handlers.onSnapshot(sim.nodeList(), []);
      const timer = setInterval(() => handlers.onEvents(advanceTo(clock(now())), true), TICK_S * 1000);
      return () => clearInterval(timer);
    },
  };
}
