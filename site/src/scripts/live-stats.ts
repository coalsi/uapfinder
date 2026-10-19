/**
 * Live network figures on the home page. Fetches /api/v1/stats and
 * /api/v1/volunteers/summary in parallel, renders them, and refreshes every
 * 30 s while the tab is visible. Any failure degrades to the "network
 * launching" message; nothing here ever throws into the page.
 */
import { buildStatsView, fetchJson, parseStats, parseVolunteerTotal, type StatsViewModel } from "../lib/stats";
import { apiUrl } from "../lib/urls";

const REFRESH_MS = 30_000;

function render(root: HTMLElement, view: StatsViewModel): void {
  root.dataset.state = view.state;
  for (const tile of view.tiles) {
    const el = root.querySelector<HTMLElement>(`[data-tile="${tile.key}"]`);
    if (!el) continue;
    const value = el.querySelector<HTMLElement>("[data-value]");
    const detail = el.querySelector<HTMLElement>("[data-detail]");
    if (value && value.textContent !== tile.value) value.textContent = tile.value;
    if (detail) detail.textContent = tile.detail;
    el.title = tile.detail;
  }
  const note = root.querySelector<HTMLElement>("[data-live-note]");
  // Only touch the live region when the message changes, so screen readers
  // are not interrupted on every refresh.
  if (note && note.textContent !== view.note) note.textContent = view.note;
}

export async function refresh(root: HTMLElement): Promise<StatsViewModel> {
  const base = root.dataset.apiBase ?? "";
  const [stats, volunteers] = await Promise.all([
    fetchJson(apiUrl(base, "/api/v1/stats")),
    fetchJson(apiUrl(base, "/api/v1/volunteers/summary")),
  ]);
  const view = buildStatsView(parseStats(stats), parseVolunteerTotal(volunteers));
  render(root, view);
  return view;
}

export function initLiveStats(): void {
  const root = document.querySelector<HTMLElement>("[data-live-stats]");
  if (!root) return;
  let timer: number | undefined;
  const schedule = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(tick, REFRESH_MS);
  };
  const tick = async () => {
    if (document.visibilityState === "visible") await refresh(root);
    schedule();
  };
  void refresh(root).finally(schedule);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      window.clearTimeout(timer);
      void tick();
    }
  });
}
