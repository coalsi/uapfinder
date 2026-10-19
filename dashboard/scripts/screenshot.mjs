// Builds the dashboard twice (base "/" and base "/map/"), serves each the way
// a static host would (the /map/ build is copied into map/ under a stand-in
// public site), opens them in demo mode in headless Chromium and saves
// desktop and mobile screenshots to screenshots/. Fails if a page throws, a
// same-origin request fails, CesiumJS's static files are not loaded from
// under the base path, or the OpenStreetMap attribution is not shown.
//
//   npm run screenshot
//
// Env: SCREENSHOT_PORT (default 5175; 5175 and 5176 are used),
// SCREENSHOT_SETTLE_MS (default 9000), CHROMIUM_PATH (a Chromium binary;
// default: Playwright's own, else the newest under PLAYWRIGHT_BROWSERS_PATH).
//
// Screenshots use the Natural Earth II imagery bundled with CesiumJS
// (?imagery=offline), so they do not depend on tile-server access. The
// OpenStreetMap layer (the production default) is checked separately with
// tile requests answered locally.

import { createReadStream, existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";
import { chromium } from "playwright";
import { build } from "vite";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "screenshots");
const basePort = Number(process.env.SCREENSHOT_PORT ?? 5175);
const settleMs = Number(process.env.SCREENSHOT_SETTLE_MS ?? 9000);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".xml": "application/xml; charset=utf-8",
};

/** Minimal static host: index.html for directories, 301 to add a trailing slash, 404 otherwise. */
function serve(dir, port) {
  const server = createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url ?? "/", "http://x").pathname);
    let file = normalize(join(dir, pathname));
    if (file !== dir && !file.startsWith(dir + sep)) {
      res.writeHead(403).end();
      return;
    }
    if (existsSync(file) && statSync(file).isDirectory()) {
      if (!pathname.endsWith("/")) {
        res.writeHead(301, { Location: `${pathname}/` }).end();
        return;
      }
      file = join(file, "index.html");
    }
    if (!existsSync(file) || !statSync(file).isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain" }).end("not found");
      return;
    }
    res.writeHead(200, { "Content-Type": MIME[extname(file).toLowerCase()] ?? "application/octet-stream" });
    createReadStream(file).pipe(res);
  });
  return new Promise((resolveListen) => server.listen(port, "127.0.0.1", () => resolveListen(server)));
}

