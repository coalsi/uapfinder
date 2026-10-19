import { Backoff } from "./backoff";
import type { ApiClient } from "./client";
import type { EventsPage, LiveEvent } from "./types";

/**
 * `live`: WebSocket open. `polling`: WebSocket unavailable, `GET /events`
 * succeeding. `offline`: polling is failing too. `connecting`: not yet known.
 */
export type ConnectionState = "connecting" | "live" | "polling" | "offline";

/** The subset of the browser WebSocket the feed uses (so tests can fake it). */
export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(): void;
}

export type SocketFactory = (url: string) => SocketLike;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface LiveFeedOptions {
  client: Pick<ApiClient, "getEvents" | "liveUrl">;
  onEvents: (events: LiveEvent[]) => void;
  onState: (state: ConnectionState) => void;
  /** null disables WebSockets entirely (pure polling). */
  createSocket?: SocketFactory | null;
  pollIntervalMs?: number;
  pageLimit?: number;
  reconnectBackoff?: Backoff;
  pollBackoff?: Backoff;
  timers?: Timers;
}

const defaultTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export const browserSocketFactory: SocketFactory | null =
  typeof WebSocket === "undefined" ? null : (url) => new WebSocket(url) as unknown as SocketLike;

/** Bounded memory of recently delivered event ids; ids are opaque, so duplicates are detected by identity. */
export class RecentIds {
  private readonly order: string[] = [];
  private readonly set = new Set<string>();

  constructor(private readonly capacity: number) {}

  /** Records `id`; returns false if it was already present. */
  add(id: string): boolean {
    if (this.set.has(id)) return false;
    this.set.add(id);
    this.order.push(id);
    if (this.order.length > this.capacity) this.set.delete(this.order.shift() as string);
    return true;
  }

  get size(): number {
    return this.set.size;
  }
}

function isLiveEvent(value: unknown): value is LiveEvent {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return typeof v.id === "string" && typeof v.type === "string" && typeof v.data === "object" && v.data !== null;
}

/**
 * Live event feed: WebSocket push with exponential-backoff reconnect, gap
 * backfill from `GET /events?after=<cursor>` whenever the socket (re)opens, and
 * `GET /events` polling whenever the socket is down. On Vercel the socket may
 * never open or may be closed when the function times out; polling covers it.
 */
export class LiveFeed {
  private readonly client: LiveFeedOptions["client"];
  private readonly onEvents: LiveFeedOptions["onEvents"];
  private readonly onState: LiveFeedOptions["onState"];
  private readonly createSocket: SocketFactory | null;
  private readonly pollIntervalMs: number;
  private readonly pageLimit: number;
  private readonly reconnectBackoff: Backoff;
  private readonly pollBackoff: Backoff;
  private readonly timers: Timers;
  private readonly seen = new RecentIds(5000);

  private cursor: string | null = null;
  private stopped = true;
  private state: ConnectionState | null = null;
  private socket: SocketLike | null = null;
  private socketOpen = false;
  /** Incremented per WebSocket event, so a slower HTTP page cannot move the cursor backwards. */
  private socketEpoch = 0;
  private polling = false;
  private pollInFlight = false;
  private pollTimer: unknown = null;
  private reconnectTimer: unknown = null;

  constructor(options: LiveFeedOptions) {
    this.client = options.client;
    this.onEvents = options.onEvents;
    this.onState = options.onState;
    this.createSocket = options.createSocket === undefined ? browserSocketFactory : options.createSocket;
    this.pollIntervalMs = options.pollIntervalMs ?? 1000;
    this.pageLimit = options.pageLimit ?? 500;
    this.reconnectBackoff = options.reconnectBackoff ?? new Backoff({ initialMs: 1000, maxMs: 30_000 });
    this.pollBackoff = options.pollBackoff ?? new Backoff({ initialMs: 1000, maxMs: 30_000 });
    this.timers = options.timers ?? defaultTimers;
  }

