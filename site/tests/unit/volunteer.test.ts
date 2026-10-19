import { describe, expect, it } from "vitest";
import {
  LIMITS,
  classifyResponse,
  codePointLength,
  isValidEmail,
  outcomeMessage,
  parseServerValidationErrors,
  readForm,
  retryAfterSeconds,
  roundCoord,
  serialiseVolunteer,
  utf8ByteLength,
  validateVolunteer,
  type VolunteerFormInput,
} from "../../src/lib/volunteer";

function input(overrides: Partial<VolunteerFormInput> = {}): VolunteerFormInput {
  return {
    email: "jane@example.org",
    name: "",
    country: "US",
    region: "",
    city: "",
    postal_code: "",
    approx_lat: "",
    approx_lon: "",
    site_type: "",
    has_hardware: "",
    interests: [],
    notes: "",
    consent: true,
    website: "",
    ...overrides,
  };
}

/** A FormData stand-in (Node has FormData too, but this keeps the test explicit). */
function formData(entries: [string, string][]) {
  return {
    get: (k: string) => entries.find(([n]) => n === k)?.[1] ?? null,
    getAll: (k: string) => entries.filter(([n]) => n === k).map(([, v]) => v),
  };
}

describe("validateVolunteer", () => {
  it("accepts the minimal required fields and sends only those", () => {
    const r = validateVolunteer(input());
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.payload).toEqual({ email: "jane@example.org", country: "US", consent: true, website: "" });
    expect(r.body).toBe('{"email":"jane@example.org","country":"US","consent":true,"website":""}');
  });

  it("serialises the full example from API.md in documented order", () => {
    const r = validateVolunteer(
      input({
        name: " Jane ",
        region: "Arizona",
        city: "Surprise",
        postal_code: "85374",
        approx_lat: "33.6292",
        approx_lon: "-112.3679",
        site_type: "rooftop",
        has_hardware: "none",
        interests: ["host_node", "software"],
        notes: "Clear view to the south-west.",
      }),
    );
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(Object.keys(r.payload)).toEqual([
      "email", "name", "country", "region", "city", "postal_code", "approx_lat", "approx_lon",
      "site_type", "has_hardware", "interests", "notes", "consent", "website",
    ]);
    expect(r.payload).toMatchObject({ name: "Jane", approx_lat: 33.63, approx_lon: -112.37, interests: ["host_node", "software"] });
    expect(JSON.parse(r.body)).toEqual(r.payload);
  });

  it("requires email, country and consent", () => {
    const r = validateVolunteer(input({ email: " ", country: "", consent: false }));
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(Object.keys(r.errors).sort()).toEqual(["consent", "country", "email"]);
  });

  it("rejects malformed emails and unknown countries", () => {
    const r = validateVolunteer(input({ email: "jane@localhost", country: "XX" }));
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.errors.email).toMatch(/valid email/);
    expect(r.errors.country).toMatch(/from the list/);
  });

  it("normalises a lower-case country code", () => {
    const r = validateVolunteer(input({ country: "gb" }));
    expect(r.ok && r.payload.country).toBe("GB");
  });

  it("enforces the server's length limits in code points", () => {
    const at = (n: number, ch = "a") => ch.repeat(n);
    expect(validateVolunteer(input({ name: at(LIMITS.name) })).ok).toBe(true);
    const r = validateVolunteer(
      input({
        name: at(LIMITS.name + 1),
        region: at(LIMITS.region + 1),
        city: at(LIMITS.city + 1),
        postal_code: at(LIMITS.postal_code + 1),
        notes: at(LIMITS.notes + 1),
      }),
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(Object.keys(r.errors).sort()).toEqual(["city", "name", "notes", "postal_code", "region"]);
    // An emoji is one code point (two UTF-16 units): 100 of them is still a valid name.
    expect(validateVolunteer(input({ name: at(100, "\u{1F52D}") })).ok).toBe(true);
  });

  it("rejects an email longer than 254 characters", () => {
    const long = `${"a".repeat(64)}@${"b".repeat(63)}.${"c".repeat(63)}.${"d".repeat(63)}.org`;
    expect(long.length).toBeGreaterThan(254);
    const r = validateVolunteer(input({ email: long }));
    expect(!r.ok && r.errors.email).toMatch(/254/);
  });

  it("rounds coordinates to two decimals before sending and needs both", () => {
    const ok = validateVolunteer(input({ approx_lat: "-33.86785", approx_lon: "151.20732" }));
    expect(ok.ok && [ok.payload.approx_lat, ok.payload.approx_lon]).toEqual([-33.87, 151.21]);
    const half = validateVolunteer(input({ approx_lat: "10.5", approx_lon: "" }));
    expect(!half.ok && half.errors.location).toBeTruthy();
    const range = validateVolunteer(input({ approx_lat: "91", approx_lon: "0" }));
    expect(!range.ok && range.errors.location).toBeTruthy();
    const nan = validateVolunteer(input({ approx_lat: "north", approx_lon: "0" }));
    expect(nan.ok).toBe(false);
  });

  it("only accepts documented enum values and de-duplicates interests", () => {
    expect(validateVolunteer(input({ site_type: "balcony" })).ok).toBe(false);
    expect(validateVolunteer(input({ has_hardware: "lots" })).ok).toBe(false);
    expect(validateVolunteer(input({ interests: ["host_node", "astrology"] })).ok).toBe(false);
    const r = validateVolunteer(input({ interests: ["outreach", "outreach", "data_analysis"] }));
    expect(r.ok && r.payload.interests).toEqual(["outreach", "data_analysis"]);
  });

  it("passes the honeypot through untouched", () => {
    const r = validateVolunteer(input({ website: "http://spam.example" }));
    expect(r.ok && r.payload.website).toBe("http://spam.example");
  });

  it("keeps the body under the 4 KB limit", () => {
    // 1000 four-byte code points pass the notes limit but not the body limit.
    const r = validateVolunteer(input({ notes: "\u{1F30C}".repeat(1000) }));
    expect(r.ok).toBe(false);
    expect(!r.ok && r.errors.notes).toMatch(/too large/);
  });
});

