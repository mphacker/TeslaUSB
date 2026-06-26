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
  SHELL_POLL_ALLOWLIST,
} from "./screen-helpers";

// Boombox UAT — live catalog-path wiring. Drives the REAL bundle webd serves at
// /boombox. The screen calls GET /api/boombox on mount (real webd endpoint),
// renders an active upload form, and shows an honest empty state when nothing is
// installed. The seed DB has no media, so the empty state always appears.
// Install/remove flows are mocked (gadgetd is not running in the UAT harness).

const PATH = "/boombox";
const SCREEN = "boombox";

test.describe("boombox UAT", () => {
  test("mocked list — audio player renders with media URL", async ({ page }) => {
    const mediaContentReqs: string[] = [];
    page.on("request", (r) => {
      if (new URL(r.url()).pathname === "/api/media/content") mediaContentReqs.push(r.url());
    });
    await page.route("**/api/boombox", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              name: "Horn.wav",
              rel_path: "Boombox/Horn.wav",
              size_bytes: 2048,
              modified: "2024-06-01T07:15:00Z",
            },
          ],
        }),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    const audio = page.locator("[data-testid=boombox-audio]");
    await expect(audio).toHaveCount(1);
    await expect(audio).toHaveAttribute("preload", "none");
    await expect(audio).toHaveAttribute("src", /\/api\/media\/content\?path=Boombox%2FHorn\.wav&v=/);
    // preload="none" must defer the byte fetch: nothing hits the content endpoint on render.
    await page.waitForTimeout(200);
    expect(
      mediaContentReqs,
      `unexpected media-content fetch on render: ${JSON.stringify(mediaContentReqs)}`,
    ).toEqual([]);
  });

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
      if (
        u.pathname.startsWith("/api/") &&
        u.pathname !== "/api/media-events" &&
        !SHELL_POLL_ALLOWLIST.has(u.pathname)
      ) {
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

  test("full-width — main-content cap removed so the card fills the width", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator(".media-pills")).toBeVisible();
    const hasClass = await page.evaluate(() =>
      document.body.classList.contains("screen-fullwidth"),
    );
    expect(hasClass, "body should carry the screen-fullwidth class").toBe(true);
    const maxWidth = await page
      .locator(".main-content")
      .evaluate((el) => getComputedStyle(el).maxWidth);
    expect(maxWidth, ".main-content max-width must be uncapped").toBe("none");
  });

  test("drag-and-drop — dropping a file stages it for upload", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=boombox-dropzone]");
    await expect(zone).toBeVisible();
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array([1, 2, 3])], "dropped.wav", {
        type: "audio/wav",
      }));
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await expect(page.getByText("dropped.wav", { exact: false })).toBeVisible();
  });

  test("upload button label is 'Upload' (not 'Install')", async ({ page }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=boombox-dropzone]");
    await expect(zone.getByRole("button", { name: "Upload" })).toBeVisible();
    await expect(zone).not.toContainText("Install");
  });

  test("drag-and-drop — multiple files stage and the button reflects the count", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=boombox-dropzone]");
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      for (const n of ["multi-a.wav", "multi-b.wav"]) {
        dt.items.add(
          new File([new Uint8Array([1, 2, 3])], n, { type: "audio/wav" }),
        );
      }
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await expect(page.getByText("multi-a.wav", { exact: false })).toBeVisible();
    await expect(page.getByText("multi-b.wav", { exact: false })).toBeVisible();
    await expect(
      zone.getByRole("button", { name: "Upload 2 files" }),
    ).toBeVisible();
  });

  test("upload flow — dropped file uploads and appears in the library without refresh", async ({
    page,
  }) => {
    let installed = false;
    await page.route("**/api/boombox", async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: installed
              ? [
                  {
                    name: "Horn.wav",
                    rel_path: "Boombox/Horn.wav",
                    size_bytes: 2048,
                    modified: "2024-06-01T07:15:00Z",
                  },
                ]
              : [],
          }),
        });
      }
      if (req.method() === "POST") {
        installed = true;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ state: "done" }),
        });
      }
      return route.continue();
    });

    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=boombox-audio]")).toHaveCount(0);

    const zone = page.locator("[data-testid=boombox-dropzone]");
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      dt.items.add(
        new File([new Uint8Array([1, 2, 3])], "Horn.wav", {
          type: "audio/wav",
        }),
      );
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await expect(zone.getByText("Horn.wav", { exact: false })).toBeVisible();

    await zone.getByRole("button", { name: "Upload" }).click();

    // Item 3: the library refetches itself — Horn.wav shows up with no reload.
    await expect(page.locator("[data-testid=boombox-audio]")).toHaveCount(1);
  });
});

