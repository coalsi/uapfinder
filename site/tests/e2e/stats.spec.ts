import { expect, test } from "@playwright/test";
import { join } from "node:path";
import { collectErrors, mockNetwork, SCREENSHOT_DIR } from "./helpers";

test.describe("live network stats", () => {
  test("degrades to 'network launching' when the API is unreachable", async ({ page }) => {
    const errors = collectErrors(page);
    const requested: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/")) requested.push(new URL(r.url()).pathname);
    });
    await mockNetwork(page, { stats: "down", volunteers: "down" });
    await page.goto("/");
    const widget = page.locator("[data-live-stats]");
    await expect(widget).toHaveAttribute("data-state", "launching");
    await expect(widget.locator("[data-live-note]")).toContainText("The network is launching");
    for (const v of await widget.locator("[data-value]").allTextContents()) expect(v.trim()).toBe("—");
    expect(requested.sort()).toEqual(["/api/v1/stats", "/api/v1/volunteers/summary"]);
    await widget.screenshot({ path: join(SCREENSHOT_DIR, `${test.info().project.name}-stats-launching.png`) });
    expect(errors).toEqual([]);
  });

  test("shows live figures from /stats and /volunteers/summary", async ({ page }) => {
    await mockNetwork(page, { stats: "ok", volunteers: "ok" });
    await page.goto("/");
    const widget = page.locator("[data-live-stats]");
    await expect(widget).toHaveAttribute("data-state", "live");
    await expect(widget.locator('[data-tile="nodes"] [data-value]')).toHaveText("12");
    await expect(widget.locator('[data-tile="tracks"] [data-value]')).toHaveText("2");
    await expect(widget.locator('[data-tile="packets"] [data-value]')).toHaveText("812");
    await expect(widget.locator('[data-tile="volunteers"] [data-value]')).toHaveText("42");
    await expect(widget.locator("[data-live-note]")).toContainText("Live from the network API");
  });

  test("keeps the volunteer count when only /stats fails, and vice versa", async ({ page }) => {
    await mockNetwork(page, { stats: "down", volunteers: "ok" });
    await page.goto("/");
    const widget = page.locator("[data-live-stats]");
    await expect(widget).toHaveAttribute("data-state", "launching");
    await expect(widget.locator('[data-tile="volunteers"] [data-value]')).toHaveText("42");
    await expect(widget.locator("[data-live-note]")).toContainText("sensor network is launching");

    await page.unrouteAll({ behavior: "ignoreErrors" });
    await mockNetwork(page, { stats: "ok", volunteers: "503" });
    await page.reload();
    await expect(widget).toHaveAttribute("data-state", "live");
    await expect(widget.locator('[data-tile="volunteers"] [data-value]')).toHaveText("—");
  });
});
