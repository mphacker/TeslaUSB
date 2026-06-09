import { test, expect, loadState, ARTIFACTS, type Probe } from "./helpers";
import type { Page, Route } from "@playwright/test";
import { resolve } from "node:path";

// ── Clip-delete UAT gate (webd `DELETE /api/clips/:id?target=car`, contract
//    §2.3 — the gadgetd eject-handoff). The destructive DELETE + its terminal
//    states are ALWAYS MOCKED here: page.route intercepts every DELETE to a clip
//    resource and fulfils a canned response, so no real delete ever reaches the
//    live webd/gadgetd. GETs (events/clip/stream) fall through to the real
//    seeded server, so the page loads genuine data and the player still streams.
//
// Progress model under test: "response is terminal". webd blocks on gadgetd and
// returns the terminal handoff state synchronously (200 {handoff_id, state:done}
// on success; a 4xx/5xx envelope otherwise). The UI shows an in-flight spinner
// during the request and renders the terminal result — no polling / SSE.
//
// What these gates prove (at BOTH viewports via the project matrix):
//  - DESTRUCTIVE GATING: clicking Delete opens a confirm dialog NAMING the clip;
//    no DELETE is issued until the user explicitly confirms; Cancel fires nothing.
//  - WIRE CONTRACT: the confirmed DELETE carries `?target=car` (never omitted).
//  - SUCCESS (200): the clip is removed from the player and a success notice shows.
//  - BUSY (409): a retryable message + a working Retry that re-issues the DELETE.
//  - VALIDATION (422): a terminal error, NO retry (Close only).
//  - DEVICE UNREACHABLE (503): a distinct, retryable message.
//  - Console/pageerror clean (modulo the browser's own "Failed to load resource"
//    line for the deliberately-mocked non-2xx — explicitly allow-listed below).

interface DeleteCall {
  url: string;
  target: string | null;
}

/** A scripted DELETE outcome (status + JSON body). */
interface MockReply {
  status: number;
  body: unknown;
}

/**
 * Intercept every DELETE to a clip resource and fulfil the reply produced by
 * `plan(callIndex)`. GETs (and any non-DELETE) fall through to the real server.
 * Returns the recorded DELETE calls so a test can assert count + `?target=car`.
 * Registered BEFORE navigation by every test ⇒ fail-closed (no real destructive
 * DELETE can escape to webd).
 */
async function mockClipDelete(
  page: Page,
  plan: (callIndex: number) => MockReply,
): Promise<DeleteCall[]> {
  const calls: DeleteCall[] = [];
  // Match the clip resource itself (/api/clips/123 [?query]) — NOT its
  // /stream or /export sub-resources, which must hit the network unchanged.
  await page.route(/\/api\/clips\/\d+(\?.*)?$/, async (route: Route) => {
    if (route.request().method() !== "DELETE") {
      await route.fallback();
      return;
    }
    const url = route.request().url();
    const reply = plan(calls.length);
    calls.push({ url, target: new URL(url).searchParams.get("target") });
    await route.fulfill({
      status: reply.status,
      contentType: "application/json",
      body: JSON.stringify(reply.body),
    });
  });
  return calls;
}

const okBody = { handoff_id: "h-uat-1", state: "done" };
const errBody = (code: string, message: string) => ({ error: { code, message } });

/** Navigate to the player and wait until a clip is loaded (Delete enabled). */
async function gotoPlayerWithClip(page: Page): Promise<number> {
  await page.goto("/events", { waitUntil: "load" });
  await expect(page.locator("[data-screen=event-player]")).toBeVisible();
  await expect(page.locator("#mainVideo")).toHaveAttribute(
    "src",
    /\/api\/clips\/\d+\/stream/,
  );
  await expect(page.locator("#deleteButton")).toHaveAttribute("aria-disabled", "false");
  const src = (await page.locator("#mainVideo").getAttribute("src")) ?? "";
  const m = src.match(/\/api\/clips\/(\d+)\/stream/);
  expect(m, `could not read clip id from src=${src}`).not.toBeNull();
  return Number(m![1]);
}

/** Assert no pageerrors and no console errors/warnings, allowing ONLY the
 *  browser's own "Failed to load resource: ... status of <code>" lines for the
 *  deliberately-mocked non-2xx responses this test triggered. */
