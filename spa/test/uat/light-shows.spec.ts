import { test, expect, loadState } from "./helpers";
import {
  gotoScreen,
  assertMediaChrome,
  assertMediaPills,
  assertCleanConsole,
  assertCleanNetwork,
  assertWiring,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

// Light Shows UAT — live catalog-path wiring. GET /api/lightshows on mount
// (real webd endpoint). Seed DB has no media, so empty state always appears.
// Install/remove flows are mocked (gadgetd not running in the UAT harness).

const PATH = "/light_shows";
const SCREEN = "light-shows";

test.describe("light shows UAT", () => {
  test("parity — media nav active, pills, requirements/upload-form/empty", async ({
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
    // Upload form is live.
    const drop = page.locator("[data-testid=light-shows-dropzone]");
    await expect(drop).toBeVisible();
    await expect(page.locator('input[type=file]')).toBeVisible();
    // Honest empty state from real GET /api/lightshows.
    await expect(page.locator("[data-testid=light-shows-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=light-shows-empty]")).toContainText(
      "No light show files installed yet",
    );
  });

  test("wiring — served HTML runs the built bundle and the light-shows module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only on load — only GET /api/lightshows fires; no mutations until acted on", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=light-shows-empty]")).toBeVisible();
    await page.waitForTimeout(200);

    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);
    expect(sockets, `websocket(s): ${JSON.stringify(sockets)}`).toEqual([]);
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (u.pathname.startsWith("/api/")) {
        expect(
          `${req.method.toUpperCase()} ${u.pathname}`,
          `unexpected API call ${req.method} ${u.pathname}`,
        ).toBe("GET /api/lightshows");
      }
    }
    const reads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/lightshows",
    );
    expect(reads.length, "expected exactly one GET /api/lightshows on load").toBe(1);
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(1);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=light-shows-empty]")).toBeVisible();
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
