/**
 * Volunteer interest form (POST {API_BASE}/api/v1/volunteers, docs/API.md).
 *
 * - Nothing is sent until the visitor presses submit.
 * - Validation mirrors the server (src/lib/volunteer.ts); server 422s are
 *   mapped back onto the same fields.
 * - "Use my approximate location" asks the browser for a position and rounds
 *   it to 2 decimal places here, before it is even put into the form.
 */
import { apiUrl } from "../lib/urls";
import {
  LIMITS,
  classifyResponse,
  codePointLength,
  outcomeMessage,
  readForm,
  roundCoord,
  validateVolunteer,
  type FieldErrors,
  type FieldName,
  type SubmitOutcome,
} from "../lib/volunteer";

const SUBMIT_TIMEOUT_MS = 15_000;

const FIELD_LABELS: Record<FieldName, string> = {
  email: "Email",
  name: "Name",
  country: "Country",
  region: "Region",
  city: "City or town",
  postal_code: "Postal code",
  approx_lat: "Approximate location",
  approx_lon: "Approximate location",
  location: "Approximate location",
  site_type: "Site type",
  has_hardware: "Hardware",
  interests: "Interests",
  notes: "Notes",
  consent: "Privacy consent",
};

/** Element that receives focus / aria-invalid for each field. */
function fieldControl(form: HTMLFormElement, field: FieldName): HTMLElement | null {
  if (field === "location" || field === "approx_lat" || field === "approx_lon") {
    return form.querySelector<HTMLElement>("[data-geo-button]");
  }
  return form.querySelector<HTMLElement>(`[name="${field}"]`);
}

function setFieldError(form: HTMLFormElement, field: FieldName, message: string | undefined): void {
  const key = field === "approx_lat" || field === "approx_lon" ? "location" : field;
  const error = form.querySelector<HTMLElement>(`[data-error-for="${key}"]`);
  if (error) {
    error.textContent = message ?? "";
    error.hidden = !message;
  }
  const group = form.querySelector<HTMLElement>(`[data-group="${key}"]`);
  group?.classList.toggle("has-error", Boolean(message));
  const controls = form.querySelectorAll<HTMLElement>(`[name="${key}"]`);
  const targets = controls.length > 0 ? Array.from(controls) : [fieldControl(form, key)].filter((x): x is HTMLElement => !!x);
  for (const el of targets) {
    if (message) el.setAttribute("aria-invalid", "true");
    else el.removeAttribute("aria-invalid");
  }
}

function clearErrors(form: HTMLFormElement): void {
  for (const el of form.querySelectorAll<HTMLElement>("[data-error-for]")) {
    el.textContent = "";
    el.hidden = true;
  }
  for (const el of form.querySelectorAll<HTMLElement>("[aria-invalid]")) el.removeAttribute("aria-invalid");
  for (const el of form.querySelectorAll<HTMLElement>(".has-error")) el.classList.remove("has-error");
  const summary = form.querySelector<HTMLElement>("[data-error-summary]");
  if (summary) summary.hidden = true;
}

function showErrors(form: HTMLFormElement, errors: FieldErrors, general: string[] = []): void {
  clearErrors(form);
  const summary = form.querySelector<HTMLElement>("[data-error-summary]");
  const list = summary?.querySelector<HTMLUListElement>("ul");
  if (list) list.replaceChildren();
  for (const [field, message] of Object.entries(errors) as [FieldName, string][]) {
    setFieldError(form, field, message);
    if (list) {
      const li = document.createElement("li");
      li.dataset.summaryFor = field === "approx_lat" || field === "approx_lon" ? "location" : field;
      const control = fieldControl(form, field);
      const a = document.createElement("a");
      a.href = control?.id ? `#${control.id}` : "#signup";
      a.textContent = `${FIELD_LABELS[field]}: ${message}`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        control?.focus();
      });
      li.append(a);
      list.append(li);
    }
  }
  for (const message of general) {
    const li = document.createElement("li");
    li.textContent = message;
    list?.append(li);
  }
  if (summary) {
    summary.hidden = false;
    summary.focus();
  }
}

function showStatus(form: HTMLFormElement, outcome: SubmitOutcome): void {
  const status = form.querySelector<HTMLElement>("[data-form-status]");
  if (!status) return;
  const msg = outcomeMessage(outcome);
  status.dataset.tone = msg.tone;
  status.dataset.outcome = outcome.kind;
  const title = document.createElement("p");
  title.className = "status-title";
  title.textContent = msg.title;
  const body = document.createElement("p");
  body.textContent = msg.body;
  status.replaceChildren(title, body);
  status.hidden = false;
  status.focus();
}

