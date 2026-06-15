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
//  - `POST /api/chimes` / `DELETE /api/chimes/LockChime` route through the
//    gadgetd eject-handoff. gadgetd is NOT running in the UAT harness (only webd
//    is spawned), so a real call would 503. The install/remove FLOWS are
//    therefore driven against Playwright route mocks (the contract is fixed by
//    docs/specs/contracts §2.3.1; mocking an absent dependency is sanctioned).
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

    // No mutating HTTP method fires on load — install/remove require a click.
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s) on load: ${JSON.stringify(mutating)}`).toEqual([]);

    // No WebSocket of any kind.
    expect(sockets, `websocket(s) opened: ${JSON.stringify(sockets)}`).toEqual([]);

    // The ONLY /api/ requests on load are the read-only `GET /api/chimes` (active
    // chime) and `GET /api/chime-scheduler` (the embedded scheduler snapshot);
    // both are GETs, same-origin, and nothing else hits /api/.
    const allowedReads = new Set(["GET /api/chimes", "GET /api/chime-scheduler"]);
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (u.pathname.startsWith("/api/")) {
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

    // Active Lock Chime card shows the live name + size + install time + Remove.
    const active = page.locator("[data-testid=active-chime]");
    await expect(active).toBeVisible();
    await expect(page.locator("[data-testid=active-chime-name]")).toHaveText(
      "LockChime.wav",
    );
    await expect(active).toContainText("215 KB");
    await expect(active).toContainText("2026-06-01 20:10");
    await expect(page.locator("[data-testid=active-chime-remove]")).toBeVisible();

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
      return jsonRoute(route, 200, { filename, bytes: 4096 });
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
    await expect(page.locator("[data-testid=chime-upload-selected]")).toContainText(
      "Chime.wav",
    );
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    // Upload → POST hits the LIBRARY endpoint, success notice shows.
    await page.locator("[data-testid=chime-upload-submit]").click();
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Added Chime.wav to your chime library",
    );

    // v1 parity: adding to the library does NOT install an active chime, so the
    // active card stays empty and nothing POSTs to the install endpoint.
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();
    const installPosts = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/chimes" && r.method === "POST",
    );
    expect(installPosts.length, "library upload must not POST /api/chimes").toBe(0);
    expect(libraryPosts, "expected exactly one library POST").toEqual(["Chime.wav"]);

    // The input is cleared for the next action, and the JS stayed clean.
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();
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
      return jsonRoute(route, 200, { filename: "Chime.wav", bytes: 4096 });
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
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    // First attempt → 503 → retryable error banner (not fatal), button live again.
    await page.locator("[data-testid=chime-upload-submit]").click();
    const err = page.locator("[data-testid=chime-upload-error]");
    await expect(err).toBeVisible();
    await expect(err).toHaveClass(/\bretryable\b/);
    await expect(err).toContainText("Try again");
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    // Retry → success.
    await page.locator("[data-testid=chime-upload-submit]").click();
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Added Chime.wav to your chime library",
    );
    await expect(page.locator("[data-testid=chime-upload-error]")).toHaveCount(0);
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
    await page.route("**/api/chimes", (route) => {
      const m = route.request().method();
      if (m === "GET") return jsonRoute(route, 200, { installed: null });
      if (m === "POST") {
        posts.push("POST");
        return jsonRoute(route, 200, { handoff_id: "x", state: "done" });
      }
      return route.continue();
    });

    await gotoMedia(page);

    // (a) Oversize (> 1 MiB) → refused with a size message, submit stays disabled.
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Big.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer({ dataLen: 1024 * 1024 }), // total > 1 MiB
    });
    const validation = page.locator("[data-testid=chime-upload-validation]");
    await expect(validation).toBeVisible();
    await expect(validation).toContainText("under 1 MB");
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();

    // (b) Wrong sample rate (32 kHz) → refused with a format message.
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Odd.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer({ sampleRate: 32000, dataLen: 256 }),
    });
    await expect(validation).toContainText("44.1 or 48");
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeDisabled();

    // (c) A valid WAV clears the error and enables Upload.
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Good.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer({ dataLen: 256 }),
    });
    await expect(page.locator("[data-testid=chime-upload-validation]")).toHaveCount(0);
    await expect(page.locator("[data-testid=chime-upload-submit]")).toBeEnabled();

    // No POST was ever attempted for the rejected files (only client validation).
    expect(posts, `unexpected POST(s): ${JSON.stringify(posts)}`).toEqual([]);
    assertCleanConsole(probe);
  });

  // ── Gate 9: remove — operator-gated confirm → DELETE → empty state ──────
  test("remove — named confirm dialog deletes via DELETE /api/chimes/LockChime", async ({
    page,
    probe,
  }) => {
    let installedNow: typeof INSTALLED | null = INSTALLED;
    const deletes: string[] = [];

    await page.route("**/api/chimes", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return jsonRoute(route, 200, { installed: installedNow });
    });
    await page.route("**/api/chimes/*", (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      deletes.push(new URL(route.request().url()).pathname);
      installedNow = null; // removed
      return jsonRoute(route, 200, { handoff_id: "h-remove-1", state: "done" });
    });

    await gotoMedia(page);
    await expect(page.locator("[data-testid=active-chime]")).toBeVisible();

    // Remove is a deliberate, named confirmation (no one-click delete).
    await page.locator("[data-testid=active-chime-remove]").click();
    const dialog = page.locator("[data-testid=chime-remove-dialog]");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("role", "alertdialog");
    await expect(dialog).toContainText("LockChime.wav");

    await page.locator("[data-testid=chime-remove-confirm]").click();

    // Dialog closes, success notice shows, and the card falls back to empty.
    await expect(dialog).toHaveCount(0);
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Removed the lock chime",
    );
    await expect(page.locator("[data-testid=active-chime-none]")).toBeVisible();
    await expect(page.locator("[data-testid=library-empty]")).toBeVisible();

    expect(deletes, "expected one DELETE /api/chimes/LockChime").toEqual([
      "/api/chimes/LockChime",
    ]);
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
});
