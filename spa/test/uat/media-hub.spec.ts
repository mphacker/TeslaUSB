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
// PARITY: the home screen is a faithful reproduction of the legacy Flask
// settings/device-status dashboard captured at
// docs/tasks/parity-baseline/media-hub/. webd's read-only catalog API does NOT
// serve the legacy system services (/api/system/health|metrics, /api/storage/
// health) or any mutation route, so the device-status, System Health, Live
// Metrics and Storage Health sections render the legacy template's DEGRADED /
// LOADING / UNKNOWN initial state — the parity the integrator endorsed ("a
// catalog-only webd organically reproduces that look — that IS parity"). We
// assert that degraded-state structure + copy and capture screenshots as
// artifacts; we do NOT pixel-diff the PNG (the captured PNG shows the populated
// dev-box probe data, which a read-only build cannot and must not fabricate).

const SECTION_ORDER = [
  "System Health",
  "Live Metrics",
  "WiFi Networks",
  "Access Point",
  "Storage & Auto-Cleanup",
  "Mapping & Indexing",
  "Network File Sharing",
  "Storage Health",
  "System",
];

/** Settle: bundle executed, dashboard structure painted. */
async function gotoDashboard(page: Page) {
  await page.goto("/", { waitUntil: "load" });
  await expect(page.locator("[data-screen=settings-dashboard]")).toBeVisible();
  await expect(page.locator(".device-status-card")).toContainText("Status Unknown");
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
  // ── Gate 1: functional + structural parity ─────────────────────────────
  test("functional parity — shell, degraded device/health/metrics, section order", async ({
    page,
    probe,
  }, testInfo) => {
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

    // Device status — degraded "unknown" banner (exact baseline copy).
    const card = page.locator(".device-status-card.device-status-unknown");
    await expect(card).toBeVisible();
    await expect(card).toContainText("Status Unknown");
    await expect(card).toContainText("Unable to determine current device status.");

    // System Health — open. The legacy /api/system/health probe is not in the
    // read-only catalog API, so the overall stays the legacy degraded default
    // and every subsystem row degrades to the legacy "—" state, EXCEPT Video
    // Indexer, which is populated from real catalog data (seed = 6 clips).
    const sh = page.locator("#system-health-section");
    await expect(sh).toHaveAttribute("open", "");
    await expect(page.locator("#system-health-overall-text")).toHaveText("Unknown");
    await expect(
      sh.locator("#system-health-overall .health-dot.health-dot-unknown"),
    ).toBeVisible();
    // 10 legacy subsystem rows × 3 grid cells = 30 direct children.
    await expect(page.locator("#system-health-rows > div")).toHaveCount(30);
    const shText = await page.locator("#system-health-rows").innerText();
    expect(shText).toContain("USB Gadget"); // a system-probe label is present
    expect(shText).toContain("Video Indexer");
    // Video Indexer carries REAL catalog data in the baseline's exact phrasing.
    expect(shText).toMatch(/6 clips indexed; newest is \d+ d old/);
    // Every other (non-catalog) subsystem degrades to "—" — none is fabricated.
    expect((shText.match(/—/g) ?? []).length).toBe(9);

    // Live Metrics — open, six zero-state tiles, "—" footer (no fabricated nums).
    const lm = page.locator("#live-metrics-section");
    await expect(lm).toHaveAttribute("open", "");
    const tiles = page.locator("#live-metrics-grid .metric-tile");
    await expect(tiles).toHaveCount(6);
    for (let i = 0; i < 6; i++) {
      await expect(tiles.nth(i).locator(".metric-value")).toHaveText("—");
    }
    await expect(page.locator("#live-metrics-foot")).toContainText("Updated");
    await expect(page.locator("#live-metrics-updated")).toHaveText("—");
    await expect(page.locator("#live-metrics-uptime")).toHaveText("—");

    // WiFi + Access Point — degraded read-only copy (no nmcli/AP tooling).
    await expect(page.locator("#savedNetworksList")).toContainText(
      "Wi-Fi management is not available in the read-only catalog build.",
    );
    const ap = page.locator("details.settings-section", {
      has: page.locator("summary", { hasText: "Access Point" }),
    });
    await expect(ap).toContainText("AP status unavailable");

    // Storage Health — static "checking" skeleton; ALL six rows are "—" (none
    // may be fabricated from a live probe webd does not serve).
    await expect(page.locator("#storage-health-summary")).toHaveText("Checking…");
    await expect(page.locator("#storage-health-grid dd")).toHaveText([
      "—", "—", "—", "—", "—", "—",
    ]);

    // System — host facts are unknown in the read-only build (not fabricated);
    // only the static B-1 version string is shown.
    const sys = page.locator("details.settings-section", {
      has: page.locator("summary", { hasText: "System" }),
    });
    await expect(sys.locator("strong")).toHaveText("—"); // Hostname value
    await expect(sys.locator("code")).toHaveText("B-1");

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
    // The config bindings + the Video Indexer enrichment prove the catalog API
    // is actually wired in (settings → forms, clips → System Health row).
    expect(apiSeen.has("/api/settings"), "/api/settings was never requested").toBe(true);
    expect(apiSeen.has("/api/clips"), "/api/clips was never requested").toBe(true);

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
    // (e.g. a timer that fetches the absent /api/system/health) would log a
    // console error / produce a non-2xx here rather than escaping the gate.
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

  // ── Gate 2: performance — capture + report (dev-box profile) ────────────
  test("perf — capture TTFB/DCL/FCP/interactive + slowest requests", async ({
    page,
  }, testInfo) => {
    const navStart = Date.now();
    await page.goto("/", { waitUntil: "load" });
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
