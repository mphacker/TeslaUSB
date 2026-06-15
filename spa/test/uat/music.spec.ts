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

// Music UAT — live catalog-path wiring. GET /api/music on mount (real webd).
// Seed DB has no media, so empty state always appears.

const PATH = "/music";
const SCREEN = "music";

test.describe("music UAT", () => {
  test("mocked list — audio player renders with media URL", async ({ page }) => {
    const mediaContentReqs: string[] = [];
    page.on("request", (r) => {
      if (new URL(r.url()).pathname === "/api/media/content") mediaContentReqs.push(r.url());
    });
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              name: "Song.mp3",
              rel_path: "Music/Song.mp3",
              size_bytes: 4096,
              modified: "2024-06-01T07:15:00Z",
            },
          ],
        }),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    const audio = page.locator("[data-testid=music-audio]");
    await expect(audio).toHaveCount(1);
    await expect(audio).toHaveAttribute("preload", "none");
    await expect(audio).toHaveAttribute("src", /\/api\/media\/content\?path=Music%2FSong\.mp3&v=/);
    // preload="none" must defer the byte fetch: nothing hits the content endpoint on render.
    await page.waitForTimeout(200);
    expect(
      mediaContentReqs,
      `unexpected media-content fetch on render: ${JSON.stringify(mediaContentReqs)}`,
    ).toEqual([]);
  });

  test("parity — media nav active, pills, info/upload-form/empty", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "music");

    await expect(
      page.locator('.container[data-screen="music"] h2'),
    ).toHaveText("Music Library");
    await expect(page.locator("[data-testid=music-info-banner]")).toBeVisible();
    await expect(page.locator("[data-testid=music-info-banner]")).toContainText(
      "Tesla only scans music inside the /Music folder",
    );
    // Upload form is live.
    const drop = page.locator("[data-testid=music-dropzone]");
    await expect(drop).toBeVisible();
    await expect(page.locator('input[type=file]')).toBeVisible();
    // Honest empty state from real GET /api/music.
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=music-empty]")).toContainText(
      "No music files installed yet",
    );
  });

  test("wiring — served HTML runs the built bundle and the music module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only on load — only GET /api/music fires; no mutations until acted on", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();
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
        ).toBe("GET /api/music");
      }
    }
    const reads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/music",
    );
    expect(reads.length, "expected exactly one GET /api/music on load").toBe(1);
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(1);
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();
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