/** A solid 256x256 PNG, standing in for OpenStreetMap tiles (blocked or rate-limited in CI). */
function solidPng(r, g, b) {
  const size = 256;
  const row = Buffer.alloc(1 + size * 3);
  for (let x = 0; x < size; x++) row.set([r, g, b], 1 + x * 3);
  const raw = Buffer.concat(Array.from({ length: size }, () => row));
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr.set([8, 2, 0, 0, 0], 8); // 8-bit RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Playwright's own Chromium if installed, else the newest one in the browsers directory. */
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  try {
    if (existsSync(chromium.executablePath())) return undefined;
  } catch {
    // Fall through to the search below.
  }
  const dirs = [process.env.PLAYWRIGHT_BROWSERS_PATH, "/opt/pw-browsers", join(process.env.HOME ?? "", ".cache", "ms-playwright")];
  for (const dir of dirs) {
    if (!dir || !existsSync(dir)) continue;
    const revisions = readdirSync(dir)
      .filter((d) => /^chromium-\d+$/.test(d))
      .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
    for (const rev of revisions) {
      for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
        const candidate = join(dir, rev, sub);
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  return undefined;
}

// --- build both variants --------------------------------------------------

const work = mkdtempSync(join(tmpdir(), "uapfinder-shots-"));
const rootSite = join(work, "root");
const mapSite = join(work, "site");
const quiet = { logLevel: "warn", configFile: join(root, "vite.config.ts"), root };
await build({ ...quiet, base: "/", build: { outDir: rootSite, emptyOutDir: true } });
await build({ ...quiet, base: "/map/", build: { outDir: join(mapSite, "map"), emptyOutDir: true } });
// Stand-in for the public website that the combined deploy serves at "/".
writeFileSync(join(mapSite, "index.html"), "<!doctype html><title>UAP Finder</title><p>Public site stand-in</p>");

const servers = [await serve(rootSite, basePort), await serve(mapSite, basePort + 1)];
const variants = {
  root: { origin: `http://127.0.0.1:${basePort}`, base: "/" },
  map: { origin: `http://127.0.0.1:${basePort + 1}`, base: "/map/" },
};

const executablePath = chromiumPath();
const browser = await chromium.launch({
  ...(executablePath ? { executablePath } : {}),
  // Software WebGL so the globe renders on machines without a GPU.
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});

const problems = [];
mkdirSync(outDir, { recursive: true });

async function open(name, variant, viewport, query) {
  const { origin, base } = variants[variant];
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, colorScheme: "dark" });
  const page = await context.newPage();
  const stats = { cesiumStatic: 0, wrongBase: 0 };
  page.on("pageerror", (err) => problems.push(`${name}: page error: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") problems.push(`${name}: console error: ${msg.text()}`);
  });
  page.on("response", (res) => {
    const url = new URL(res.url());
    if (url.origin !== origin) return;
    if (res.status() >= 400) problems.push(`${name}: HTTP ${res.status()} for ${url.pathname}`);
    if (url.pathname.startsWith(`${base}cesiumStatic/`)) stats.cesiumStatic += 1;
    else if (url.pathname.includes("cesiumStatic/")) stats.wrongBase += 1;
  });
  await page.goto(`${origin}${base}?${query}`, { waitUntil: "load" });
  await page.locator(".cesium-widget canvas").waitFor({ timeout: 30_000 });
  await page.waitForTimeout(settleMs);
  const brand = await page.locator(".brand-name").textContent();
  if (brand !== "UAP Finder") problems.push(`${name}: header brand is ${JSON.stringify(brand)}`);
  if (!(await page.getByText("DEMO DATA", { exact: true }).isVisible())) problems.push(`${name}: DEMO DATA badge not visible`);
  return { page, context, stats, base };
}

function checkCesiumBase(name, { stats, base }) {
  if (stats.cesiumStatic === 0) problems.push(`${name}: no CesiumJS static file was loaded from ${base}cesiumStatic/`);
  if (stats.wrongBase > 0) problems.push(`${name}: ${stats.wrongBase} CesiumJS file(s) requested outside ${base}`);
}

async function shoot(name, variant, viewport, actions = async () => {}) {
  const session = await open(name, variant, viewport, "demo=1&imagery=offline");
  await actions(session.page);
  checkCesiumBase(name, session);
  const file = join(outDir, `${name}.png`);
  await session.page.screenshot({ path: file });
  console.log(`saved ${file} (${variants[variant].base})`);
  await session.context.close();
}

const desktop = { width: 1440, height: 900 };
const mobile = { width: 390, height: 844 };

try {
  await shoot("desktop-coverage", "root", desktop);
  await shoot("mobile-overview", "root", mobile);
  await shoot("desktop-track", "map", desktop, async (page) => {
    await page.getByRole("tab", { name: /Tracks/ }).click();
    await page.locator(".tracks .row:has(.class-hypersonic)").first().click();
    await page.waitForTimeout(4000);
  });
  await shoot("mobile-tracks", "map", mobile, async (page) => {
    await page.getByRole("tab", { name: /Tracks/ }).click();
    await page.waitForTimeout(1500);
  });

  // Production default imagery: OpenStreetMap tiles (answered locally) and their attribution.
  {
    const name = "osm-attribution";
    const tile = solidPng(38, 44, 56);
    const context = await browser.newContext({ viewport: mobile, colorScheme: "dark" });
    const page = await context.newPage();
    let tiles = 0;
    await context.route("https://tile.openstreetmap.org/**", (route) => {
      tiles += 1;
      return route.fulfill({ status: 200, contentType: "image/png", body: tile });
    });
    page.on("pageerror", (err) => problems.push(`${name}: page error: ${err.message}`));
    // As linked from the website's "Where nodes are needed" button.
    await page.goto(`${variants.map.origin}/map/?demo=1&layer=coverage`, { waitUntil: "load" });
    await page.waitForTimeout(settleMs);
    const credits = (await page.locator(".cesium-widget-credits").innerText()).replace(/\s+/g, " ");
    if (tiles === 0) problems.push(`${name}: no OpenStreetMap tile was requested`);
    if (!/OpenStreetMap/.test(credits)) problems.push(`${name}: OpenStreetMap attribution not on screen (credits: "${credits}")`);
    else console.log(`checked ${name}: ${tiles} OSM tiles requested, on-screen credit "${credits.trim()}"`);
    const about = await page.locator(".about-link").getAttribute("href");
    if (about !== `${variants.map.origin}/`) problems.push(`${name}: About link points to ${about}`);
    if (!(await page.locator(".panel.open #tab-coverage[aria-selected=true]").isVisible())) {
      problems.push(`${name}: ?layer=coverage did not open the coverage panel`);
    }
    const host = await page.locator(".cta a").getAttribute("href");
    if (host !== `${variants.map.origin}/host/`) problems.push(`${name}: Host a node links to ${host}`);
    await context.close();
  }
} finally {
  await browser.close();
  await Promise.all(servers.map((s) => new Promise((done) => s.close(done))));
  rmSync(work, { recursive: true, force: true });
}

if (problems.length > 0) {
  console.error(`\n${problems.length} problem(s):\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log("\nAll pages rendered without errors.");
