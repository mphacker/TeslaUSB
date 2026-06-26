import {
  test,
  expect,
  loadState,
  ALLOWED_API,
  ARTIFACTS,
  type Probe,
} from "./helpers";
import type { Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

// ── Task 5.2 UAT gate (spa.md §5/§6) ──────────────────────────────────────
// Each test drives the REAL bundle served by webd against a seeded read-only
// catalog (global-setup). Fresh context per test ⇒ cold cache, no bleed.
//
// PARITY + LIVE DATA: the home screen reproduces the legacy Flask settings /
// device-status dashboard captured at docs/tasks/parity-baseline/media-hub/.
// As of 5.1d, webd serves read-only device-status probes (/api/system/health,
// /api/system/metrics, /api/storage/health) and the screen renders them. Those
// handlers never 5xx and degrade to unknown/null for anything webd cannot
// observe honestly, so unprobed subsystems still render the legacy "unknown /
// —" state. To keep assertions deterministic and host-independent, the
// functional-parity test intercepts the three probes with fixtures and proves
// the UI renders them; the clean/read-only/wiring tests hit the REAL endpoints
// to prove they return 2xx with a clean console. We capture screenshots as
// artifacts; we do NOT pixel-diff the PNG.

const SECTION_ORDER = [
  "System Health",
  "Live Metrics",
  "USB Drive",
  "WiFi Networks",
  "Access Point",
  "Storage & Auto-Cleanup",
  "Mapping & Indexing",
  "Network File Sharing",
  "Storage Health",
  "System",
];

// Deterministic device-status fixtures (5.1d). Field names mirror the webd
// serde DTOs exactly. Used by the functional-parity test via route interception
// so the assertions never depend on the build host's real /proc or disk.
const HEALTH_FIXTURE = {
  overall: "ok",
  subsystems: {
    gadget: { severity: "ok", message: "USB gadget configured (attached)" },
    worker: { severity: "ok", message: "Idle, queue empty" },
    disk: { severity: "ok", message: "50.0 GB free of 64.0 GB (78%)" },
    storage_writable: { severity: "ok", message: "archive root writable" },
  },
};
const METRICS_FIXTURE = {
  uptime_s: 123456,
  load: { one: 0.15, five: 0.22, fifteen: 0.18 },
  mem: { total_bytes: 536870912, available_bytes: 268435456, used_pct: 50 },
  swap: null,
  cpu_temp_c: 47.2,
  updated_at: 1700000000,
};
const STORAGE_FIXTURE = {
  severity: "ok",
  summary: "50.0 GB free of 64.0 GB",
  device: "/dev/mmcblk0p2",
  fstype: "ext4",
  mount: "/data",
  used_bytes: 15032385536,
  total_bytes: 68719476736,
  fs_errors: null,
  io_errors_24h: null,
  trim: null,
};
// USB-gadget status fixture. Field names mirror webd's /api/gadget/status DTO.
// gadgetd is NOT spawned in the UAT harness, so the live socket read 503s; we
// mock the daemon-present (200) state — the normal production state — exactly as
// every other gadgetd-dependent flow (install/remove handoffs) is mocked here.
const GADGET_FIXTURE = {
  present: true,
  bound: true,
  bound_udc: "fe980000.usb",
  udc_state: "configured",
  lun_file: "/data/teslausb/cam.img",
  media_lun_file: "/data/teslausb/media.img",
  handoff_active: false,
  pending_mutations: 0,
  applying_mutations: 0,
  media_ro_mounted: true,
  media_ro_path: "/run/teslausb/media-ro",
  media_ro_error: null,
  last_handoff_id: "h-42",
  last_result: "done",
};

/** Intercept the three device-status probes with deterministic fixtures so the
 *  functional-parity assertions are host-independent. Must be called BEFORE the
 *  navigation that triggers the screen's mount-time fetches. */
async function routeSystemProbes(page: Page) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/system/health", (r) => r.fulfill(json(HEALTH_FIXTURE)));
  await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
  await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_FIXTURE)));
}

