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
  "Storage Health",
  "Filesystem Health Check",
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
    indexer: { severity: "ok", message: "Indexer healthy" },
    disk: { severity: "ok", message: "50.0 GB free of 64.0 GB (78%)" },
    storage_writable: { severity: "ok", message: "archive root writable" },
  },
};
const METRICS_FIXTURE = {
  hostname: "cybertruck",
  ip_address: "192.168.1.42",
  platform: "Raspberry Pi Zero 2 W Rev 1.0",
  uptime_s: 123456,
  load: { one: 0.15, five: 0.22, fifteen: 0.18 },
  mem: { total_bytes: 536870912, available_bytes: 268435456, used_pct: 50 },
  swap: null,
  cpu_temp_c: 47.2,
  cpu_times: { total: 1000000, idle: 800000 },
  sd_io: { read_bytes: 1048576, write_bytes: 2097152 },
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
  trim: null,
};

const FSCK_STATUS_FIXTURE = {
  running: false,
  partition: "part1",
  mode: "quick",
  progress: "Complete",
  start_time: null,
  result: "healthy",
  details: "No structural errors found",
  duration: 2.3,
  error: null,
};

const FSCK_HISTORY_FIXTURE = [
  {
    timestamp: "2026-08-10T14:00:00Z",
    partition: "part1",
    mode: "quick",
    result: "healthy",
    details: "No structural errors found",
    duration_seconds: 2.3,
  },
  {
    timestamp: "2026-08-10T10:00:00Z",
    partition: "part2",
    mode: "repair",
    result: "repaired",
    details: "Recovered directory chain",
    duration_seconds: 14.7,
  },
];
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

const GADGET_MODE_FIXTURE = {
  mode: "presented",
  present: true,
  bound: true,
  bound_udc: "fe980000.usb",
  udc_state: "configured",
  handoff: {
    state: "idle",
    active: false,
    pending_mutations: 0,
    applying_mutations: 0,
    last_handoff_id: "h-42",
    last_result: "done",
  },
  lun: {
    teslacam: { image: "/data/teslausb/cam.img", loaded: true },
    media: { image: "/data/teslausb/media.img", loaded: true },
  },
  recovery: {
    chime_reenum_pending: false,
    last_reenum: { result: "done", disconnect_ms: 420, reason: "chime_apply" },
    media_ro_mounted: true,
    media_ro_path: "/run/teslausb/media-ro",
    media_ro_error: null,
  },
  banner: null,
};

const WIFI_STATUS_FIXTURE = {
  connected: true,
  ssid: "Trez",
  signal: 52,
  security: "WPA2",
  ip: "192.168.1.42",
  iface: "wlan0",
};

const WIFI_NETWORKS_FIXTURE = {
  networks: [
    {
      ssid: "Trez",
      signal: 52,
      security: "WPA2",
      saved: true,
      active: true,
      protected: true,
    },
    {
      ssid: "Garage-Guest",
      signal: 74,
      security: "",
      saved: false,
      active: false,
      protected: false,
    },
    {
      ssid: "Office-5G",
      signal: 60,
      security: "WPA2",
      saved: false,
      active: false,
      protected: false,
    },
    {
      ssid: "Old-Cafe",
      signal: 40,
      security: "WPA2",
      saved: true,
      active: false,
      protected: false,
    },
  ],
};

const WIFI_SAVED_FIXTURE = {
  networks: [
    {
      ssid: "Trez",
      priority: 100,
      autoconnect: true,
      protected: true,
    },
    {
      ssid: "Old-Cafe",
      priority: 10,
      autoconnect: true,
      protected: false,
    },
    {
      ssid: "Garage",
      priority: 9,
      autoconnect: true,
      protected: false,
    },
  ],
};

const WIFI_AP_FIXTURE = {
  ap: {
    mode: "auto",
    active: false,
    ssid: "TeslaUSB-Setup",
    client_count: 0,
    ip: null,
  },
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

async function routeFsckProbes(page: Page) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/fsck/status", (r) => r.fulfill(json(FSCK_STATUS_FIXTURE)));
  await page.route("**/api/fsck/history", (r) => r.fulfill(json(FSCK_HISTORY_FIXTURE)));
  await page.route("**/api/fsck/last-check/1", (r) =>
    r.fulfill(
      json({
        timestamp: "2026-08-10T14:00:00Z",
        result: "healthy",
        details: "No structural errors found",
        age_hours: 1.5,
      }),
    ),
  );
  await page.route("**/api/fsck/last-check/2", (r) =>
    r.fulfill(
      json({
        timestamp: "2026-08-10T10:00:00Z",
        result: "repaired",
        details: "Recovered directory chain",
        age_hours: 5.5,
      }),
    ),
  );
  await page.route("**/api/fsck/last-check/3", (r) =>
    r.fulfill(json({ timestamp: null, result: "never_checked" })),
  );
}

