import { Backoff } from "../api/backoff";
import type { ApiClient } from "../api/client";
import { LiveFeed, type SocketFactory, browserSocketFactory } from "../api/live";
import type { Backend, FeedHandlers } from "./backend";

/** Recent events fetched at start-up: they give the cursor and recent track points. */
const BOOTSTRAP_EVENTS = 500;
const SNAPSHOT_TRACKS = 100;
/**
 * GET /nodes is re-read this often: a node that stops sending goes offline on
 * the server without necessarily producing a `node` event.
 */
export const NODE_REFRESH_MS = 30_000;

export function createApiBackend(client: ApiClient, createSocket: SocketFactory | null = browserSocketFactory): Backend {
  return {
    kind: "api",
    getTrack: (id) => client.getTrack(id),
    getStats: () => client.getStats(),
    getCoverage: (query, signal) => client.getCoverage(query, signal),
    link: (url) => client.link(url),
    connect(handlers: FeedHandlers) {
      const feed = new LiveFeed({
        client,
        createSocket,
        onEvents: (events) => handlers.onEvents(events, true),
        onState: handlers.onState,
      });
      let cancelled = false;
      let retryTimer: ReturnType<typeof setTimeout> | undefined;
      let nodeTimer: ReturnType<typeof setInterval> | undefined;
      const backoff = new Backoff();

      const refreshNodes = async () => {
        try {
          const nodes = await client.getNodes();
          if (!cancelled) handlers.onSnapshot(nodes, []);
        } catch {
          // The next refresh (or the live feed) catches up.
        }
      };

      // The event cursor is taken *before* the snapshot, so nothing that
      // happens in between is lost; replays are idempotent in the reducer.
      const bootstrap = async () => {
        try {
          const recent = await client.getEvents(null, BOOTSTRAP_EVENTS);
          const [nodes, tracks] = await Promise.all([
            client.getNodes(),
            client.getTracks({ limit: SNAPSHOT_TRACKS }),
          ]);
          if (cancelled) return;
          handlers.onEvents(recent.events, false);
          handlers.onSnapshot(nodes, tracks);
          feed.start(
            recent.cursor ?? recent.events.at(-1)?.id ?? null,
            recent.events.map((e) => e.id),
          );
          nodeTimer = setInterval(() => void refreshNodes(), NODE_REFRESH_MS);
        } catch {
          if (cancelled) return;
          handlers.onState("offline");
          retryTimer = setTimeout(() => void bootstrap(), backoff.next());
        }
      };

      handlers.onState("connecting");
      void bootstrap();
      return () => {
        cancelled = true;
        clearTimeout(retryTimer);
        clearInterval(nodeTimer);
        feed.stop();
      };
    },
  };
}
