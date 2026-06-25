import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page, Route } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Media (Lock Chimes) UAT — v1 parity ───────────────────────────────────
// Drives the REAL bundle served by webd at `/media`. Parity target: the legacy
// Flask app's `/media/` 302-redirected to `/lock_chimes/`, so the visible
// "media page" was the LOCK CHIMES manager with a media pill sub-nav
// (Chimes/Music/Boombox/Shows/Wraps/Plates). This screen reproduces that v1
// look using the carried-over legacy stylesheet (`/static/css/style.css`).
//
// Backend reality:
//  - `GET /api/chimes` (read-only, routed through the catalog — NOT the gadgetd
//    eject-handoff) reports the installed lock chime. The screen renders that
//    live fact, degrading to honest empty states when nothing is installed (the
//    UAT seed has no media). This read hits REAL webd.
//  - `POST /api/chimes` routes through the gadgetd eject-handoff. gadgetd is
//    NOT running in the UAT harness (only webd is spawned), so a real call would
//    503. The install flow is therefore driven against Playwright route mocks
//    (the contract is fixed by docs/specs/contracts §2.3.1; mocking an absent
//    dependency is sanctioned).
//    The READ path and the no-mutation-on-load guarantee are still verified
//    against real webd.

/** The media pill sub-nav, in v1 render order. Only "chimes" is active/built. */
const EXPECT_PILLS = ["chimes", "music", "boombox", "shows", "wraps", "plates"];

interface MediaHooks {
  build: string;
  screen: string;
}

function hooks(page: Page): Promise<MediaHooks | undefined> {
  return page.evaluate(
    () =>
      (window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: MediaHooks })
        .__TESLAUSB_MEDIA_HOOKS__,
  );
}

/** A structurally-valid, EMPTY scheduler snapshot. The Lock Chimes screen now
 *  embeds the schedulerd-backed <ChimeScheduler/>, which fetches this on mount.
 *  In this harness schedulerd is never spawned, so we mock the read to a clean
 *  empty snapshot — the scheduler/groups/library sections render their honest
 *  empty states and the console stays clean. Deep scheduler behavior is covered
 *  by chime-scheduler.spec.ts. Menus mirror schedulerd::model::SchedulerMenus. */
const SCHED_MENUS = {
  holidays: [
    "New Year's Day",
    "Martin Luther King Jr. Day",
    "Valentine's Day",
    "Presidents' Day",
    "St. Patrick's Day",
    "Easter",
    "Mother's Day",
    "Memorial Day",
    "Father's Day",
    "Independence Day",
    "Labor Day",
    "Columbus Day",
    "Halloween",
    "Veterans Day",
    "Thanksgiving",
    "Christmas Eve",
    "Christmas Day",
    "New Year's Eve",
  ],
  intervals: ["on_boot", "15min", "30min", "1hour", "2hour", "4hour", "6hour", "12hour"],
  weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
};

const SCHED_EMPTY = {
  schedules: [],
  groups: [],
  randomMode: { enabled: false },
  library: [],
  menus: SCHED_MENUS,
};

/** Register a default mock for the scheduler snapshot read so the embedded
 *  <ChimeScheduler/> settles cleanly. Tests that need a populated snapshot
 *  register their own (later-registered, more specific) route first. */
async function mockSchedulerSnapshot(page: Page, snap: unknown = SCHED_EMPTY) {
  await page.route("**/api/chime-scheduler", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return jsonRoute(route, 200, snap);
  });
}

/** Navigate to /media and wait until the Lock Chimes screen has rendered. */
async function gotoMedia(page: Page) {
  await mockSchedulerSnapshot(page);
  await page.goto("/media", { waitUntil: "load" });
  await expect(page.locator(".container[data-screen=media]")).toBeVisible();
  await page.waitForFunction(() => {
    const h = (
      window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { screen: string } }
    ).__TESLAUSB_MEDIA_HOOKS__;
    return !!h && h.screen === "lock-chimes";
  });
}

function assertCleanConsole(probe: Probe) {
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  expect(
    probe.consoleErrors,
    `console error(s): ${JSON.stringify(probe.consoleErrors)}`,
  ).toEqual([]);
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
}

/**
 * Chromium emits a console *error* of the form
 *   "Failed to load resource: the server responded with a status of 409 …"
 * for any fetch whose response status is >= 400 — this is the browser logging
 * the network transaction, NOT a JS/page fault. A gate that DELIBERATELY drives
 * a 4xx (e.g. the busy-retry flow) must tolerate exactly that one message while
 * still proving no real JS error, warning, or pageerror leaked. `statuses` is
 * the set of HTTP codes the gate intentionally provoked.
 */
function assertCleanConsoleExceptResourceStatus(probe: Probe, statuses: number[]) {
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
  const allowed = (e: { text: string }) =>
    /Failed to load resource/i.test(e.text) &&
    statuses.some((s) => e.text.includes(`status of ${s}`));
  const leaked = probe.consoleErrors.filter((e) => !allowed(e));
  expect(leaked, `unexpected console error(s): ${JSON.stringify(leaked)}`).toEqual([]);
}

/** A canonical InstalledChime DTO for the mocked GET /api/chimes. */
const INSTALLED = {
  name: "LockChime.wav",
  rel_path: "LockChime.wav",
  size_bytes: 219770,
  modified: "2026-06-01T20:10:04",
};

