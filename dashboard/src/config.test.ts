import { describe, expect, it } from "vitest";
import { CESIUM_STATIC_DIR, normalizeBase } from "./basePath";
import { readConfig, resolveSiteUrl } from "./config";

const env = (vars: Partial<Record<keyof ImportMetaEnv, string>> = {}) => vars as ImportMetaEnv;

describe("normalizeBase (VITE_BASE_PATH)", () => {
  it("defaults to the root", () => {
    expect(normalizeBase(undefined)).toBe("/");
    expect(normalizeBase("")).toBe("/");
    expect(normalizeBase(" / ")).toBe("/");
  });

  it("always has leading and trailing slashes", () => {
    expect(normalizeBase("/map/")).toBe("/map/");
    expect(normalizeBase("/map")).toBe("/map/");
    expect(normalizeBase("map")).toBe("/map/");
    expect(normalizeBase("//apps//map//")).toBe("/apps/map/");
  });

  it("keeps absolute URLs (CDN) and adds the trailing slash", () => {
    expect(normalizeBase("https://cdn.example.org/map")).toBe("https://cdn.example.org/map/");
  });

  it("puts the CesiumJS static files under the base", () => {
    expect(`${normalizeBase("/map")}${CESIUM_STATIC_DIR}/`).toBe("/map/cesiumStatic/");
  });
});

describe("readConfig", () => {
  const page = "https://uapfinder.example.org/map/";

  it("needs no settings: same-origin API, OpenStreetMap imagery, site at /", () => {
    const c = readConfig(env(), page);
    expect(c).toEqual({
      apiBase: "https://uapfinder.example.org",
      demo: false,
      ionToken: null,
      imagery: { kind: "tiles", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", credit: expect.stringContaining("OpenStreetMap") },
      siteUrl: "https://uapfinder.example.org/",
      hostUrl: "https://uapfinder.example.org/host/",
      initialPanel: null,
    });
  });

  it("opens the panel the website links to with ?layer=", () => {
    expect(readConfig(env(), `${page}?layer=coverage`).initialPanel).toBe("coverage");
    expect(readConfig(env(), `${page}?layer=tracks`).initialPanel).toBe("tracks");
    expect(readConfig(env(), `${page}?layer=bogus`).initialPanel).toBeNull();
  });

  it("reads VITE_API_BASE, VITE_SITE_URL and VITE_DEMO", () => {
    const c = readConfig(
      env({ VITE_API_BASE: "https://studio.tail1234.ts.net", VITE_SITE_URL: "https://uapfinder.org/about", VITE_DEMO: "1" }),
      page,
    );
    expect(c.apiBase).toBe("https://studio.tail1234.ts.net");
    expect(c.siteUrl).toBe("https://uapfinder.org/about");
    expect(c.hostUrl).toBe("https://uapfinder.org/host/");
    expect(c.demo).toBe(true);
  });

  it("lets ?demo= override VITE_DEMO either way", () => {
    expect(readConfig(env({ VITE_DEMO: "1" }), `${page}?demo=0`).demo).toBe(false);
    expect(readConfig(env(), `${page}?demo=1`).demo).toBe(true);
    expect(readConfig(env(), `${page}?demo=true`).demo).toBe(true);
  });

  it("uses Cesium ion imagery only with a token, and honours ?imagery=", () => {
    expect(readConfig(env({ VITE_CESIUM_ION_TOKEN: "  tok  " }), page)).toMatchObject({ ionToken: "tok", imagery: { kind: "ion" } });
    expect(readConfig(env({ VITE_CESIUM_ION_TOKEN: "" }), page).ionToken).toBeNull();
    expect(readConfig(env(), `${page}?imagery=offline`).imagery).toEqual({ kind: "offline" });
    expect(readConfig(env({ VITE_IMAGERY_URL: "https://tiles.example/{z}/{x}/{y}.png", VITE_IMAGERY_CREDIT: "© Example" }), page).imagery).toEqual({
      kind: "tiles",
      url: "https://tiles.example/{z}/{x}/{y}.png",
      credit: "© Example",
    });
  });
});

describe("resolveSiteUrl", () => {
  it("resolves relative paths against the page and refuses non-http(s) links", () => {
    expect(resolveSiteUrl("/", "https://a.example/map/")).toBe("https://a.example/");
    expect(resolveSiteUrl("../", "https://a.example/map/")).toBe("https://a.example/");
    expect(resolveSiteUrl("javascript:alert(1)", "https://a.example/map/")).toBe("https://a.example/");
  });
});
