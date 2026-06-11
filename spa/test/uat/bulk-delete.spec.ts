import { test, expect } from "./helpers";
import type { Page, Route } from "@playwright/test";
import { gotoScreen, assertCleanConsole } from "./screen-helpers";

// Bulk-delete UAT (A2). Drives the REAL bundle webd serves, but the seed
// catalog has no toybox media, so each list is empty and the bulk affordances
// never appear on the live data. To exercise the multi-select + single-handoff
// bulk-delete flow we MOCK the category GET (to inject items) and the
// POST /api/<cat>/bulk-delete handoff (gadgetd is not running in the harness),
// exactly like the existing clip-delete / chimes mutation specs.

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

function items(dir: string, names: string[]) {
  return {
    items: names.map((n, i) => ({
      name: n,
      rel_path: `${dir}/${n}`,
      size_bytes: 1024 * (i + 1),
    })),
  };
}

/**
 * Mock the list GET for `apiPath` with `names`, and the bulk-delete POST to
 * capture the request body. Returns a getter for the captured `names` array so
 * a test can assert the single-handoff payload.
 */
async function mockCategory(
  page: Page,
  apiPath: string,
  dir: string,
  names: string[],
) {
  let captured: string[] | null = null;
  await page.route(`**/api/${apiPath}`, (r: Route) => {
    if (r.request().method() === "POST") {
      // never hit (POST goes to /bulk-delete) — guard anyway.
      return r.fulfill(json({ handoff_id: "h", state: "done" }));
    }
    return r.fulfill(json(items(dir, names)));
  });
  await page.route(`**/api/${apiPath}/bulk-delete`, (r: Route) => {
    const body = r.request().postDataJSON() as { names?: string[] };
    captured = body?.names ?? [];
    return r.fulfill(json({ handoff_id: "bulk-1", state: "done" }));
  });
  return () => captured;
}

