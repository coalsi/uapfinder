/**
 * Live network figures for the home page, from GET /api/v1/stats and
 * GET /api/v1/volunteers/summary (docs/API.md). Pure parsing and formatting,
 * plus a fetch helper with a timeout; the DOM wiring lives in
 * src/scripts/live-stats.ts.
 */

export interface NetworkStats {
  activeNodes: number;
  activeTracks: number;
  packetsPerS: number;
}

export type TileKey = "nodes" | "tracks" | "packets" | "volunteers";

export interface StatTile {
  key: TileKey;
  label: string;
  value: string;
  /** Full, unabbreviated value for screen readers and tooltips. */
  detail: string;
}

export interface StatsViewModel {
  state: "live" | "launching";
  tiles: StatTile[];
  note: string;
}

const isCount = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0;

export function parseStats(json: unknown): NetworkStats | null {
  if (!json || typeof json !== "object") return null;
  const o = json as Record<string, unknown>;
  if (!isCount(o.active_nodes) || !isCount(o.active_tracks) || !isCount(o.packets_per_s)) return null;
  return { activeNodes: o.active_nodes, activeTracks: o.active_tracks, packetsPerS: o.packets_per_s };
}

export function parseVolunteerTotal(json: unknown): number | null {
  if (!json || typeof json !== "object") return null;
  const total = (json as Record<string, unknown>).total;
  return isCount(total) ? total : null;
}

const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function formatCount(n: number): string {
  return n >= 100_000 ? compact.format(n) : integer.format(Math.round(n));
}

export function formatRate(n: number): string {
  if (n >= 100_000) return compact.format(n);
  if (n >= 100) return integer.format(Math.round(n));
  return oneDecimal.format(n);
}

const DASH = "—";

export function buildStatsView(stats: NetworkStats | null, volunteers: number | null): StatsViewModel {
  const tiles: StatTile[] = [
    {
      key: "nodes",
      label: "Active nodes",
      value: stats ? formatCount(stats.activeNodes) : DASH,
      detail: stats ? `${integer.format(stats.activeNodes)} nodes reported in the last 60 seconds` : "Not available",
    },
    {
      key: "tracks",
      label: "Active 3D tracks",
      value: stats ? formatCount(stats.activeTracks) : DASH,
      detail: stats ? `${integer.format(stats.activeTracks)} objects currently being triangulated` : "Not available",
    },
    {
      key: "packets",
      label: "Packets per second",
      value: stats ? formatRate(stats.packetsPerS) : DASH,
      detail: stats ? `${oneDecimal.format(stats.packetsPerS)} metadata packets per second, 10-second average` : "Not available",
    },
    {
      key: "volunteers",
      label: "Volunteers signed up",
      value: volunteers !== null ? formatCount(volunteers) : DASH,
      detail: volunteers !== null ? `${integer.format(volunteers)} people have registered interest` : "Not available",
    },
  ];

  if (!stats) {
    return {
      state: "launching",
      tiles,
      note:
        volunteers !== null
          ? "The sensor network is launching. Live node and track figures will appear here once the server is online."
          : "The network is launching. Live figures will appear here once the server is online.",
    };
  }
  const note =
    stats.activeNodes === 0
      ? "Live from the network API. No nodes are online yet: the network is launching."
      : stats.activeNodes === 1
        ? "Live from the network API. Triangulation needs at least two nodes that see the same patch of sky."
        : "Live from the network API. Most tracked objects turn out to be ordinary once measured.";
  return { state: "live", tiles, note };
}

export type FetchLike = (input: string, init?: { signal?: AbortSignal; headers?: Record<string, string> }) => Promise<{
  ok: boolean;
  json(): Promise<unknown>;
}>;

/** GET a JSON document; resolves to null on any network error, non-2xx or timeout. */
export async function fetchJson(url: string, timeoutMs = 6000, fetchImpl: FetchLike = fetch): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, { signal: controller.signal, headers: { Accept: "application/json" } });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
