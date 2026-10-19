import { describe, expect, it } from "vitest";
import { ApiClient, ApiError, resolveApiBase, resolveServerLink, toWebSocketUrl } from "./client";

const PAGE = "https://uapfinder.example.org/map/?demo=0";

function recordingFetch(response: () => Response) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return response();
  }) as typeof fetch;
  return { calls, impl };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("resolveApiBase", () => {
  it("defaults to the page's origin (same-origin deploy), ignoring the page path", () => {
    expect(resolveApiBase(undefined, PAGE)).toBe("https://uapfinder.example.org");
    expect(resolveApiBase("", PAGE)).toBe("https://uapfinder.example.org");
    expect(resolveApiBase("   ", PAGE)).toBe("https://uapfinder.example.org");
  });

  it("accepts a cross-origin server and strips trailing slashes", () => {
    expect(resolveApiBase("https://studio.tail1234.ts.net/", PAGE)).toBe("https://studio.tail1234.ts.net");
    expect(resolveApiBase("http://192.168.1.20:8000", PAGE)).toBe("http://192.168.1.20:8000");
    expect(resolveApiBase("https://example.org/skynet//", PAGE)).toBe("https://example.org/skynet");
  });

  it("tolerates a base that already ends in /api/v1", () => {
    expect(resolveApiBase("https://studio.tail1234.ts.net/api/v1/", PAGE)).toBe("https://studio.tail1234.ts.net");
    expect(resolveApiBase("https://example.org/skynet/api/v1", PAGE)).toBe("https://example.org/skynet");
  });

  it("resolves a relative base against the page", () => {
    expect(resolveApiBase("/backend/", PAGE)).toBe("https://uapfinder.example.org/backend");
  });

  it("rejects non-http(s) bases", () => {
    expect(() => resolveApiBase("ftp://example.org", PAGE)).toThrow(/http/);
  });
});

describe("WebSocket URL", () => {
  it("derives wss:// from https:// and ws:// from http://", () => {
    expect(toWebSocketUrl("https://a.example/api/v1/live")).toBe("wss://a.example/api/v1/live");
    expect(toWebSocketUrl("http://localhost:8000/api/v1/live")).toBe("ws://localhost:8000/api/v1/live");
    expect(() => toWebSocketUrl("ftp://a.example/x")).toThrow();
  });

  it("is the live endpoint under the API base, keeping port and prefix", () => {
    expect(new ApiClient("https://studio.tail1234.ts.net").liveUrl()).toBe("wss://studio.tail1234.ts.net/api/v1/live");
    expect(new ApiClient("http://127.0.0.1:8000/skynet").liveUrl()).toBe("ws://127.0.0.1:8000/skynet/api/v1/live");
  });
});

describe("ApiClient requests", () => {
  it("builds /api/v1 URLs, encoding query values and omitting null/undefined", () => {
    const c = new ApiClient("https://api.example");
    expect(c.url("/tracks", { limit: 100, since_ns: undefined, classification: null })).toBe(
      "https://api.example/api/v1/tracks?limit=100",
    );
    expect(c.url("/events", { after: "1782345600012-0", limit: 500 })).toBe(
      "https://api.example/api/v1/events?after=1782345600012-0&limit=500",
    );
    expect(c.url("/x", { q: "a b&c" })).toBe("https://api.example/api/v1/x?q=a+b%26c");
  });

  it("GET /events sends the cursor, or only the limit for the most recent page", async () => {
    const { calls, impl } = recordingFetch(() => json({ events: [], cursor: null }));
    const c = new ApiClient("https://api.example", impl);
    await c.getEvents("42-7", 500);
    await c.getEvents(null, 200);
    expect(calls.map((x) => x.url)).toEqual([
      "https://api.example/api/v1/events?after=42-7&limit=500",
      "https://api.example/api/v1/events?limit=200",
    ]);
  });

  it("sends only CORS-safelisted headers (no preflight cross-origin)", async () => {
    const { calls, impl } = recordingFetch(() => json({ nodes: [] }));
    await new ApiClient("https://api.example", impl).getNodes();
    expect(calls[0]?.init?.headers).toEqual({ Accept: "application/json" });
    expect(calls[0]?.init?.credentials).toBeUndefined();
  });

  it("GET /coverage passes the bounding box and slice", async () => {
    const { calls, impl } = recordingFetch(() =>
      json({ type: "FeatureCollection", params: {}, summary: { blind: 0, mono: 0, stereo: 0 }, suggestions: [], features: [] }),
    );
    await new ApiClient("https://api.example", impl).getCoverage({
      min_lat: 33,
      min_lon: -113,
      max_lat: 34,
      max_lon: -112,
      cell_km: 5,
      ref_alt_m: 3000,
    });
    const url = new URL(calls[0]?.url ?? "");
    expect(url.pathname).toBe("/api/v1/coverage");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      min_lat: "33",
      min_lon: "-113",
      max_lat: "34",
      max_lon: "-112",
      cell_km: "5",
      ref_alt_m: "3000",
    });
  });

  it("encodes track ids in the path", async () => {
    const { calls, impl } = recordingFetch(() => json({ track_id: "a/b", points: [], clips: [] }));
    await new ApiClient("https://api.example", impl).getTrack("a/b");
    expect(calls[0]?.url).toBe("https://api.example/api/v1/tracks/a%2Fb");
  });

  it("raises ApiError with FastAPI's detail", async () => {
    const c400 = new ApiClient("https://api.example", recordingFetch(() => json({ detail: "grid too large" }, 400)).impl);
    await expect(c400.getStats()).rejects.toMatchObject({ name: "ApiError", status: 400, detail: "grid too large" });

    const validation = [{ loc: ["query", "min_lat"], msg: "field required" }];
    const c422 = new ApiClient("https://api.example", recordingFetch(() => json({ detail: validation }, 422)).impl);
    await expect(c422.getStats()).rejects.toMatchObject({ status: 422, detail: JSON.stringify(validation) });

    const c502 = new ApiClient(
      "https://api.example",
      recordingFetch(() => new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" })).impl,
    );
    const err = await c502.getStats().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).detail).toBe("Bad Gateway");
  });
});

describe("server links (clip downloads)", () => {
  const base = "https://studio.tail1234.ts.net";

  it("resolves relative links against the API server, not the dashboard", () => {
    expect(resolveServerLink("/api/v1/clips/c1/file", base)).toBe("https://studio.tail1234.ts.net/api/v1/clips/c1/file");
    expect(resolveServerLink("clips/c1.mp4", `${base}/skynet`)).toBe("https://studio.tail1234.ts.net/skynet/clips/c1.mp4");
    expect(new ApiClient(base).link("/api/v1/clips/c1/file")).toBe("https://studio.tail1234.ts.net/api/v1/clips/c1/file");
  });

  it("keeps absolute http(s) links (presigned object storage)", () => {
    const signed = "https://bucket.s3.amazonaws.com/c1.mp4?X-Amz-Signature=abc";
    expect(resolveServerLink(signed, base)).toBe(signed);
  });

  it("refuses anything that is not http(s)", () => {
    expect(resolveServerLink("javascript:alert(1)", base)).toBeNull();
    expect(resolveServerLink("data:text/html,<b>x</b>", base)).toBeNull();
    expect(resolveServerLink(null, base)).toBeNull();
    expect(resolveServerLink("", base)).toBeNull();
  });
});