test.describe("media bulk-delete UAT (A2)", () => {
  test("boombox — select-all, delete selected, one handoff carries all names", async ({
    page,
  }) => {
    const captured = await mockCategory(page, "boombox", "Boombox", [
      "horn.wav",
      "airhorn.mp3",
      "klaxon.wav",
    ]);
    await gotoScreen(page, "/boombox", "boombox");

    // List rendered (not the empty state).
    await expect(page.locator("[data-testid=boombox-list]")).toBeVisible();
    const bar = page.locator("[data-testid=bulk-bar]");
    await expect(bar).toBeVisible();

    // Delete-selected starts disabled (nothing selected).
    const delBtn = page.locator("[data-testid=bulk-delete-btn]");
    await expect(delBtn).toBeDisabled();

    // Select all via the toolbar checkbox.
    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await expect(page.locator(".bulk-select-all")).toContainText("3 selected");
    await expect(delBtn).toBeEnabled();
    await expect(delBtn).toContainText("(3)");

    // Confirm dialog, then confirm the delete.
    await delBtn.click();
    const confirm = page.locator("[data-testid=bulk-confirm]");
    await expect(confirm).toBeVisible();
    await expect(confirm).toContainText("Remove 3");
    await page.locator("[data-testid=bulk-confirm-btn]").click();

    // Success notice; the POST carried all three names in one request.
    await expect(page.locator("[role=status]")).toContainText("Removed 3 items");
    expect(captured()).toEqual(["horn.wav", "airhorn.mp3", "klaxon.wav"]);
  });

  test("boombox queued — 202 queued bulk-delete shows the syncing notice", async ({
    page,
  }) => {
    // Mock the list, then a bulk-delete that gadgetd accepts into its durable
    // queue (202 queued) because the car is connected. The UI must treat this
    // as success and communicate the syncing state, never an error.
    await page.route("**/api/boombox", (r: Route) =>
      r.fulfill(json(items("Boombox", ["a.wav", "b.wav"]))),
    );
    await page.route("**/api/boombox/bulk-delete", (r: Route) =>
      r.fulfill(json({ state: "queued", job_id: "m-bulk" }, 202)),
    );
    await gotoScreen(page, "/boombox", "boombox");
    await expect(page.locator("[data-testid=boombox-list]")).toBeVisible();

    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await page.locator("[data-testid=bulk-delete-btn]").click();
    await page.locator("[data-testid=bulk-confirm-btn]").click();

    await expect(page.locator("[role=status]")).toContainText("syncing to the car");
  });

  test("boombox — deselecting trims the batch to the chosen names", async ({
    page,
  }) => {
    const captured = await mockCategory(page, "boombox", "Boombox", [
      "a.wav",
      "b.wav",
      "c.wav",
    ]);
    await gotoScreen(page, "/boombox", "boombox");
    await expect(page.locator("[data-testid=boombox-list]")).toBeVisible();

    // Select only the first and third rows.
    const rowChecks = page.locator("[data-testid=boombox-list] .bulk-row-check");
    await rowChecks.nth(0).check();
    await rowChecks.nth(2).check();
    await expect(page.locator(".bulk-select-all")).toContainText("2 selected");

    await page.locator("[data-testid=bulk-delete-btn]").click();
    await page.locator("[data-testid=bulk-confirm-btn]").click();

    await expect(page.locator("[role=status]")).toContainText("Removed 2 items");
    expect(captured()).toEqual(["a.wav", "c.wav"]);
  });

  test("wraps — image table also exposes the bulk bar and single-handoff delete", async ({
    page,
  }) => {
    const captured = await mockCategory(page, "wraps", "LightShow/wraps", [
      "matte_black.png",
      "chrome.png",
    ]);
    await gotoScreen(page, "/wraps", "wraps");

    await expect(page.locator(".wraps-table")).toBeVisible();
    await expect(page.locator("[data-testid=bulk-bar]")).toBeVisible();

    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await page.locator("[data-testid=bulk-delete-btn]").click();
    await page.locator("[data-testid=bulk-confirm-btn]").click();

    await expect(page.locator("[role=status]")).toContainText("Removed 2 items");
    expect(captured()).toEqual(["matte_black.png", "chrome.png"]);
  });

  test("boombox — cancel keeps the selection and fires no handoff", async ({
    page,
    probe,
  }) => {
    await mockCategory(page, "boombox", "Boombox", ["a.wav", "b.wav"]);
    await gotoScreen(page, "/boombox", "boombox");
    await expect(page.locator("[data-testid=boombox-list]")).toBeVisible();

    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await page.locator("[data-testid=bulk-delete-btn]").click();
    await expect(page.locator("[data-testid=bulk-confirm]")).toBeVisible();
    await page.locator("[data-testid=bulk-confirm]")
      .getByRole("button", { name: "Cancel" })
      .click();

    await expect(page.locator("[data-testid=bulk-confirm]")).toHaveCount(0);
    // Selection survives a cancel.
    await expect(page.locator(".bulk-select-all")).toContainText("2 selected");
    const posts = probe.requests.filter(
      (r) => r.method.toUpperCase() === "POST",
    );
    expect(posts, `unexpected POST(s): ${JSON.stringify(posts)}`).toEqual([]);
  });

  test("boombox — a failed handoff surfaces an error and keeps the dialog open", async ({
    page,
  }) => {
    await page.route("**/api/boombox", (r: Route) =>
      r.fulfill(json(items("Boombox", ["a.wav", "b.wav"]))),
    );
    await page.route("**/api/boombox/bulk-delete", (r: Route) =>
      r.fulfill(
        json({ error: { code: "vehicle_busy", message: "Saving a clip." } }, 409),
      ),
    );
    await gotoScreen(page, "/boombox", "boombox");
    await expect(page.locator("[data-testid=boombox-list]")).toBeVisible();

    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await page.locator("[data-testid=bulk-delete-btn]").click();
    await page.locator("[data-testid=bulk-confirm-btn]").click();

    const confirm = page.locator("[data-testid=bulk-confirm]");
    await expect(confirm).toBeVisible();
    await expect(confirm.locator("[role=alert]")).toContainText("Saving a clip.");
  });

  test("clean — bulk flow produces no console warnings/errors", async ({
    page,
    probe,
  }) => {
    await mockCategory(page, "boombox", "Boombox", ["a.wav"]);
    await gotoScreen(page, "/boombox", "boombox");
    await expect(page.locator("[data-testid=bulk-bar]")).toBeVisible();
    await page.locator(".bulk-select-all input[type=checkbox]").check();
    await page.locator("[data-testid=bulk-delete-btn]").click();
    await page.locator("[data-testid=bulk-confirm-btn]").click();
    await expect(page.locator("[role=status]")).toContainText("Removed");
    assertCleanConsole(probe);
  });
});
