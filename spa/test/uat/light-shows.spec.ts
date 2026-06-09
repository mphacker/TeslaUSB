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

// Light Shows UAT — v1 parity, strictly read-only. Drives the REAL bundle webd
// serves at /light_shows. Parity target: legacy light_shows.html (media pills +
// requirements guidance + drag/drop zone + show library). B-1 has no toybox
// endpoint, so the page reproduces the v1 look but makes zero API calls, renders
// the drop zone inert, and shows an honest pending state instead of fabricated
// rows/actions.

const PATH = "/light_shows";
const SCREEN = "light-shows";

test.describe("light shows UAT", () => {
  test("parity — media nav active, pills, requirements/dropzone/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "shows");

    await expect(
      page.locator('.container[data-screen="light-shows"] h2'),
    ).toHaveText("Light Shows");
    await expect(page.locator("[data-testid=light-shows-requirements]")).toBeVisible();
    await expect(page.locator("[data-testid=light-shows-requirements]")).toContainText(
      "Tesla Light Show Requirements",
    );
    await expect(page.locator("[data-testid=light-shows-requirements]")).toContainText(
      "Individual files: .fseq, .mp3, .wav",
    );
    await expect(page.locator("[data-testid=light-shows-library]")).toBeVisible();
    await expect(page.locator("[data-testid=light-shows-library]")).toContainText(
      "Show Name",
    );

    const drop = page.locator("[data-testid=light-shows-dropzone]");
    await expect(drop).toBeVisible();
    await expect(drop).toHaveClass(/\bis-disabled\b/);
    await expect(drop).toContainText("Uploads are managed on the device");

    await expect(page.locator("[data-testid=light-shows-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=light-shows-empty]")).toContainText(
      "will be listed once webd can read the media partition",
    );
  });

  test("wiring — served HTML runs the built bundle and the light-shows module ran", async ({
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
