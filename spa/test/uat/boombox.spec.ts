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

// Boombox UAT — v1 parity, strictly read-only. Drives the REAL bundle webd
// serves at /boombox. Parity target: legacy boombox.html (NHTSA warning +
// requirements card + upload zone + sound library). B-1 has no toybox endpoint,
// so the page reproduces the v1 look but makes zero API calls, renders the drop
// zone inert, and shows the v1 empty-state instead of fabricated rows.

const PATH = "/boombox";
const SCREEN = "boombox";

test.describe("boombox UAT", () => {
  test("parity — media nav active, pills, warning/requirements/dropzone/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "boombox");

    await expect(
      page.locator('.container[data-screen="boombox"] h2'),
    ).toHaveText("Boombox");
    // Static v1 guidance renders verbatim.
    await expect(page.locator(".boombox-nhtsa-warning")).toBeVisible();
    await expect(page.locator(".boombox-nhtsa-warning")).toContainText(
      "only play while the vehicle is in Park",
    );
    await expect(page.locator(".boombox-requirements")).toContainText(
      "Tesla Boombox Requirements",
    );
    await expect(page.locator(".boombox-requirements")).toContainText(
      "MP3 or WAV only",
    );
    // The drop zone renders in its inert disabled state (no upload wired).
    const drop = page.locator("[data-testid=boombox-dropzone]");
    await expect(drop).toBeVisible();
    await expect(drop).toHaveClass(/\bis-disabled\b/);
    // Honest empty library (no list endpoint).
    await expect(page.locator("[data-testid=boombox-empty]")).toBeVisible();
  });

  test("wiring — served HTML runs the built bundle and the boombox module ran", async ({
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