describe("readForm", () => {
  it("reads checkboxes, multi-value interests and missing fields", () => {
    const v = readForm(
      formData([
        ["email", "a@b.co"],
        ["country", "DE"],
        ["interests", "software"],
        ["interests", "outreach"],
        ["consent", "on"],
      ]),
    );
    expect(v).toMatchObject({ email: "a@b.co", country: "DE", interests: ["software", "outreach"], consent: true, website: "", notes: "" });
    expect(readForm(formData([])).consent).toBe(false);
  });
});

describe("helpers", () => {
  it("isValidEmail", () => {
    for (const ok of ["jane@example.org", "j.doe+uap@mail.example.co.uk", "x@bücher.de"]) expect(isValidEmail(ok)).toBe(true);
    for (const bad of ["", "jane", "@example.org", "jane@", "jane@example", "ja ne@example.org", "a@b@c.org", "a..b@example.org", "a@-x.org", "a@x.123"]) {
      expect(isValidEmail(bad), bad).toBe(false);
    }
  });

  it("roundCoord is symmetric and never returns -0", () => {
    expect(roundCoord(33.6292)).toBe(33.63);
    expect(roundCoord(-112.3679)).toBe(-112.37);
    expect(roundCoord(-0.001)).toBe(0);
    expect(Object.is(roundCoord(-0.001), -0)).toBe(false);
  });

  it("counts code points and UTF-8 bytes", () => {
    expect(codePointLength("a\u{1F52D}")).toBe(2);
    expect(utf8ByteLength("a\u{1F52D}")).toBe(5);
  });

  it("serialiseVolunteer is plain JSON", () => {
    expect(serialiseVolunteer({ email: "a@b.co", country: "US", consent: true, website: "" })).toBe(
      '{"email":"a@b.co","country":"US","consent":true,"website":""}',
    );
  });

  it("retryAfterSeconds handles seconds and HTTP dates", () => {
    expect(retryAfterSeconds("120")).toBe(120);
    expect(retryAfterSeconds(null)).toBeUndefined();
    expect(retryAfterSeconds("soon")).toBeUndefined();
    const now = Date.parse("2026-10-19T12:00:00Z");
    expect(retryAfterSeconds("Mon, 19 Oct 2026 12:30:00 GMT", now)).toBe(1800);
  });
});

describe("server responses", () => {
  it("maps FastAPI 422 details onto fields", () => {
    const { fields, general } = parseServerValidationErrors({
      detail: [
        { loc: ["body", "email"], msg: "value is not a valid email address", type: "value_error" },
        { loc: ["body", "approx_lat"], msg: "Input should be less than or equal to 90", type: "less_than_equal" },
        { loc: ["body"], msg: "Body too large", type: "value_error" },
      ],
    });
    expect(fields.email).toBe("Value is not a valid email address.");
    expect(fields.location).toMatch(/less than or equal to 90/);
    expect(general).toEqual(["Body too large."]);
  });

  it("handles string details and empty bodies", () => {
    expect(parseServerValidationErrors({ detail: "invalid country" }).general).toEqual(["Invalid country."]);
    expect(parseServerValidationErrors(null).general).toHaveLength(1);
  });

  it("classifies statuses", () => {
    expect(classifyResponse(201, { status: "received" }).kind).toBe("success");
    expect(classifyResponse(422, { detail: [] }).kind).toBe("validation");
    expect(classifyResponse(429, { detail: "rate limited" }, "600")).toEqual({ kind: "rate_limited", retryAfterS: 600 });
    expect(classifyResponse(429, null)).toEqual({ kind: "rate_limited" });
    expect(classifyResponse(503, { detail: "sign-up unavailable" })).toEqual({ kind: "unavailable" });
    expect(classifyResponse(500, null)).toEqual({ kind: "error", status: 500 });
  });

  it("produces a message for every outcome", () => {
    expect(outcomeMessage({ kind: "success" }).tone).toBe("success");
    expect(outcomeMessage({ kind: "rate_limited" }).body).toMatch(/an hour/);
    expect(outcomeMessage({ kind: "rate_limited", retryAfterS: 600 }).body).toMatch(/about 10 minutes/);
    expect(outcomeMessage({ kind: "unavailable" }).title).toMatch(/temporarily unavailable/);
    expect(outcomeMessage({ kind: "network" }).tone).toBe("error");
    expect(outcomeMessage({ kind: "error", status: 500 }).body).toMatch(/HTTP 500/);
    expect(outcomeMessage({ kind: "validation", fields: {}, general: ["Bad."] }).body).toBe("Bad.");
  });
});
