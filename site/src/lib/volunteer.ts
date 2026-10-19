/**
 * Volunteer interest form: client-side validation and serialisation.
 *
 * Mirrors the server rules in docs/API.md ("Volunteer sign-up"), so most
 * mistakes are caught before anything is sent. The server remains the
 * authority: its 422 responses are mapped back onto the same fields.
 *
 * Pure functions only (no DOM), so Vitest can cover them directly.
 */
import { isCountryCode } from "./countries";

export const SITE_TYPES = ["rooftop", "yard", "rural_land", "other"] as const;
export const HARDWARE_STATUSES = ["none", "some", "full"] as const;
export const INTERESTS = ["host_node", "software", "data_analysis", "outreach"] as const;

export type SiteType = (typeof SITE_TYPES)[number];
export type HardwareStatus = (typeof HARDWARE_STATUSES)[number];
export type Interest = (typeof INTERESTS)[number];

/** Server limits (API.md). Lengths count Unicode code points, like Python's len(). */
export const LIMITS = {
  email: 254,
  name: 100,
  region: 100,
  city: 100,
  postal_code: 20,
  notes: 1000,
  /** The server accepts bodies of "at most 4 KB"; stay safely under it. */
  bodyBytes: 4000,
  /** Decimal places kept for approx_lat / approx_lon (0.01 degrees is about 1 km). */
  coordDecimals: 2,
} as const;

/** Raw values as read from the form (all strings, as the DOM gives them). */
export interface VolunteerFormInput {
  email: string;
  name: string;
  country: string;
  region: string;
  city: string;
  postal_code: string;
  approx_lat: string;
  approx_lon: string;
  site_type: string;
  has_hardware: string;
  interests: string[];
  notes: string;
  consent: boolean;
  website: string;
}

/** The JSON body sent to POST /api/v1/volunteers. Empty optional fields are omitted. */
export interface VolunteerPayload {
  email: string;
  name?: string;
  country: string;
  region?: string;
  city?: string;
  postal_code?: string;
  approx_lat?: number;
  approx_lon?: number;
  site_type?: SiteType;
  has_hardware?: HardwareStatus;
  interests?: Interest[];
  notes?: string;
  consent: true;
  website: string;
}

export type FieldName = Exclude<keyof VolunteerFormInput, "website"> | "location";
export type FieldErrors = Partial<Record<FieldName, string>>;

export type ValidationResult =
  | { ok: true; payload: VolunteerPayload; body: string }
  | { ok: false; errors: FieldErrors };