async function routeWifiProbes(
  page: Page,
  ap = { ...WIFI_AP_FIXTURE.ap },
) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/wifi/status", (r) => r.fulfill(json(WIFI_STATUS_FIXTURE)));
  await page.route("**/api/wifi/networks", (r) => r.fulfill(json(WIFI_NETWORKS_FIXTURE)));
  await page.route("**/api/wifi/saved", (r) => r.fulfill(json(WIFI_SAVED_FIXTURE)));
  await page.route("**/api/wifi/ap", (r) => r.fulfill(json({ ap })));
}

async function routeWifiMutations(
  page: Page,
  seen: { connect: unknown[]; forget: unknown[]; priority: unknown[] },
) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/wifi/connect", async (r) => {
    seen.connect.push(r.request().postDataJSON());
    const body = r.request().postDataJSON() as { ssid: string };
    await r.fulfill(
      json({ connected: true, ssid: body.ssid, ip: "192.168.1.99", autoconnect: false }),
    );
  });
  await page.route("**/api/wifi/forget", async (r) => {
    seen.forget.push(r.request().postDataJSON());
    await r.fulfill(json({ forgotten: true, count: 1 }));
  });
  await page.route("**/api/wifi/priority", async (r) => {
    const body = r.request().postDataJSON() as { order?: unknown[] };
    seen.priority.push(body);
    await r.fulfill(json({ ok: true, count: Array.isArray(body.order) ? body.order.length : 0 }));
  });
}

async function routeApMutations(
  page: Page,
  seen: { mode: unknown[]; config: unknown[] },
  ap?: {
    mode: string;
    active: boolean;
    ssid: string | null;
    client_count: number;
    ip: string | null;
  },
) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
  await page.route("**/api/wifi/ap/mode", async (r) => {
    const body = r.request().postDataJSON() as { mode?: string };
    seen.mode.push(body);
    if (
      ap &&
      (body.mode === "auto" || body.mode === "force_on" || body.mode === "force_off")
    ) {
      ap.mode = body.mode;
      ap.active = body.mode === "force_on";
      if (body.mode !== "force_on") ap.client_count = 0;
    }
    await r.fulfill(json({ ok: true }));
  });
  await page.route("**/api/wifi/ap/config", async (r) => {
    const body = r.request().postDataJSON() as { ssid?: string; passphrase?: string };
    seen.config.push(body);
    if (ap && typeof body.ssid === "string") ap.ssid = body.ssid;
    await r.fulfill(json({ ok: true }));
  });
}

/** Mock gadgetd-present (200) for `/api/gadget/status` + `/api/gadget/mode-status`.
 *  gadgetd is not spawned
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
  await page.route("**/api/gadget/mode-status", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(GADGET_MODE_FIXTURE),
    }),
  );
}

/** wifid is a Linux-only unix-socket daemon that is NOT spawned in the UAT
 *  harness, so the live GET /api/wifi/ap read honestly 503s (exactly like
 *  gadgetd). Mock it "present, Auto, inactive" for every test so the AP panel
 *  renders against a daemon-up fixture and the browser never logs a 503
 *  resource error. The dedicated wifi/AP tests override this via routeWifiProbes
 *  to assert specific AP state; webd's honest 503-on-unavailable contract is
 *  intact and covered by webd's own unit tests. */
