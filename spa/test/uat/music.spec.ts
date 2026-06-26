import { test, expect, loadState, GADGET_STATUS_OK } from "./helpers";
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

// Music UAT — covers live catalog-path wiring (real webd) and mocked-API
// interaction tests for the new Music-library features (folders, upload,
// delete, navigation, convergence).

const PATH = "/music";
const SCREEN = "music";

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Fulfil with a MediaList JSON response. */
function musicListBody(
  items: Array<{
    name: string;
    rel_path: string;
    size_bytes: number;
    modified: string | null;
  }>,
) {
  return JSON.stringify({ items });
}

/** A single queued handoff result (202). */
const QUEUED_BODY = JSON.stringify({ state: "queued", job_id: "j-1" });

// ── Viewport-aware selectors ────────────────────────────────────────────────
// The Music screen renders two parallel layouts toggled by CSS media query:
// desktop tables (testids `music-folder-list`/`music-list`, per-item
// `music-folder-row-*`/`music-move-*`/`music-delete-*`) and mobile cards
// (`music-mobile-folders`/`music-mobile-files`, per-item
// `music-mobile-folder-row-*`/`music-mobile-move-*`/`music-mobile-delete-*`).
// Interaction tests must drive the VISIBLE layout for their project, so we pick
// selectors off the project name (the repo convention used across UAT specs).
function viewportSelectors(projectName: string) {
  const mobile = projectName.includes("375");
  return {
    mobile,
    folderList: mobile
      ? "[data-testid=music-mobile-folders]"
      : "[data-testid=music-folder-list]",
    fileList: mobile
      ? "[data-testid=music-mobile-files]"
      : "[data-testid=music-list]",
    folderRow: (folder: string) =>
      mobile
        ? `[data-testid="music-mobile-folder-row-${folder}"]`
        : `[data-testid="music-folder-row-${folder}"]`,
    moveBtn: (name: string) =>
      mobile
        ? `[data-testid="music-mobile-move-${name}"]`
        : `[data-testid="music-move-${name}"]`,
    deleteBtn: (name: string) =>
      mobile
        ? `[data-testid="music-mobile-delete-${name}"]`
        : `[data-testid="music-delete-${name}"]`,
  };
}

