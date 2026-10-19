/** URL helpers shared by build-time config and client scripts. */

/** Strip trailing slashes, keeping a bare "/" intact. */
export function trimTrailingSlashes(url: string): string {
  const trimmed = url.replace(/\/+$/, "");
  return trimmed === "" && url.startsWith("/") ? "/" : trimmed;
}

/**
 * Append query parameters to an absolute or root-relative URL without
 * disturbing an existing query string or fragment.
 */
export function withQuery(url: string, params: Record<string, string>): string {
  const [beforeHash = "", hash] = url.split("#", 2);
  const query = new URLSearchParams(params).toString();
  if (!query) return url;
  const sep = beforeHash.includes("?") ? (beforeHash.endsWith("?") || beforeHash.endsWith("&") ? "" : "&") : "?";
  return `${beforeHash}${sep}${query}${hash !== undefined ? `#${hash}` : ""}`;
}

/**
 * Put a root-relative site path ("/host/") under the base path the site is
 * deployed at: "/" on Vercel, "/uapfinder/" for a GitHub Pages project site.
 */
export function joinBase(base: string, path: string): string {
  const b = base.trim().replace(/\/+$/, "");
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${b}${p}`;
}

/**
 * Join the API base (empty = same origin) with an API path such as
 * "/api/v1/stats".
 */
export function apiUrl(base: string, path: string): string {
  const b = trimTrailingSlashes(base.trim());
  const p = path.startsWith("/") ? path : `/${path}`;
  return b === "" || b === "/" ? p : `${b}${p}`;
}
