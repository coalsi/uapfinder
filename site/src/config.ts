/**
 * Build-time site configuration from PUBLIC_* environment variables
 * (see .env.example and README.md). Vite inlines these at build time, so a
 * change needs a rebuild.
 */
import { trimTrailingSlashes, withQuery } from "./lib/urls";

const env = import.meta.env;

function clean(value: string | undefined): string | undefined {
  const v = value?.trim();
  return v ? v : undefined;
}

export const SITE_NAME = "UAP Finder";

/** Source repository, without a trailing slash. */
export const REPO_URL = trimTrailingSlashes(clean(env.PUBLIC_REPO_URL) ?? "https://github.com/coalsi/uapfinder");

/** Contact address, or null to hide every contact-email element. */
export const CONTACT_EMAIL: string | null = clean(env.PUBLIC_CONTACT_EMAIL) ?? null;

/** API origin; "" means same origin as the site. */
export const API_BASE = trimTrailingSlashes(clean(env.PUBLIC_API_BASE) ?? "");

/** Live 3D map (another app, deployed at /map/ by default). */
export const MAP_URL = clean(env.PUBLIC_MAP_URL) ?? "/map/";

export const COVERAGE_MAP_URL = withQuery(MAP_URL, { layer: "coverage" });

export const links = {
  repo: REPO_URL,
  edgeReadme: `${REPO_URL}/tree/main/edge`,
  serverCode: `${REPO_URL}/tree/main/server`,
  dashboardCode: `${REPO_URL}/tree/main/dashboard`,
  siteCode: `${REPO_URL}/tree/main/site`,
  mathDoc: `${REPO_URL}/blob/main/docs/MATH.md`,
  protocolDoc: `${REPO_URL}/blob/main/docs/PROTOCOL.md`,
  apiDoc: `${REPO_URL}/blob/main/docs/API.md`,
  issues: `${REPO_URL}/issues`,
  map: MAP_URL,
  coverageMap: COVERAGE_MAP_URL,
} as const;

/** "Prices as of" month for the hardware list (update with the prices). */
export const PRICES_AS_OF = "October 2026";
