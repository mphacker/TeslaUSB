import { test, expect } from "./helpers";
import {
  gotoScreen,
  assertMediaChrome,
  assertMediaPills,
  assertCleanConsole,
  assertCleanNetwork,
  assertReadOnly,
  assertWiring,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

// License Plates UAT — v1 parity, strictly read-only. Drives the REAL bundle
// webd serves at /license_plates. Parity target: legacy license_plates.html
// (requirements card + upload zone + license-plate library table). B-1 has no
// toybox endpoint, so the page reproduces the v1 look but makes zero API calls,
// renders the drop zone inert, and shows an honest pending library state instead
// of fabricated rows/actions.

const PATH = "/license_plates";
const SCREEN = "plates";

test.describe("license plates UAT", () => {
  test("parity — media nav active, pills, requirements/dropzone/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "plates");

    await expect(
      page.locator('.container[data-screen="plates"] h2'),
    ).toHaveText("Custom License Plates");
    await expect(
      page.locator("[data-testid=license-plates-requirements]"),
    ).toBeVisible();
    await expect(
      page.locator("[data-testid=license-plates-requirements]"),
    ).toContainText("Tesla License-Plate Requirements");
    await expect(
      page.locator("[data-testid=license-plates-requirements]"),
    ).toContainText("PNG only");
    await expect(
      page.locator("[data-testid=license-plates-requirements]"),
    ).toContainText("420x75");
    await expect(
      page.locator("[data-testid=license-plates-requirements]"),
    ).toContainText("492x75");

    const drop = page.locator("[data-testid=license-plates-dropzone]");
    await expect(drop).toBeVisible();
    await expect(drop).toHaveClass(/\bis-disabled\b/);

    await expect(
      page.locator("[data-testid=license-plates-library]"),
    ).toBeVisible();
    await expect(
      page.locator("[data-testid=license-plates-empty]"),
    ).toBeVisible();
  });

  test("wiring — served HTML runs the built bundle and the plates module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only — no mutations, no API calls, no mutation surface", async ({
    page,
    probe,
  }) => {
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    await assertReadOnly(page, probe, sockets);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    assertCleanConsole(probe);
    assertCleanNetwork(probe);
  });

  test("perf — capture TTFB/FCP + slowest requests", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await capturePerf(page, testInfo, SCREEN);
  });

  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator(".media-pills")).toBeVisible();
    await captureScreenshot(page, testInfo, SCREEN);
  });
});