  /** The cursor the next request would resume from. */
  get lastCursor(): string | null {
    return this.cursor;
  }

  /** Start from `cursor`; `knownIds` are events the caller already applied. */
  start(cursor: string | null, knownIds: Iterable<string> = []): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.cursor = cursor;
    for (const id of knownIds) this.seen.add(id);
    this.setState("connecting");
    this.openSocket();
    // If the socket opens first this poll is cancelled; if it never opens
    // (e.g. Vercel), polling is already running.
    this.startPolling(this.pollIntervalMs);
  }

  stop(): void {
    this.stopped = true;
    this.stopPolling();
    if (this.reconnectTimer !== null) this.timers.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const socket = this.socket;
    this.socket = null;
    this.socketOpen = false;
    socket?.close();
  }

  private setState(state: ConnectionState): void {
    if (state === this.state) return;
    this.state = state;
    this.onState(state);
  }

  private deliver(events: LiveEvent[]): void {
    const fresh = events.filter((e) => this.seen.add(e.id));
    if (fresh.length > 0) this.onEvents(fresh);
  }

  // --- WebSocket -----------------------------------------------------------

  private openSocket(): void {
    this.reconnectTimer = null;
    if (this.stopped || !this.createSocket) return;
    let socket: SocketLike;
    try {
      socket = this.createSocket(this.client.liveUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.socketOpen = true;
      this.reconnectBackoff.reset();
      this.stopPolling();
      this.setState("live");
      void this.backfill();
    };
    socket.onmessage = (ev) => {
      if (this.socket === socket) this.handleMessage(ev.data);
    };
    socket.onerror = () => {
      // A close event always follows; reconnect is handled there.
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.socketOpen = false;
      if (this.stopped) return;
      if (this.state === "live") this.setState("polling");
      this.startPolling(0);
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== null) return;
    this.reconnectTimer = this.timers.setTimeout(() => this.openSocket(), this.reconnectBackoff.next());
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return;
    }
    if (!isLiveEvent(parsed)) return;
    this.socketEpoch += 1;
    this.cursor = parsed.id;
    this.deliver([parsed]);
  }

  /** Fetch everything after the cursor that the socket did not deliver. */
  private async backfill(): Promise<void> {
    try {
      while (this.socketOpen && !this.stopped) {
        const count = await this.fetchPage();
        if (count < this.pageLimit) return;
      }
    } catch {
      // The socket is live; the next reconnect backfills again.
    }
  }

  // --- HTTP polling --------------------------------------------------------

  private async fetchPage(): Promise<number> {
    const epoch = this.socketEpoch;
    const page: EventsPage = await this.client.getEvents(this.cursor, this.pageLimit);
    if (this.stopped) return 0;
    this.deliver(page.events);
    const next = page.cursor ?? page.events.at(-1)?.id ?? null;
    if (next !== null && epoch === this.socketEpoch) this.cursor = next;
    return page.events.length;
  }

  private startPolling(delayMs: number): void {
    if (this.polling || this.stopped) return;
    this.polling = true;
    this.schedulePoll(delayMs);
  }

  private stopPolling(): void {
    this.polling = false;
    if (this.pollTimer !== null) this.timers.clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  private schedulePoll(delayMs: number): void {
    if (!this.polling || this.stopped || this.pollTimer !== null || this.pollInFlight) return;
    this.pollTimer = this.timers.setTimeout(() => {
      this.pollTimer = null;
      void this.poll();
    }, delayMs);
  }

  private async poll(): Promise<void> {
    if (!this.polling || this.stopped) return;
    this.pollInFlight = true;
    let delay = this.pollIntervalMs;
    try {
      const count = await this.fetchPage();
      this.pollBackoff.reset();
      if (!this.socketOpen) this.setState("polling");
      if (count >= this.pageLimit) delay = 0;
    } catch {
      if (!this.socketOpen) this.setState("offline");
      delay = this.pollBackoff.next();
    } finally {
      this.pollInFlight = false;
    }
    this.schedulePoll(delay);
  }
}