test.describe("music UAT", () => {
  // ── Existing passing tests (kept verbatim) ──────────────────────────────────

  test("mocked list — audio player renders with media URL", async ({ page }) => {
    const mediaContentReqs: string[] = [];
    page.on("request", (r) => {
      if (new URL(r.url()).pathname === "/api/media/content")
        mediaContentReqs.push(r.url());
    });
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody([
          {
            name: "Song.mp3",
            rel_path: "Music/Song.mp3",
            size_bytes: 4096,
            modified: "2024-06-01T07:15:00Z",
          },
        ]),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    const audio = page.locator("[data-testid=music-audio]");
    await expect(audio).toHaveCount(1);
    await expect(audio).toHaveAttribute("preload", "none");
    await expect(audio).toHaveAttribute(
      "src",
      /\/api\/media\/content\?path=Music%2FSong\.mp3&v=/,
    );
    // preload="none" must defer the byte fetch.
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
    await expect(
      page.locator("[data-testid=music-info-banner]"),
    ).toContainText("Tesla only scans music inside the /Music folder");
    // Upload controls are live.
    const drop = page.locator("[data-testid=music-dropzone]");
    await expect(drop).toBeVisible();
    await expect(page.locator("input[type=file]")).toBeVisible();
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

  test("read-only on load — only GET /api/music and GET /api/storage fire; no mutations until acted on", async ({
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
    expect(
      mutating,
      `mutating request(s): ${JSON.stringify(mutating)}`,
    ).toEqual([]);
    expect(sockets, `websocket(s): ${JSON.stringify(sockets)}`).toEqual([]);

    // Allow the two read-only fetches that fire on mount.
    const ALLOWED_API = new Set(["/api/music", "/api/storage"]);
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (
        u.pathname.startsWith("/api/") &&
        u.pathname !== "/api/media-events" &&
        !SHELL_POLL_ALLOWLIST.has(u.pathname)
      ) {
        const call = `${req.method.toUpperCase()} ${u.pathname}`;
        expect(
          ALLOWED_API.has(u.pathname) && req.method.toUpperCase() === "GET",
          `unexpected API call: ${call}`,
        ).toBe(true);
      }
    }
    const musicReads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/music",
    );
    expect(
      musicReads.length,
      "expected exactly one GET /api/music on load",
    ).toBe(1);
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

  // ── New tests for the 6 UX fixes ───────────────────────────────────────────

  test("upload button label is 'Upload' (not 'Install')", async ({ page }) => {
    await gotoScreen(page, PATH, SCREEN);
    const btn = page.locator("[data-testid=music-upload-btn]");
    await expect(btn).toBeVisible();
    await expect(btn).toHaveText("Upload");
    // Intro copy must not say "Installing"
    const panel = page.locator("[data-testid=music-upload-panel]");
    await expect(panel).not.toContainText("Installing");
  });

  test("audio element has controls attribute", async ({ page }) => {
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody([
          {
            name: "track.mp3",
            rel_path: "Music/track.mp3",
            size_bytes: 2048,
            modified: null,
          },
        ]),
      });
    });
    await gotoScreen(page, PATH, SCREEN);
    const audio = page.locator("[data-testid=music-audio]");
    await expect(audio).toHaveCount(1);
    // controls attribute must be present for a real, usable player
    await expect(audio).toHaveAttribute("controls", "");
    // Sanity: preload=none so it doesn't auto-fetch
    await expect(audio).toHaveAttribute("preload", "none");
  });

  test("create folder — appears in folder list after convergence", async ({
    page,
  }, testInfo) => {
    const sel = viewportSelectors(testInfo.project.name);
    let musicCallCount = 0;
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      musicCallCount++;
      // Second GET (poll after POST) returns the placeholder keep-file
      const items =
        musicCallCount >= 2
          ? [
              {
                name: ".teslausb-keep",
                rel_path: "Music/NewFolder/.teslausb-keep",
                size_bytes: 0,
                modified: null,
              },
            ]
          : [];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody(items),
      });
    });
    await page.route("**/api/music/folder", (route) =>
      route.fulfill({
        status: 202,
        contentType: "application/json",
        body: QUEUED_BODY,
      }),
    );

    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();

    // Type a folder name and create it
    await page
      .locator("[data-testid=music-folder-name-input]")
      .fill("NewFolder");
    await page.locator("[data-testid=music-create-folder-btn]").click();

    // Folder must appear in the (visible) folder list after convergence poll.
    await expect(page.locator(sel.folderList)).toContainText("NewFolder", {
      timeout: 8000,
    });
  });

  test("navigate into folder — breadcrumb updates and only folder files shown", async ({
    page,
  }, testInfo) => {
    const sel = viewportSelectors(testInfo.project.name);
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody([
          // A file at the root
          {
            name: "root.mp3",
            rel_path: "Music/root.mp3",
            size_bytes: 1024,
            modified: null,
          },
          // A file in a subfolder
          {
            name: "deep.mp3",
            rel_path: "Music/DaftPunk/deep.mp3",
            size_bytes: 2048,
            modified: null,
          },
        ]),
      });
    });

    await gotoScreen(page, PATH, SCREEN);

    // Root view: folder DaftPunk visible, root.mp3 visible (in visible layout)
    await expect(page.locator(sel.folderList)).toContainText("DaftPunk");
    await expect(page.locator(sel.fileList)).toContainText("root.mp3");

    // Enter the folder by clicking the row in the visible layout
    await page.locator(sel.folderRow("DaftPunk")).click();

    // Breadcrumb should show DaftPunk
    await expect(
      page.locator("[data-testid=music-breadcrumb]"),
    ).toContainText("DaftPunk");

    // Only deep.mp3 should be listed (root.mp3 is not in DaftPunk/)
    await expect(page.locator(sel.fileList)).toContainText("deep.mp3");
    await expect(page.locator(sel.fileList)).not.toContainText("root.mp3");
  });

  test("delete file — removed from list after convergence", async ({
    page,
  }, testInfo) => {
    const sel = viewportSelectors(testInfo.project.name);
    let musicCallCount = 0;
    await page.route("**/api/music", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      musicCallCount++;
      // First GET: file present. Second+ GET (poll): file gone.
      const items =
        musicCallCount === 1
          ? [
              {
                name: "gone.mp3",
                rel_path: "Music/gone.mp3",
                size_bytes: 512,
                modified: null,
              },
            ]
          : [];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody(items),
      });
    });
    await page.route("**/api/music/delete", (route) =>
      route.fulfill({
        status: 202,
        contentType: "application/json",
        body: QUEUED_BODY,
      }),
    );

    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator(sel.fileList)).toContainText("gone.mp3");

    // Capture the delete request to verify the path is stripped (no "Music/" prefix).
    const deleteReqPromise = page.waitForRequest("**/api/music/delete");

    // Click the per-row Delete button in the visible layout
    await page.locator(sel.deleteBtn("gone.mp3")).click();

    // Verify the request body sends the STRIPPED subpath (the bug was "Music/gone.mp3").
    const deleteReq = await deleteReqPromise;
    expect(JSON.parse(deleteReq.postData() ?? "{}")).toEqual({
      paths: ["gone.mp3"],
    });

    // After convergence (≤ 2 s poll), the list empties → both layouts unmount and
    // the global empty state appears. Assert the user-visible outcome, plus that
    // the deleted item's button is gone from the active layout.
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible({
      timeout: 8000,
    });
    await expect(page.locator(sel.deleteBtn("gone.mp3"))).toHaveCount(0);
  });

  for (const tc of [
    {
      name: "rename + move preserves source extension",
      typedName: "renamed",
      expectedTo: "DaftPunk/renamed.mp3",
      expectedDestName: "renamed.mp3",
    },
    {
      name: "blank rename keeps original filename",
      typedName: "",
      expectedTo: "DaftPunk/track.mp3",
      expectedDestName: "track.mp3",
    },
    {
      name: "explicit extension is respected",
      typedName: "song.wav",
      expectedTo: "DaftPunk/song.wav",
      expectedDestName: "song.wav",
    },
  ]) {
    test(`move file — ${tc.name}`, async ({ page }, testInfo) => {
      const sel = viewportSelectors(testInfo.project.name);
      let musicCallCount = 0;
      await page.route("**/api/music", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        musicCallCount++;
        let items: Array<{
          name: string;
          rel_path: string;
          size_bytes: number;
          modified: string | null;
        }>;
        if (musicCallCount === 1) {
          // Initial: source at root + a keep-file so DaftPunk appears as a folder.
          items = [
            {
              name: "track.mp3",
              rel_path: "Music/track.mp3",
              size_bytes: 1024,
              modified: null,
            },
            {
              name: ".teslausb-keep",
              rel_path: "Music/DaftPunk/.teslausb-keep",
              size_bytes: 0,
              modified: null,
            },
          ];
        } else if (musicCallCount === 2) {
          // After copy lands: both source and destination present → phase 1 converges.
          items = [
            {
              name: "track.mp3",
              rel_path: "Music/track.mp3",
              size_bytes: 1024,
              modified: null,
            },
            {
              name: ".teslausb-keep",
              rel_path: "Music/DaftPunk/.teslausb-keep",
              size_bytes: 0,
              modified: null,
            },
            {
              name: tc.expectedDestName,
              rel_path: `Music/DaftPunk/${tc.expectedDestName}`,
              size_bytes: 1024,
              modified: null,
            },
          ];
        } else {
          // After source-delete: only destination present → phase 2 converges.
          items = [
            {
              name: ".teslausb-keep",
              rel_path: "Music/DaftPunk/.teslausb-keep",
              size_bytes: 0,
              modified: null,
            },
            {
              name: tc.expectedDestName,
              rel_path: `Music/DaftPunk/${tc.expectedDestName}`,
              size_bytes: 1024,
              modified: null,
            },
          ];
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: musicListBody(items),
        });
      });

      await page.route("**/api/music/move", (route) =>
        route.fulfill({
          status: 202,
          contentType: "application/json",
          body: QUEUED_BODY,
        }),
      );
      await page.route("**/api/gadget/status", (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(GADGET_STATUS_OK),
        }),
      );
      await page.route("**/api/music/delete", (route) =>
        route.fulfill({
          status: 202,
          contentType: "application/json",
          body: QUEUED_BODY,
        }),
      );

      const consoleErrors: string[] = [];
      page.on("console", (msg) => {
        if (msg.type() === "error") consoleErrors.push(msg.text());
      });
      page.on("pageerror", (err) => consoleErrors.push(err.message));

      await gotoScreen(page, PATH, SCREEN);
      // Source file visible at root.
      await expect(page.locator(sel.fileList)).toContainText("track.mp3");

      // Open the move dialog from the visible layout.
      await page.locator(sel.moveBtn("track.mp3")).click();
      // Select DaftPunk as the destination.
      await page.locator("[data-testid=music-move-dest]").selectOption("DaftPunk");
      await page.locator("[data-testid=music-move-newname]").fill(tc.typedName);

      // Capture move + phase-2 source-delete BEFORE clicking confirm.
      const moveReqPromise = page.waitForRequest("**/api/music/move");
      const deleteReqPromise = page.waitForRequest("**/api/music/delete");
      await page.locator("[data-testid=music-move-confirm-btn]").click();

      const moveReq = await moveReqPromise;
      const moveBody = JSON.parse(moveReq.postData() ?? "{}");
      expect(moveBody).toEqual({
        from: "track.mp3",
        to: tc.expectedTo,
      });
      if (tc.typedName === "song.wav") {
        expect(moveBody.to.endsWith(".wav.mp3")).toBe(false);
      }

      // Phase-1 convergence fires the source-delete; verify stripped subpath.
      const deleteReq = await deleteReqPromise;
      expect(JSON.parse(deleteReq.postData() ?? "{}")).toEqual({
        paths: ["track.mp3"],
      });

      // Phase-2 convergence: source is absent → root shows empty-folder state.
      await expect(
        page.locator("[data-testid=music-empty-folder]"),
      ).toBeVisible({ timeout: 10000 });

      // DaftPunk folder still present in folder list (visible layout).
      await expect(page.locator(sel.folderList)).toContainText("DaftPunk");

      // Navigate into DaftPunk and confirm the destination file is there.
      await page.locator(sel.folderRow("DaftPunk")).click();
      await expect(page.locator(sel.fileList)).toContainText(tc.expectedDestName);

      expect(
        consoleErrors,
        `console errors during move flow: ${JSON.stringify(consoleErrors)}`,
      ).toEqual([]);
    });
  }

  test("file input accepts multiple files", async ({ page }) => {
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("input[type=file]")).toHaveAttribute(
      "multiple",
      "",
    );
  });

  test("drag-and-drop multiple files — stages then uploads all", async ({
    page,
  }, testInfo) => {
    const sel = viewportSelectors(testInfo.project.name);
    let musicGetCount = 0;
    const postFilenames: string[] = [];
    await page.route("**/api/gadget/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(GADGET_STATUS_OK),
      }),
    );
    await page.route("**/api/music", (route) => {
      const req = route.request();
      if (req.method() !== "GET") {
        // Capture the multipart filename for assertion.
        const body = req.postData() ?? "";
        const m = body.match(/filename="([^"]+)"/);
        if (m) postFilenames.push(m[1]);
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: QUEUED_BODY,
        });
      }
      musicGetCount++;
      // First GET empty; after the uploads land, both files are present.
      const items =
        musicGetCount === 1
          ? []
          : [
              {
                name: "alpha.mp3",
                rel_path: "Music/alpha.mp3",
                size_bytes: 3,
                modified: null,
              },
              {
                name: "beta.mp3",
                rel_path: "Music/beta.mp3",
                size_bytes: 3,
                modified: null,
              },
            ];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: musicListBody(items),
      });
    });

    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(err.message));

    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("[data-testid=music-empty]")).toBeVisible();

    // Simulate a real OS drag-drop of two audio files onto the drop zone.
    await page.locator("[data-testid=music-dropzone]").evaluate((el) => {
      const dt = new DataTransfer();
      dt.items.add(
        new File([new Uint8Array([1, 2, 3])], "alpha.mp3", {
          type: "audio/mpeg",
        }),
      );
      dt.items.add(
        new File([new Uint8Array([4, 5, 6])], "beta.mp3", {
          type: "audio/mpeg",
        }),
      );
      for (const type of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(type, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    });

    // Both dropped files are staged (not yet uploaded).
    const staged = page.locator("[data-testid=music-selected-files]");
    await expect(staged).toContainText("alpha.mp3");
    await expect(staged).toContainText("beta.mp3");
    expect(postFilenames, "no upload should fire on drop alone").toEqual([]);

    // Upload all staged files.
    await page.locator("[data-testid=music-upload-btn]").click();

    // Both files each POST to /api/music, then appear after convergence.
    await expect(page.locator(sel.fileList)).toContainText("alpha.mp3", {
      timeout: 8000,
    });
    await expect(page.locator(sel.fileList)).toContainText("beta.mp3");
    expect(postFilenames.sort()).toEqual(["alpha.mp3", "beta.mp3"]);

    expect(
      consoleErrors,
      `console errors during DnD flow: ${JSON.stringify(consoleErrors)}`,
    ).toEqual([]);
  });
});
