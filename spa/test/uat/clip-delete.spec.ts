import { test, expect, loadState, type Probe } from "./helpers";
import type { Page, Route } from "@playwright/test";

interface DeleteCall {
  url: string;
  target: string | null;
  requestId: string | null;
  idempotencyKey: string | null;
  requestHash: string | null;
}

interface StatusCall {
  url: string;
  jobId: string;
}

interface MockReply {
  status: number;
  body: unknown;
  delayMs?: number;
}

async function mockArchiveDeleteFlow(
  page: Page,
  deletePlan: (callIndex: number) => MockReply,
  statusPlan: (callIndex: number) => MockReply,
): Promise<{ deleteCalls: DeleteCall[]; statusCalls: StatusCall[] }> {
  const deleteCalls: DeleteCall[] = [];
  const statusCalls: StatusCall[] = [];
  await page.route(/\/api\/clips\/\d+(\?.*)?$/, async (route: Route) => {
    if (route.request().method() !== "DELETE") {
      await route.fallback();
      return;
    }
    const reply = deletePlan(deleteCalls.length);
    if ((reply.delayMs ?? 0) > 0) {
      await new Promise((resolve) => setTimeout(resolve, reply.delayMs));
    }
    const url = new URL(route.request().url());
    deleteCalls.push({
      url: route.request().url(),
      target: url.searchParams.get("target"),
      requestId: url.searchParams.get("requestId"),
      idempotencyKey: url.searchParams.get("idempotencyKey"),
      requestHash: url.searchParams.get("requestHash"),
    });
    await route.fulfill({
      status: reply.status,
      contentType: "application/json",
      body: JSON.stringify(reply.body),
    });
  });
  await page.route(/\/api\/jobs\/archive-delete\/[^/?#]+$/, async (route: Route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    const reply = statusPlan(statusCalls.length);
    if ((reply.delayMs ?? 0) > 0) {
      await new Promise((resolve) => setTimeout(resolve, reply.delayMs));
    }
    const url = route.request().url();
    statusCalls.push({
      url,
      jobId: url.split("/").pop() ?? "",
    });
    await route.fulfill({
      status: reply.status,
      contentType: "application/json",
      body: JSON.stringify(reply.body),
    });
  });
  return { deleteCalls, statusCalls };
}

function acceptedDelete(jobId: string, requestId: string): MockReply {
  return {
    status: 202,
    body: {
      status: "accepted",
      target: "archive",
      jobId,
      requestId,
      state: "queued",
      statusUrl: `/api/jobs/archive-delete/${jobId}`,
    },
  };
}

function statusReply(
  jobId: string,
  requestId: string,
  state: "queued" | "running" | "done" | "refused" | "failed",
  detail?: string,
): MockReply {
  return {
    status: 200,
    body: {
      jobId,
      requestId,
      state,
      responseStatus: state === "done" ? "replay" : state === "refused" ? "rejected" : "accepted",
      responseCode: state === "done" ? 200 : state === "refused" ? 409 : 202,
      detail: detail ?? null,
    },
  };
}

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

