import { test, expect, loadState } from "./helpers";
import type { Page } from "@playwright/test";
import {
  SHELL_POLL_ALLOWLIST,
  assertCleanConsole,
  assertCleanNetwork,
  assertWiring,
  capturePerf,
  captureScreenshot,
  gotoScreen,
} from "./screen-helpers";

// Captive Portal / Wi-Fi setup UAT — parity UI that uses existing typed webd
// routes for scan/connect/forget/priority/AP controls. Mutations must remain
// same-origin and operator-confirmed.

const PATH = "/captive-portal";
const SCREEN = "captive-portal";
const ALLOWED_READ_API = new Set([
  "/api/wifi/status",
  "/api/wifi/networks",
  "/api/wifi/saved",
  "/api/wifi/ap",
]);

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

const READ_FIXTURE = {
  status: {
    connected: true,
    ssid: "Trez",
    signal: 72,
    security: "WPA2",
    ip: "192.168.1.50",
    iface: "wlan0",
  },
  networks: {
    networks: [
      {
        ssid: "Trez",
        signal: 72,
        security: "WPA2",
        saved: true,
        active: true,
        protected: true,
      },
      {
        ssid: "Office",
        signal: 63,
        security: "WPA2",
        saved: true,
        active: false,
        protected: true,
      },
      {
        ssid: "Guest",
        signal: 48,
        security: "",
        saved: false,
        active: false,
        protected: false,
      },
    ],
  },
  saved: {
    networks: [
      { ssid: "Trez", priority: 100, autoconnect: true, active: true },
      { ssid: "Office", priority: 90, autoconnect: true, active: false },
      { ssid: "Guest", priority: 10, autoconnect: true, active: false },
    ],
  },
  ap: {
    ap: {
      mode: "auto",
      active: true,
      ssid: "TeslaUSB-Setup",
      client_count: 1,
      ip: "192.168.92.1",
    },
  },
};

async function routeWifiReads(
  page: Page,
  fixture = READ_FIXTURE,
  statusCode = 200,
) {
  await page.route("**/api/wifi/status", (route) =>
    route.fulfill(json(fixture.status, statusCode)),
  );
  await page.route("**/api/wifi/networks", (route) =>
    route.fulfill(json(fixture.networks, statusCode)),
  );
  await page.route("**/api/wifi/saved", (route) =>
    route.fulfill(json(fixture.saved, statusCode)),
  );
  await page.route("**/api/wifi/ap", (route) =>
    route.fulfill(json(fixture.ap, statusCode)),
  );
}

async function routeWifiMutations(page: Page, sink: {
  scan: unknown[];
  connect: unknown[];
  forget: unknown[];
  select: unknown[];
  priority: unknown[];
  apMode: unknown[];
  apConfig: unknown[];
}) {
  await page.route("**/api/wifi/scan", async (route) => {
    sink.scan.push(true);
    await route.fulfill(json(READ_FIXTURE.networks));
  });
  await page.route("**/api/wifi/connect", async (route) => {
    const body = route.request().postDataJSON();
    sink.connect.push(body);
    await route.fulfill(
      json({
        connected: true,
        ssid: (body as { ssid?: string }).ssid ?? "unknown",
        ip: "192.168.1.99",
        autoconnect: true,
      }),
    );
  });
  await page.route("**/api/wifi/forget", async (route) => {
    sink.forget.push(route.request().postDataJSON());
    await route.fulfill(json({ forgotten: true, count: 1 }));
  });
  await page.route("**/api/wifi/select", async (route) => {
    const body = route.request().postDataJSON();
    sink.select.push(body);
    await route.fulfill(
      json({
        connected: true,
        ssid: (body as { ssid?: string }).ssid ?? "unknown",
        ip: "192.168.1.77",
      }),
    );
  });
  await page.route("**/api/wifi/priority", async (route) => {
    sink.priority.push(route.request().postDataJSON());
    await route.fulfill(json({ ok: true, count: 3 }));
  });
  await page.route("**/api/wifi/ap/mode", async (route) => {
    sink.apMode.push(route.request().postDataJSON());
    await route.fulfill(json({ ok: true }));
  });
  await page.route("**/api/wifi/ap/config", async (route) => {
    sink.apConfig.push(route.request().postDataJSON());
    await route.fulfill(json({ ok: true }));
  });
}