function assertConsoleClean(probe: Probe, allowStatuses: number[] = []) {
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  const unexpected = probe.consoleErrors.filter((e) => {
    const m = e.text.match(/status of (\d{3})/);
    return !(m && allowStatuses.includes(Number(m[1])));
  });
  expect(
    unexpected,
    `unexpected console error(s): ${JSON.stringify(unexpected)}`,
  ).toEqual([]);
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
}

test.describe("clip-delete UAT", () => {
  // Global invariants for EVERY test: requests stay same-origin, and any DELETE
  // that fired must have carried `?target=car` (the destructive op is never sent
  // without the explicit, non-defaulted target).
  test.afterEach(async ({ probe }) => {
    const origin = new URL(loadState().baseURL).origin;
    for (const req of probe.requests) {
      expect(new URL(req.url).origin, `off-origin request to ${req.url}`).toBe(origin);
    }
    const deletes = probe.requests.filter((r) => r.method.toUpperCase() === "DELETE");
    for (const d of deletes) {
      expect(
        new URL(d.url).searchParams.get("target"),
        `DELETE without ?target=car: ${d.url}`,
      ).toBe("car");
    }
  });

  // ── Gate 1: destructive gating — confirm names the clip; no DELETE until
  //    confirm; Cancel fires nothing. ──────────────────────────────────────
  test("confirm dialog gates the delete; cancel issues no DELETE", async ({ page, probe }) => {
    const calls = await mockClipDelete(page, () => ({ status: 200, body: okBody }));
    await gotoPlayerWithClip(page);

    // Opening the dialog issues NO request and names the clip.
    await page.locator("#deleteButton").click();
    const dialog = page.locator("[data-testid=delete-dialog]");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("Delete this clip?");
    // The named clip carries the event date (tabular clip label) — a non-empty,
    // human reference so the operator knows exactly what they're deleting.
    await expect(page.locator(".delete-modal-clip")).not.toBeEmpty();
    expect(calls.length, "no DELETE may fire from merely opening the dialog").toBe(0);

    // Cancel closes it; still no DELETE.
    await page.locator(".delete-modal-btn.cancel").click();
    await expect(dialog).toHaveCount(0);
    expect(calls.length, "Cancel must not issue a DELETE").toBe(0);

    expect(
      probe.requests.filter((r) => r.method.toUpperCase() === "DELETE"),
      "no DELETE request may have left the page",
    ).toEqual([]);
    assertConsoleClean(probe);
  });

  // ── Gate 2: success (200) — one DELETE with ?target=car removes the clip. ─
  test("confirm sends one DELETE ?target=car; 200 removes the clip", async ({ page, probe }) => {
    const calls = await mockClipDelete(page, () => ({ status: 200, body: okBody }));
    const deletedId = await gotoPlayerWithClip(page);

    await page.locator("#deleteButton").click();
    await expect(page.locator("[data-testid=delete-dialog]")).toBeVisible();
    await page.locator("[data-testid=delete-confirm]").click();

    // Terminal success: a notice appears and the dialog closes.
    await expect(page.locator("[data-testid=delete-notice]")).toBeVisible();
    await expect(page.locator("[data-testid=delete-notice]")).toContainText(/deleted/i);
    await expect(page.locator("[data-testid=delete-dialog]")).toHaveCount(0);

    // Exactly one DELETE, to the deleted clip, with ?target=car.
    expect(calls.length, "exactly one DELETE").toBe(1);
    expect(calls[0].target).toBe("car");
    expect(new URL(calls[0].url).pathname).toBe(`/api/clips/${deletedId}`);

    // The removed clip is no longer the streamed clip (the player advanced to the
    // next clip or emptied) — its stream URL must not reference the deleted id.
    await expect(page.locator("#mainVideo")).not.toHaveAttribute(
      "src",
      new RegExp(`/api/clips/${deletedId}/stream`),
    );
    assertConsoleClean(probe);
  });

  // ── Gate 3: busy (409) — retryable message + a Retry that re-issues DELETE. ─
  test("409 shows a retryable busy message; Retry re-issues the DELETE and succeeds", async ({
    page,
    probe,
  }) => {
    // First DELETE → 409 busy; second (Retry) → 200 done.
    const calls = await mockClipDelete(page, (i) =>
      i === 0
        ? { status: 409, body: errBody("handoff_busy", "another change is already in progress") }
        : { status: 200, body: okBody },
    );
    await gotoPlayerWithClip(page);

    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();

    // The busy error is shown and the dialog stays open with a Retry affordance.
    const err = page.locator("[data-testid=delete-error]");
    await expect(err).toBeVisible();
    await expect(err).toHaveClass(/retryable/);
    const retry = page.locator("[data-testid=delete-confirm]");
    await expect(retry).toHaveText(/Retry/);
    expect(calls.length, "one DELETE so far").toBe(1);

    // Retry succeeds.
    await retry.click();
    await expect(page.locator("[data-testid=delete-notice]")).toBeVisible();
    await expect(page.locator("[data-testid=delete-dialog]")).toHaveCount(0);
    expect(calls.length, "Retry must re-issue the DELETE").toBe(2);
    expect(calls.every((c) => c.target === "car")).toBe(true);

    assertConsoleClean(probe, [409]);
  });

  // ── Gate 4: validation (422) — terminal error, NO retry (Close only). ─────
  test("422 shows a terminal error with no Retry", async ({ page, probe }) => {
    const calls = await mockClipDelete(page, () => ({
      status: 422,
      body: errBody("not_car_deletable", "this clip is not deletable from the car"),
    }));
    await gotoPlayerWithClip(page);

    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();

    const err = page.locator("[data-testid=delete-error]");
    await expect(err).toBeVisible();
    await expect(err).toHaveClass(/fatal/);
    await expect(err).toContainText(/not deletable/i);
    // Terminal: the confirm/retry button is gone; only Close remains.
    await expect(page.locator("[data-testid=delete-confirm]")).toHaveCount(0);
    await expect(page.locator(".delete-modal-btn.cancel")).toHaveText("Close");
    expect(calls.length, "a 422 must not be retried automatically").toBe(1);

    // Close dismisses the dialog; the clip is NOT removed (validation failure).
    await page.locator(".delete-modal-btn.cancel").click();
    await expect(page.locator("[data-testid=delete-dialog]")).toHaveCount(0);

    assertConsoleClean(probe, [422]);
  });

  // ── Gate 5: device unreachable (503) — distinct, retryable message. ───────
  test("503 shows a distinct device-unreachable message (retryable)", async ({ page, probe }) => {
    const calls = await mockClipDelete(page, () => ({
      status: 503,
      body: errBody("gadgetd_unavailable", "gadgetd is not reachable"),
    }));
    await gotoPlayerWithClip(page);

    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();

    const err = page.locator("[data-testid=delete-error]");
    await expect(err).toBeVisible();
    await expect(err).toHaveClass(/retryable/);
    await expect(err).toContainText(/unreachable/i);
    await expect(page.locator("[data-testid=delete-confirm]")).toHaveText(/Retry/);
    expect(calls.length).toBe(1);

    assertConsoleClean(probe, [503]);
  });

  // ── Gate 6: screenshots — the player + the confirm dialog at this viewport. ─
  test("screenshots — player and confirm dialog", async ({ page }, testInfo) => {
    const viewport = testInfo.project.name;
    await mockClipDelete(page, () => ({ status: 200, body: okBody }));
    await gotoPlayerWithClip(page);

    const playerShot = resolve(ARTIFACTS, `clip-delete-player-${viewport}.png`);
    await page.screenshot({ path: playerShot, fullPage: true });
    await testInfo.attach(`clip-delete-player-${viewport}.png`, {
      path: playerShot,
      contentType: "image/png",
    });

    await page.locator("#deleteButton").click();
    await expect(page.locator("[data-testid=delete-dialog]")).toBeVisible();
    const dialogShot = resolve(ARTIFACTS, `clip-delete-dialog-${viewport}.png`);
    await page.screenshot({ path: dialogShot, fullPage: true });
    await testInfo.attach(`clip-delete-dialog-${viewport}.png`, {
      path: dialogShot,
      contentType: "image/png",
    });
    console.log(`[uat][screenshot:clip-delete:${viewport}] ${playerShot} ; ${dialogShot}`);
  });
});