function assertConsoleClean(probe: Probe) {
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

test.describe("clip-delete UAT (durable archive-delete)", () => {
  test.afterEach(async ({ probe }) => {
    const origin = new URL(loadState().baseURL).origin;
    for (const req of probe.requests) {
      expect(new URL(req.url).origin, `off-origin request to ${req.url}`).toBe(origin);
    }
    const deletes = probe.requests.filter((r) => r.method.toUpperCase() === "DELETE");
    for (const d of deletes) {
      const url = new URL(d.url);
      expect(url.searchParams.get("target")).toBe("archive");
      expect(url.searchParams.get("requestId")).toBeTruthy();
      expect(url.searchParams.get("idempotencyKey")).toBeTruthy();
      expect(url.searchParams.get("requestHash")).toMatch(/^[a-f0-9]{64}$/);
    }
  });

  test("confirm dialog gates delete; cancel issues no DELETE", async ({ page, probe }) => {
    const { deleteCalls } = await mockArchiveDeleteFlow(
      page,
      () => acceptedDelete("m-uat-cancel", "req-uat-cancel"),
      () => statusReply("m-uat-cancel", "req-uat-cancel", "done"),
    );
    await gotoPlayerWithClip(page);
    await page.locator("#deleteButton").click();
    const dialog = page.locator("[data-testid=delete-dialog]");
    await expect(dialog).toBeVisible();
    await expect(page.locator(".delete-modal-clip")).not.toBeEmpty();
    await page.locator(".delete-modal-btn.cancel").click();
    await expect(dialog).toHaveCount(0);
    expect(deleteCalls.length).toBe(0);
    assertConsoleClean(probe);
  });

  test("accepted/queued delete polls status and only removes clip after terminal done", async ({
    page,
    probe,
  }) => {
    const { deleteCalls, statusCalls } = await mockArchiveDeleteFlow(
      page,
      () => acceptedDelete("m-uat-done", "req-uat-done"),
      (i) =>
        i === 0
          ? statusReply("m-uat-done", "req-uat-done", "queued")
          : statusReply("m-uat-done", "req-uat-done", "done", "archive item deleted"),
    );
    const deletedId = await gotoPlayerWithClip(page);
    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();
    await expect(page.locator("[data-testid=delete-progress]")).toContainText(/queued/i);
    await expect(page.locator("[data-testid=delete-dialog]")).toBeVisible();
    await expect(page.locator("[data-testid=delete-notice]")).toContainText(/deleted/i);
    await expect(page.locator("[data-testid=delete-dialog]")).toHaveCount(0);
    expect(deleteCalls.length).toBe(1);
    expect(deleteCalls[0].target).toBe("archive");
    expect(deleteCalls[0].requestId).toBeTruthy();
    expect(deleteCalls[0].idempotencyKey).toBeTruthy();
    expect(deleteCalls[0].requestHash).toMatch(/^[a-f0-9]{64}$/);
    expect(statusCalls.length).toBeGreaterThanOrEqual(2);
    expect(statusCalls.every((c) => c.jobId === "m-uat-done")).toBe(true);
    await expect(page.locator("#mainVideo")).not.toHaveAttribute(
      "src",
      new RegExp(`/api/clips/${deletedId}/stream`),
    );
    assertConsoleClean(probe);
  });

  test("terminal refused shows fatal error and no retry", async ({ page, probe }) => {
    await mockArchiveDeleteFlow(
      page,
      () => acceptedDelete("m-uat-refused", "req-uat-refused"),
      () =>
        statusReply(
          "m-uat-refused",
          "req-uat-refused",
          "refused",
          "clip is not linked to a live archive item; refresh required",
        ),
    );
    await gotoPlayerWithClip(page);
    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();
    const err = page.locator("[data-testid=delete-error]");
    await expect(err).toBeVisible();
    await expect(err).toContainText(/refresh required/i);
    await expect(err).toHaveClass(/fatal/);
    await expect(page.locator("[data-testid=delete-confirm]")).toHaveCount(0);
    await expect(page.locator(".delete-modal-btn.cancel")).toHaveText("Close");
    assertConsoleClean(probe);
  });

  test("terminal failed keeps dialog open with retry affordance", async ({ page, probe }) => {
    await mockArchiveDeleteFlow(
      page,
      () => acceptedDelete("m-uat-failed", "req-uat-failed"),
      () =>
        statusReply(
          "m-uat-failed",
          "req-uat-failed",
          "failed",
          "delete worker failed to remove archive item",
        ),
    );
    await gotoPlayerWithClip(page);
    await page.locator("#deleteButton").click();
    await page.locator("[data-testid=delete-confirm]").click();
    const err = page.locator("[data-testid=delete-error]");
    await expect(err).toBeVisible();
    await expect(err).toContainText(/failed/i);
    await expect(err).toHaveClass(/retryable/);
    await expect(page.locator("[data-testid=delete-confirm]")).toHaveText(/Retry/);
    assertConsoleClean(probe);
  });
});
