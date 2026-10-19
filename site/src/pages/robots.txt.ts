import type { APIRoute } from "astro";
import { sitePath } from "../config";

export const GET: APIRoute = ({ site }) => {
  const origin = site ?? new URL("http://localhost:5180");
  const body = ["User-agent: *", "Allow: /", "", `Sitemap: ${new URL(sitePath("/sitemap.xml"), origin).href}`, ""].join(
    "\n",
  );
  return new Response(body, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
};
