import { expect, test, type Page, type Request } from "@playwright/test";
import { join } from "node:path";
import { collectErrors, mockNetwork, SCREENSHOT_DIR } from "./helpers";

const GEO = { latitude: 33.629212, longitude: -112.367934 };

test.use({ geolocation: GEO, permissions: ["geolocation"] });

async function openForm(page: Page) {
  await mockNetwork(page);
  await page.goto("/get-involved/");
  await expect(page.getByRole("button", { name: "Register interest" })).toBeEnabled();
}

/** Intercept POST /api/v1/volunteers, recording each request. */
async function interceptSignup(
  page: Page,
  respond: { status: number; body?: unknown; headers?: Record<string, string> } | "abort",
): Promise<Request[]> {
  const seen: Request[] = [];
  await page.route("**/api/v1/volunteers", async (route) => {
    seen.push(route.request());
    if (respond === "abort") return route.abort("connectionrefused");
    return route.fulfill({
      status: respond.status,
      contentType: "application/json",
      headers: respond.headers,
      body: JSON.stringify(respond.body ?? {}),
    });
  });
  return seen;
}

async function fillMinimal(page: Page) {
  await page.getByLabel("Email").fill("jane@example.org");
  await page.getByLabel("Country").selectOption("US");
  await page.getByRole("checkbox", { name: /I agree/ }).check();
}

