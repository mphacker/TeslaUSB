import { test, expect, loadState } from "./helpers";
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

// Cloud Archive UAT — v1 parity with live provider credentials. Drives the REAL
// bundle webd serves at /cloud. Parity target: legacy cloud_archive.html (sync
// status + stat cards + provider setup + sync settings + queue + history). In
// B-1 only the provider credential lane is live (`GET/POST/DELETE
// /api/cloud/credentials`); all other sections stay inert scaffolding.

const PATH = "/cloud";
const SCREEN = "cloud-archive";

function json(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

async function routeCloudGet(page: Page, state: unknown) {
  await page.route("**/api/cloud/credentials", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill(json(state));
  });
}

/** App-shell chrome: brand present + the CLOUD nav entry active. */
async function assertChrome(page: Page, testInfo: TestInfo) {
  await expect(page.locator(".top-bar .top-bar-title")).toHaveText("TeslaUSB");
  const isMobile = testInfo.project.name.includes("375");
  const activeNav = page.locator(
    isMobile ? ".bottom-tabs .tab-item.active" : ".sidebar-rail .nav-item.active",
  );
  await expect(activeNav).toBeVisible();
  await expect(activeNav).toHaveAttribute("aria-current", "page");
  await expect(activeNav).toContainText("Cloud");
}

