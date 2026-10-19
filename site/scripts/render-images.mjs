// Renders the raster images the site needs from SVG/HTML drawn here:
//   public/og.png               1200x630 OpenGraph / social card
//   public/apple-touch-icon.png 180x180
//   public/favicon-32.png       32x32 fallback for browsers without SVG favicons
//
//   npm run images
//
// The outputs are committed, so this only needs re-running when the artwork
// changes. Uses Playwright's Chromium (CHROMIUM_PATH overrides the binary).
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pub = join(root, "public");
const favicon = readFileSync(join(pub, "favicon.svg"), "utf8");

function stars(n, w, h, seed) {
  let s = seed >>> 0;
  const r = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  return Array.from({ length: n }, () => {
    const x = (r() * w).toFixed(1);
    const y = (r() * h).toFixed(1);
    const rad = (0.5 + r() ** 3 * 1.6).toFixed(2);
    const o = (0.25 + r() * 0.6).toFixed(2);
    return `<circle cx="${x}" cy="${y}" r="${rad}" fill="#dbe6f7" opacity="${o}"/>`;
  }).join("");
}

const og = `<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;width:1200px;height:630px;background:#060a13;overflow:hidden}
  body{font-family:system-ui,"Segoe UI",Inter,"Helvetica Neue",Arial,sans-serif;color:#e8edf6}
  svg.bg{position:absolute;inset:0}
  .text{position:absolute;left:72px;top:76px;width:640px}
  .brand{display:flex;align-items:center;gap:16px;font-size:34px;font-weight:700;letter-spacing:-0.01em}
  .brand svg{width:56px;height:56px}
  h1{font-size:64px;line-height:1.05;letter-spacing:-0.03em;margin:44px 0 22px;font-weight:700}
  p{font-size:27px;line-height:1.4;color:#b6c2d6;margin:0}
  .tag{position:absolute;left:72px;bottom:64px;font:600 18px ui-monospace,"DejaVu Sans Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:#ffd27a}
</style></head><body>
<svg class="bg" width="1200" height="630" viewBox="0 0 1200 630">
  <defs>
    <radialGradient id="g"><stop offset="0" stop-color="#ffd27a" stop-opacity=".55"/><stop offset="1" stop-color="#ffd27a" stop-opacity="0"/></radialGradient>
    <radialGradient id="sky" cx=".8" cy="0" r="1"><stop offset="0" stop-color="#5cb8ff" stop-opacity=".16"/><stop offset=".6" stop-color="#5cb8ff" stop-opacity="0"/></radialGradient>
    <linearGradient id="hz" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5cb8ff" stop-opacity="0"/><stop offset="1" stop-color="#5cb8ff" stop-opacity=".18"/></linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#sky)"/>
  <rect y="380" width="1200" height="200" fill="url(#hz)"/>
  ${stars(120, 1200, 540, 7)}
  <path d="M790 566 L960 190" stroke="#8fd0ff" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M1130 568 L968 190" stroke="#8fd0ff" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M790 566 L947 186 L973 199 Z" fill="#8fd0ff" fill-opacity=".12"/>
  <path d="M1130 568 L952 197 L984 183 Z" fill="#8fd0ff" fill-opacity=".12"/>
  <circle cx="964" cy="190" r="48" fill="url(#g)"/>
  <circle cx="964" cy="190" r="18" fill="none" stroke="#ffd27a" stroke-opacity=".5" stroke-width="1.6"/>
  <circle cx="964" cy="190" r="6" fill="#ffd27a"/>
  <path d="M0 580 Q600 534 1200 580 L1200 630 L0 630 Z" fill="#0a111e"/>
  <path d="M0 580 Q600 534 1200 580" fill="none" stroke="#2a3958" stroke-width="2"/>
  <circle cx="790" cy="568" r="8" fill="#8fd0ff"/>
  <circle cx="1130" cy="570" r="8" fill="#8fd0ff"/>
</svg>
<div class="text">
  <div class="brand">${favicon.replace("<svg ", '<svg width="56" height="56" ')}<span>UAP Finder</span></div>
  <h1>Triangulating unidentified aerial objects</h1>
  <p>A citizen-science network of passive sky cameras measuring position, altitude and speed, with error bars.</p>
</div>
<div class="tag">Open source · Measurement, not claims</div>
</body></html>`;

const icon = (size) => `<!doctype html><html><head><style>
  html,body{margin:0;width:${size}px;height:${size}px;background:transparent;overflow:hidden}
  svg{display:block;width:${size}px;height:${size}px}
</style></head><body>${favicon}</body></html>`;

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
try {
  const jobs = [
    { file: "og.png", html: og, w: 1200, h: 630, omitBackground: false },
    { file: "apple-touch-icon.png", html: icon(180), w: 180, h: 180, omitBackground: false },
    { file: "favicon-32.png", html: icon(32), w: 32, h: 32, omitBackground: true },
  ];
  for (const job of jobs) {
    const page = await browser.newPage({ viewport: { width: job.w, height: job.h }, deviceScaleFactor: 1 });
    await page.setContent(job.html, { waitUntil: "load" });
    const png = await page.screenshot({ type: "png", omitBackground: job.omitBackground });
    writeFileSync(join(pub, job.file), png);
    console.log(`wrote public/${job.file} (${png.length} bytes)`);
    await page.close();
  }
} finally {
  await browser.close();
}