/** Build a minimal but structurally-valid PCM WAV (matches webd's validator). */
function wavBuffer(
  opts: {
    channels?: number;
    sampleRate?: number;
    bits?: number;
    audioFormat?: number;
    dataLen?: number;
  } = {},
): Buffer {
  const channels = opts.channels ?? 1;
  const sampleRate = opts.sampleRate ?? 44100;
  const bits = opts.bits ?? 16;
  const audioFormat = opts.audioFormat ?? 1;
  const dataLen = opts.dataLen ?? 256;
  const blockAlign = channels * (bits / 8);
  const byteRate = sampleRate * blockAlign;
  const buf = Buffer.alloc(44 + dataLen);
  buf.write("RIFF", 0, "ascii");
  buf.writeUInt32LE(36 + dataLen, 4);
  buf.write("WAVE", 8, "ascii");
  buf.write("fmt ", 12, "ascii");
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(audioFormat, 20);
  buf.writeUInt16LE(channels, 22);
  buf.writeUInt32LE(sampleRate, 24);
  buf.writeUInt32LE(byteRate, 28);
  buf.writeUInt16LE(blockAlign, 32);
  buf.writeUInt16LE(bits, 34);
  buf.write("data", 36, "ascii");
  buf.writeUInt32LE(dataLen, 40);
  return buf;
}

