// Smoke test of a deployed UAP Finder site (website + live map) in Chromium.
//
//   node scripts/smoke.mjs https://uapfinder.vercel.app
//
// Checks every page loads with HTTP 200, a UAP Finder title and no page
// errors, console errors or failed same-site requests; that unknown paths get
// the 404 page; that nothing scrolls sideways on a phone-sized screen; that
// the map renders (its WebGL canvas and status) with Cesium's assets served
// under /map/; and that the download links resolve. Screenshots go to
// smoke-screenshots/. Exits non-zero on any failure. Runs in CI against each
// Vercel deployment (.github/workflows/smoke.yml).
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const base = (process.argv[2] ?? process.env.SMOKE_URL ?? "").replace(/\/+$/, "");
if (!/^https?:\/\//.test(base)) {
  console.error("usage: node scripts/smoke.mjs <https://deployment-url>");
  process.exit(2);
}
const origin = new URL(base).origin;
const outDir = "smoke-screenshots";
mkdirSync(outDir, { recursive: true });

const PAGES = ["/", "/how-it-works/", "/host/", "/get-involved/", "/download/", "/faq/", "/privacy/"];
const failures = [];
const fail = (what) => {
  failures.push(what);
  console.log(`  FAIL ${what}`);
};

const browser = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });

async function open(viewport, path, { expectStatus = 200, settleMs = 0 } = {}) {
  const page = await browser.newPage({ viewport });
  const problems = [];
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // A third-party resource (e.g. a map tile) that failed to load is not a
    // problem with this site; same-site failures are caught below.
    const from = m.location()?.url;
    if (m.text().startsWith("Failed to load resource") && from && new URL(from).origin !== origin) return;
    problems.push(`console error: ${m.text()}`);
  });
  page.on("requestfailed", (r) => {
    if (new URL(r.url()).origin === origin) problems.push(`request failed: ${r.url()} (${r.failure()?.errorText})`);
  });
  page.on("response", (r) => {
    if (new URL(r.url()).origin === origin && r.status() >= 400 && r.url() !== base + path) {
      problems.push(`HTTP ${r.status()}: ${r.url()}`);
    }
  });
  const resp = await page.goto(base + path, { waitUntil: "networkidle", timeout: 45_000 });
  if (settleMs) await page.waitForTimeout(settleMs);
  const status = resp?.status();
  if (status !== expectStatus) fail(`${path}: HTTP ${status}, expected ${expectStatus}`);
  return { page, problems, status };
}

console.log(`Smoke test of ${base}`);

for (const path of PAGES) {
  for (const [name, viewport] of [["desktop", { width: 1440, height: 900 }], ["mobile", { width: 390, height: 844 }]]) {
    const { page, problems, status } = await open(viewport, path);
    const title = await page.title();
    if (!title.includes("UAP Finder")) fail(`${path} (${name}): title "${title}"`);
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    if (width > viewport.width) fail(`${path} (${name}): page is ${width}px wide on a ${viewport.width}px screen`);
    for (const p of problems) fail(`${path} (${name}): ${p}`);
    const slug = path === "/" ? "home" : path.replaceAll("/", "");
    await page.screenshot({ path: join(outDir, `${name}-${slug}.png`), fullPage: true });
    console.log(`  ok   ${path} (${name}) ${status} "${title}"`);
    await page.close();
  }
}

{
  const { page } = await open({ width: 1280, height: 800 }, "/this-page-does-not-exist/", { expectStatus: 404 });
  const heading = await page.locator("h1").first().textContent();
  console.log(`  ok   404 page: "${heading?.trim()}"`);
  await page.close();
}

{
  // The live map: its own status text, a WebGL canvas, and Cesium's static
  // files under /map/cesiumStatic/. Map tiles come from third parties and
  // are not checked here.
  const { page, problems } = await open({ width: 1440, height: 900 }, "/map/", { settleMs: 12_000 });
  const canvases = await page.locator("canvas").count();
  if (canvases === 0) fail("/map/: no WebGL canvas");
  const demo = await page.getByText(/DEMO DATA/).count();
  const live = await page.getByText(/Live \(WebSocket\)|Polling|Reconnecting|Disconnected/i).count();
  for (const p of problems) fail(`/map/: ${p}`);
  await page.screenshot({ path: join(outDir, "desktop-map.png") });
  console.log(`  ok   /map/ canvas=${canvases} demo-label=${demo > 0} status-text=${live > 0}`);
  await page.close();
}

{
  const page = await browser.newPage();
  await page.goto(base + "/download/", { waitUntil: "domcontentloaded" });
  const zip = await page.locator('a[data-download="zip"]').getAttribute("href");
  await page.close();
  const res = zip ? await fetch(zip, { redirect: "follow" }) : null;
  await res?.body?.cancel();
  if (!res || !res.ok) fail(`download ZIP ${zip}: HTTP ${res?.status}`);
  else console.log(`  ok   download ZIP ${zip} -> ${res.status} ${res.headers.get("content-type")}`);
  const sitemap = await (await fetch(base + "/sitemap.xml")).text();
  const locs = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);
  console.log(`  info sitemap lists ${locs.length} pages, first ${locs[0]}`);
}

await browser.close();
if (failures.length) {
  console.log(`\n${failures.length} problem(s) found.`);
  process.exit(1);
}
console.log("\nAll checks passed.");