/** Minimal structural view of FormData so tests don't need a DOM. */
export interface FormDataLike {
  get(name: string): unknown;
  getAll(name: string): unknown[];
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function readForm(data: FormDataLike): VolunteerFormInput {
  const consent = str(data.get("consent"));
  return {
    email: str(data.get("email")),
    name: str(data.get("name")),
    country: str(data.get("country")),
    region: str(data.get("region")),
    city: str(data.get("city")),
    postal_code: str(data.get("postal_code")),
    approx_lat: str(data.get("approx_lat")),
    approx_lon: str(data.get("approx_lon")),
    site_type: str(data.get("site_type")),
    has_hardware: str(data.get("has_hardware")),
    interests: data.getAll("interests").map(str).filter(Boolean),
    notes: str(data.get("notes")),
    consent: consent === "on" || consent === "true" || consent === "yes",
    website: str(data.get("website")),
  };
}

/** Length in Unicode code points (what Python's len() and the server count). */
export function codePointLength(s: string): number {
  let n = 0;
  for (const _ of s) n += 1;
  return n;
}

export function utf8ByteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

/**
 * Round a coordinate to `decimals` places, symmetrically for negative values.
 * Two decimals is about 1.1 km of latitude (less of longitude away from the
 * equator), which is all the project ever needs for planning coverage.
 */
export function roundCoord(value: number, decimals: number = LIMITS.coordDecimals): number {
  const f = 10 ** decimals;
  const r = (Math.sign(value) * Math.round(Math.abs(value) * f)) / f;
  return Object.is(r, -0) ? 0 : r;
}

const DOMAIN_LABEL = /^(?!-)[A-Za-z0-9¡-￿-]{1,63}(?<!-)$/;
const LOCAL_PART = /^[^\s@"(),:;<>[\\\]]+$/;

/**
 * Pragmatic address check: one "@", a plausible local part and a dotted
 * domain. Deliberately permissive (internationalised domains are allowed);
 * the server's validator has the final say.
 */
export function isValidEmail(raw: string): boolean {
  const email = raw.trim();
  if (email.length === 0 || codePointLength(email) > LIMITS.email) return false;
  const at = email.lastIndexOf("@");
  if (at <= 0 || at !== email.indexOf("@")) return false;
  const local = email.slice(0, at);
  const domain = email.slice(at + 1);
  if (local.length > 64 || !LOCAL_PART.test(local)) return false;
  if (local.startsWith(".") || local.endsWith(".") || local.includes("..")) return false;
  const labels = domain.split(".");
  if (labels.length < 2) return false;
  if (!labels.every((l) => DOMAIN_LABEL.test(l))) return false;
  const tld = labels[labels.length - 1] ?? "";
  return tld.length >= 2 && !/^\d+$/.test(tld);
}

function parseCoord(raw: string, min: number, max: number): number | null | "invalid" {
  const s = raw.trim();
  if (s === "") return null;
  const n = Number(s);
  if (!Number.isFinite(n) || n < min || n > max) return "invalid";
  return roundCoord(n);
}

function tooLong(label: string, max: number): string {
  return `${label} must be ${max} characters or fewer.`;
}

export function validateVolunteer(input: VolunteerFormInput): ValidationResult {
  const errors: FieldErrors = {};

  const email = input.email.trim();
  if (email === "") errors.email = "Enter your email address.";
  else if (codePointLength(email) > LIMITS.email) errors.email = tooLong("Email", LIMITS.email);
  else if (!isValidEmail(email)) errors.email = "Enter a valid email address, like name@example.org.";

  const name = input.name.trim();
  if (codePointLength(name) > LIMITS.name) errors.name = tooLong("Name", LIMITS.name);

  const country = input.country.trim().toUpperCase();
  if (country === "") errors.country = "Choose your country.";
  else if (!isCountryCode(country)) errors.country = "Choose a country from the list.";

  const region = input.region.trim();
  if (codePointLength(region) > LIMITS.region) errors.region = tooLong("Region", LIMITS.region);
  const city = input.city.trim();
  if (codePointLength(city) > LIMITS.city) errors.city = tooLong("City", LIMITS.city);
  const postal = input.postal_code.trim();
  if (codePointLength(postal) > LIMITS.postal_code) errors.postal_code = tooLong("Postal code", LIMITS.postal_code);

  const lat = parseCoord(input.approx_lat, -90, 90);
  const lon = parseCoord(input.approx_lon, -180, 180);
  if (lat === "invalid" || lon === "invalid" || (lat === null) !== (lon === null)) {
    errors.location = "The approximate location is incomplete or out of range. Remove it and try again.";
  }

  const siteType = input.site_type.trim();
  if (siteType !== "" && !(SITE_TYPES as readonly string[]).includes(siteType)) {
    errors.site_type = "Choose one of the listed site types.";
  }
  const hardware = input.has_hardware.trim();
  if (hardware !== "" && !(HARDWARE_STATUSES as readonly string[]).includes(hardware)) {
    errors.has_hardware = "Choose one of the listed options.";
  }

  const interests: Interest[] = [];
  for (const raw of input.interests) {
    if (!(INTERESTS as readonly string[]).includes(raw)) {
      errors.interests = "Choose interests from the list.";
      break;
    }
    if (!interests.includes(raw as Interest)) interests.push(raw as Interest);
  }

  const notes = input.notes.trim();
  if (codePointLength(notes) > LIMITS.notes) errors.notes = tooLong("Notes", LIMITS.notes);

  if (!input.consent) errors.consent = "Please confirm that you agree to the privacy notice.";

  if (Object.keys(errors).length > 0) return { ok: false, errors };

  // Assemble in the documented field order, omitting empty optional values.
  const payload: VolunteerPayload = {
    email,
    ...(name && { name }),
    country,
    ...(region && { region }),
    ...(city && { city }),
    ...(postal && { postal_code: postal }),
    ...(typeof lat === "number" && typeof lon === "number" && { approx_lat: lat, approx_lon: lon }),
    ...(siteType && { site_type: siteType as SiteType }),
    ...(hardware && { has_hardware: hardware as HardwareStatus }),
    ...(interests.length > 0 && { interests }),
    ...(notes && { notes }),
    consent: true,
    website: input.website,
  };

  const body = serialiseVolunteer(payload);
  if (utf8ByteLength(body) > LIMITS.bodyBytes) {
    return {
      ok: false,
      errors: { notes: "Your submission is too large to send (about 4 KB maximum). Please shorten the notes." },
    };
  }
  return { ok: true, payload, body };
}

export function serialiseVolunteer(payload: VolunteerPayload): string {
  return JSON.stringify(payload);
}

// ---------------------------------------------------------------------------
// Server responses

const FIELD_NAMES: ReadonlySet<string> = new Set([
  "email", "name", "country", "region", "city", "postal_code", "site_type",
  "has_hardware", "interests", "notes", "consent",
]);

function sentence(msg: string): string {
  const m = msg.trim();
  if (!m) return m;
  const s = m.charAt(0).toUpperCase() + m.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

/**
 * Map a FastAPI/pydantic 422 body ({"detail": [{loc, msg}, ...]} or
 * {"detail": "..."}) onto form fields. Unknown locations become general
 * messages so nothing the server says is lost.
 */
export function parseServerValidationErrors(body: unknown): { fields: FieldErrors; general: string[] } {
  const fields: FieldErrors = {};
  const general: string[] = [];
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string") {
    general.push(sentence(detail));
  } else if (Array.isArray(detail)) {
    for (const item of detail) {
      if (!item || typeof item !== "object") continue;
      const { loc, msg } = item as { loc?: unknown; msg?: unknown };
      const text = typeof msg === "string" ? sentence(msg) : "This value was rejected.";
      const path = Array.isArray(loc) ? loc.filter((p): p is string => typeof p === "string" && p !== "body") : [];
      const field = path[0];
      if (field === "approx_lat" || field === "approx_lon") {
        fields.location ??= text;
      } else if (field && FIELD_NAMES.has(field)) {
        fields[field as FieldName] ??= text;
      } else {
        general.push(text);
      }
    }
  }
  if (Object.keys(fields).length === 0 && general.length === 0) {
    general.push("The server could not accept the form. Please check your entries and try again.");
  }
  return { fields, general };
}

/** Retry-After as seconds (delta-seconds or an HTTP date), if usable. */
export function retryAfterSeconds(header: string | null | undefined, now: number = Date.now()): number | undefined {
  if (!header) return undefined;
  const s = header.trim();
  if (/^\d+$/.test(s)) return Number(s);
  const t = Date.parse(s);
  if (Number.isNaN(t)) return undefined;
  return Math.max(0, Math.round((t - now) / 1000));
}

export type SubmitOutcome =
  | { kind: "success" }
  | { kind: "validation"; fields: FieldErrors; general: string[] }
  | { kind: "rate_limited"; retryAfterS?: number }
  | { kind: "unavailable" }
  | { kind: "error"; status: number }
  | { kind: "network" };

export function classifyResponse(status: number, body: unknown, retryAfter?: string | null): SubmitOutcome {
  if (status === 200 || status === 201) return { kind: "success" };
  if (status === 422) return { kind: "validation", ...parseServerValidationErrors(body) };
  if (status === 429) {
    const s = retryAfterSeconds(retryAfter);
    return s === undefined ? { kind: "rate_limited" } : { kind: "rate_limited", retryAfterS: s };
  }
  if (status === 503) return { kind: "unavailable" };
  return { kind: "error", status };
}

function humanDuration(seconds: number): string {
  if (seconds < 90) return "a minute or two";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 90) return `about ${minutes} minutes`;
  const hours = Math.round(minutes / 60);
  return `about ${hours} hour${hours === 1 ? "" : "s"}`;
}

export interface OutcomeMessage {
  tone: "success" | "error" | "warning";
  title: string;
  body: string;
}

export function outcomeMessage(outcome: SubmitOutcome): OutcomeMessage {
  switch (outcome.kind) {
    case "success":
      return {
        tone: "success",
        title: "Thank you, your interest is registered.",
        body:
          "We will only use your details to contact you about UAP Finder. Submitting again with the same email address updates your earlier entry.",
      };
    case "validation":
      return {
        tone: "error",
        title: "Some entries need attention.",
        body: outcome.general.length > 0 ? outcome.general.join(" ") : "Please correct the highlighted fields and submit again.",
      };
    case "rate_limited":
      return {
        tone: "warning",
        title: "Too many submissions from this connection.",
        body: `To prevent abuse, sign-ups are limited to a few per hour from each network. Nothing new was saved. Please try again in ${
          outcome.retryAfterS !== undefined ? humanDuration(outcome.retryAfterS) : "an hour"
        }.`,
      };
    case "unavailable":
      return {
        tone: "warning",
        title: "Sign-up is temporarily unavailable.",
        body: "The server is not accepting sign-ups right now, so nothing was saved. Please try again later.",
      };
    case "network":
      return {
        tone: "error",
        title: "We could not reach the UAP Finder server.",
        body: "Your entry may not have been received. Check your connection and try again; submitting twice with the same email is safe.",
      };
    case "error":
      return {
        tone: "error",
        title: "Something went wrong.",
        body: `The server answered with an unexpected error (HTTP ${outcome.status}), so your entry may not have been saved. Please try again later.`,
      };
  }
}
