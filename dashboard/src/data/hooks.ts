import { useCallback, useEffect, useRef, useState } from "react";
import type { ConnectionState } from "../api/live";
import type { Coverage, CoverageQuery, LiveEvent, Stats } from "../api/types";
import { type GeoRect, coverageQueryForView, coverageQueryKey } from "../geo/coverageQuery";
import type { Store } from "../store/store";
import type { Backend } from "./backend";

/** Live events are applied in batches so a burst of triangulations costs one render. */
const EVENT_BATCH_MS = 150;

/** Connect the backend's snapshot + live feed to the store; returns the connection state. */
export function useLiveFeed(backend: Backend, store: Store): ConnectionState | "demo" {
  const [state, setState] = useState<ConnectionState | "demo">("connecting");
  useEffect(() => {
    let queue: LiveEvent[] = [];
    let timer: ReturnType<typeof setTimeout> | undefined;
    const flush = () => {
      timer = undefined;
      const events = queue;
      queue = [];
      if (events.length > 0) store.dispatch({ type: "events", events, live: true, receivedAt: Date.now() });
    };
    const stop = backend.connect({
      onSnapshot: (nodes, tracks) => store.dispatch({ type: "snapshot", nodes, tracks }),
      onEvents: (events, live) => {
        if (!live) {
          store.dispatch({ type: "events", events, live: false, receivedAt: Date.now() });
          return;
        }
        queue.push(...events);
        timer ??= setTimeout(flush, EVENT_BATCH_MS);
      },
      onState: setState,
    });
    return () => {
      stop();
      clearTimeout(timer);
    };
  }, [backend, store]);
  return state;
}

export interface Polled<T> {
  data: T | null;
  error: string | null;
}

/** Poll `fetcher` every `intervalMs`, keeping the last good value on errors. */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): Polled<T> {
  const [result, setResult] = useState<Polled<T>>({ data: null, error: null });
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const data = await fetcher();
        if (!cancelled) setResult({ data, error: null });
      } catch (e) {
        if (!cancelled) setResult((prev) => ({ data: prev.data, error: e instanceof Error ? e.message : String(e) }));
      }
      if (!cancelled) timer = setTimeout(() => void tick(), intervalMs);
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [fetcher, intervalMs]);
  return result;
}

export function useStats(backend: Backend): Polled<Stats> {
  const fetcher = useCallback(() => backend.getStats(), [backend]);
  return usePolling(fetcher, 2000);
}

export interface CoverageResult {
  coverage: Coverage | null;
  query: CoverageQuery | null;
  loading: boolean;
  error: string | null;
}

const COVERAGE_DEBOUNCE_MS = 450;
/** Coverage changes as nodes come and go, so the visible area is re-read this often. */
const COVERAGE_REFRESH_MS = 60_000;

/**
 * Coverage for the visible area, refetched (debounced) when the camera
 * settles or the altitude slice changes, and refreshed every minute.
 */
export function useCoverage(backend: Backend, view: GeoRect | null, refAltM: number, enabled: boolean): CoverageResult {
  const [result, setResult] = useState<CoverageResult>({ coverage: null, query: null, loading: false, error: null });
  const [epoch, setEpoch] = useState(0);
  const lastKey = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => setEpoch((e) => e + 1), COVERAGE_REFRESH_MS);
    return () => clearInterval(timer);
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !view) return;
    const query = coverageQueryForView(view, refAltM);
    const key = `${coverageQueryKey(query)}#${epoch}`;
    if (key === lastKey.current) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setResult((prev) => ({ ...prev, loading: true }));
      try {
        const coverage = await backend.getCoverage(query, controller.signal);
        lastKey.current = key;
        setResult({ coverage, query, loading: false, error: null });
      } catch (e) {
        const error = controller.signal.aborted ? null : e instanceof Error ? e.message : String(e);
        setResult((prev) => ({ ...prev, loading: false, error }));
      }
    }, COVERAGE_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [backend, view, refAltM, enabled, epoch]);

  return result;
}