/** Mock gadgetd-present (200) for `/api/gadget/status`. gadgetd is not spawned
 *  in the harness, so the live socket read would 503; mocking the daemon-up
 *  state mirrors the established gadgetd-flow mocking and represents the normal
 *  production state. The `gadget-status — unavailable` test overrides this with
 *  a 503 to prove graceful degradation. */
async function routeGadgetStatus(page: Page) {
  await page.route("**/api/gadget/status", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(GADGET_FIXTURE),
    }),
  );
}

/** Settle: bundle executed, dashboard structure painted. */
async function gotoDashboard(page: Page) {
  await page.goto("/settings", { waitUntil: "load" });
  await expect(page.locator("[data-screen=settings-dashboard]")).toBeVisible();
  await expect(page.locator(".device-status-card")).toBeVisible();
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

test.describe("settings dashboard UAT", () => {
  // gadgetd is not spawned in the harness; mock its control-socket read as
  // "present" (the normal production state) for every test. The dedicated
  // degradation test below overrides this to assert the 503 → unavailable path.
  test.beforeEach(async ({ page }) => {
    await routeGadgetStatus(page);
  });

  // ── Gate 1: functional + structural parity ─────────────────────────────
  test("functional parity — shell, live device/health/metrics, section order", async ({
    page,
    probe,
  }, testInfo) => {
    await routeSystemProbes(page);
    await gotoDashboard(page);

    // App shell (base.html parity): brand + toast region.
    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    await expect(page.locator("#toast-container")).toHaveCount(1);

    // Active nav is Settings (the captured page is /settings/). Assert against
    // the nav actually visible at this breakpoint (rail >=1024px else tabs).
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toBeVisible();
    await expect(activeNav).toHaveAttribute("aria-current", "page");
    await expect(activeNav).toContainText("Settings");

    // Device status — driven by health.overall ("ok" fixture → Online banner).
    const card = page.locator(".device-status-card.device-status-ok");
    await expect(card).toBeVisible();
    await expect(card).toContainText("Online");
    await expect(card).toContainText("All systems nominal.");

    // System Health — open. overall + the four probed subsystem rows come from
    // the fixture; Video Indexer comes from the real catalog (seed = 30 clips);
    // the remaining five subsystems have no probe data and degrade to "—".
    const sh = page.locator("#system-health-section");
    await expect(sh).toHaveAttribute("open", "");
    await expect(page.locator("#system-health-overall-text")).toHaveText("Healthy");
    await expect(
      sh.locator("#system-health-overall .health-dot.health-dot-ok"),
    ).toBeVisible();
    // 10 subsystem rows × 3 grid cells = 30 direct children.
    await expect(page.locator("#system-health-rows > div")).toHaveCount(30);
    const shText = await page.locator("#system-health-rows").innerText();
    expect(shText).toContain("USB Gadget");
    expect(shText).toContain("USB gadget configured (attached)"); // probe message
    expect(shText).toContain("Background Worker");
    expect(shText).toContain("Idle, queue empty");
    expect(shText).toContain("archive root writable");
    expect(shText).toContain("Video Indexer");
    // Video Indexer carries REAL catalog data in the baseline's exact phrasing.
    expect(shText).toMatch(/30 clips indexed; newest is \d+ d old/);
    // The five unprobed subsystems degrade to "—" — none is fabricated.
    expect((shText.match(/—/g) ?? []).length).toBe(5);
    const workerLabel = sh.locator("#system-health-rows > div", {
      hasText: "Background Worker",
    });
    await expect(workerLabel).toHaveText("Background Worker");
    await expect(
      workerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='ok']"),
    ).toBeVisible();
    await expect(workerLabel.locator("xpath=following-sibling::div[1]")).toHaveText(
      "Idle, queue empty",
    );

    // Live Metrics — open; load/mem/uptime from the fixture, CPU + SD/USB I/O
    // and the (null) swap stay "—" (webd does not sample those — not fabricated).
    const lm = page.locator("#live-metrics-section");
    await expect(lm).toHaveAttribute("open", "");
    const tiles = page.locator("#live-metrics-grid .metric-tile");
    await expect(tiles).toHaveCount(7);
    await expect(page.locator("#metric-load .metric-value")).toHaveText(
      "0.15 / 0.22 / 0.18",
    );
    await expect(page.locator("#metric-mem .metric-value")).toHaveText("50%");
    await expect(page.locator("#metric-mem .metric-detail")).toHaveText(
      "256 MB / 512 MB",
    );
    await expect(page.locator("#metric-cpu .metric-value")).toHaveText("—");
    await expect(page.locator("#metric-temp .metric-value")).toHaveText("47.2 \u00b0C");
    await expect(page.locator("#metric-swap .metric-value")).toHaveText("—");
    await expect(page.locator("#live-metrics-foot")).toContainText("Updated");
    await expect(page.locator("#live-metrics-uptime")).toHaveText("up 1d 10h 17m");

    // USB Drive — open; live gadgetd control-socket read (mocked "present").
    // Proves the first cross-daemon control-socket read renders end-to-end.
    const usb = page.locator("#usb-gadget-section");
    await expect(usb).toHaveAttribute("open", "");
    await expect(page.locator("[data-testid=usb-present]")).toHaveText("Yes");
    await expect(page.locator("[data-testid=usb-bound]")).toHaveText(
      "Yes (configured)",
    );
    await expect(page.locator("#usb-gadget-card")).toContainText(
      "/data/teslausb/cam.img",
    );
    await expect(page.locator("#usb-gadget-card")).toContainText(
      "/data/teslausb/media.img",
    );
    await expect(page.locator("[data-testid=usb-media-ro]")).toHaveText(
      "Mounted (/run/teslausb/media-ro)",
    );

    // WiFi + Access Point — degraded read-only copy (no nmcli/AP tooling).
    await expect(page.locator("#savedNetworksList")).toContainText(
      "Wi-Fi management is not available in the read-only catalog build.",
    );
    const ap = page.locator("details.settings-section", {
      has: page.locator("summary", { hasText: "Access Point" }),
    });
    await expect(ap).toContainText("AP status unavailable");

    // Storage Health — severity/summary/device/fs/mount from the fixture; the
    // wear-telemetry rows (fs errors, I/O errors, TRIM) stay "—" (SD exposes
    // none — never fabricated).
    await expect(page.locator("#storage-health-summary")).toHaveText(
      "50.0 GB free of 64.0 GB",
    );
    await expect(page.locator("#storage-health-grid dd")).toHaveText([
      "/dev/mmcblk0p2",
      "ext4",
      "/data",
      "—",
      "—",
      "—",
    ]);

    // System — Uptime + Memory now come from the metrics fixture; Hostname / IP
    // / Platform remain unknown in the read-only build (not fabricated).
    const sys = page.locator("details.settings-section", {
      has: page.locator("summary", { hasText: /^System$/ }),
    });
    await expect(sys.locator("strong")).toHaveText("—"); // Hostname value
    await expect(sys.locator("code")).toHaveText("B-1");
    await expect(sys).toContainText("up 1d 10h 17m");
    await expect(sys).toContainText("256 MB / 512 MB");

    // /api/settings binding proof — these fields are populated from the live
    // settings response and the seed sets them to NON-DEFAULT values, so each
    // assertion holds ONLY if the screen actually bound the response (a screen
    // that ignored /api/settings would show the template defaults mph/85/10/""/
    // unchecked). Elements live in collapsed <details>; value/checked are still
    // readable without expanding.
    await expect(page.locator("input[name=trip_gap_minutes]")).toHaveValue("15");
    await expect(page.locator("#mapping-speed-limit")).toHaveValue("75");
    await expect(page.locator("#mapping-speed-units")).toHaveValue("kph");
    await expect(page.locator("#mapping-display-timezone")).toHaveValue(
      "America/Los_Angeles",
    );
    await expect(page.locator("#samba_enabled")).toBeChecked();

    // Section order + count — exact parity with the captured dashboard.
    const summaries = page.locator("details.settings-section > summary");
    await expect(summaries).toHaveCount(SECTION_ORDER.length);
    await expect(summaries).toHaveText(SECTION_ORDER);

    assertCleanConsole(probe);
  });

  // ── Gate 5: wiring proof (the freshly-built bundle is what executed) ─────
  test("wiring proof — served HTML loads the hashed bundle that actually ran", async ({
    page,
  }) => {
    const state = loadState();
    await gotoDashboard(page);

    // (a) The build id baked into the on-disk bundle equals the one the live
    //     page exposes ⇒ the executed JS IS the freshly-built bundle.
    const winBuild = await page.evaluate(
      () => (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__,
    );
    expect(winBuild, "window.__TESLAUSB_BUILD__ must be defined").toBeTruthy();
    expect(winBuild).not.toBe("dev"); // not the un-built dev entry
    expect(winBuild).toBe(state.buildId);

    // (b) The served HTML references the hashed assets, not the TS dev entry.
    const html = await (await page.request.get("/")).text();
    expect(html).toContain(state.jsAsset);
    expect(html).not.toContain("/src/main.tsx");
    expect(html).toMatch(/\/assets\/index-[\w-]+\.js/);
    if (state.cssAsset) expect(html).toContain(state.cssAsset);

    // (c) The JS asset is served as JavaScript (not HTML via SPA-fallback).
    const jsResp = await page.request.get(state.jsAsset);
    expect(jsResp.status()).toBe(200);
    expect(jsResp.headers()["content-type"] ?? "").toMatch(/javascript/);

    // (d) The hashed bundle CSS is served as CSS (not a fallback-HTML 200).
    if (state.cssAsset) {
      const bundleCss = await page.request.get(state.cssAsset);
      expect(bundleCss.status()).toBe(200);
      expect(bundleCss.headers()["content-type"] ?? "").toMatch(/css/);
    }

    // (e) Carried legacy CSS resolves as CSS too.
    const legacyCss = await page.request.get("/static/css/style.css");
    expect(legacyCss.status()).toBe(200);
    expect(legacyCss.headers()["content-type"] ?? "").toMatch(/css/);
  });

  // ── Gate 3 (read-only): no mutations; config forms are inert ────────────
  test("read-only — no mutating requests, config forms cannot submit", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoDashboard(page);
    await page.waitForLoadState("networkidle");

    // Every request is same-origin (no third-party calls / exfiltration), and
    // every /api/ call is a GET to a whitelisted path. Factored so we can re-run
    // it AFTER actively exercising the forms (a mutation could be a stray GET to
    // a non-whitelisted path, which a method-only filter would miss).
    function assertOnlyWhitelistedApi(): Map<string, string> {
      const seen = new Map<string, string>(); // pathname -> search
      for (const req of probe.requests) {
        const u = new URL(req.url);
        expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
        if (!u.pathname.startsWith("/api/")) continue;
        expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
        expect(ALLOWED_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
        seen.set(u.pathname, u.search);
      }
      return seen;
    }
    const apiSeen = assertOnlyWhitelistedApi();
    // The config bindings + the Video Indexer enrichment + the device-status
    // probes prove the catalog API is actually wired in.
    expect(apiSeen.has("/api/settings"), "/api/settings was never requested").toBe(true);
    expect(apiSeen.has("/api/clips"), "/api/clips was never requested").toBe(true);
    expect(
      apiSeen.has("/api/system/health"),
      "/api/system/health was never requested",
    ).toBe(true);
    expect(
      apiSeen.has("/api/system/metrics"),
      "/api/system/metrics was never requested",
    ).toBe(true);
    expect(
      apiSeen.has("/api/storage/health"),
      "/api/storage/health was never requested",
    ).toBe(true);

    // ACTIVELY exercise the mutation surface: expand every section, then prove
    // each config <form> swallows its submit (onSubmit preventDefault) and that
    // pressing Enter in a field issues no request and does not navigate.
    await page.locator("details.settings-section").evaluateAll((els) =>
      els.forEach((d) => d.setAttribute("open", "")),
    );
    const allPrevented = await page.locator("form").evaluateAll((forms) =>
      forms.map((f) => {
        const ev = new Event("submit", { cancelable: true, bubbles: true });
        f.dispatchEvent(ev);
        return ev.defaultPrevented;
      }),
    );
    expect(allPrevented.length, "dashboard should have config forms").toBeGreaterThan(0);
    expect(allPrevented.every(Boolean), "every form must preventDefault on submit").toBe(true);

    const urlBefore = page.url();
    await page.locator("#mapping-speed-limit").press("Enter");
    await page.locator("#samba_password").press("Enter");
    // Settle: let any (errant) delayed/debounced submit fetch reach the wire
    // before we assert the read-only invariant.
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(500);
    expect(page.url(), "pressing Enter in a field must not navigate").toBe(urlBefore);

    // Re-assert the FULL whitelist after the exercise — catches a stray GET to a
    // non-whitelisted path as well as any mutating method.
    assertOnlyWhitelistedApi();
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(mutating, `mutating request(s): ${JSON.stringify(mutating)}`).toEqual([]);

    // The Save buttons are disabled (cannot be the source of a mutation).
    await expect(page.locator("form button[disabled]")).toHaveCount(2);
  });

  // ── Gate 3 (console + network): zero warnings/errors/pageerror, no failures ─
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    await gotoDashboard(page);
    await page.waitForLoadState("networkidle");
    // Bounded post-load window: a regression that introduces a delayed poller
    // or a probe that 5xx's (e.g. a timer hammering /api/system/health) would
    // log a console error / produce a non-2xx here rather than escaping the gate.
    await page.waitForTimeout(750);

    assertCleanConsole(probe);

    expect(
      probe.failedRequests,
      `failed request(s): ${JSON.stringify(probe.failedRequests)}`,
    ).toEqual([]);

    // No same-origin response should be an error status. (webd's SPA fallback
    // returns 200 for unknown routes, so a 4xx/5xx here is a real problem.)
    const bad = probe.responses.filter(
      (r) => new URL(r.url).origin === origin && r.status >= 400,
    );
    expect(bad, `non-2xx response(s): ${JSON.stringify(bad)}`).toEqual([]);
  });

  // ── Gate: graceful degradation when gadgetd is unreachable ──────────────
  test("gadget-status — 503 renders an honest 'unavailable' state, console clean", async ({
    page,
    probe,
  }) => {
    // Override the beforeEach "present" mock: simulate gadgetd down (the live
    // on-device failure mode, and the harness's real state). The screen must
    // show the honest unavailable copy — never a fabricated "connected" — and
    // the handled 503 must NOT leak a console error or pageerror.
    await page.unroute("**/api/gadget/status");
    await page.route("**/api/gadget/status", (r) =>
      r.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "gadgetd_unavailable", message: "gadgetd is not reachable" },
        }),
      }),
    );
    await gotoDashboard(page);
    await expect(page.locator("[data-testid=usb-gadget-unavailable]")).toBeVisible();
    await expect(page.locator("[data-testid=usb-present]")).toHaveCount(0);
    // Chromium itself logs a "Failed to load resource: …503…" console error for
    // each failed fetch — that's the browser, not the app. Two independent
    // consumers poll /api/gadget/status when gadgetd is down — the settings USB
    // card AND the global shell mode dot — so >=1 such browser log is expected
    // (each honestly degrades). Tolerate any number of these /api/gadget/status
    // 503 logs; assert nothing else leaked (no pageerror, no warning, no other
    // console error).
    const expected503 = probe.consoleErrors.filter(
      (e) => e.text.includes("503") && e.location.includes("/api/gadget/status"),
    );
    expect(
      expected503.length,
      "expected at least one 503 resource-load log",
    ).toBeGreaterThanOrEqual(1);
    const other = probe.consoleErrors.filter(
      (e) => !(e.text.includes("503") && e.location.includes("/api/gadget/status")),
    );
    expect(other, `unexpected console error(s): ${JSON.stringify(other)}`).toEqual([]);
    expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
    expect(
      probe.consoleWarnings,
      `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
    ).toEqual([]);
  });

  // ── Gate 2: performance — capture + report (dev-box profile) ────────────
  test("perf — capture TTFB/DCL/FCP/interactive + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await page.goto("/settings", { waitUntil: "load" });
    await expect(page.locator("[data-screen=settings-dashboard]")).toBeVisible();
    // Dashboard structure painted, measured in-page against the navigation time
    // origin (independent of the runner's clock). This is a "content visible"
    // proxy, not a formal TTI — labelled as such in the report.
    const contentVisibleMs = await page.evaluate(() => performance.now());

    const timings = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
      const fcp = performance
        .getEntriesByType("paint")
        .find((p) => p.name === "first-contentful-paint");
      const resources = (performance.getEntriesByType("resource") as PerformanceResourceTiming[])
        .map((r) => ({
          url: r.name,
          ms: Math.round(r.duration * 10) / 10,
          type: r.initiatorType,
        }))
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

    // Interaction responsiveness: a real user action (theme toggle) must take
    // effect — proves the app is genuinely interactive, not just painted.
    const beforeTheme = await page.evaluate(
      () => document.documentElement.getAttribute("data-theme") ?? "light",
    );
    const tToggleStart = Date.now();
    await page.locator(".theme-toggle-btn").click();
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.getAttribute("data-theme") ?? "light"),
      )
      .not.toBe(beforeTheme);
    const themeToggleMs = Date.now() - tToggleStart;

    const report = {
      environment:
        "dev webd (cargo debug build) on Windows host; Chromium via Playwright; " +
        "fresh context per test (cold cache). NOTE: spa.md's <~2s 'interactive' " +
        "target is the ON-DEVICE (Raspberry Pi) profile — these are dev-box " +
        "numbers and are reported, not asserted, against that bar.",
      viewport: testInfo.project.name,
      ttfbMs: timings.ttfbMs,
      domContentLoadedMs: timings.domContentLoadedMs,
      domInteractiveMs: timings.domInteractiveMs,
      loadMs: timings.loadMs,
      fcpMs: timings.fcpMs,
      contentVisibleMs: Math.round(contentVisibleMs),
      themeToggleResponseMs: themeToggleMs,
      wallClockNavMs: Date.now() - navStart,
      slowestRequests: timings.slowestRequests,
    };

    const out = resolve(ARTIFACTS, `perf-${testInfo.project.name}.json`);
    writeFileSync(out, JSON.stringify(report, null, 2));
    await testInfo.attach(`perf-${testInfo.project.name}.json`, {
      body: JSON.stringify(report, null, 2),
      contentType: "application/json",
    });
    console.log(`[uat][perf:${testInfo.project.name}]`, JSON.stringify(report, null, 2));

    // Loose dev-box sanity bounds — catch gross regressions without flaking on
    // the on-device target. (FCP/interactive on a debug webd + cold cache.)
    expect(report.fcpMs, "FCP should be present").not.toBeNull();
    expect(report.fcpMs!).toBeLessThan(5000);
    expect(report.contentVisibleMs).toBeLessThan(5000);
  });

  // ── Gate 4: responsive — render + screenshot at this project's viewport ─
  test("responsive — renders at viewport and screenshot captured", async ({
    page,
  }, testInfo) => {
    await gotoDashboard(page);

    // Content present regardless of breakpoint.
    await expect(page.locator(".device-status-card")).toBeVisible();
    await expect(page.locator("#live-metrics-grid")).toBeVisible();

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

    const shot = resolve(ARTIFACTS, `dashboard-${testInfo.project.name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await testInfo.attach(`dashboard-${testInfo.project.name}.png`, {
      path: shot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:${testInfo.project.name}] ${shot}`);
  });
});
