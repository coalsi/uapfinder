/**
 * Normalises VITE_BASE_PATH into a Vite `base`: "" -> "/", "map" or "/map" ->
 * "/map/". Absolute http(s) URLs (a CDN) only gain a trailing slash. Used by
 * vite.config.ts, so it must not touch the DOM.
 */
export function normalizeBase(raw: string | undefined): string {
  const value = (raw ?? "").trim();
  if (value === "" || value === "/") return "/";
  if (/^https?:\/\//i.test(value)) return value.endsWith("/") ? value : `${value}/`;
  return `/${value}/`.replace(/\/{2,}/g, "/");
}

/** Directory (under the base) that CesiumJS's Workers, Assets, Widgets and ThirdParty are served from. */
export const CESIUM_STATIC_DIR = "cesiumStatic";
