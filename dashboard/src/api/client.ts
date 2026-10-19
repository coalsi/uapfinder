import type {
  Coverage,
  CoverageQuery,
  EventsPage,
  Node,
  Stats,
  Track,
  TrackDetail,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

/**
 * Absolute http(s) root of the API server without a trailing slash. An empty
 * `raw` value means same origin (the Vercel deployment routes /api/* there).
 */
export function resolveApiBase(raw: string | undefined, pageHref: string): string {
  const url = new URL(raw && raw.trim() !== "" ? raw.trim() : "/", pageHref);
  return `${url.origin}${url.pathname}`.replace(/\/+$/, "");
}

export class ApiClient {
  readonly base: string;
  private readonly fetchImpl: typeof fetch;

  /** `base` comes from {@link resolveApiBase}; requests go to `${base}/api/v1/...`. */
  constructor(base: string, fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.base = base;
    this.fetchImpl = fetchImpl;
  }

  url(path: string, query: Query = {}): string {
    const url = new URL(`${this.base}/api/v1${path}`);
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    return url.toString();
  }

  /** WebSocket URL of the live event feed. */
  liveUrl(): string {
    return this.url("/live").replace(/^http/, "ws");
  }

  private async get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
    const init: RequestInit = { headers: { Accept: "application/json" } };
    if (signal) init.signal = signal;
    const res = await this.fetchImpl(this.url(path, query), init);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body: unknown = await res.json();
        if (body && typeof body === "object" && "detail" in body) {
          detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch {
        // Non-JSON error body: keep the status text.
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as T;
  }

  async getNodes(): Promise<Node[]> {
    return (await this.get<{ nodes: Node[] }>("/nodes")).nodes;
  }

  async getTracks(query: { since_ns?: number; limit?: number; classification?: string } = {}): Promise<Track[]> {
    return (await this.get<{ tracks: Track[] }>("/tracks", query)).tracks;
  }

  getTrack(trackId: string): Promise<TrackDetail> {
    return this.get<TrackDetail>(`/tracks/${encodeURIComponent(trackId)}`);
  }

  /** Events after `after` (oldest first), or the most recent `limit` events when `after` is null. */
  getEvents(after: string | null, limit: number): Promise<EventsPage> {
    return this.get<EventsPage>("/events", { after, limit });
  }

  getStats(): Promise<Stats> {
    return this.get<Stats>("/stats");
  }

  getCoverage(query: CoverageQuery, signal?: AbortSignal): Promise<Coverage> {
    return this.get<Coverage>("/coverage", { ...query }, signal);
  }
}
