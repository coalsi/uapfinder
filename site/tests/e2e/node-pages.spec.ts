import { expect, test } from "@playwright/test";
import { join } from "node:path";
import { collectErrors, mockNetwork, SCREENSHOT_DIR } from "./helpers";

test.describe("device and build pages", () => {
  test("header navigation reaches the node and build pages", async ({ page }, info) => {
    await mockNetwork(page);
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Main" });
    if (info.project.name === "mobile") await page.getByRole("button", { name: "Menu" }).click();
    await nav.getByRole("link", { name: "The node" }).click();
    await expect(page).toHaveURL(/\/device\/$/);
    await expect(page.locator("h1")).toHaveText(/sky camera/i);
    if (info.project.name === "mobile") await page.getByRole("button", { name: "Menu" }).click();
    await nav.getByRole("link", { name: "Build a node" }).click();
    await expect(page).toHaveURL(/\/build\/$/);
    await expect(page.locator('#site-nav a[aria-current="page"]')).toHaveText("Build a node");
  });

  test("device page diagrams have accessible names", async ({ page }) => {
    await mockNetwork(page);
    await page.goto("/device/");
    for (const name of [
      /Cutaway of a finished UAP Finder node/,
      /Two nodes 12 km apart/,
      /What camera a sees/,
      /What camera b sees/,
      /How GPS time lets two nodes/,
      /Successive fixes and the fitted path/,
      /Speed from point-to-point differences/,
      /Before calibration/,
      /After calibration/,
      /setup page before the node is claimed/,
      /node's status page/,
    ]) {
      await expect(page.getByRole("img", { name }).first()).toBeVisible();
    }
    await expect(page.getByRole("table", { name: /Node specifications/ })).toBeVisible();
  });

  test("stereo animation plays, pauses, and moves the object", async ({ page }) => {
    const errors = collectErrors(page);
    await mockNetwork(page);
    await page.goto("/device/#pair");
    const button = page.getByRole("button", { name: /animation/ });
    await expect(button).toHaveText("Pause animation");
    const obj = page.locator('[data-s="obj"]');
    const x0 = await obj.getAttribute("cx");
    await expect.poll(async () => obj.getAttribute("cx"), { timeout: 3000 }).not.toBe(x0);
    await button.click();
    await expect(button).toHaveText("Play animation");
    await expect(button).toHaveAttribute("aria-pressed", "false");
    const paused = await obj.getAttribute("cx");
    await page.waitForTimeout(300);
    expect(await obj.getAttribute("cx")).toBe(paused);
    expect(errors).toEqual([]);
  });

  test("with reduced motion the stereo animation does not start on its own", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await mockNetwork(page);
    await page.goto("/device/#pair");
    const button = page.getByRole("button", { name: /animation/ });
    await expect(button).toHaveText("Play animation");
    const obj = page.locator('[data-s="obj"]');
    const x0 = await obj.getAttribute("cx");
    await page.waitForTimeout(400);
    expect(await obj.getAttribute("cx")).toBe(x0);
    const running = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running").length);
    expect(running).toBe(0);
  });

  test("build guide: pin table, wiring diagram, steps and checklist", async ({ page }, info) => {
    await mockNetwork(page);
    await page.goto("/build/");
    const table = page.getByRole("table", { name: /40-pin header connections/ });
    await expect(table).toBeVisible();
    // Exactly the pins of docs/NODE_APPLIANCE.md, and what goes on them.
    const rows = await table.locator("tbody tr").evaluateAll((trs) =>
      trs.map((tr) => [tr.querySelector("th")!.textContent!.trim(), tr.querySelectorAll("td")[1]!.textContent!.trim()]),
    );
    expect(rows).toEqual([
      ["1", "GPS VIN (3.3 V modules only); BNO055 VIN"],
      ["2", "GPS VIN (modules that take 5 V)"],
      ["3", "BNO055 SDA"],
      ["5", "BNO055 SCL"],
      ["6", "GPS GND"],
      ["7", "GPS PPS"],
      ["8", "GPS RX"],
      ["9", "BNO055 GND"],
      ["10", "GPS TX"],
    ]);
    await expect(page.getByRole("img", { name: /Wiring the GPS module/ })).toBeVisible();
    for (const name of [/balenaEtcher/, /firmware version/, /NVMe SSD/, /camera ribbon into CAM0/, /microSD card goes/, /Mounting the node/, /How to aim/]) {
      await expect(page.getByRole("img", { name }).first()).toBeAttached();
    }
    // 16 numbered steps, each with a check box.
    await expect(page.locator(".bstep")).toHaveCount(16);
    await expect(page.locator(".bstep-check")).toHaveCount(16);
    for (const id of ["firmware", "flash", "camera", "gps", "setup", "troubleshooting", "command-line"]) {
      await expect(page.locator(`#${id}`)).toHaveCount(1);
    }
    await expect(page.getByRole("link", { name: "Get the system image" })).toHaveAttribute("href", /\/releases\/latest$/);
    const box = page.getByRole("checkbox", { name: /NVIDIA Jetson Orin Nano 8 GB developer kit/ });
    await box.check();
    await expect(box).toBeChecked();
    await expect(page.getByRole("button", { name: "Print checklist" })).toBeVisible();
    // Troubleshooting entries open.
    await page.getByText("No setup page found").click();
    await expect(page.getByText(/router's list of devices/)).toBeVisible();
    if (info.project.name === "mobile") {
      await page.locator("#gps").scrollIntoViewIfNeeded();
      await page.locator("#gps").screenshot({ path: join(SCREENSHOT_DIR, "mobile-build-gps-step.png") });
    }
  });

  test("download page leads with the node image", async ({ page }) => {
    await mockNetwork(page);
    await page.goto("/download/");
    const primary = page.locator(".page-hero .btn-primary");
    await expect(primary).toHaveText("Get the node image");
    await expect(primary).toHaveAttribute("href", /\/releases\/latest$/);
    await expect(page.getByText(/uapfinder-node-<version>-orin-nano\.img\.xz/).first()).toBeVisible();
    await expect(page.locator("#command-line")).toHaveCount(1);
  });
});
