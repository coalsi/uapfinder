import { resolveApiBase } from "./api/client";

export type Imagery =
  | { kind: "ion" }
  | { kind: "tiles"; url: string; credit: string }
  /** Natural Earth II, bundled with CesiumJS: works with no network access at all. */
  | { kind: "offline" };

export interface AppConfig {
  apiBase: string;
  demo: boolean;
  ionToken: string | null;
  imagery: Imagery;
}

const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_CREDIT =
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

/**
 * Build-time settings come from VITE_* variables; `?demo=1|0` and
 * `?imagery=offline|osm` override them per page load.
 */
export function readConfig(env: ImportMetaEnv, pageHref: string): AppConfig {
  const params = new URL(pageHref).searchParams;
  const flag = (value: string | null | undefined) => value === "1" || value === "true";
  const demo = params.has("demo") ? flag(params.get("demo")) : flag(env.VITE_DEMO);
  const ionToken = env.VITE_CESIUM_ION_TOKEN?.trim() || null;

  const tiles: Imagery = {
    kind: "tiles",
    url: env.VITE_IMAGERY_URL?.trim() || OSM_TILES,
    credit: env.VITE_IMAGERY_CREDIT?.trim() || OSM_CREDIT,
  };
  const requested = params.get("imagery");
  let imagery: Imagery = ionToken ? { kind: "ion" } : tiles;
  if (requested === "offline") imagery = { kind: "offline" };
  else if (requested === "osm") imagery = { kind: "tiles", url: OSM_TILES, credit: OSM_CREDIT };

  return { apiBase: resolveApiBase(env.VITE_API_BASE, pageHref), demo, ionToken, imagery };
}
