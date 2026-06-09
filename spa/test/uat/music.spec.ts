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

// Music UAT — v1 parity, strictly read-only. Drives the REAL bundle webd
// serves at /music. Parity target: legacy music.html (info banner + storage
// summary + browser panel + upload card). B-1 has no music endpoint, so the page
// reproduces the v1 look but makes zero API calls, renders the drop zone inert,
// and shows an honest pending state instead of fabricated rows.

const PATH = "/music";
const SCREEN = "music";

test.describe("music UAT", () => {
  test("parity — media nav active, pills, info/dropzone/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "music");

    await expect(
      page.locator('.container[data-screen="music"] h2'),
    ).toHaveText("Music Library");
    // Static v1 guidance renders verbatim.
    await expect(page.locator("[data-testid=music-info-banner]")).toBeVisible();
    await expect(page.locator("[data-testid=music-info-banner]")).toContainText(
      "Tesla only scans music inside the /Music folder",
    );
    // The drop zone renders in its inert disabled state (no upload wired).
    const drop = page.locator("[data-testid=music-dropzone]");
    await expect(drop).toBeVisible();
    await expect(drop).toHaveClass(/\bis-disabled\b/);
    // Honest empty library (no list endpoint).
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=music-empty]")).toContainText(
      "will list folders and files once webd can read the media partition",
    );
  });

  test("wiring — served HTML runs the built bundle and the music module ran", async ({
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