test.describe("captive-portal UAT", () => {
  test("parity — hero, status, manual form, saved list, AP panel", async ({
    page,
  }, testInfo) => {
    await routeWifiReads(page);
    await gotoScreen(page, PATH, SCREEN);

    await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
    const isMobile = testInfo.project.name.includes("375");
    const activeNav = page.locator(
      isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
    );
    await expect(activeNav).toContainText("Settings");

    await expect(page.locator(".captive-title")).toHaveText("Connect TeslaUSB to Wi-Fi");
    await expect(page.locator("[data-testid=captive-banner]")).toContainText(
      "Operator-confirmed actions",
    );
    await expect(page.locator(".captive-status-chip.is-online")).toContainText("Connected");
    await expect(page.locator("[data-testid=captive-networks-list] .captive-network-item")).toHaveCount(3);
    await expect(page.locator("[data-testid=captive-saved-list] .captive-saved-item")).toHaveCount(3);
    await expect(page.locator("[data-testid=captive-ap-status]")).toContainText("TeslaUSB-Setup");
    await expect(page.locator('[data-testid="captive-ap-mode"] option[value="force_off"]')).toHaveCount(0);
    await expect(page.locator("[data-testid=captive-manual-ssid]")).toBeEnabled();
    await expect(page.locator("[data-testid=captive-manual-psk]")).toBeEnabled();
    await expect(page.locator("[data-testid=captive-connect-manual]")).toBeDisabled();
  });

  test("wiring — served HTML runs the built bundle and module", async ({ page }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("operator actions — confirmed mutation routes are called with typed payloads", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      const prompts: string[] = [];
      (window as unknown as { __captiveConfirmPrompts: string[] }).__captiveConfirmPrompts = prompts;
      window.confirm = (message?: string) => {
        prompts.push(String(message ?? ""));
        return true;
      };
    });

    await routeWifiReads(page);
    const sink = {
      scan: [] as unknown[],
      connect: [] as unknown[],
      forget: [] as unknown[],
      select: [] as unknown[],
      priority: [] as unknown[],
      apMode: [] as unknown[],
      apConfig: [] as unknown[],
    };
    await routeWifiMutations(page, sink);
    await gotoScreen(page, PATH, SCREEN);

    await page.click('[data-testid="captive-scan"]');
    await expect.poll(() => sink.scan.length).toBe(1);

    await page
      .locator('[data-testid="captive-networks-list"] .captive-network-item', { hasText: "Guest" })
      .locator('[data-testid="captive-use-ssid"]')
      .click();
    await page.fill('[data-testid="captive-manual-psk"]', "supersecret1");
    await page.click('[data-testid="captive-connect-manual"]');
    await expect.poll(() => sink.connect.length).toBe(1);
    expect(sink.connect[0]).toEqual({ ssid: "Guest", psk: "supersecret1" });

    await page
      .locator('[data-testid="captive-saved-list"] .captive-saved-item', { hasText: "Office" })
      .locator('[data-testid="captive-saved-connect"]')
      .click();
    await expect.poll(() => sink.select.length).toBe(1);
    expect(sink.select[0]).toEqual({ ssid: "Office" });

    await page
      .locator('[data-testid="captive-saved-list"] .captive-saved-item', { hasText: "Guest" })
      .locator('[data-testid="captive-saved-forget"]')
      .click();
    await expect.poll(() => sink.forget.length).toBe(1);
    expect(sink.forget[0]).toEqual({ ssid: "Guest" });

    await page.click('[data-testid="captive-priority-down"]');
    await page.click('[data-testid="captive-priority-save"]');
    await expect.poll(() => sink.priority.length).toBe(1);
    expect(sink.priority[0]).toEqual({ order: ["Office", "Trez", "Guest"] });

    await expect(page.locator('[data-testid="captive-ap-mode"]')).toBeEnabled();
    await expect.poll(async () => {
      await page.selectOption('[data-testid="captive-ap-mode"]', "force_on");
      return await page.locator('[data-testid="captive-ap-mode-save"]').isEnabled();
    }).toBe(true);
    await page.click('[data-testid="captive-ap-mode-save"]');
    await expect.poll(() => sink.apMode.length).toBe(1);
    expect(sink.apMode[0]).toEqual({ mode: "force_on" });

    await expect(page.locator('[data-testid="captive-ap-toggle-edit"]')).toBeEnabled();
    await page.click('[data-testid="captive-ap-toggle-edit"]');
    await page.fill('[data-testid="captive-ap-ssid"]', "Setup-Updated");
    await page.fill('[data-testid="captive-ap-pass"]', "anothersecret");
    await page.click('[data-testid="captive-ap-config-save"]');
    await expect.poll(() => sink.apConfig.length).toBe(1);
    expect(sink.apConfig[0]).toEqual({
      ssid: "Setup-Updated",
      passphrase: "anothersecret",
    });

    const prompts = await page.evaluate(
      () =>
        (window as unknown as { __captiveConfirmPrompts: string[] })
          .__captiveConfirmPrompts,
    );
    expect(prompts.length).toBeGreaterThanOrEqual(7);
    expect(prompts.some((msg) => msg.includes("Connect to Wi-Fi network"))).toBe(true);
    expect(prompts.some((msg) => msg.includes("Forget saved network"))).toBe(true);
    expect(prompts.some((msg) => msg.includes("Save setup AP credentials"))).toBe(true);
  });

  test("unavailable + recovery — handled load failure then retry success", async ({ page }) => {
    await routeWifiReads(
      page,
      {
        status: { error: { code: "unavailable", message: "wifi status unavailable" } },
        networks: { error: { code: "unavailable", message: "wifi networks unavailable" } },
        saved: { error: { code: "unavailable", message: "wifi saved unavailable" } },
        ap: { error: { code: "unavailable", message: "wifi ap unavailable" } },
      },
      503,
    );
    await gotoScreen(page, PATH, SCREEN);

    await expect(page.locator('[data-testid="captive-load-error"]')).toBeVisible();
    await expect(page.locator('[data-testid="captive-ap-unavailable"]')).toBeVisible();

    for (const p of ["/api/wifi/status", "/api/wifi/networks", "/api/wifi/saved", "/api/wifi/ap"]) {
      await page.unroute(`**${p}*`);
    }
    await routeWifiReads(page);
    await page.click('[data-testid="captive-retry-load"]');
    await expect(page.locator('[data-testid="captive-networks-list"] .captive-network-item')).toHaveCount(3);
    await expect(page.locator('[data-testid="captive-ap-status"]')).toBeVisible();
  });

  test("baseline load path — GET-only until operator clicks an action", async ({
    page,
    probe,
  }) => {
    await routeWifiReads(page);
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForLoadState("networkidle");

    const origin = new URL(loadState().baseURL).origin;
    const seen = new Set<string>();
    for (const req of probe.requests) {
      const u = new URL(req.url);
      expect(u.origin, `off-origin request to ${req.url}`).toBe(origin);
      if (!u.pathname.startsWith("/api/")) continue;
      if (SHELL_POLL_ALLOWLIST.has(u.pathname)) continue;
      expect(req.method.toUpperCase(), `${req.method} ${u.pathname}`).toBe("GET");
      expect(ALLOWED_READ_API.has(u.pathname), `unexpected API path ${u.pathname}`).toBe(true);
      seen.add(u.pathname);
    }
    for (const path of ALLOWED_READ_API) {
      expect(seen.has(path), `${path} should be fetched on load`).toBe(true);
    }
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
    await routeWifiReads(page);
    await gotoScreen(page, PATH, SCREEN);
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
    await expect(page.locator(".captive-page")).toBeVisible();
    await captureScreenshot(page, testInfo, SCREEN);
  });
});