test.describe("cloud-archive UAT", () => {
  test.beforeEach(async ({ page }) => {
    await routeCloudGet(page, {
      state: "not_configured",
      provider: null,
      updated_at: null,
    });
  });

  test("parity — cloud nav active, status/provider/settings/queue/history", async ({
    page,
  }, testInfo) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertChrome(page, testInfo);

    // Idle sync status banner.
    await expect(page.locator("#syncStatusCard")).toContainText("Cloud Sync");
    await expect(
      page.locator("[data-testid=cloud-sync-subtitle]"),
    ).toContainText("Configure a provider below");
    // The v1 section scaffolding is present.
    await expect(page.locator(".settings-section summary")).toHaveCount(4);
    await expect(
      page.locator(".settings-section summary").filter({ hasText: "Cloud Provider" }),
    ).toHaveCount(1);
    await expect(
      page.locator(".settings-section summary").filter({ hasText: "Sync Settings" }),
    ).toHaveCount(1);
    await expect(page.locator(".settings-section .info-box").last()).toContainText(
      "How sync works",
    );
    // Provider control is now active; the rest of the screen remains inert.
    await expect(page.locator("#providerSelect")).toBeEnabled();
    // Queue + history render honest pending/empty states.
    await expect(page.locator("[data-testid=cloud-queue-empty]")).toBeAttached();
    await expect(
      page.locator("[data-testid=cloud-history-empty]"),
    ).toContainText("No sync sessions yet");
  });

  test("wiring — served HTML runs the built bundle and the module ran", async ({
    page,
  }) => {
    await gotoScreen(page, PATH, SCREEN);
    await assertWiring(page, PATH, SCREEN);
  });

  test("read-only — mount issues one credentials GET and no mutations", async ({
    page,
    probe,
  }) => {
    const sockets: string[] = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));
    await gotoScreen(page, PATH, SCREEN);
    await page.waitForTimeout(200);
    // This screen is no longer strictly read-only — it can save credentials —
    // but *mounting* it must still mutate nothing. Allow only the mount-time
    // credentials GET on top of the shell polls, and keep every other
    // read-only invariant (no forms, no submit buttons, no file inputs).
    await assertReadOnly(page, probe, sockets, new Set(["/api/cloud/credentials"]));
    const cloudGets = probe.requests.filter(
      (r) => new URL(r.url).pathname === "/api/cloud/credentials",
    );
    expect(
      cloudGets.length,
      "cloud credentials should be fetched exactly once on mount",
    ).toBe(1);
  });

  test("credentials status — not_configured shows empty state", async ({ page }) => {
    await page.unroute("**/api/cloud/credentials");
    await routeCloudGet(page, {
      state: "not_configured",
      provider: null,
      updated_at: null,
    });
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("#cloudCredStatus")).toContainText(
      "No cloud provider configured.",
    );
    await expect(page.locator("#cloudRemoveBtn")).toHaveCount(0);
  });

  test("credentials status — configured shows provider and remove", async ({ page }) => {
    await page.unroute("**/api/cloud/credentials");
    await routeCloudGet(page, {
      state: "configured",
      provider: "onedrive",
      updated_at: 1730419200,
    });
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("#cloudCredStatus")).toContainText(
      "Connected to OneDrive",
    );
    await expect(page.locator("#cloudRemoveBtn")).toHaveCount(1);
  });

  test("credentials status — unreadable explains re-paste flow", async ({ page }) => {
    await page.unroute("**/api/cloud/credentials");
    await routeCloudGet(page, {
      state: "unreadable",
      provider: null,
      updated_at: null,
    });
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("#cloudCredStatus")).toContainText(
      "cannot be decrypted on this hardware",
    );
  });

  // Every other credentials test mocks the API. This one deliberately does NOT.
  // It is the only test that proves the SPA and webd actually agree on the path,
  // method and DTO shape — a field rename (state/provider/updated_at) or a route
  // that was never registered passes all the mocked tests and fails only here.
  test("live webd — the real credentials endpoint drives the empty state", async ({
    page,
  }) => {
    await page.unroute("**/api/cloud/credentials");
    await gotoScreen(page, PATH, SCREEN);
    await expect(page.locator("#cloudCredStatus")).toContainText(
      "No cloud provider configured.",
    );
    const live = await page.request.get(
      `${loadState().baseURL}/api/cloud/credentials`,
    );
    expect(live.status()).toBe(200);
    expect(await live.json()).toEqual({
      state: "not_configured",
      provider: null,
      updated_at: null,
    });
  });

  test("save credentials — posts provider/token once, clears token, never re-renders token", async ({
    page,
  }) => {
    await page.unroute("**/api/cloud/credentials");
    const savedBodies: Array<{ provider?: string; token?: string }> = [];
    await page.route("**/api/cloud/credentials", async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        await route.fulfill(
          json({ state: "not_configured", provider: null, updated_at: null }),
        );
        return;
      }
      if (req.method() === "POST") {
        savedBodies.push(req.postDataJSON() as { provider?: string; token?: string });
        await route.fulfill(
          json({
            state: "configured",
            provider: "onedrive",
            updated_at: 1730419200,
          }),
        );
        return;
      }
      await route.continue();
    });

    const token =
      "Paste the following into your remote machine --->\n{\"access_token\":\"super-secret-token\"}\n<---End paste";
    await gotoScreen(page, PATH, SCREEN);
    await page.selectOption("#providerSelect", "onedrive");
    await page.fill("#cloudTokenInput", token);
    await page.click("#cloudSaveBtn");
    await expect.poll(() => savedBodies.length).toBe(1);
    expect(savedBodies[0]).toEqual({ provider: "onedrive", token });
    await expect(page.locator("#cloudTokenInput")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText(token);
  });

  test("save credentials — invalid_token error surfaces API message", async ({
    page,
  }) => {
    await page.unroute("**/api/cloud/credentials");
    await page.route("**/api/cloud/credentials", async (route) => {
      const req = route.request();
      if (req.method() === "GET") {
        await route.fulfill(
          json({ state: "not_configured", provider: null, updated_at: null }),
        );
        return;
      }
      if (req.method() === "POST") {
        await route.fulfill(
          json(
            {
              error: {
                code: "invalid_token",
                message: "token is not valid JSON from `rclone authorize`",
              },
            },
            400,
          ),
        );
        return;
      }
      await route.continue();
    });
    await gotoScreen(page, PATH, SCREEN);
    await page.fill("#cloudTokenInput", "bad-token");
    await page.click("#cloudSaveBtn");
    await expect(page.locator("#cloudCredStatus")).toContainText(
      "token is not valid JSON from `rclone authorize`",
    );
    await expect(page.locator("#cloudCredStatus")).toHaveAttribute(
      "style",
      /accent-error/,
    );
  });

  test("clean — zero console warnings/errors and no failed/non-2xx requests", async ({
    page,
    probe,
  }) => {
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
    await expect(page.locator("#syncStatusCard")).toBeVisible();
    await captureScreenshot(page, testInfo, SCREEN);
  });
});
