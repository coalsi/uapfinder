import { describe, expect, it } from "vitest";
import { COUNTRY_CODES, countryOptions, isCountryCode } from "../../src/lib/countries";
import { apiUrl, trimTrailingSlashes, withQuery } from "../../src/lib/urls";

describe("urls", () => {
  it("withQuery appends to plain, queried and fragment URLs", () => {
    expect(withQuery("/map/", { layer: "coverage" })).toBe("/map/?layer=coverage");
    expect(withQuery("https://x.org/map?demo=1", { layer: "coverage" })).toBe("https://x.org/map?demo=1&layer=coverage");
    expect(withQuery("/map/#globe", { layer: "coverage" })).toBe("/map/?layer=coverage#globe");
  });
  it("apiUrl joins a base with an API path", () => {
    expect(apiUrl("", "/api/v1/stats")).toBe("/api/v1/stats");
    expect(apiUrl("https://api.example.org/", "/api/v1/stats")).toBe("https://api.example.org/api/v1/stats");
    expect(apiUrl("https://api.example.org", "api/v1/stats")).toBe("https://api.example.org/api/v1/stats");
  });
  it("trimTrailingSlashes", () => {
    expect(trimTrailingSlashes("https://github.com/coalsi/uapfinder/")).toBe("https://github.com/coalsi/uapfinder");
    expect(trimTrailingSlashes("/")).toBe("/");
  });
});

describe("countries", () => {
  it("has the 249 assigned ISO 3166-1 alpha-2 codes, unique and well formed", () => {
    expect(COUNTRY_CODES).toHaveLength(249);
    expect(new Set(COUNTRY_CODES).size).toBe(249);
    expect(COUNTRY_CODES.every((c) => /^[A-Z]{2}$/.test(c))).toBe(true);
    expect(isCountryCode("US")).toBe(true);
    expect(isCountryCode("UK")).toBe(false);
  });
  it("produces sorted English names", () => {
    const opts = countryOptions();
    expect(opts).toHaveLength(249);
    expect(opts.find((o) => o.code === "DE")?.name).toBe("Germany");
    const names = opts.map((o) => o.name);
    const sorted = [...names].sort(new Intl.Collator("en", { sensitivity: "base" }).compare);
    expect(names).toEqual(sorted);
  });
});
