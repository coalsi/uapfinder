import type { Page } from "@playwright/test";
import { join } from "node:path";

export const SCREENSHOT_DIR = join(import.meta.dirname, "..", "..", "screenshots");

export const STATS_OK = {
  uptime_s: 3600.0,
  packets_total: 123456,
  packets_per_s: 812.4,
  rejected_total: { bad_signature: 3, stale: 10 },
  matches_total: 4567,
  matches_per_s: 31.0,
  active_nodes: 12,
  active_tracks: 2,
  processing_latency_ms: { p50: 0.08, p95: 0.2, p99: 0.4, max: 3.1 },
  backsignal_latency_ms: { p50: 0.3, p95: 0.6, p99: 1.1, max: 4.0 },
};

export const VOLUNTEERS_OK = { total: 42, cells: [{ lat: 33.5, lon: -112.5, count: 7 }] };

type Mode = "ok" | "down" | "503";

/** Answer the read-only endpoints used by the live-stats widget. */
export async function mockNetwork(page: Page, opts: { stats?: Mode; volunteers?: Mode } = {}): Promise<void> {
  const { stats = "down", volunteers = "down" } = opts;
  const answer = (mode: Mode, body: unknown) => async (route: import("@playwright/test").Route) => {
    if (mode === "ok") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (mode === "503") {
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "sign-up unavailable" }) });
    }
    return route.abort("connectionrefused");
  };
  await page.route("**/api/v1/stats", answer(stats, STATS_OK));
  await page.route("**/api/v1/volunteers/summary", answer(volunteers, VOLUNTEERS_OK));
}

/**
 * Collect console errors and uncaught exceptions. Failed requests to the
 * (mocked) API are expected in the degraded tests and are ignored, as is
 * the document's own status when `allowDocumentStatus` is set (404 page).
 */
export function collectErrors(page: Page, opts: { allowDocumentStatus?: boolean } = {}): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const url = m.location().url ?? "";
    if (/Failed to load resource/.test(m.text())) {
      if (url.includes("/api/v1/")) return;
      if (opts.allowDocumentStatus && url === page.url()) return;
    }
    errors.push(`console: ${m.text()} (${url})`);
  });
  return errors;
}