function jsonRoute(route: Route, status: number, body: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test.describe("media (lock chimes) UAT", () => {
  // ── Gate 1: v1 parity — chrome, pill sub-nav, lock-chime sections ───────
  test("parity — Media nav active, media pills, Lock Chimes sections", async ({
    page,
  }, testInfo) => {
    await gotoMedia(page);

    // App shell (base.html parity): brand present, MEDIA nav active.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Media");

    // (a) Media pill sub-nav — all six, in v1 order; every pill is a real
    //     in-app link to its v1 route, with "chimes" the active page.
    const pills = page.locator(".media-pills .media-pill");
    await expect(pills).toHaveCount(EXPECT_PILLS.length);
    for (let i = 0; i < EXPECT_PILLS.length; i++) {
      await expect(pills.nth(i)).toHaveAttribute("data-pill", EXPECT_PILLS[i]);
    }
    const chimes = page.locator(".media-pill[data-pill=chimes]");
    await expect(chimes).toHaveClass(/\bactive\b/);
    await expect(chimes).toHaveAttribute("href", "/media");
    // All six pills are real anchors now (v1 parity — no dead/disabled pills).
    await expect(page.locator("a.media-pill")).toHaveCount(6);
    await expect(page.locator(".media-pill.media-pill-disabled")).toHaveCount(0);
    // The other five link to their v1 routes.
    await expect(page.locator(".media-pill[data-pill=music]")).toHaveAttribute("href", "/music");
    await expect(page.locator(".media-pill[data-pill=boombox]")).toHaveAttribute("href", "/boombox");
    await expect(page.locator(".media-pill[data-pill=shows]")).toHaveAttribute("href", "/light_shows");
    await expect(page.locator(".media-pill[data-pill=wraps]")).toHaveAttribute("href", "/wraps");
    await expect(page.locator(".media-pill[data-pill=plates]")).toHaveAttribute("href", "/license_plates");

    // (b) Lock Chimes heading + the v1 section set (each present and honest).
    await expect(
      page.locator(".container[data-screen=media] h2"),
    ).toHaveText("Lock Chimes");
    await expect(page.locator("#activeChimeSection")).toBeVisible();
    await expect(page.locator("#chimeUploadControls summary")).toHaveText(
      "Upload New Chime",
    );
    await expect(page.locator("#scheduler-section summary")).toHaveText(
      "Chime Scheduler",
    );
    await expect(page.locator("#groups-section summary")).toHaveText(
      "Random Chime Groups",
    );
    await expect(page.locator("#library-section summary")).toHaveText(
      "Chime Library",
    );

    // (c) The Upload New Chime manage surface is wired and present: a real file
    //     input + a submit button that starts DISABLED (no file selected yet),
    //     so install is always a deliberate pick-a-file → Upload action.
    await expect(page.locator("[data-testid=chime-file-input]")).toBeVisible();
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeVisible();
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();

    // The seed has no media on p2, so webd reports `{installed: null}` and the
    // data sections settle into their honest EMPTY states (never fabricated).
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();
    await expect(page.locator("[data-testid=library-empty]")).toBeVisible();
  });

  // ── Legacy direct route: /lock_chimes (v1's lock-chimes URL) must resolve to
  //    the Media lock-chimes screen via webd's SPA fallback + the client router
  //    alias, so old bookmarks don't dead-end. ──
  test("legacy route — /lock_chimes lands on the lock-chimes screen", async ({
    page,
  }) => {
    await page.goto("/lock_chimes", { waitUntil: "load" });
    await expect(page.locator(".container[data-screen=media]")).toBeVisible();
    await page.waitForFunction(() => {
      const h = (
        window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { screen: string } }
      ).__TESLAUSB_MEDIA_HOOKS__;
      return !!h && h.screen === "lock-chimes";
    });
    await expect(page.locator(".container[data-screen=media] h2")).toHaveText(
      "Lock Chimes",
    );
  });

  // ── Gate 2: wiring proof — the served HTML runs the freshly-built bundle ─
  test("wiring — served HTML runs the built bundle and the media module ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoMedia(page);

    // (a) build id baked on disk == build id the live page exposes.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev");
    expect(winBuild).toBe(state.buildId);

    // (b) the Media module's OWN hook reports the SAME build + this screen.
    const h = await hooks(page);
    expect(h, "window.__TESLAUSB_MEDIA_HOOKS__ must exist").toBeTruthy();
    expect(h!.build).toBe(state.buildId);
    expect(h!.screen).toBe("lock-chimes");

    // (c) the ACTUALLY-EXECUTED document loaded the hashed bundle, not the dev TS.
    const loadedScripts = await page.evaluate(() =>
      Array.from(document.scripts).map((s) => s.src),
    );
    expect(
      loadedScripts.some((s) => s.includes(state.jsAsset)),
      `executed document must load ${state.jsAsset}; saw ${JSON.stringify(loadedScripts)}`,
    ).toBe(true);
    expect(loadedScripts.some((s) => s.includes("/src/main.tsx"))).toBe(false);

    // (d) served index references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/media")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (e) the legacy stylesheet that carries the v1 look is referenced.
    expect(html).toContain("/static/css/style.css");
  });

  // ── Gate 3: on load, only the GET read fires; the manage surface is present
  //    but inert until the operator acts (no mutation without a deliberate click).
  test("read-only on load — only GET /api/chimes, no mutation until acted on", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;

    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));

    await gotoMedia(page);
    // Let the single read-only fetch settle.
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();
    await page.waitForTimeout(200);

    // No mutating HTTP method fires on load — install requires a click.
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s) on load: ${JSON.stringify(mutating)}`).toEqual([]);

    // No WebSocket of any kind.
    expect(sockets, `websocket(s) opened: ${JSON.stringify(sockets)}`).toEqual([]);

    // The ONLY /api/ requests on load are the read-only `GET /api/chimes` (active
    // chime), `GET /api/chime-scheduler` (the embedded scheduler snapshot), and
    // `GET /api/system/timezone` (the scheduler's clock/timezone gate); all are
    // GETs, same-origin, and nothing else hits /api/.
    const allowedReads = new Set([
      "GET /api/chimes",
      "GET /api/chime-scheduler",
      "GET /api/system/timezone",
    ]);
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (u.pathname.startsWith("/api/") && u.pathname !== "/api/media-events") {
        expect(
          allowedReads.has(`${req.method.toUpperCase()} ${u.pathname}`),
          `unexpected API call ${req.method} ${u.pathname}`,
        ).toBe(true);
      }
    }
    const chimeReads = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/chimes",
    );
    expect(chimeReads.length, "expected exactly one GET /api/chimes on load").toBe(1);

    // The manage surface is a deliberate two-step action, never a native POST:
    // the form does not submit to the server (SPA preventDefault) and there is
    // no `method=post` form. v1 parity: a single uploader (the "Upload New
    // Chime" panel) feeds the library — the scheduler's library section no
    // longer has its own file input.
    await expect(page.locator("form[method='post' i]")).toHaveCount(0);
    await expect(page.locator("input[type=file]")).toHaveCount(1);
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();
  });

  // ── Gate 4 (console + network): zero warnings/errors, no failed/non-2xx ──
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoMedia(page);
    await page.evaluate(
      () =>
        new Promise<void>((r) =>
          requestAnimationFrame(() => requestAnimationFrame(() => r())),
        ),
    );
    await page.waitForTimeout(200);

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    const offOrigin = probe.requests.filter((r) => new URL(r.url).origin !== origin);
    expect(offOrigin, `off-origin request(s): ${JSON.stringify(offOrigin)}`).toEqual([]);

    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate 5: installed chime renders when GET /api/chimes reports one ─────
  test("installed — active chime card renders from GET /api/chimes", async ({
    page,
  }) => {
    // Mock the read so the test is independent of the seed (which has no media).
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return jsonRoute(route, 200, { installed: INSTALLED });
    });

    await gotoMedia(page);

    // Active Lock Chime card shows the live name + size + install time + player.
    const active = page.locator("[data-testid=active-chime]");
    const audio = page.locator("[data-testid=active-chime-audio]");
    await expect(active).toBeVisible();
    await expect(page.locator("[data-testid=active-chime-name]")).toHaveText(
      "LockChime.wav",
    );
    await expect(active).toContainText("215 KB");
    await expect(active).toContainText("2026-06-01 20:10");
    await expect(audio).toBeVisible();
    await expect(audio).toHaveAttribute("src", /\/api\/media\/content\?path=LockChime\.wav/);

    // No empty state when a chime is installed.
    await expect(page.locator("[data-testid=active-chime-none]")).toHaveCount(0);
  });

  // ── Gate 6: upload adds to the library — POST library + clean notice ────
  test("library upload — valid WAV POSTs to the chime library and clears the input", async ({
    page,
    probe,
  }) => {
    const libraryPosts: string[] = [];

    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const post = route.request().postData() ?? "";
      const m = post.match(/filename="([^"]+)"/);
      const filename = m ? m[1] : "Chime.wav";
      libraryPosts.push(filename);
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ state: "queued", job_id: "job-1" }),
      });
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();

    // Pick a structurally-valid WAV; client validation passes → button enables.
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Chime.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer({ channels: 2, sampleRate: 48000, dataLen: 512 }),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await expect(page.locator("[data-testid=chime-editor-upload]")).toBeEnabled();

    // Upload → POST hits the LIBRARY endpoint, success notice shows.
    await page
      .locator("[data-testid=chime-editor-upload]")
      .click({ force: true });
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing",
    );
    await expect(page.locator("[data-testid=chime-editor]")).toHaveCount(0);

    // v1 parity: adding to the library does NOT install an active chime, so the
    // active card stays empty and nothing POSTs to the install endpoint.
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();
    const installPosts = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/chimes" && r.method === "POST",
    );
    expect(installPosts.length, "library upload must not POST /api/chimes").toBe(0);
    expect(libraryPosts, "expected exactly one library POST").toEqual(["Chime.wav"]);

    // The picker is ready for the next action, and the JS stayed clean.
    await expect(page.locator("[data-testid=chime-file-input]")).toBeVisible();
    assertCleanConsole(probe);
  });

  test("library upload via drag-and-drop — dropping a valid WAV stages it and uploads", async ({
    page,
    probe,
  }) => {
    const libraryPosts: string[] = [];

    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const post = route.request().postData() ?? "";
      const m = post.match(/filename="([^"]+)"/);
      libraryPosts.push(m ? m[1] : "Dropped.wav");
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ state: "queued", job_id: "job-dnd" }),
      });
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);
    const zone = page.locator("[data-testid=chime-dropzone]");
    await expect(zone).toBeVisible();

    // Drop a structurally-valid WAV onto the zone → client validation passes,
    // it stages exactly like the file picker does and enables Upload.
    const bytes = Array.from(wavBuffer({ channels: 2, sampleRate: 48000, dataLen: 512 }));
    await zone.evaluate((el, b) => {
      const dt = new DataTransfer();
      dt.items.add(
        new File([new Uint8Array(b as number[])], "Dropped.wav", { type: "audio/wav" }),
      );
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    }, bytes);

    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await expect(page.locator("[data-testid=chime-editor-upload]")).toBeEnabled();

    await page
      .locator("[data-testid=chime-editor-upload]")
      .click({ force: true });
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing",
    );
    expect(libraryPosts, "expected one library POST from the dropped file").toEqual([
      "Dropped.wav",
    ]);
    assertCleanConsole(probe);
  });

  test("library upload — multiple WAVs via drag-and-drop upload sequentially", async ({
    page,
    probe,
  }) => {
    const libraryPosts: string[] = [];

    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const post = route.request().postData() ?? "";
      const m = post.match(/filename="([^"]+)"/);
      libraryPosts.push(m ? m[1] : "Chime.wav");
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ state: "queued", job_id: "job-multi" }),
      });
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);
    const zone = page.locator("[data-testid=chime-dropzone]");
    await expect(zone).toBeVisible();

    const first = Array.from(wavBuffer({ channels: 2, sampleRate: 48000, dataLen: 256 }));
    const second = Array.from(wavBuffer({ channels: 1, sampleRate: 44100, dataLen: 128 }));
    await zone.evaluate((el, files) => {
      const dt = new DataTransfer();
      for (const [name, bytes] of files as Array<[string, number[]]>) {
        dt.items.add(
          new File([new Uint8Array(bytes)], name, { type: "audio/wav" }),
        );
      }
      for (const t of ["dragenter", "dragover", "drop"]) {
        const ev = new DragEvent(t, { bubbles: true, cancelable: true });
        Object.defineProperty(ev, "dataTransfer", { value: dt });
        el.dispatchEvent(ev);
      }
    }, [
      ["First.wav", first],
      ["Second.wav", second],
    ]);

    await expect(page.locator("[data-testid=chime-upload-staged]")).toContainText(
      "First.wav",
    );
    await expect(page.locator("[data-testid=chime-upload-staged]")).toContainText(
      "Second.wav",
    );
    await expect(page.locator("[data-testid=chime-upload-submit]")).toContainText(
      "Upload 2 chimes",
    );
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    await page.locator("[data-testid=chime-upload-submit]").click();
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing 2 chimes",
    );
    expect(libraryPosts, "expected two sequential library POSTs").toEqual([
      "First.wav",
      "Second.wav",
    ]);
    assertCleanConsole(probe);
  });

  test("library upload — an unattempted (invalid) staged file is not dropped after a sibling uploads", async ({
    page,
    probe,
  }) => {
    const libraryPosts: string[] = [];
    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const post = route.request().postData() ?? "";
      const m = post.match(/filename="([^"]+)"/);
      libraryPosts.push(m ? m[1] : "Chime.wav");
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ state: "queued", job_id: "job-keep" }),
      });
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);

    // Stage one oversized (invalid) file alongside one valid file. The invalid
    // file is never uploaded; it must survive the post-upload staged cleanup so
    // the operator still sees (and can remove/fix) it.
    await page.locator("[data-testid=chime-file-input]").setInputFiles([
      {
        name: "TooBig.wav",
        mimeType: "audio/wav",
        buffer: wavBuffer({ dataLen: 1024 * 1024 }), // > 1 MiB → rejected client-side
      },
      {
        name: "Good.wav",
        mimeType: "audio/wav",
        buffer: wavBuffer({ dataLen: 256 }),
      },
    ]);

    await expect(page.locator("[data-testid=chime-staged-error]")).toHaveCount(1);
    // validCount === 1 → singular "Upload" label, enabled (no row still validating).
    await expect(page.locator("[data-testid=chime-upload-submit]")).toContainText("Upload");
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    await page.locator("[data-testid=chime-upload-submit]").click();
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing",
    );

    // Only the valid file was POSTed; the invalid row remains staged.
    expect(libraryPosts).toEqual(["Good.wav"]);
    const staged = page.locator("[data-testid=chime-upload-staged]");
    await expect(staged).toContainText("TooBig.wav");
    await expect(staged).not.toContainText("Good.wav");
    await expect(page.locator("[data-testid=chime-staged-error]")).toHaveCount(1);
    assertCleanConsole(probe);
  });

  test("library upload retry — a transient 503 is retryable, retry then succeeds", async ({
    page,
    probe,
  }) => {
    let attempt = 0;

    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      attempt += 1;
      if (attempt === 1) {
        return jsonRoute(route, 503, {
          error: { code: "schedulerd_unavailable", message: "The chime service is restarting." },
        });
      }
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ state: "queued", job_id: "job-2" }),
      });
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Chime.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer({ dataLen: 256 }),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await expect(page.locator("[data-testid=chime-editor-upload]")).toBeEnabled();

    // First attempt → 503 → retryable error banner (not fatal), button live again.
    await page
      .locator("[data-testid=chime-editor-upload]")
      .click({ force: true });
    const err = page.locator(".chime-upload-status.retryable");
    await expect(err).toBeVisible();
    await expect(err).toContainText("Try again");
    await expect(page.locator("[data-testid=chime-editor-upload]")).toBeEnabled();

    // Retry → success.
    await page
      .locator("[data-testid=chime-editor-upload]")
      .click({ force: true });
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing",
    );
    await expect(page.locator(".chime-upload-status.fatal")).toHaveCount(0);
    expect(attempt, "expected two POST attempts (503, then success)").toBe(2);

    // The retry flow deliberately provoked one 503 response; Chromium logs that
    // as a resource-load console error. No OTHER console error / warning /
    // pageerror is tolerated.
    assertCleanConsoleExceptResourceStatus(probe, [503]);
  });

  // ── Gate 8: client-side WAV validation refuses bad files before any POST ─
  test("install validation — oversize + non-PCM WAV are refused client-side", async ({
    page,
    probe,
  }) => {
    const posts: string[] = [];
    await page.route("**/api/chime-scheduler/library", (route) => {
      if (route.request().method() === "POST") posts.push("POST");
      return route.continue();
    });
    await page.route("**/api/chimes", (route) => {
      if (route.request().method() === "GET") return jsonRoute(route, 200, { installed: null });
      return route.continue();
    });

    await gotoMedia(page);

    // Select two files so the batch validator path is used (single-file now opens
    // the editor). Both invalid files should be rejected before any POST.
    await page.locator("[data-testid=chime-file-input]").setInputFiles([
      {
        name: "Big.wav",
        mimeType: "audio/wav",
        buffer: wavBuffer({ dataLen: 1024 * 1024 }), // total > 1 MiB
      },
      {
        name: "Odd.wav",
        mimeType: "audio/wav",
        buffer: wavBuffer({ sampleRate: 32000, dataLen: 256 }),
      },
    ]);
    const stagedErrors = page.locator("[data-testid=chime-staged-error]");
    await expect(stagedErrors).toHaveCount(2);
    await expect(stagedErrors.first()).toBeVisible();
    await expect(stagedErrors.first()).toContainText("under 1 MB");
    await expect(stagedErrors.nth(1)).toBeVisible();
    await expect(stagedErrors.nth(1)).toContainText("44.1 or 48");
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();

    // No POST was ever attempted for the rejected files (only client validation).
    expect(posts, `unexpected POST(s): ${JSON.stringify(posts)}`).toEqual([]);
    assertCleanConsole(probe);
  });

  // ── Gate 10: performance — capture + report (dev-box profile) ───────────
  test("perf — capture TTFB/DCL/FCP + slowest requests", async ({ page }, testInfo) => {
    const navStart = Date.now();
    await gotoMedia(page);
    const readyMs = await page.evaluate(() => performance.now());

    const timings = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const fcp = performance
        .getEntriesByType("paint")
        .find((p) => p.name === "first-contentful-paint");
      const resources = (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
        .map((r) => ({ url: r.name, ms: Math.round(r.duration * 10) / 10, type: r.initiatorType }))
        .sort((a, b) => b.ms - a.ms)
        .slice(0, 10);
      return {
        ttfbMs: Math.round(nav.responseStart - nav.requestStart),
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd),
        domInteractiveMs: Math.round(nav.domInteractive),
        loadMs: Math.round(nav.loadEventEnd),
        fcpMs: fcp ? Math.round(fcp.startTime) : null,
        slowestRequests: resources,
      };
    });

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache). NOTE: spa.md's <~2s 'interactive' " +
        "target is the ON-DEVICE (Raspberry Pi) profile — these are dev-box " +
        "numbers, reported not asserted against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      screenReadyMs: Math.round(readyMs),
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-media-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-media-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:media:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(6000);
    expect(report.screenReadyMs).toBeLessThan(8000);
  });

  // ── Gate 11: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoMedia(page);

    // Content present regardless of breakpoint.
    await expect(page.locator(".media-pills")).toBeVisible();
    await expect(page.locator("#activeChimeSection")).toBeVisible();
    await expect(page.locator("[data-testid=chime-file-input]")).toBeVisible();

    // Breakpoint-specific chrome: desktop shows the rail, mobile the bottom tabs.
    const isMobile = testInfo.project.name.includes("375");
    const rail = page.locator(".sidebar-rail");
    const tabs = page.locator(".bottom-tabs");
    if (isMobile) {
      await expect(tabs).toBeVisible();
      await expect(rail).toBeHidden();
    } else {
      await expect(rail).toBeVisible();
      await expect(tabs).toBeHidden();
    }

    const shot = resolve(ARTIFACTS, `media-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`media-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:media:${testInfo.project.name}] ${shot}`);
  });

  test("full-width — main-content cap removed so the card fills the width", async ({
    page,
  }) => {
    await gotoMedia(page);
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

  test.describe("Set Active — auto-refresh", () => {
    const SCHED_MENUS = {
      holidays: [],
      intervals: [],
      weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    };

    function snapshot(library: { filename: string; bytes: number }[]) {
      return {
        schedules: [],
        groups: [],
        randomMode: { enabled: false },
        library,
        menus: SCHED_MENUS,
      };
    }

    const GADGET_BASE = {
      present: true,
      bound: false,
      bound_udc: null,
      udc_state: "configured",
      lun_file: "/data/teslausb/cam.img",
      media_lun_file: "/data/teslausb/media.img",
      handoff_active: false,
      pending_mutations: 0,
      applying_mutations: 0,
      media_ro_mounted: true,
      media_ro_path: "/run/teslausb/media-ro",
      media_ro_error: null,
      chime_reenum_pending: false,
      last_reenum: null,
      last_handoff_id: null,
      last_result: null,
    };

    async function gotoActivationMedia(page: Page) {
      await page.goto("/media", { waitUntil: "load" });
      await expect(page.locator(".container[data-screen=media]")).toBeVisible();
      await expect(page.locator("[data-testid=active-chime]")).toBeVisible();
    }

    test("auto-refresh on activate updates the active card", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const newInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      let installed = oldInstalled;
      let navigations = 0;

      page.on("framenavigated", () => {
        navigations += 1;
      });

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await page.goto("/media", { waitUntil: "load" });
      await expect(page.locator(".container[data-screen=media]")).toBeVisible();
      navigations = 0;

      await expect(page.locator("[data-testid=active-chime]")).toContainText("1 KB");
      await expect(page.locator("[data-testid=active-chime]")).toContainText("2026-06-15 23:57");

      await page.locator("[data-testid=library-set-active]").first().click();
      await expect(page.locator("[data-testid=activation-status]")).toContainText("Applying “Sparkle.wav”");

      installed = newInstalled;
      await page.clock.fastForward(2000);

      await expect(page.locator("[data-testid=active-chime]")).toContainText("2 KB");
      await expect(page.locator("[data-testid=active-chime-audio]")).toHaveAttribute(
        "src",
        /v=2026-06-15T23%3A58%3A00/,
      );
      await expect(page.locator("[data-testid=activation-notice]")).toContainText("is now your active lock chime");
      expect(navigations).toBe(0);
    });

    test("slow activate shows busy overlay, blocks actions, then clears", async ({ page }) => {
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const convergedInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      let activated = false;
      let deleteCalls = 0;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(
          route,
          200,
          snapshot([
            { filename: "Sparkle.wav", bytes: 2048 },
            { filename: "Bell.wav", bytes: 4096 },
          ]),
        );
      });
      await page.route("**/api/chime-scheduler/library/*/activate", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        await new Promise((resolve) => setTimeout(resolve, 2500));
        activated = true;
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "m-uat" }),
        });
      });
      await page.route("**/api/chime-scheduler/library/*", (route) => {
        if (route.request().method() !== "DELETE") return route.continue();
        deleteCalls += 1;
        return jsonRoute(route, 200, {});
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed: activated ? convergedInstalled : oldInstalled });
      });
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { ...GADGET_BASE, chime_reenum_pending: false });
      });

      await gotoActivationMedia(page);
      const busyOverlay = page.locator("[data-testid=busy-overlay]");
      const busyOverlayCard = page.locator("[data-testid=busy-overlay-card]");
      await page.locator("[data-testid=library-set-active]").first().click();
      const cardTimeline = page.evaluate(async () => {
        const started = performance.now();
        let seenBefore700 = false;
        let seenBy1500 = false;
        while (performance.now() - started < 1700) {
          const elapsed = performance.now() - started;
          const seen = !!document.querySelector("[data-testid=busy-overlay-card]");
          if (seen && elapsed < 700) seenBefore700 = true;
          if (seen && elapsed <= 1500) seenBy1500 = true;
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        }
        return { seenBefore700, seenBy1500 };
      });
      await page.waitForTimeout(150);
      await expect(busyOverlay).toHaveCount(1);

      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0);

      const secondDelete = page.locator("[data-testid=library-delete]").nth(1);
      await expect(secondDelete).toBeEnabled();
      let blocked = false;
      try {
        await secondDelete.click({ timeout: 400 });
      } catch {
        blocked = true;
      }
      expect(blocked, "overlay should intercept pointer actions while busy").toBe(true);
      expect(deleteCalls, "blocked click must not trigger delete").toBe(0);
      const cardStates = await cardTimeline;
      expect(cardStates.seenBefore700, "busy card should not flash in the first ~700ms").toBe(false);
      expect(cardStates.seenBy1500, "busy card should appear once the 1s debounce elapses").toBe(
        true,
      );
      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0);
      await expect(busyOverlayCard).toHaveCount(0, { timeout: 5000 });

      await expect(busyOverlay).toHaveCount(0, { timeout: 5000 });
    });

    test("fast activate never flashes busy overlay", async ({ page }) => {
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const convergedInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      let activated = false;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        activated = true;
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "m-fast" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed: activated ? convergedInstalled : oldInstalled });
      });
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { ...GADGET_BASE, chime_reenum_pending: false });
      });

      await gotoActivationMedia(page);
      await page.locator("[data-testid=library-set-active]").first().click();
      const sawBusy = await page.evaluate(async () => {
        const started = performance.now();
        while (performance.now() - started < 1300) {
          if (document.querySelector("[data-testid=busy-overlay-card]")) return true;
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        }
        return !!document.querySelector("[data-testid=busy-overlay-card]");
      });

      expect(sawBusy).toBe(false);
      await expect(page.locator("[data-testid=busy-overlay-card]")).toHaveCount(0);
    });

    test("set-active shows the keep-doors-closed reenum overlay until reenum clears", async ({
      page,
      probe,
    }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let activated = false;
      let gadgetReads = 0;
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const convergedInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        activated = true;
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed: activated ? convergedInstalled : oldInstalled });
      });
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        gadgetReads += 1;
        return jsonRoute(route, 200, {
          ...GADGET_BASE,
          chime_reenum_pending: gadgetReads <= 2,
          last_reenum: null,
        });
      });

      await gotoActivationMedia(page);
      await page.locator("[data-testid=library-set-active]").first().click();
      await expect(page.locator("[data-testid=reenum-overlay]")).toBeVisible();
      await expect(page.locator("[data-testid=reenum-overlay-message]")).toContainText(
        /keep the car.?s doors closed/i,
      );

      await page.clock.fastForward(7000);
      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0, { timeout: 10000 });
      await expect(page.locator("[data-testid=activation-notice]")).toContainText("next lock");
      assertCleanConsole(probe);
    });

    test("reenum overlay is dismissable", async ({ page, probe }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let activated = false;
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const convergedInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        activated = true;
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed: activated ? convergedInstalled : oldInstalled });
      });
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, {
          ...GADGET_BASE,
          chime_reenum_pending: true,
          last_reenum: null,
        });
      });

      await gotoActivationMedia(page);
      await page.locator("[data-testid=library-set-active]").first().click();
      await expect(page.locator("[data-testid=reenum-overlay]")).toBeVisible();
      await page.locator("[data-testid=reenum-overlay-dismiss]").click();
      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0);
      assertCleanConsole(probe);
    });

    test("a stale reenum poll from an earlier activation can't mislabel a later one", async ({
      page,
      probe,
    }) => {
      // Regression: activation A converges (so its Set Active buttons re-enable)
      // but the SPA never observes chime_reenum_pending===true for A — so A's
      // reenum poll keeps running. The user then activates B. A's still-running
      // poll must NOT own B's reenum and label the "next lock" notice with A's
      // filename. Activation tokens must be globally unique for this to hold.
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      const v0 = { name: "LockChime.wav", rel_path: "LockChime.wav", size_bytes: 512, modified: "2026-06-15T23:57:58" };
      const v1 = { name: "LockChime.wav", rel_path: "LockChime.wav", size_bytes: 1024, modified: "2026-06-15T23:58:30" };
      const v2 = { name: "LockChime.wav", rel_path: "LockChime.wav", size_bytes: 2048, modified: "2026-06-15T23:59:10" };
      let activatePosts = 0;
      let gadgetReadsAfterB = 0;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(
          route,
          200,
          snapshot([
            { filename: "AlphaChime.wav", bytes: 1024 },
            { filename: "BravoChime.wav", bytes: 2048 },
          ]),
        );
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        activatePosts += 1;
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: `a${activatePosts}` }),
        });
      });
      // Each activation's convergence lands on the very next /api/chimes read, so
      // the per-op poll resolves immediately without depending on a fake re-arm.
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        const installed = activatePosts >= 2 ? v2 : activatePosts >= 1 ? v1 : v0;
        return jsonRoute(route, 200, { installed });
      });
      // Pending is only ever reported true AFTER B is activated, and only for a
      // couple of reads — so A never latches an overlay, and only B's reenum does.
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        let pending = false;
        if (activatePosts >= 2) {
          gadgetReadsAfterB += 1;
          pending = gadgetReadsAfterB <= 2;
        }
        return jsonRoute(route, 200, { ...GADGET_BASE, chime_reenum_pending: pending });
      });

      await gotoActivationMedia(page);
      const buttons = page.locator("[data-testid=library-set-active]");

      // Activation A (Alpha, 1 KB) — converges immediately (512 B → 1 KB),
      // re-enabling the buttons; its reenum poll keeps running with pending=false.
      await buttons.first().click();
      await expect(page.locator("[data-testid=active-chime]")).toContainText("1 KB");
      await expect(buttons.nth(1)).toBeEnabled();

      // Activation B (Bravo) — converges (2 KB → 4 KB); its reenum goes pending
      // then clears, and only B's poll may own the resulting notice.
      await buttons.nth(1).click();
      await page.clock.fastForward(3000);
      await expect(page.locator("[data-testid=reenum-overlay]")).toBeVisible();
      await page.clock.fastForward(5000);
      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0, { timeout: 10000 });

      const notice = page.locator("[data-testid=activation-notice]");
      await expect(notice).toContainText("next lock");
      await expect(notice).toContainText("BravoChime.wav");
      await expect(notice).not.toContainText("AlphaChime.wav");
      assertCleanConsole(probe);
    });

    test("reenum that clears before convergence still yields the next-lock notice", async ({
      page,
      probe,
    }) => {
      // Regression: the reenum poll and the chime-convergence poll are independent.
      // If reenum clears FIRST (sets the "next lock" notice while it's still hidden
      // behind pendingActivation), the later convergence must NOT overwrite it with
      // the plain success copy — i3's whole point is the "next lock" guidance.
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      const v0 = { name: "LockChime.wav", rel_path: "LockChime.wav", size_bytes: 512, modified: "2026-06-15T23:57:58" };
      const v1 = { name: "LockChime.wav", rel_path: "LockChime.wav", size_bytes: 1024, modified: "2026-06-15T23:58:30" };
      let chimeConverged = false;
      let gadgetReads = 0;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 1024 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed: chimeConverged ? v1 : v0 });
      });
      // Reenum reports pending on the first read only, so it clears on the first
      // fast-forward — before convergence is allowed to happen.
      await page.route("**/api/gadget/status", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        gadgetReads += 1;
        return jsonRoute(route, 200, { ...GADGET_BASE, chime_reenum_pending: gadgetReads <= 1 });
      });

      await gotoActivationMedia(page);
      await page.locator("[data-testid=library-set-active]").first().click();
      // The overlay appears while reenum is pending.
      await expect(page.locator("[data-testid=reenum-overlay]")).toBeVisible();

      // First fast-forward: reenum clears (notice queued but hidden behind
      // pendingActivation); convergence is still gated off, so pending stays.
      await page.clock.fastForward(2000);
      await expect(page.locator("[data-testid=reenum-overlay]")).toHaveCount(0, { timeout: 10000 });
      await expect(page.locator("[data-testid=activation-status]")).toBeVisible();

      // Now allow convergence and let the convergence poll re-arm fire.
      chimeConverged = true;
      await page.clock.fastForward(2000);

      const notice = page.locator("[data-testid=activation-notice]");
      await expect(notice).toBeVisible();
      await expect(notice).toContainText("next lock");
      await expect(notice).toContainText("Sparkle.wav");
      assertCleanConsole(probe);
    });

    test("all Set Active buttons disable while pending", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([
          { filename: "Sparkle.wav", bytes: 2048 },
          { filename: "Bell.wav", bytes: 4096 },
        ]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await gotoActivationMedia(page);

      const buttons = page.locator("[data-testid=library-set-active]");
      await buttons.first().click();
      await expect(page.locator("[data-testid=activation-status]")).toContainText("Applying “Sparkle.wav”");
      await expect(buttons.nth(0)).toBeDisabled();
      await expect(buttons.nth(1)).toBeDisabled();
    });

    test("timeout shows waiting and Refresh now", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await gotoActivationMedia(page);

      await page.locator("[data-testid=library-set-active]").first().click();
      await expect(page.locator("[data-testid=activation-status]")).toContainText("Applying “Sparkle.wav”");
      await page.waitForTimeout(50);
      await page.clock.fastForward(65000);
      await page.waitForTimeout(0);
      await expect(page.locator("[data-testid=activation-status]")).toContainText("Still applying", {
        timeout: 20000,
      });
      await expect(page.locator("[data-testid=activation-refresh-now]")).toBeVisible();
      // Buttons stay disabled through the waiting phase too, so a still-applying
      // handoff can't be raced by a second activation (prevents misattribution).
      await expect(page.locator("[data-testid=library-set-active]").first()).toBeDisabled();

      installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      await page.locator("[data-testid=activation-refresh-now]").click();
      await expect(page.locator("[data-testid=activation-notice]")).toContainText("is now your active lock chime");
      // Once converged, Set Active re-enables.
      await expect(page.locator("[data-testid=library-set-active]").first()).toBeEnabled();
    });

    test("same-size activation converges on modified change", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 1024 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await gotoActivationMedia(page);
      await page.locator("[data-testid=library-set-active]").first().click();

      installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:58:00",
      };
      await page.clock.fastForward(2000);

      await expect(page.locator("[data-testid=activation-notice]")).toContainText("is now your active lock chime");
      await expect(page.locator("[data-testid=active-chime-audio]")).toHaveAttribute(
        "src",
        /v=2026-06-15T23%3A58%3A00/,
      );
    });

    test("activating the first chime shows status with no chime installed", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      let installed: {
        name: string;
        rel_path: string;
        size_bytes: number;
        modified: string;
      } | null = null;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "Sparkle.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await page.goto("/media", { waitUntil: "load" });
      await expect(page.locator(".container[data-screen=media]")).toBeVisible();
      // Nothing installed yet — the empty state renders, but the activation
      // status must still show (blocker: status was nested in the installed branch).
      await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();

      await page.locator("[data-testid=library-set-active]").first().click();
      await expect(page.locator("[data-testid=activation-status]")).toContainText("Applying “Sparkle.wav”");

      installed = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      await page.clock.fastForward(2000);

      await expect(page.locator("[data-testid=activation-notice]")).toContainText("is now your active lock chime");
      await expect(page.locator("[data-testid=active-chime]")).toContainText("2 KB");
    });

    test("active card shows the activated source name, not LockChime.wav", async ({ page }) => {
      await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
      const oldInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 1024,
        modified: "2026-06-15T23:57:58",
      };
      const newInstalled = {
        name: "LockChime.wav",
        rel_path: "LockChime.wav",
        size_bytes: 2048,
        modified: "2026-06-15T23:58:00",
      };
      let installed = oldInstalled;

      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, snapshot([{ filename: "MarioFart.wav", bytes: 2048 }]));
      });
      await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
        if (route.request().method() !== "POST") return route.continue();
        return route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({ state: "queued", job_id: "a1" }),
        });
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, { installed });
      });

      await gotoActivationMedia(page);
      // Before activation the size doesn't match the library, so the honest
      // device filename shows.
      await expect(page.locator("[data-testid=active-chime-name]")).toHaveText("LockChime.wav");
      await expect(page.locator("[data-testid=active-chime-source]")).toHaveCount(0);

      await page.locator("[data-testid=library-set-active]").first().click();
      installed = newInstalled;
      await page.clock.fastForward(2000);

      // After activation the card shows the SOURCE name, with the device file as
      // a subtitle.
      await expect(page.locator("[data-testid=active-chime-name]")).toHaveText("MarioFart.wav");
      await expect(page.locator("[data-testid=active-chime-source]")).toHaveText(
        "Installed as LockChime.wav",
      );
    });

    test("cold load resolves the source name via a unique library size match", async ({ page }) => {
      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(
          route,
          200,
          snapshot([
            { filename: "MarioFart.wav", bytes: 2048 },
            { filename: "Other.wav", bytes: 4096 },
          ]),
        );
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, {
          installed: {
            name: "LockChime.wav",
            rel_path: "LockChime.wav",
            size_bytes: 2048,
            modified: "2026-06-15T23:58:00",
          },
        });
      });

      await gotoActivationMedia(page);
      await expect(page.locator("[data-testid=active-chime-name]")).toHaveText("MarioFart.wav");
      await expect(page.locator("[data-testid=active-chime-source]")).toHaveText(
        "Installed as LockChime.wav",
      );
    });

    test("size collision falls back to the honest LockChime.wav name", async ({ page }) => {
      await page.route("**/api/chime-scheduler", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(
          route,
          200,
          snapshot([
            { filename: "A.wav", bytes: 2048 },
            { filename: "B.wav", bytes: 2048 },
          ]),
        );
      });
      await page.route("**/api/chimes", (route) => {
        if (route.request().method() !== "GET") return route.continue();
        return jsonRoute(route, 200, {
          installed: {
            name: "LockChime.wav",
            rel_path: "LockChime.wav",
            size_bytes: 2048,
            modified: "2026-06-15T23:58:00",
          },
        });
      });

      await gotoActivationMedia(page);
      await expect(page.locator("[data-testid=active-chime-name]")).toHaveText("LockChime.wav");
      await expect(page.locator("[data-testid=active-chime-source]")).toHaveCount(0);
    });
  });
});
