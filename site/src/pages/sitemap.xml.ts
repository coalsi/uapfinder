import type { APIRoute } from "astro";
import { PAGES } from "../lib/pages";

export const GET: APIRoute = ({ site }) => {
  const origin = site ?? new URL("http://localhost:5180");
  const urls = PAGES.map(
    (p) =>
      `  <url>\n    <loc>${new URL(p.path, origin).href}</loc>\n    <changefreq>${p.changefreq}</changefreq>\n    <priority>${p.priority.toFixed(1)}</priority>\n  </url>`,
  ).join("\n");
  const body = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`;
  return new Response(body, { headers: { "Content-Type": "application/xml; charset=utf-8" } });
};