test.describe("volunteer form", () => {
  test("validates on the client and sends nothing until the form is valid", async ({ page }) => {
    const errors = collectErrors(page);
    await openForm(page);
    const posts = await interceptSignup(page, { status: 201, body: { status: "received" } });

    await page.getByRole("button", { name: "Register interest" }).click();
    const summary = page.locator("[data-error-summary]");
    await expect(summary).toBeVisible();
    await expect(summary).toBeFocused();
    await expect(summary.locator("li")).toHaveCount(3);
    await expect(page.getByLabel("Email")).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("#f-country-err")).toHaveText("Choose your country.");
    await expect(page.locator("#f-consent-err")).toBeVisible();

    await page.getByLabel("Email").fill("jane@localhost");
    await page.getByLabel("Country").selectOption("GB");
    await page.getByRole("checkbox", { name: /I agree/ }).check();
    await page.getByRole("button", { name: "Register interest" }).click();
    await expect(page.locator("#f-email-err")).toContainText("valid email");
    await expect(page.locator("#f-country-err")).toBeHidden();

    // Editing a field clears its error and its line in the summary.
    await expect(summary.locator("li")).toHaveCount(1);
    await page.locator("#signup-form").screenshot({ path: join(SCREENSHOT_DIR, `${test.info().project.name}-form-validation.png`) });
    await page.getByLabel("Email").fill("jane@example.org");
    await expect(page.locator("#f-email-err")).toBeHidden();
    await expect(summary).toBeHidden();

    expect(posts).toHaveLength(0);
    expect(errors).toEqual([]);
  });

  test("201: sends the documented JSON body with a rounded location and shows success", async ({ page }) => {
    const errors = collectErrors(page);
    await openForm(page);
    const posts = await interceptSignup(page, { status: 201, body: { status: "received" } });

    await page.getByLabel("Email").fill("  jane@example.org ");
    await page.getByLabel("Name").fill("Jane");
    await page.getByLabel("Country").selectOption("US");
    await page.getByLabel("State or region").fill("Arizona");
    await page.getByLabel("City or town").fill("Surprise");
    await page.getByLabel("Postal code").fill("85374");
    await page.getByRole("button", { name: "Use my approximate location" }).click();
    await expect(page.locator("[data-geo-output]")).toContainText("33.63, -112.37");
    await page.getByRole("radio", { name: "Rooftop" }).check();
    await page.getByRole("radio", { name: "None yet" }).check();
    await page.getByRole("checkbox", { name: /Host a node/ }).check();
    await page.getByRole("checkbox", { name: /Software/ }).check();
    await page.getByLabel("Anything else?").fill("Clear view to the south-west.");
    await page.getByRole("checkbox", { name: /I agree/ }).check();
    expect(posts).toHaveLength(0); // nothing sent before submit

    await page.getByRole("button", { name: "Register interest" }).click();
    const success = page.locator("[data-form-success]");
    await expect(success).toBeVisible();
    await expect(success).toBeFocused();
    await expect(success).toContainText("your interest is registered");
    await expect(page.locator("#signup-form")).toBeHidden();

    expect(posts).toHaveLength(1);
    const req = posts[0]!;
    expect(req.method()).toBe("POST");
    expect(req.headers()["content-type"]).toBe("application/json");
    expect(req.postDataJSON()).toEqual({
      email: "jane@example.org",
      name: "Jane",
      country: "US",
      region: "Arizona",
      city: "Surprise",
      postal_code: "85374",
      approx_lat: 33.63,
      approx_lon: -112.37,
      site_type: "rooftop",
      has_hardware: "none",
      interests: ["host_node", "software"],
      notes: "Clear view to the south-west.",
      consent: true,
      website: "",
    });
    // The precise browser position never appears in the request.
    expect(req.postData()).not.toContain("33.6292");
    await success.screenshot({ path: join(SCREENSHOT_DIR, `${test.info().project.name}-form-success.png`) });
    expect(errors).toEqual([]);
  });

  test("location can be removed again", async ({ page }) => {
    await openForm(page);
    const posts = await interceptSignup(page, { status: 201, body: { status: "received" } });
    await page.getByRole("button", { name: "Use my approximate location" }).click();
    await expect(page.locator("[data-geo-output]")).toContainText("33.63");
    await page.getByRole("button", { name: "Remove location" }).click();
    await fillMinimal(page);
    await page.getByRole("button", { name: "Register interest" }).click();
    await expect(page.locator("[data-form-success]")).toBeVisible();
    expect(posts[0]!.postDataJSON()).toEqual({ email: "jane@example.org", country: "US", consent: true, website: "" });
  });

  test("422: maps server validation errors onto fields", async ({ page }) => {
    await openForm(page);
    await interceptSignup(page, {
      status: 422,
      body: { detail: [{ loc: ["body", "email"], msg: "value is not a valid email address", type: "value_error" }] },
    });
    await fillMinimal(page);
    await page.getByRole("button", { name: "Register interest" }).click();
    await expect(page.locator("#f-email-err")).toHaveText("Value is not a valid email address.");
    await expect(page.getByLabel("Email")).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("[data-error-summary]")).toBeFocused();
    await expect(page.locator("[data-form-success]")).toBeHidden();
  });

  test("429: explains the rate limit", async ({ page }) => {
    await openForm(page);
    await interceptSignup(page, { status: 429, body: { detail: "rate limit exceeded" }, headers: { "Retry-After": "1800" } });
    await fillMinimal(page);
    await page.getByRole("button", { name: "Register interest" }).click();
    const status = page.locator("[data-form-status]");
    await expect(status).toBeVisible();
    await expect(status).toHaveAttribute("data-outcome", "rate_limited");
    await expect(status).toContainText("Too many submissions");
    await expect(status).toContainText("about 30 minutes");
    await expect(page.getByRole("button", { name: "Register interest" })).toBeEnabled();
    await status.screenshot({ path: join(SCREENSHOT_DIR, `${test.info().project.name}-form-429.png`) });
  });

  test("503: reports that sign-up is unavailable and keeps the entries", async ({ page }) => {
    await openForm(page);
    await interceptSignup(page, { status: 503, body: { detail: "sign-up unavailable" } });
    await fillMinimal(page);
    await page.getByRole("button", { name: "Register interest" }).click();
    const status = page.locator("[data-form-status]");
    await expect(status).toHaveAttribute("data-outcome", "unavailable");
    await expect(status).toContainText("temporarily unavailable");
    await expect(page.getByLabel("Email")).toHaveValue("jane@example.org");
  });

  test("network failure is reported", async ({ page }) => {
    await openForm(page);
    await interceptSignup(page, "abort");
    await fillMinimal(page);
    await page.getByRole("button", { name: "Register interest" }).click();
    await expect(page.locator("[data-form-status]")).toContainText("could not reach");
  });

  test("honeypot is invisible and unreachable by keyboard; every control is labelled", async ({ page }) => {
    await openForm(page);
    const hp = page.locator('input[name="website"]');
    await expect(hp).toHaveAttribute("tabindex", "-1");
    const box = await hp.boundingBox();
    expect(box === null || box.x + box.width < 0).toBe(true);
    await expect(page.locator(".hp")).toHaveAttribute("aria-hidden", "true");

    const unlabelled = await page.$$eval("#signup-form input:not([type=hidden]), #signup-form select, #signup-form textarea", (els) =>
      els
        .filter((el) => (el as HTMLInputElement).name !== "website")
        .filter((el) => {
          const id = el.id;
          const byFor = id && document.querySelector(`label[for="${id}"]`);
          return !byFor && !el.closest("label") && !el.getAttribute("aria-label");
        })
        .map((el) => (el as HTMLInputElement).name),
    );
    expect(unlabelled).toEqual([]);
  });
});
