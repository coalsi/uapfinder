// Serves the production build (dist/) in demo mode and saves desktop and
// mobile screenshots to screenshots/. Run `npm run build` first.
//
//   npm run screenshot
//
// Env: SCREENSHOT_PORT (default 5175), SCREENSHOT_IMAGERY (default "offline":
// the Natural Earth II imagery bundled with CesiumJS, so the result does not
// depend on tile-server access; use "osm" for the production default),
// CHROMIUM_PATH (a Chromium binary; default: Playwright's own).

import { existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { preview } from "vite";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "screenshots");
const port = Number(process.env.SCREENSHOT_PORT ?? 5175);
const imagery = process.env.SCREENSHOT_IMAGERY ?? "offline";
const settleMs = Number(process.env.SCREENSHOT_SETTLE_MS ?? 9000);

if (!existsSync(join(root, "dist", "index.html"))) {
  console.error("dist/ is missing: run `npm run build` first.");
  process.exit(1);
}
mkdirSync(outDir, { recursive: true });

const server = await preview({ root, preview: { port, strictPort: true, host: "127.0.0.1" } });
const url = `http://127.0.0.1:${port}/?demo=1&imagery=${imagery}`;

const browser = await chromium.launch({
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  // Software WebGL so the globe renders on machines without a GPU.
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});

const problems = [];

async function shoot(name, viewport, actions = async () => {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, colorScheme: "dark" });
  const page = await context.newPage();
  page.on("pageerror", (err) => problems.push(`${name}: page error: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") problems.push(`${name}: console: ${msg.text()}`);
  });
  await page.goto(url, { waitUntil: "load" });
  await page.locator(".cesium-widget canvas").waitFor({ timeout: 30_000 });
  await page.waitForTimeout(settleMs);
  await actions(page);
  const file = join(outDir, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`saved ${file}`);
  await context.close();
}

const desktop = { width: 1440, height: 900 };
const mobile = { width: 390, height: 844 };

try {
  await shoot("desktop-coverage", desktop);
  await shoot("desktop-track", desktop, async (page) => {
    await page.getByRole("tab", { name: /Tracks/ }).click();
    await page.locator(".tracks .row:has(.class-hypersonic)").first().click();
    await page.waitForTimeout(4000);
  });
  await shoot("mobile-overview", mobile);
  await shoot("mobile-tracks", mobile, async (page) => {
    await page.getByRole("tab", { name: /Tracks/ }).click();
    await page.waitForTimeout(1500);
  });
} finally {
  await browser.close();
  await new Promise((resolve) => server.httpServer.close(resolve));
}

if (problems.length > 0) {
  console.log(`\n${problems.length} browser error(s):\n  ${problems.join("\n  ")}`);
}
