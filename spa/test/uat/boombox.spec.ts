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

// Boombox UAT — live catalog-path wiring. Drives the REAL bundle webd serves at
// /boombox. The screen calls GET /api/boombox on mount (real webd endpoint),
// renders an active upload form, and shows an honest empty state when nothing is
// installed. The seed DB has no media, so the empty state always appears.
// Install/remove flows are mocked (gadgetd is not running in the UAT harness).

const PATH = "/boombox";
const SCREEN = "boombox";

test.describe("boombox UAT", () => {
  test("parity — media nav active, pills, warning/requirements/upload-form/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "boombox");

    await expect(
      page.locator('.container[data-screen="boombox"] h2'),
    ).toHaveText("Boombox");
    // Static v1 guidance renders.
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
    // The upload form is live (not disabled).
    const drop = page.locator("[data-testid=boombox-dropzone]");
    await expect(drop).toBeVisible();
    await expect(page.locator('input[type=file]')).toBeVisible();
    // Honest empty state from the real GET /api/boombox response.
    await expect(page.locator("[data-testid=boombox-empty]")).toBeVisible();
  });

  test("wiring — served HTML runs the built bundle and the boombox module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only on load — only GET /api/boombox fires; no mutations until acted on", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=boombox-empty]")).toBeVisible();
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
        ).toBe("GET /api/boombox");
      }
    }
    const reads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/boombox",
    );
    expect(reads.length, "expected exactly one GET /api/boombox on load").toBe(1);
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(1);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=boombox-empty]")).toBeVisible();
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

