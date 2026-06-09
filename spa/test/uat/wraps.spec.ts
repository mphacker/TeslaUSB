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

// Wraps UAT — v1 parity, strictly read-only. Drives the REAL bundle webd
// serves at /wraps. Parity target: legacy wraps.html (requirements card +
// upload zone + wrap library). B-1 has no toybox endpoint, so the page
// reproduces the v1 look but makes zero API calls, renders the drop zone inert,
// and shows an honest empty/pending state instead of fabricated rows/actions.

const PATH = "/wraps";
const SCREEN = "wraps";

test.describe("wraps UAT", () => {
  test("parity — media nav active, pills, requirements/dropzone/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "wraps");

    await expect(page.locator('.container[data-screen="wraps"] h2')).toHaveText(
      "Custom Wraps",
    );
    // Static v1 guidance renders in the carried-over requirements card.
    const requirements = page.locator("[data-testid=wraps-requirements]");
    await expect(requirements).toBeVisible();
    await expect(requirements).toContainText("Tesla Wrap Requirements");
    await expect(requirements).toContainText("PNG only");
    await expect(requirements).toContainText("512x512 to 1024x1024 pixels");
    await expect(requirements).toContainText("/LightShow/wraps");
    // The drop zone renders in its inert disabled state (no upload wired).
    const drop = page.locator("[data-testid=wraps-dropzone]");
    await expect(drop).toBeVisible();
    await expect(drop).toHaveClass(/\bis-disabled\b/);
    await expect(drop).toContainText("Uploads are managed on the device");
    // Honest empty library (no list endpoint).
    await expect(page.locator("[data-testid=wraps-library]")).toBeVisible();
    await expect(page.locator("[data-testid=wraps-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=wraps-empty]")).toContainText(
      "webd can read the media partition",
    );
  });

  test("wiring — served HTML runs the built bundle and the wraps module ran", async ({
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