async function post(url: string, body: string): Promise<SubmitOutcome> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), SUBMIT_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body,
      signal: controller.signal,
      credentials: "omit",
    });
    let json: unknown = null;
    try {
      json = await res.json();
    } catch {
      json = null;
    }
    return classifyResponse(res.status, json, res.headers.get("Retry-After"));
  } catch {
    return { kind: "network" };
  } finally {
    window.clearTimeout(timer);
  }
}

function initGeolocation(form: HTMLFormElement): void {
  const button = form.querySelector<HTMLButtonElement>("[data-geo-button]");
  const clear = form.querySelector<HTMLButtonElement>("[data-geo-clear]");
  const output = form.querySelector<HTMLElement>("[data-geo-output]");
  const lat = form.querySelector<HTMLInputElement>('[name="approx_lat"]');
  const lon = form.querySelector<HTMLInputElement>('[name="approx_lon"]');
  if (!button || !clear || !output || !lat || !lon) return;

  if (!("geolocation" in navigator)) {
    button.disabled = true;
    output.textContent = "Your browser does not offer location. Country and city are enough.";
    return;
  }

  const set = (la: number | null, lo: number | null, message: string) => {
    lat.value = la === null ? "" : String(la);
    lon.value = lo === null ? "" : String(lo);
    output.textContent = message;
    clear.hidden = la === null;
    setFieldError(form, "location", undefined);
  };

  button.addEventListener("click", () => {
    button.disabled = true;
    output.textContent = "Asking your browser for your location…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        // Round on this device, immediately: the precise position is never stored in the form.
        const la = roundCoord(pos.coords.latitude);
        const lo = roundCoord(pos.coords.longitude);
        button.disabled = false;
        set(la, lo, `Approximate location: ${la.toFixed(2)}, ${lo.toFixed(2)} (rounded to about 1 km). It is sent only if you submit.`);
      },
      (err) => {
        button.disabled = false;
        const reason =
          err.code === err.PERMISSION_DENIED
            ? "Location permission was declined."
            : err.code === err.TIMEOUT
              ? "Your browser took too long to find a location."
              : "Your browser could not determine a location.";
        set(null, null, `${reason} That's fine: country and city are enough.`);
      },
      { enableHighAccuracy: false, maximumAge: 10 * 60 * 1000, timeout: 15_000 },
    );
  });

  clear.addEventListener("click", () => {
    set(null, null, "Location removed.");
    button.focus();
  });
}

function initCounter(form: HTMLFormElement): void {
  const notes = form.querySelector<HTMLTextAreaElement>('[name="notes"]');
  const counter = form.querySelector<HTMLElement>("[data-notes-counter]");
  if (!notes || !counter) return;
  const update = () => {
    const n = codePointLength(notes.value.trim());
    counter.textContent = `${n} / ${LIMITS.notes}`;
    counter.classList.toggle("over", n > LIMITS.notes);
  };
  notes.addEventListener("input", update);
  update();
}

export function initVolunteerForm(): void {
  const form = document.querySelector<HTMLFormElement>("[data-volunteer-form]");
  if (!form) return;
  const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
  const success = document.querySelector<HTMLElement>("[data-form-success]");
  const endpoint = apiUrl(form.dataset.apiBase ?? "", "/api/v1/volunteers");

  initGeolocation(form);
  initCounter(form);
  if (submit) submit.disabled = false;

  // Clear a field's error, and its line in the summary, as soon as the
  // visitor edits it.
  const onEdit = (e: Event) => {
    const target = e.target as HTMLInputElement | null;
    if (!target?.name || target.name === "website") return;
    setFieldError(form, target.name as FieldName, undefined);
    const summary = form.querySelector<HTMLElement>("[data-error-summary]");
    summary?.querySelector(`[data-summary-for="${target.name}"]`)?.remove();
    if (summary && !summary.querySelector("li")) summary.hidden = true;
  };
  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);

  let sending = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (sending) return;
    const status = form.querySelector<HTMLElement>("[data-form-status]");
    if (status) status.hidden = true;

    const result = validateVolunteer(readForm(new FormData(form)));
    if (!result.ok) {
      showErrors(form, result.errors);
      return;
    }
    clearErrors(form);

    sending = true;
    const label = submit?.textContent ?? "";
    if (submit) {
      submit.disabled = true;
      submit.textContent = "Sending…";
    }
    form.setAttribute("aria-busy", "true");
    const outcome = await post(endpoint, result.body);
    form.removeAttribute("aria-busy");
    sending = false;
    if (submit) {
      submit.disabled = false;
      submit.textContent = label;
    }

    if (outcome.kind === "success") {
      form.hidden = true;
      if (success) {
        success.hidden = false;
        success.focus();
      }
      return;
    }
    if (outcome.kind === "validation" && Object.keys(outcome.fields).length > 0) {
      showErrors(form, outcome.fields, outcome.general);
      return;
    }
    showStatus(form, outcome);
  });
}
