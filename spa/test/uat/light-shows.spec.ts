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

// Light Shows UAT — live catalog-path wiring. GET /api/lightshows on mount
// (real webd endpoint). Seed DB has no media, so empty state always appears.
// Install/remove flows are mocked (gadgetd not running in the UAT harness).

const PATH = "/light_shows";
const SCREEN = "light-shows";

test.describe("light shows UAT", () => {
  test("mocked list — only audio rows render a player", async ({ page }) => {
    const mediaContentReqs: string[] = [];
    page.on("request", (r) => {
      if (new URL(r.url()).pathname === "/api/media/content") mediaContentReqs.push(r.url());
    });
    await page.route("**/api/lightshows", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              name: "Show.mp3",
              rel_path: "LightShow/Show.mp3",
              size_bytes: 8192,
              modified: "2024-06-01T07:15:00Z",
            },
            {
              name: "Show.fseq",
              rel_path: "LightShow/Show.fseq",
              size_bytes: 100000,
              modified: "2024-06-01T07:15:00Z",
            },
          ],
        }),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    const audio = page.locator("[data-testid=light-shows-audio]");
    await expect(audio).toHaveCount(1);
    await expect(audio.first()).toHaveAttribute("preload", "none");
    await expect(audio.first()).toHaveAttribute("src", /\/api\/media\/content\?path=LightShow%2FShow\.mp3&v=/);
    // preload="none" must defer the byte fetch: nothing hits the content endpoint on render.
    await page.waitForTimeout(200);
    expect(
      mediaContentReqs,
      `unexpected media-content fetch on render: ${JSON.stringify(mediaContentReqs)}`,
    ).toEqual([]);
  });

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
      if (
        u.pathname.startsWith("/api/") &&
        u.pathname !== "/api/media-events" &&
        !SHELL_POLL_ALLOWLIST.has(u.pathname)
      ) {
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

  test("drag-and-drop — dropping a file stages it for upload", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=light-shows-dropzone]");
    await expect(zone).toBeVisible();
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array([1, 2, 3])], "dropped.fseq", {
        type: "application/octet-stream",
      }));
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await expect(page.getByText("dropped.fseq", { exact: false })).toBeVisible();
  });

  test("upload button label is 'Upload' (not 'Install')", async ({ page }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=light-shows-dropzone]");
    await expect(zone.getByRole("button", { name: "Upload" })).toBeVisible();
    await expect(zone).not.toContainText("Install");
  });

  test("drag-and-drop — multiple files stage and the button reflects the count", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=light-shows-dropzone]");
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      for (const n of ["multi-a.fseq", "multi-b.fseq"]) {
        dt.items.add(
          new File([new Uint8Array([1, 2, 3])], n, {
            type: "application/octet-stream",
          }),
        );
      }
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await expect(page.getByText("multi-a.fseq", { exact: false })).toBeVisible();
    await expect(page.getByText("multi-b.fseq", { exact: false })).toBeVisible();
    await expect(
      zone.getByRole("button", { name: "Upload 2 files" }),
    ).toBeVisible();
  });

  test("oversize file — a 413 surfaces a friendly 'too large' message, not a connection error", async ({
    page,
  }) => {
    await page.route("**/api/lightshows", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items: [] }),
        });
      }
      // Mimic webd's DefaultBodyLimit rejection (plain-text 413).
      return route.fulfill({
        status: 413,
        contentType: "text/plain",
        body: "length limit exceeded",
      });
    });
    await gotoScreen(page, PATH, SCREEN);
    const zone = page.locator("[data-testid=light-shows-dropzone]");
    await zone.evaluate((el) => {
      const dt = new DataTransfer();
      dt.items.add(
        new File([new Uint8Array([1, 2, 3])], "huge.mp3", {
          type: "audio/mpeg",
        }),
      );
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });
    await zone.getByRole("button", { name: "Upload" }).click();
    await expect(
      page.getByText("That file is too large to upload", { exact: false }).first(),
    ).toBeVisible();
    await expect(page.getByText("Couldn't reach the device", { exact: false })).toHaveCount(0);
  });

  test("layout — row checkbox sits in its own column, not over the show name", async ({
    page,
  }) => {
    await page.route("**/api/lightshows", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              name: "CYBERTRUCK - A Very Long Light Show Name That Wraps.fseq",
              rel_path: "LightShow/CYBERTRUCK - A Very Long Light Show Name That Wraps.fseq",
              size_bytes: 1153434,
              modified: "2024-06-01T07:15:00Z",
            },
          ],
        }),
      });
    });
    await gotoScreen(page, PATH, SCREEN);
    const checkbox = page.locator(".light-shows-video-table tbody .bulk-row-check").first();
    const nameCell = page
      .locator(".light-shows-video-table tbody tr")
      .first()
      .locator("td")
      .nth(1);
    await expect(checkbox).toBeVisible();
    const cb = await checkbox.boundingBox();
    const name = await nameCell.boundingBox();
    expect(cb).not.toBeNull();
    expect(name).not.toBeNull();
    // The checkbox must finish before the name cell begins — i.e. its own column.
    expect(cb!.x + cb!.width).toBeLessThanOrEqual(name!.x + 1);
  });
});
