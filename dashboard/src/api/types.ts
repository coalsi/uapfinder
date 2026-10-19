// Wire types for the SKY-NET server HTTP API v1 (docs/API.md). Timestamps are
// integer nanoseconds since the Unix epoch; they exceed 2^53, so they arrive as
// (slightly rounded) JSON numbers and are only used for ordering and display.

export type Classification = "unknown" | "ballistic" | "hypersonic" | "high_g" | "stationary";

export const CLASSIFICATIONS: readonly Classification[] = [
  "unknown",
  "ballistic",
  "hypersonic",
  "high_g",
  "stationary",
];

export interface NodeStatus {
  fps?: number;
  mem_used_mb?: number;
  gpu_temp_c?: number;
  tracks_active?: number;
  clock_offset_ms?: number;
  [key: string]: unknown;
}

export interface Node {
  node_id: string;
  label: string | null;
  lat: number | null;
  lon: number | null;
  alt_m: number | null;
  yaw_deg: number | null;
  pitch_deg: number | null;
  roll_deg: number | null;
  fov_h_deg: number | null;
  fov_v_deg: number | null;
  last_seen_ns: number | null;
  online: boolean;
  status: NodeStatus | null;
  software_version: string | null;
}

export interface TrackPoint {
  t_ns: number;
  lat: number;
  lon: number;
  alt_m: number;
  sigma_m: number;
  error_margin_m: number;
  node_ids: string[];
}

export interface Track {
  track_id: string;
  first_ns: number;
  last_ns: number;
  node_ids: string[];
  point_count: number;
  last_point: TrackPoint | null;
  speed_mps: number;
  max_speed_mps: number;
  max_accel_mps2: number;
  classification: Classification;
  flags: string[];
  points?: TrackPoint[];
}

export type ClipStatus = "requested" | "uploaded" | "failed";

export interface TrackClip {
  request_id: string;
  node_id: string;
  status: ClipStatus;
  download_url: string | null;
}

export interface TrackDetail extends Track {
  points: TrackPoint[];
  clips: TrackClip[];
}

export interface Ray {
  node_id: string;
  az_deg: number;
  el_deg: number;
  node_lat: number;
  node_lon: number;
  node_alt_m: number;
}

export interface Triangulation {
  event_id: string;
  track_id: string;
  t_ns: number;
  lat: number;
  lon: number;
  alt_m: number;
  sigma_m: number;
  error_margin_m: number;
  parallax_deg: number;
  rays: Ray[];
}

export interface ClipEvent {
  request_id: string;
  event_id: string;
  track_id: string;
  node_id: string;
  status: ClipStatus;
  download_url: string | null;
}

interface EventBase<T extends string, D> {
  id: string;
  type: T;
  t_ns: number;
  data: D;
}

export type LiveEvent =
  | EventBase<"triangulation", Triangulation>
  | EventBase<"track", Track>
  | EventBase<"node", Node>
  | EventBase<"clip", ClipEvent>;

export interface EventsPage {
  events: LiveEvent[];
  cursor: string | null;
}

/** Milliseconds. Tolerated as null in case a window has no samples yet. */
export interface LatencySummary {
  p50: number | null;
  p95: number | null;
  p99: number | null;
  max: number | null;
}

export interface Stats {
  uptime_s: number;
  packets_total: number;
  packets_per_s: number;
  rejected_total: Record<string, number>;
  matches_total: number;
  matches_per_s: number;
  active_nodes: number;
  active_tracks: number;
  processing_latency_ms: LatencySummary;
  backsignal_latency_ms: LatencySummary;
}

export type CoverageStatus = "blind" | "mono" | "stereo";

export interface CoverageQuery {
  min_lat: number;
  min_lon: number;
  max_lat: number;
  max_lon: number;
  cell_km: number;
  ref_alt_m: number;
  min_elevation_deg?: number;
  min_parallax_deg?: number;
}

export interface CoverageCell {
  type: "Feature";
  geometry: { type: "Polygon"; coordinates: [number, number][][] };
  properties: { seeing: number; stereo_pairs: number; status: CoverageStatus };
}

export interface CoverageSuggestion {
  lat: number;
  lon: number;
  gain_cells: number;
}

export interface Coverage {
  type: "FeatureCollection";
  params: Record<string, unknown>;
  summary: Record<CoverageStatus, number>;
  suggestions: CoverageSuggestion[];
  features: CoverageCell[];
}
