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

// Wraps UAT — live catalog-path wiring. GET /api/wraps on mount (real webd).
// Seed DB has no media, so empty state always appears.
// Install/remove flows are mocked (gadgetd not running in the UAT harness).

const PATH = "/wraps";
const SCREEN = "wraps";

test.describe("wraps UAT", () => {
  test("mocked list — preview thumbnail renders real image bytes", async ({ page }) => {
    await page.route("**/api/wraps", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              name: "UAT-Wrap.png",
              rel_path: "Wraps/UAT-Wrap.png",
              size_bytes: 18420,
              modified: "2024-06-01T07:15:00Z",
            },
          ],
        }),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    const thumb = page.locator("[data-testid=wraps-thumb]");
    await expect(thumb).toHaveCount(1);
    await expect(thumb).toHaveAttribute(
      "src",
      /\/api\/media\/content\?path=Wraps%2FUAT-Wrap\.png&v=/,
    );
    await expect.poll(async () => thumb.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
  });

  test("parity — media nav active, pills, requirements/upload-form/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "wraps");

    await expect(page.locator('.container[data-screen="wraps"] h2')).toHaveText(
      "Custom Wraps",
    );
    const requirements = page.locator("[data-testid=wraps-requirements]");
    await expect(requirements).toBeVisible();
    await expect(requirements).toContainText("Tesla Wrap Requirements");
    await expect(requirements).toContainText("PNG only");
    await expect(requirements).toContainText("512x512 to 1024x1024 pixels");
    await expect(requirements).toContainText("/Wraps");
    // Upload form is live (not disabled).
    const drop = page.locator("[data-testid=wraps-dropzone]");
    await expect(drop).toBeVisible();
    await expect(page.locator('input[type=file]')).toBeVisible();
    // Honest empty state from real GET /api/wraps.
    await expect(page.locator("[data-testid=wraps-library]")).toBeVisible();
    await expect(page.locator("[data-testid=wraps-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=wraps-empty]")).toContainText(
      "No custom wraps installed yet",
    );
  });

  test("wiring — served HTML runs the built bundle and the wraps module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only on load — only GET /api/wraps fires; no mutations until acted on", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=wraps-empty]")).toBeVisible();
    await page.waitForTimeout(200);

    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);
    expect(sockets, `websocket(s): ${JSON.stringify(sockets)}`).toEqual([]);
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (u.pathname.startsWith("/api/") && u.pathname !== "/api/media-events") {
        expect(
          `${req.method.toUpperCase()} ${u.pathname}`,
          `unexpected API call ${req.method} ${u.pathname}`,
        ).toBe("GET /api/wraps");
      }
    }
    const reads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/wraps",
    );
    expect(reads.length, "expected exactly one GET /api/wraps on load").toBe(1);
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(1);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=wraps-empty]")).toBeVisible();
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