async function routeApStatus(page: Page) {
  await page.route("**/api/wifi/ap", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ap: { ...WIFI_AP_FIXTURE.ap } }),
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
  // gadgetd and wifid are not spawned in the harness; mock their control-socket
  // reads as "present" (the normal production state) for every test. The
  // dedicated degradation test below overrides gadget to assert the 503 →
  // unavailable path; the wifi/AP tests override /api/wifi/ap via routeWifiProbes.
  test.beforeEach(async ({ page }) => {
    await routeGadgetStatus(page);
    await routeApStatus(page);
    await routeFsckProbes(page);
  });

  // ── Gate 1: functional + structural parity ─────────────────────────────
  test("functional parity — shell, live device/health/metrics, section order", async ({
    page,
    probe,
  }, testInfo) => {
    const json = (body: unknown) => ({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    await routeSystemProbes(page);
    await routeWifiProbes(page);
    let mtick = 0;
    await page.route("**/api/system/metrics", (r) => {
      mtick += 1;
      r.fulfill(
        json({
          ...METRICS_FIXTURE,
          // Δidle/Δtotal = 100000/200000 = 0.5 → CPU 50%
          cpu_times: { total: 1000000 + mtick * 200000, idle: 800000 + mtick * 100000 },
          sd_io: { read_bytes: 1048576 + mtick * 2097152, write_bytes: 2097152 + mtick * 1048576 },
        }),
      );
    });
    await page.route("**/api/settings", async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      const body = JSON.parse(route.request().postData() || "{}") as {
        key?: string;
        value?: string;
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ key: body.key, value: body.value }),
      });
    });
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
    // the remaining two subsystems have no probe data and degrade to "—".
    const sh = page.locator("#system-health-section");
    await expect(sh).toHaveAttribute("open", "");
    await expect(page.locator("#system-health-overall-text")).toHaveText("Healthy");
    await expect(
      sh.locator("#system-health-overall .health-dot.health-dot-ok"),
    ).toBeVisible();
    // 7 subsystem rows × 3 grid cells = 21 direct children.
    await expect(page.locator("#system-health-rows > div")).toHaveCount(21);
    const shText = await page.locator("#system-health-rows").innerText();
    expect(shText).toContain("USB Gadget");
    expect(shText).toContain("USB gadget configured (attached)"); // probe message
    expect(shText).toContain("Background Worker");
    expect(shText).toContain("Idle, queue empty");
    expect(shText).toContain("archive root writable");
    expect(shText).toContain("Video Indexer");
    // Video Indexer carries REAL catalog data in the baseline's exact phrasing.
    expect(shText).toMatch(/\d+ clips indexed; newest is \d+ d old/);
    expect(shText).not.toContain("Indexer healthy");
    // The two unprobed subsystems degrade to "—" — none is fabricated.
    expect((shText.match(/—/g) ?? []).length).toBe(2);
    const indexerLabel = sh.locator("#system-health-rows > div", {
      hasText: "Video Indexer",
    });
    await expect(
      indexerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='ok']"),
    ).toBeVisible();
    await expect(indexerLabel.locator("xpath=following-sibling::div[1]")).toHaveText(
      /\d+ clips indexed; newest is \d+ d old/,
    );
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

    // Live Metrics — open; load/mem/uptime from fixture, CPU/SD from polling deltas.
    const lm = page.locator("#live-metrics-section");
    await expect(lm).toHaveAttribute("open", "");
    const tiles = page.locator("#live-metrics-grid .metric-tile");
    await expect(tiles).toHaveCount(6);
    await expect(page.locator("#metric-load .metric-value")).toHaveText(
      "0.15 / 0.22 / 0.18",
    );
    await expect(page.locator("#metric-mem .metric-value")).toHaveText("50%");
    await expect(page.locator("#metric-mem .metric-detail")).toHaveText(
      "256 MB / 512 MB",
    );
    await expect(page.locator("#metric-cpu .metric-value")).toHaveText("50%");
    await expect(page.locator("#metric-temp .metric-value")).toHaveText("47.2 \u00b0C");
    await expect(page.locator("#metric-swap .metric-value")).toHaveText("—");
    await expect(page.locator("#metric-sd .metric-value")).toHaveText(/\/s/);
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
    await expect(page.locator("[data-testid=usb-mode]")).toHaveText("Presented");
    await expect(page.locator("[data-testid=usb-handoff]")).toContainText("Idle");
    await expect(page.locator("[data-testid=usb-handoff]")).toContainText(
      "Queue 0 pending / 0 applying",
    );
    await expect(page.locator("[data-testid=usb-handoff]")).toContainText(
      "Last done (h-42)",
    );
    await expect(page.locator("#usb-gadget-card")).toContainText(
      "Loaded (/data/teslausb/cam.img)",
    );
    await expect(page.locator("#usb-gadget-card")).toContainText(
      "Loaded (/data/teslausb/media.img)",
    );
    await expect(page.locator("[data-testid=usb-recovery]")).toContainText(
      "Last USB re-enumeration: done",
    );
    await expect(page.locator("[data-testid=usb-media-ro]")).toHaveText(
      "Mounted (/run/teslausb/media-ro)",
    );

    // WiFi Networks — read-only live data from the mocked probes. Open the
    // collapsible section first so its controls are actually visible.
    await page
      .locator("#wifi-networks-section")
      .evaluate((el) => el.setAttribute("open", ""));
    await expect(page.locator("#wifi-current-connection")).toContainText("Trez");
    await expect(page.locator("#wifi-current-connection")).toContainText("52%");
    await expect(page.locator("#wifi-current-connection")).toContainText("Connected");
    const savedRows = page.locator("#wifi-saved-list .wifi-saved-row");
    await expect(savedRows).toHaveCount(3);
    await expect(savedRows.nth(0)).toContainText("Trez");
    await expect(savedRows.nth(0)).toContainText("Connected");
    await expect(savedRows.nth(0).locator(".wifi-forget-btn")).toHaveCount(0);
    await expect(savedRows.nth(1)).toContainText("Old-Cafe");
    await expect(savedRows.nth(1).locator(".wifi-forget-btn")).toHaveCount(1);
    const availableSelect = page.locator("#wifi-available-select");
    await expect(availableSelect).toBeVisible();
    await expect(availableSelect.locator("option")).toHaveCount(3);
    await expect(page.locator("#wifi-join-btn")).toBeDisabled();
    await expect(page.locator("#btnWifiScan")).toBeEnabled();

    const ap = page.locator("#access-point-section");
    await ap.evaluate((el) => el.setAttribute("open", ""));
    await expect(page.locator("#ap-status")).toContainText("TeslaUSB-Setup");
    await expect(page.locator("#ap-status")).toContainText("Inactive");

    // Storage Health — severity/summary/device/fs/mount from the fixture; the
    // wear-telemetry rows (fs errors, TRIM) stay "—" (SD exposes little stable
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
    ]);

    await expect(page.locator("[data-testid=fsck-status-line]")).toContainText(
      "Last result: healthy",
    );
    await expect(page.locator("[data-testid=fsck-last-part1]")).toContainText("Last checked");
    await expect(page.locator("[data-testid=fsck-last-part3]")).toContainText("Never checked");
    await expect(page.locator("[data-testid=fsck-history-table] tbody tr")).toHaveCount(2);

    // System — Hostname / IP / Platform / Uptime / Memory now come live from
    // the metrics fixture (webd reads them on-device); rendered, not fabricated.
    const sys = page.locator("details.settings-section", {
      has: page.locator("summary", { hasText: /^System$/ }),
    });
    await expect(sys.locator("strong")).toHaveText("cybertruck"); // Hostname value
    await expect(sys.locator("code")).toHaveText("B-1");
    await expect(sys).toContainText("192.168.1.42");
    await expect(sys).toContainText("Raspberry Pi Zero 2 W Rev 1.0");
    await expect(sys).toContainText("up 1d 10h 17m");
    await expect(sys).toContainText("256 MB / 512 MB");

    // /api/settings binding proof — these fields are populated from the live
    // settings response and the seed sets them to NON-DEFAULT values, so each
    // assertion holds ONLY if the screen actually bound the response (a screen
    // that ignored /api/settings would show the template defaults mph/80/5/""/
    // unchecked). Elements live in collapsed <details>; value/checked are still
    // readable without expanding.
    await expect(page.locator("input[name=trip_gap_minutes]")).toHaveValue("15");
    await expect(page.locator("#mapping-speed-limit")).toHaveValue("75");
    await expect(page.locator("#mapping-speed-units")).toHaveValue("kph");
    await expect(page.locator("#mapping-display-timezone")).toHaveValue(
      "America/Los_Angeles",
    );
    // The Save button lives inside the collapsed Mapping <details>, so it is hidden
    // from the accessibility tree; assert enabled via a locator (works on attached,
    // non-visible elements) rather than getByRole (which excludes hidden nodes).
    await expect(page.locator("button:has-text('Save Mapping Settings')")).toBeEnabled();

    // Section order + count — exact parity with the captured dashboard.
    const summaries = page.locator("details.settings-section > summary");
    await expect(summaries).toHaveCount(SECTION_ORDER.length);
    await expect(summaries).toHaveText(SECTION_ORDER);

    assertCleanConsole(probe);
  });

  test("wifi networks — join open network without PSK", async ({ page, probe }) => {
    const seen = { connect: [] as unknown[], forget: [] as unknown[], priority: [] as unknown[] };
    await routeWifiProbes(page);
    await routeWifiMutations(page, seen);
    await gotoDashboard(page);
    await page.locator("#wifi-networks-section").evaluate((el) => el.setAttribute("open", ""));
    await expect(page.locator("#wifi-available-select option")).toHaveCount(3);
    await expect(page.locator("#wifi-join-btn")).toBeDisabled();
    await page.selectOption("#wifi-available-select", "Garage-Guest");
    await expect(page.locator("#wifi-join-btn")).toBeEnabled();
    await page.click("#wifi-join-btn");
    await expect.poll(() => seen.connect.length).toBe(1);
    expect(seen.connect[0]).toEqual({ ssid: "Garage-Guest" });
    await expect(page.locator("#wifi-action-status")).toContainText(
      "Connected to Garage-Guest",
    );
    assertCleanConsole(probe);
  });

  test("wifi networks — join secured network with PSK", async ({ page, probe }) => {
    const seen = { connect: [] as unknown[], forget: [] as unknown[], priority: [] as unknown[] };
    await routeWifiProbes(page);
    await routeWifiMutations(page, seen);
    await gotoDashboard(page);
    await page.locator("#wifi-networks-section").evaluate((el) => el.setAttribute("open", ""));
    await page.selectOption("#wifi-available-select", "Office-5G");
    await page.click("#wifi-join-btn");
    await expect(page.locator(".wifi-psk-input")).toBeVisible();
    await page.fill(".wifi-psk-input", "hunter2pass");
    await page.click(".wifi-connect-confirm");
    await expect.poll(() => seen.connect.length).toBe(1);
    expect(seen.connect[seen.connect.length - 1]).toEqual({
      ssid: "Office-5G",
      psk: "hunter2pass",
    });
    await expect(page.locator("#wifi-action-status")).toContainText(
      "Connected to Office-5G",
    );
    assertCleanConsole(probe);
  });

  test("wifi networks — forget saved non-active network", async ({ page, probe }) => {
    const seen = { connect: [] as unknown[], forget: [] as unknown[], priority: [] as unknown[] };
    await routeWifiProbes(page);
    await routeWifiMutations(page, seen);
    await gotoDashboard(page);
    await page.locator("#wifi-networks-section").evaluate((el) => el.setAttribute("open", ""));
    await page.click('.wifi-saved-row[data-ssid="Old-Cafe"] .wifi-forget-btn');
    await expect(page.locator(".wifi-forget-confirm")).toBeVisible();
    await page.click(".wifi-forget-confirm");
    await expect.poll(() => seen.forget.length).toBe(1);
    expect(seen.forget[0]).toEqual({ ssid: "Old-Cafe" });
    await expect(page.locator("#wifi-action-status")).toContainText(
      "Removed saved network Old-Cafe",
    );
    assertCleanConsole(probe);
  });

  test("wifi networks — reorder saved networks", async ({ page, probe }) => {
    const seen = { connect: [] as unknown[], forget: [] as unknown[], priority: [] as unknown[] };
    await routeWifiProbes(page);
    await routeWifiMutations(page, seen);
    await gotoDashboard(page);
    await page.locator("#wifi-networks-section").evaluate((el) => el.setAttribute("open", ""));
    const trezRow = page.locator('.wifi-saved-row[data-ssid="Trez"]');
    // Trez is first (highest priority) so its move-up is disabled; every saved
    // row is reorderable in the redesign.
    await expect(trezRow.locator(".wifi-move-up")).toBeDisabled();
    const saveBtn = page.locator("#wifi-order-save");
    await expect(saveBtn).toBeDisabled();
    await page.click('.wifi-saved-row[data-ssid="Old-Cafe"] .wifi-move-down');
    await expect(saveBtn).toBeEnabled();
    await saveBtn.click();
    await expect.poll(() => seen.priority.length).toBe(1);
    expect(seen.priority[0]).toEqual({ order: ["Trez", "Garage", "Old-Cafe"] });
    assertCleanConsole(probe);
  });

  test("wifi networks — active protected row has no mutation controls", async ({
    page,
    probe,
  }) => {
    await routeWifiProbes(page);
    await gotoDashboard(page);
    await page.locator("#wifi-networks-section").evaluate((el) => el.setAttribute("open", ""));
    const trezRow = page.locator('.wifi-saved-row[data-ssid="Trez"]');
    await expect(trezRow).toBeVisible();
    await expect(trezRow.locator(".wifi-forget-btn")).toHaveCount(0);
    assertCleanConsole(probe);
  });

  test("access point — change mode forwards POST", async ({ page, probe }) => {
    const seen = { mode: [] as unknown[], config: [] as unknown[] };
    const ap = { ...WIFI_AP_FIXTURE.ap };
    await routeWifiProbes(page, ap);
    await routeApMutations(page, seen, ap);
    await gotoDashboard(page);
    await page
      .locator("#access-point-section")
      .evaluate((el) => el.setAttribute("open", ""));
    await page.selectOption("#ap-mode-select", "force_on");
    await expect.poll(() => seen.mode.length).toBe(1);
    expect(seen.mode[0]).toEqual({ mode: "force_on" });
    await expect(page.locator("#ap-action-status")).toContainText(
      "Access point updated",
    );
    assertCleanConsole(probe);
  });

  test("access point — Off shows lockout warning", async ({ page, probe }) => {
    const seen = { mode: [] as unknown[], config: [] as unknown[] };
    const ap = { ...WIFI_AP_FIXTURE.ap };
    await routeWifiProbes(page, ap);
    await routeApMutations(page, seen, ap);
    await gotoDashboard(page);
    await page
      .locator("#access-point-section")
      .evaluate((el) => el.setAttribute("open", ""));
    await page.selectOption("#ap-mode-select", "force_off");
    await expect.poll(() => seen.mode.length).toBe(1);
    await expect(page.locator("#ap-lockout-warning")).toBeVisible();
    assertCleanConsole(probe);
  });

  test("access point — save config forwards write-only body", async ({
    page,
    probe,
  }) => {
    const seen = { mode: [] as unknown[], config: [] as unknown[] };
    const ap = { ...WIFI_AP_FIXTURE.ap };
    await routeWifiProbes(page, ap);
    await routeApMutations(page, seen, ap);
    await gotoDashboard(page);
    await page
      .locator("#access-point-section")
      .evaluate((el) => el.setAttribute("open", ""));
    await page.click("#ap-edit-btn");
    await page.fill("#ap-ssid-input", "MyAP");
    await page.fill(".ap-pass-input", "supersecret1");
    await page.click(".ap-config-save");
    await expect.poll(() => seen.config.length).toBe(1);
    expect(seen.config[0]).toEqual({
      ssid: "MyAP",
      passphrase: "supersecret1",
    });
    assertCleanConsole(probe);
  });

  test("access point — status card renders SSID and Inactive state", async ({
    page,
    probe,
  }) => {
    // Default fixture: Auto mode, not broadcasting. The status card shows the
    // configured SSID + an "Inactive" pill and omits the client/IP line.
    await routeWifiProbes(page, { ...WIFI_AP_FIXTURE.ap });
    await gotoDashboard(page);
    await page
      .locator("#access-point-section")
      .evaluate((el) => el.setAttribute("open", ""));
    const status = page.locator("#ap-status");
    await expect(status).toContainText("TeslaUSB-Setup");
    await expect(status).toContainText("Inactive");
    await expect(status).not.toContainText("connected");
    assertCleanConsole(probe);
  });

  test("access point — active status card shows client count and IP", async ({
    page,
    probe,
  }) => {
    // When the AP is broadcasting, the card flips to an "Active" pill and adds
    // the connected-client count and the AP's IP address.
    await routeWifiProbes(page, {
      mode: "force_on",
      active: true,
      ssid: "TeslaUSB-Setup",
      client_count: 2,
      ip: "192.168.4.1",
    });
    await gotoDashboard(page);
    await page
      .locator("#access-point-section")
      .evaluate((el) => el.setAttribute("open", ""));
    const status = page.locator("#ap-status");
    await expect(status).toContainText("TeslaUSB-Setup");
    await expect(status).toContainText("Active");
    await expect(status).toContainText("2 connected");
    await expect(status).toContainText("192.168.4.1");
    assertCleanConsole(probe);
  });

  test("system health — indexer error dot and composed reason/catalog text", async ({
    page,
  }) => {
    const json = (body: unknown) => ({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    await page.route("**/api/system/health", (r) =>
      r.fulfill(
        json({
          ...HEALTH_FIXTURE,
          subsystems: {
            ...HEALTH_FIXTURE.subsystems,
            indexer: { severity: "error", message: "Indexer not running" },
          },
        }),
      ),
    );
    await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
    await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_FIXTURE)));
    await gotoDashboard(page);
    const indexerLabel = page.locator("#system-health-rows > div", {
      hasText: "Video Indexer",
    });
    await expect(
      indexerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='error']"),
    ).toBeVisible();
    await expect(indexerLabel.locator("xpath=following-sibling::div[1]")).toHaveText(
      /^Indexer not running — \d+ clips indexed; newest is \d+ d old$/,
    );
  });

  test("system health — indexer warn dot composes reason with catalog text", async ({
    page,
  }) => {
    const json = (body: unknown) => ({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    await page.route("**/api/system/health", (r) =>
      r.fulfill(
        json({
          ...HEALTH_FIXTURE,
          subsystems: {
            ...HEALTH_FIXTURE.subsystems,
            indexer: { severity: "warn", message: "Indexer stalled" },
          },
        }),
      ),
    );
    await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
    await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_FIXTURE)));
    await gotoDashboard(page);
    const indexerLabel = page.locator("#system-health-rows > div", {
      hasText: "Video Indexer",
    });
    await expect(
      indexerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='warn']"),
    ).toBeVisible();
    await expect(indexerLabel.locator("xpath=following-sibling::div[1]")).toHaveText(
      /^Indexer stalled — \d+ clips indexed; newest is \d+ d old$/,
    );
  });

  test("system health — webd indexer unknown falls back to catalog severity and text", async ({
    page,
  }) => {
    // When webd cannot read the indexd heartbeat (severity 'unknown'), the dot
    // must fall back to the client-derived catalog severity and the message must
    // be the plain catalog text — NOT composed with the 'unknown' reason.
    const json = (body: unknown) => ({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    await page.route("**/api/system/health", (r) =>
      r.fulfill(
        json({
          ...HEALTH_FIXTURE,
          subsystems: {
            ...HEALTH_FIXTURE.subsystems,
            indexer: { severity: "unknown", message: "Indexer status unavailable" },
          },
        }),
      ),
    );
    await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
    await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_FIXTURE)));
    await gotoDashboard(page);
    const indexerLabel = page.locator("#system-health-rows > div", {
      hasText: "Video Indexer",
    });
    // Dot reflects the healthy catalog (ok), not webd's 'unknown'.
    await expect(
      indexerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='ok']"),
    ).toBeVisible();
    const msg = indexerLabel.locator("xpath=following-sibling::div[1]");
    await expect(msg).toHaveText(/^\d+ clips indexed; newest is \d+ d old$/);
    await expect(msg).not.toContainText("Indexer status unavailable");
  });

  test("system health — missing webd indexer block falls back to catalog", async ({
    page,
  }) => {
    // webd omits the indexer subsystem entirely (e.g. staged deploy: webd
    // updated before indexd writes its first heartbeat). Row must still render
    // from catalog data, never blank or fabricated.
    const json = (body: unknown) => ({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    const { indexer: _omit, ...withoutIndexer } = HEALTH_FIXTURE.subsystems as Record<
      string,
      unknown
    >;
    await page.route("**/api/system/health", (r) =>
      r.fulfill(json({ ...HEALTH_FIXTURE, subsystems: withoutIndexer })),
    );
    await page.route("**/api/system/metrics", (r) => r.fulfill(json(METRICS_FIXTURE)));
    await page.route("**/api/storage/health", (r) => r.fulfill(json(STORAGE_FIXTURE)));
    await gotoDashboard(page);
    const indexerLabel = page.locator("#system-health-rows > div", {
      hasText: "Video Indexer",
    });
    await expect(
      indexerLabel.locator("xpath=preceding-sibling::div[1]//span[@aria-label='ok']"),
    ).toBeVisible();
    await expect(indexerLabel.locator("xpath=following-sibling::div[1]")).toHaveText(
      /^\d+ clips indexed; newest is \d+ d old$/,
    );
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

  // ── Gate 3: mapping settings form persists via /api/settings PUT ─────────
  test("mapping settings form saves changed values", async ({
    page,
    probe,
  }) => {
    const savedPuts: { key?: string; value?: string }[] = [];
    await page.route("**/api/settings", async (route) => {
      const req = route.request();
      if (req.method() !== "PUT") {
        await route.continue();
        return;
      }
      const body = JSON.parse(req.postData() || "{}") as { key?: string; value?: string };
      savedPuts.push(body);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ key: body.key, value: body.value }),
      });
    });

    const origin = new URL(loadState().baseURL).origin;
    await gotoDashboard(page);
    await page.waitForLoadState("networkidle");
    const allowedApi = new Set([...ALLOWED_API, "/api/wifi/saved", "/api/wifi/ap"]);

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
        const method = req.method.toUpperCase();
        if (u.pathname === "/api/settings" && method === "PUT") continue;
        expect(method, `${req.method} ${u.pathname}`).toBe("GET");
        expect(allowedApi.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
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
    expect(apiSeen.has("/api/fsck/status"), "/api/fsck/status was never requested").toBe(true);
    expect(apiSeen.has("/api/fsck/history"), "/api/fsck/history was never requested").toBe(true);
    expect(apiSeen.has("/api/fsck/last-check/1"), "/api/fsck/last-check/1 missing").toBe(true);
    expect(apiSeen.has("/api/fsck/last-check/2"), "/api/fsck/last-check/2 missing").toBe(true);
    expect(apiSeen.has("/api/fsck/last-check/3"), "/api/fsck/last-check/3 missing").toBe(true);
    expect(
      apiSeen.has("/api/gadget/mode-status"),
      "/api/gadget/mode-status was never requested",
    ).toBe(true);
    expect(apiSeen.has("/api/wifi/status"), "/api/wifi/status was never requested").toBe(true);
    expect(
      apiSeen.has("/api/wifi/networks"),
      "/api/wifi/networks was never requested",
    ).toBe(true);
    expect(apiSeen.has("/api/wifi/saved"), "/api/wifi/saved was never requested").toBe(true);
    expect(apiSeen.has("/api/wifi/ap"), "/api/wifi/ap was never requested").toBe(true);

    // Exercise Mapping & Indexing save path and assert exactly the changed keys
    // are sent to /api/settings.
    await page.locator("details.settings-section").evaluateAll((els) =>
      els.forEach((d) => d.setAttribute("open", "")),
    );

    const urlBefore = page.url();
    await page.fill("input[name=trip_gap_minutes]", "12");
    await page.selectOption("#mapping-speed-units", "mph");
    await page.click("button:has-text('Save Mapping Settings')");
    await page.waitForLoadState("networkidle");
    await expect.poll(() => savedPuts.length).toBe(2);
    expect(page.url(), "pressing Enter in a field must not navigate").toBe(urlBefore);
    expect(savedPuts).toEqual(
      expect.arrayContaining([
        { key: "trip_gap_minutes", value: "12" },
        { key: "speed_unit", value: "mph" },
      ]),
    );

    // Re-assert whitelist after the mutation exercise — catches any stray path.
    assertOnlyWhitelistedApi();
    const mutating = probe.requests.filter((r) =>
      ["POST", "PUT", "PATCH", "DELETE"].includes(r.method.toUpperCase()),
    );
    expect(
      mutating.filter((r) => new URL(r.url).pathname !== "/api/settings"),
      `unexpected mutating request(s): ${JSON.stringify(mutating)}`,
    ).toEqual([]);
  });

  // ── Gate 3 (console + network): zero warnings/errors/pageerror, no failures ─
  test("clean — zero console warnings/errors/pageerror and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    const origin = new URL(loadState().baseURL).origin;
    // /api/wifi/ap is mocked "present" by beforeEach (routeApStatus) — wifid is
    // not in the harness, so the real read would 503 and the browser would log a
    // resource error; the mock keeps this gate testing the app, not the absent
    // daemon.
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
    await page.unroute("**/api/gadget/mode-status");
    await page.route("**/api/gadget/status", (r) =>
      r.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "gadgetd_unavailable", message: "gadgetd is not reachable" },
        }),
      }),
    );
    await page.route("**/api/gadget/mode-status", (r) =>
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
      (e) =>
        e.text.includes("503") &&
        (e.location.includes("/api/gadget/status") ||
          e.location.includes("/api/gadget/mode-status")),
    );
    expect(
      expected503.length,
      "expected at least one 503 resource-load log",
    ).toBeGreaterThanOrEqual(1);
    const other = probe.consoleErrors.filter(
      (e) =>
        !(
          e.text.includes("503") &&
          (e.location.includes("/api/gadget/status") ||
            e.location.includes("/api/gadget/mode-status"))
        ),
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
