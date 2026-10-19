import { describe, expect, it } from "vitest";
import { Backoff } from "./backoff";
import { type ConnectionState, LiveFeed, RecentIds, type SocketLike, type Timers } from "./live";
import type { EventsPage, LiveEvent } from "./types";

/** Manually advanced clock for the feed's injected timers. */
class FakeTimers implements Timers {
  now = 0;
  private seq = 0;
  readonly pending = new Map<number, { at: number; ms: number; fn: () => void }>();

  setTimeout(fn: () => void, ms: number): unknown {
    const id = ++this.seq;
    this.pending.set(id, { at: this.now + ms, ms, fn });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.pending.delete(handle as number);
  }

  /** Delays (ms) of the timers currently scheduled. */
  delays(): number[] {
    return [...this.pending.values()].map((t) => t.ms).sort((a, b) => a - b);
  }

  /** Run every timer due within `ms`, in order, letting promises settle in between. */
  async advance(ms: number): Promise<void> {
    const end = this.now + ms;
    for (;;) {
      await settle();
      const next = [...this.pending.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      this.pending.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
    }
    this.now = end;
    await settle();
  }
}

/** Let pending promise callbacks run. */
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

class FakeSocket implements SocketLike {
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {}

  open() {
    this.onopen?.({});
  }
  send(event: LiveEvent | string) {
    this.onmessage?.({ data: typeof event === "string" ? event : JSON.stringify(event) });
  }
  drop() {
    this.onerror?.({});
    this.onclose?.({});
  }
  close() {
    this.closed = true;
  }
}

const ev = (id: string): LiveEvent => ({
  id,
  type: "node",
  t_ns: 0,
  data: {
    node_id: "n",
    label: null,
    lat: null,
    lon: null,
    alt_m: null,
    yaw_deg: null,
    pitch_deg: null,
    roll_deg: null,
    fov_h_deg: null,
    fov_v_deg: null,
    last_seen_ns: null,
    online: true,
    status: null,
    software_version: null,
  },
});

interface Harness {
  feed: LiveFeed;
  timers: FakeTimers;
  sockets: FakeSocket[];
  calls: (string | null)[];
  delivered: string[];
  states: ConnectionState[];
  /** Responses for successive GET /events calls; a function may throw or return a pending promise. */
  pages: (EventsPage | Error | (() => Promise<EventsPage>))[];
}

function harness(opts: { sockets?: boolean; pageLimit?: number } = {}): Harness {
  const timers = new FakeTimers();
  const h: Harness = {
    timers,
    sockets: [],
    calls: [],
    delivered: [],
    states: [],
    pages: [],
    feed: undefined as unknown as LiveFeed,
  };
  const client = {
    liveUrl: () => "wss://api.example/api/v1/live",
    getEvents: async (after: string | null): Promise<EventsPage> => {
      h.calls.push(after);
      const next = h.pages.shift() ?? { events: [], cursor: after };
      if (next instanceof Error) throw next;
      if (typeof next === "function") return next();
      return next;
    },
  };
  const noJitter = () => 0.5;
  h.feed = new LiveFeed({
    client,
    onEvents: (events) => h.delivered.push(...events.map((e) => e.id)),
    onState: (s) => h.states.push(s),
    createSocket:
      opts.sockets === false
        ? null
        : (url) => {
            const s = new FakeSocket(url);
            h.sockets.push(s);
            return s;
          },
    pollIntervalMs: 1000,
    pageLimit: opts.pageLimit ?? 500,
    reconnectBackoff: new Backoff({ initialMs: 1000, maxMs: 30_000, random: noJitter }),
    pollBackoff: new Backoff({ initialMs: 1000, maxMs: 30_000, random: noJitter }),
    timers,
  });
  return h;
}

describe("LiveFeed", () => {
  it("polls GET /events every second from the cursor while the WebSocket is not open", async () => {
    const h = harness();
    h.pages.push({ events: [ev("e1"), ev("e2")], cursor: "e2" }, { events: [ev("e3")], cursor: "e3" });
    h.feed.start("e0");
    expect(h.states).toEqual(["connecting"]);
    expect(h.sockets[0]?.url).toBe("wss://api.example/api/v1/live");

    await h.timers.advance(1000);
    expect(h.calls).toEqual(["e0"]);
    expect(h.delivered).toEqual(["e1", "e2"]);
    expect(h.states.at(-1)).toBe("polling");

    await h.timers.advance(1000);
    expect(h.calls).toEqual(["e0", "e2"]);
    expect(h.feed.lastCursor).toBe("e3");
  });

  it("switches to the WebSocket when it opens: stops polling and backfills the gap", async () => {
    const h = harness();
    h.pages.push({ events: [ev("e1")], cursor: "e1" });
    h.feed.start("e0");
    h.sockets[0]?.open();
    await h.timers.advance(0);
    expect(h.states).toEqual(["connecting", "live"]);
    expect(h.calls).toEqual(["e0"]); // the backfill
    expect(h.delivered).toEqual(["e1"]);

    await h.timers.advance(10_000);
    expect(h.calls).toEqual(["e0"]); // no polling while live
  });

  it("delivers pushed events once, advancing the cursor", async () => {
    const h = harness();
    h.feed.start("e0", ["e0"]);
    const socket = h.sockets[0] as FakeSocket;
    socket.open();
    await h.timers.advance(0);
    socket.send(ev("e1"));
    socket.send(ev("e1")); // duplicate
    socket.send(ev("e0")); // already known at start
    socket.send("not json");
    socket.send(JSON.stringify({ hello: "world" }));
    expect(h.delivered).toEqual(["e1"]);
    expect(h.feed.lastCursor).toBe("e1");
  });

  it("keeps the cursor contiguous while the backfill catches up", async () => {
    const h = harness({ pageLimit: 2 });
    let release: (page: EventsPage) => void = () => {};
    h.pages.push(
      () => new Promise<EventsPage>((resolve) => (release = resolve)),
      { events: [ev("e3")], cursor: "e3" },
    );
    h.feed.start("e0");
    const socket = h.sockets[0] as FakeSocket;
    socket.open();
    await settle();
    // Pushed while the (full) first backfill page is in flight: delivered, but
    // the cursor must not jump past the events still missing (e3).
    socket.send(ev("e9"));
    expect(h.feed.lastCursor).toBe("e0");
    release({ events: [ev("e1"), ev("e2")], cursor: "e2" });
    await settle();
    expect(h.calls).toEqual(["e0", "e2"]);
    expect(h.delivered).toEqual(["e9", "e1", "e2", "e3"]);
    expect(h.feed.lastCursor).toBe("e3");
    // Caught up: from now on the socket stream advances the cursor.
    socket.send(ev("e10"));
    expect(h.feed.lastCursor).toBe("e10");
  });

  it("retries a failed backfill while the socket stays open", async () => {
    const h = harness();
    h.pages.push(new Error("503"), { events: [ev("e1")], cursor: "e1" });
    h.feed.start("e0");
    (h.sockets[0] as FakeSocket).open();
    await h.timers.advance(0);
    expect(h.timers.delays()).toEqual([1000]);
    await h.timers.advance(1000);
    expect(h.calls).toEqual(["e0", "e0"]);
    expect(h.delivered).toEqual(["e1"]);
    expect(h.feed.lastCursor).toBe("e1");
  });

  it("a poll that straddles a socket open does not move the cursor", async () => {
    const h = harness();
    let release: (page: EventsPage) => void = () => {};
    h.pages.push(() => new Promise<EventsPage>((resolve) => (release = resolve)), { events: [], cursor: "e0" });
    h.feed.start("e0");
    await h.timers.advance(1000); // poll in flight
    (h.sockets[0] as FakeSocket).open(); // backfill from e0 (empty page)
    await settle();
    (h.sockets[0] as FakeSocket).send(ev("e5"));
    (h.sockets[0] as FakeSocket).drop(); // closed again before the old poll returns
    release({ events: [ev("e4")], cursor: "e4" });
    await settle();
    expect(h.feed.lastCursor).toBe("e5");
    expect(h.states.at(-1)).toBe("polling");
  });

  it("falls back to polling at once when the socket drops, and reconnects with exponential backoff", async () => {
    const h = harness();
    h.feed.start("e0");
    (h.sockets[0] as FakeSocket).open();
    await h.timers.advance(0);
    const callsWhileLive = h.calls.length;

    (h.sockets[0] as FakeSocket).drop();
    expect(h.states.at(-1)).toBe("polling");
    expect(h.timers.delays()).toEqual([0, 1000]); // immediate poll + first reconnect
    await h.timers.advance(0);
    expect(h.calls.length).toBe(callsWhileLive + 1);

    // Reconnect attempts that fail before opening back off: 1 s, 2 s, 4 s ...
    await h.timers.advance(1000);
    expect(h.sockets).toHaveLength(2);
    (h.sockets[1] as FakeSocket).drop();
    expect(h.timers.delays()).toContain(2000);
    await h.timers.advance(2000);
    expect(h.sockets).toHaveLength(3);
    (h.sockets[2] as FakeSocket).drop();
    expect(h.timers.delays()).toContain(4000);

    // A successful open resets the backoff.
    await h.timers.advance(4000);
    (h.sockets[3] as FakeSocket).open();
    await h.timers.advance(0);
    expect(h.states.at(-1)).toBe("live");
    (h.sockets[3] as FakeSocket).drop();
    expect(h.timers.delays()).toContain(1000);
  });

  it("reports offline and backs off while GET /events fails, then recovers", async () => {
    const h = harness({ sockets: false });
    h.pages.push(new Error("down"), new Error("down"), { events: [ev("e1")], cursor: "e1" });
    h.feed.start(null);
    await h.timers.advance(1000);
    expect(h.states.at(-1)).toBe("offline");
    expect(h.timers.delays()).toEqual([1000]);
    await h.timers.advance(1000);
    expect(h.timers.delays()).toEqual([2000]);
    await h.timers.advance(2000);
    expect(h.states.at(-1)).toBe("polling");
    expect(h.delivered).toEqual(["e1"]);
    expect(h.timers.delays()).toEqual([1000]);
  });

  it("keeps paging without delay while pages come back full", async () => {
    const h = harness({ sockets: false, pageLimit: 2 });
    h.pages.push({ events: [ev("e1"), ev("e2")], cursor: "e2" }, { events: [ev("e3")], cursor: "e3" });
    h.feed.start("e0");
    await h.timers.advance(1000);
    expect(h.calls).toEqual(["e0", "e2"]);
    expect(h.delivered).toEqual(["e1", "e2", "e3"]);
  });

  it("uses the last event id when a page has no cursor", async () => {
    const h = harness({ sockets: false });
    h.pages.push({ events: [ev("e7")], cursor: null });
    h.feed.start("e0");
    await h.timers.advance(1000);
    expect(h.feed.lastCursor).toBe("e7");
  });

  it("stop() closes the socket and cancels every timer", async () => {
    const h = harness();
    h.feed.start("e0");
    h.feed.stop();
    expect(h.sockets[0]?.closed).toBe(true);
    expect(h.timers.pending.size).toBe(0);
    await h.timers.advance(60_000);
    expect(h.calls).toEqual([]);
  });
});

describe("RecentIds", () => {
  it("remembers a bounded number of ids", () => {
    const ids = new RecentIds(3);
    expect(["a", "b", "c"].map((id) => ids.add(id))).toEqual([true, true, true]);
    expect(ids.add("a")).toBe(false);
    expect(ids.add("d")).toBe(true); // evicts "a"
    expect(ids.size).toBe(3);
    expect(ids.add("a")).toBe(true);
  });
});
