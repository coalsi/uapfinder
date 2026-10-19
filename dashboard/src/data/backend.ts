import type { ConnectionState } from "../api/live";
import type { Coverage, CoverageQuery, LiveEvent, Node, Stats, Track, TrackDetail } from "../api/types";

export interface FeedHandlers {
  /** Current nodes and recent tracks (with up to 200 points each); also used for periodic node refreshes. */
  onSnapshot(nodes: Node[], tracks: Track[]): void;
  /** `live` is false for history fetched at start-up (no ray flashes). */
  onEvents(events: LiveEvent[], live: boolean): void;
  onState(state: ConnectionState | "demo"): void;
}

/** Everything the UI reads, served either by the SKY-NET API or by the demo simulator. */
export interface Backend {
  readonly kind: "api" | "demo";
  getTrack(trackId: string): Promise<TrackDetail>;
  getStats(): Promise<Stats>;
  getCoverage(query: CoverageQuery, signal?: AbortSignal): Promise<Coverage>;
  /** A server-provided link (clip download) made safe to render: absolute, or null if unusable. */
  link(url: string | null): string | null;
  /** Start the snapshot + live feed; returns a function that stops it. */
  connect(handlers: FeedHandlers): () => void;
}
