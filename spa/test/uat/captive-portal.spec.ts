import { test, expect } from "./helpers";
import type { Page, TestInfo } from "@playwright/test";
import {
  gotoScreen,
  assertCleanConsole,
  assertCleanNetwork,
  assertReadOnly,
  assertWiring,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

// Captive Portal / Wi-Fi setup UAT — v1 parity, strictly read-only. Drives the
// REAL bundle webd serves at /captive-portal. Parity target: legacy
// captive_portal.html (status card + available/saved networks + manual form).
// B-1 exposes read-only wifid status, discovered-network, and saved-profile
// endpoints. Mutating controls remain inert until operator-gated workflows land.

const PATH = "/captive-portal";
const SCREEN = "captive-portal";

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

async function routeWifiReads(page: Page) {
  await page.route("**/api/wifi/status", (route) =>
    route.fulfill(
      json({
        connected: false,
        ssid: null,
        signal: null,
        security: null,
        ip: null,
        iface: null,
      }),
    ),
  );
  await page.route("**/api/wifi/networks", (route) =>
    route.fulfill(json({ networks: [] })),
  );
  await page.route("**/api/wifi/saved", (route) =>
    route.fulfill(json({ networks: [] })),
  );
}

/** App-shell chrome: brand present + the SETTINGS nav entry active. */
async function assertChrome(page: Page, testInfo: TestInfo) {
  await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
  const isMobile = testInfo.project.name.includes("375");
  const activeNav = page.locator(
    isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
  );
  await expect(activeNav).toBeVisible();
  await expect(activeNav).toHaveAttribute("aria-current", "page");
  await expect(activeNav).toContainText("Settings");
}

test.describe("captive-portal UAT", () => {
  test("parity — settings nav active, hero/status/networks/manual", async ({
    page,
  }, testInfo) => {
    await routeWifiReads(page);
    await gotoScreen(page, PATH, SCREEN);
    await assertChrome(page, testInfo);

    await expect(page.locator(".captive-title")).toHaveText(
      "Connect TeslaUSB to Wi-Fi",
    );
    // Honest read-only banner (live reads, no mutations).
    await expect(page.locator("[data-testid=captive-banner]")).toContainText(
      "operator-gated",
    );
    // Status degrades to an honest "not connected / offline" state.
    await expect(page.locator(".captive-status-chip.is-offline")).toContainText(
      "Not connected",
    );
    // Network lists render their empty-states from the live read APIs.
    await expect(
      page.locator("[data-testid=captive-networks-empty]"),
    ).toBeVisible();
    await expect(
      page.locator("[data-testid=captive-saved-empty]"),
    ).toBeVisible();
    // The manual form's inputs and buttons are inert.
    await expect(page.locator("#manual-ssid")).toBeDisabled();
    await expect(page.locator("#manual-passphrase")).toBeDisabled();
    const buttons = page.locator(".captive-page button");
    await expect(buttons.first()).toBeDisabled();
  });

  test("wiring — served HTML runs the built bundle and the module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only — reads status and profiles, no mutations", async ({
    page,
    probe,
  }) => {
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await routeWifiReads(page);
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    await assertReadOnly(
      page,
      probe,
      sockets,
      new Set(["/api/wifi/status", "/api/wifi/networks", "/api/wifi/saved"]),
    );
    for (const path of ["/api/wifi/status", "/api/wifi/networks", "/api/wifi/saved"]) {
      expect(
        probe.requests.filter((request) => new URL(request.url).pathname === path),
        `${path} should be fetched once`,
      ).toHaveLength(1);
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
