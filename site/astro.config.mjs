// Astro configuration for the UAP Finder public website.
//
// The site is fully static (output: "static"): `npm run build` writes plain
// HTML/CSS/JS to dist/, which Vercel (or any static host) serves as-is. The
// live 3D map is a separate app that the deployment copies into dist/map/,
// so nothing here generates pages under /map/ or /api/.
import { defineConfig } from "astro/config";

/**
 * Absolute origin used for canonical URLs, OpenGraph images and the sitemap.
 * Order: PUBLIC_SITE_URL, then Vercel's production domain (a system env var
 * available at build time, without the scheme), then a local fallback.
 */
function resolveSite() {
  const explicit = process.env.PUBLIC_SITE_URL?.trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL?.trim();
  if (vercel) return `https://${vercel.replace(/^https?:\/\//, "").replace(/\/+$/, "")}`;
  return "http://localhost:5180";
}

/**
 * Path the site is served under: "/" (Vercel, any root host) or a
 * sub-path such as "/uapfinder/" for a GitHub Pages project site. Internal
 * links go through `sitePath()` in src/config.ts so they follow it.
 */
function resolveBase() {
  const base = process.env.PUBLIC_BASE_PATH?.trim();
  if (!base || base === "/") return "/";
  return `/${base.replace(/^\/+|\/+$/g, "")}/`;
}

export default defineConfig({
  site: resolveSite(),
  base: resolveBase(),
  output: "static",
  trailingSlash: "ignore",
  build: {
    format: "directory",
    inlineStylesheets: "auto",
  },
  // Astro 7 defaults to JSX whitespace rules ("jsx"), which drop the space
  // between a line-wrapped word and a following inline element such as a
  // link. This site is mostly prose, so keep HTML-aware, lossless compression.
  compressHTML: true,
  devToolbar: { enabled: false },
  server: { port: 5180, host: "127.0.0.1" },
  preview: { port: 5181, host: "127.0.0.1" },
});
