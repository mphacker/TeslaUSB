import { test, expect, type Probe } from "./helpers";
import type { Page, Route, TestInfo } from "@playwright/test";
import {
  assertMediaChrome,
  assertMediaPills,
  assertWiring,
  capturePerf,
  captureScreenshot,
} from "./screen-helpers";

// ── Chime Scheduler UAT (A3b) ─────────────────────────────────────────────
// Drives the REAL bundle webd serves at /media. The Lock Chimes screen embeds
// <ChimeScheduler/>, which bootstraps from a single GET /api/chime-scheduler
// snapshot and forwards every mutation to schedulerd (which webd proxies under
// /api/chime-scheduler/*). schedulerd is NOT spawned in this harness, so the
// snapshot read + all mutations are `page.route`-MOCKED behind a small stateful
// fixture: mutations update an in-memory snapshot so the component's
// refetch-after-success always reflects "daemon-owned" state — exactly how the
// real flow behaves, minus the daemon. (Sanctioned mock-of-absent-dependency,
// same posture as the chimes/bulk-delete specs.)

const MENUS = {
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

interface LibEntry {
  filename: string;
  bytes: number;
}
interface Group {
  id: string;
  name: string;
  description: string;
  chimes: string[];
}
interface Schedule {
  id: string;
  name: string;
  chimeFilename: string;
  scheduleType: string;
  days?: string[];
  month?: number;
  day?: number;
  holiday?: string;
  interval?: string;
  hour?: number;
  minute?: number;
  enabled: boolean;
}
interface Snapshot {
  schedules: Schedule[];
  groups: Group[];
  randomMode: { enabled: boolean; groupId?: string };
  library: LibEntry[];
  menus: typeof MENUS;
}

function emptySnapshot(): Snapshot {
  return { schedules: [], groups: [], randomMode: { enabled: false }, library: [], menus: MENUS };
}

function populatedSnapshot(): Snapshot {
  return {
    schedules: [
      {
        id: "sch-seed",
        name: "Seed Weekly",
        chimeFilename: "Sparkle.wav",
        scheduleType: "weekly",
        days: ["Monday", "Friday"],
        hour: 8,
        minute: 30,
        enabled: true,
      },
    ],
    groups: [
      {
        id: "grp-seed",
        name: "Night Set",
        description: "Quiet chimes",
        chimes: ["Sparkle.wav"],
      },
    ],
    randomMode: { enabled: false },
    library: [
      { filename: "Sparkle.wav", bytes: 2048 },
      { filename: "Chime2.wav", bytes: 4096 },
    ],
    menus: MENUS,
  };
}

const json200 = (route: Route, body: unknown) =>
  route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

interface Captured {
  schedulePost: unknown[];
  schedulePut: { id: string; body: unknown }[];
  scheduleDelete: string[];
  groupPost: unknown[];
  groupPut: { id: string; body: unknown }[];
  groupDelete: string[];
  randomMode: { enabled: boolean; groupId?: string | null }[];
  libraryPost: string[];
  libraryDelete: string[];
  libraryBulkDelete: string[][];
  libraryActivate: string[];
  libraryRename: { from: string; to: string }[];
}

/** Install the stateful scheduler mock. Returns the live snapshot + captured
 *  request payloads so each test can assert the exact wire shape it provoked. */
async function installScheduler(
  page: Page,
  initial: Snapshot = emptySnapshot(),
  onUpload?: (entry: LibEntry, snap: Snapshot) => void,
  onGet?: (snap: Snapshot, readCount: number) => void,
  onDelete?: (filename: string, snap: Snapshot) => void,
  onBulkDelete?: (names: string[], snap: Snapshot) => void,
  onRename?: (from: string, to: string, snap: Snapshot) => void,
) {
  const snap: Snapshot = JSON.parse(JSON.stringify(initial));
  const cap: Captured = {
    schedulePost: [],
    schedulePut: [],
    scheduleDelete: [],
    groupPost: [],
    groupPut: [],
    groupDelete: [],
    randomMode: [],
    libraryPost: [],
    libraryDelete: [],
    libraryBulkDelete: [],
    libraryActivate: [],
    libraryRename: [],
  };
  let seq = 0;
  let getCount = 0;
  const tail = (url: string) => decodeURIComponent(url.split("?")[0].split("/").pop() ?? "");
  const ci = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

  /** Mirror schedulerd's rename cascade: rewrite schedule + group references
   *  (case-insensitive match, verbatim write, case-insensitive member dedupe). */
  function cascadeRename(from: string, to: string) {
    for (const s of snap.schedules) {
      if (ci(s.chimeFilename, from)) s.chimeFilename = to;
    }
    for (const g of snap.groups) {
      if (!g.chimes.some((c) => ci(c, from))) continue;
      const next: string[] = [];
      for (const m of g.chimes) {
        const mapped = ci(m, from) ? to : m;
        if (!next.some((x) => ci(x, mapped))) next.push(mapped);
      }
      g.chimes = next;
    }
  }

  // Snapshot read (exact path only).
  await page.route("**/api/chime-scheduler", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    getCount += 1;
    onGet?.(snap, getCount);
    return json200(route, snap);
  });

  // Schedules collection (POST).
  await page.route("**/api/chime-scheduler/schedules", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as Schedule;
    cap.schedulePost.push(body);
    const stored = { ...body, id: `sch-${++seq}` };
    snap.schedules.push(stored);
    return json200(route, stored);
  });

  // Schedule item (PUT / DELETE).
  await page.route("**/api/chime-scheduler/schedules/*", (route) => {
    const m = route.request().method();
    const id = tail(route.request().url());
    if (m === "PUT") {
      const body = route.request().postDataJSON() as Schedule;
      cap.schedulePut.push({ id, body });
      const i = snap.schedules.findIndex((s) => s.id === id);
      if (i >= 0) snap.schedules[i] = { ...body, id };
      return json200(route, { ...body, id });
    }
    if (m === "DELETE") {
      cap.scheduleDelete.push(id);
      snap.schedules = snap.schedules.filter((s) => s.id !== id);
      return json200(route, {});
    }
    return route.continue();
  });

  // Groups collection (POST).
  await page.route("**/api/chime-scheduler/groups", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as Group;
    cap.groupPost.push(body);
    const stored = { ...body, id: `grp-${++seq}` };
    snap.groups.push(stored);
    return json200(route, stored);
  });

  // Group item (PUT / DELETE).
  await page.route("**/api/chime-scheduler/groups/*", (route) => {
    const m = route.request().method();
    const id = tail(route.request().url());
    if (m === "PUT") {
      const body = route.request().postDataJSON() as Group;
      cap.groupPut.push({ id, body });
      const i = snap.groups.findIndex((g) => g.id === id);
      if (i >= 0) snap.groups[i] = { ...body, id };
      return json200(route, { ...body, id });
    }
    if (m === "DELETE") {
      cap.groupDelete.push(id);
      snap.groups = snap.groups.filter((g) => g.id !== id);
      return json200(route, {});
    }
    return route.continue();
  });

  // Random-on-boot mode (PUT).
  await page.route("**/api/chime-scheduler/random-mode", (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const body = route.request().postDataJSON() as { enabled: boolean; groupId?: string | null };
    cap.randomMode.push(body);
    snap.randomMode = body.enabled
      ? { enabled: true, groupId: body.groupId ?? undefined }
      : { enabled: false };
    return json200(route, snap.randomMode);
  });

  function extractMultipartBytes(body: Buffer | null): number {
    if (!body) return 0;
    const text = body.toString("binary");
    const match = text.match(/filename="([^"]+)"\r\nContent-Type:[^\r\n]*\r\n\r\n([\s\S]*?)\r\n--/);
    return match ? Buffer.from(match[2], "binary").length : 0;
  }

  // Library upload (POST multipart).
  await page.route("**/api/chime-scheduler/library", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const post = route.request().postDataBuffer() ?? Buffer.alloc(0);
    const match = post.toString("binary").match(/filename="([^"]+)"/);
    const rawName = match ? match[1] : `chime-${++seq}.wav`;
    // Mirror webd's sanitise_filename: last path component, then trim. The
    // catalog reports this transformed name, so a client that keyed on the raw
    // File.name would never converge on hardware — keep the mock faithful so
    // that regression is caught here, not in the field.
    const filename = (rawName.split(/[\\/]/).pop() ?? rawName).trim();
    const bytes = extractMultipartBytes(post);
    cap.libraryPost.push(filename);
    const entry = { filename, bytes };
    // Hardware answers 202 {state:"queued", job_id} with NO filename/bytes — the
    // file lands in the catalog only on a later scannerd-driven snapshot. Drive
    // that lag through the `onGet`/`onUpload` callbacks, never a synchronous push.
    onUpload?.(entry, snap);
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ state: "queued", job_id: `job-${++seq}` }),
    });
  });

  // Library item (DELETE). Hardware answers 202/200 and the row leaves the
  // catalog only on a LATER scannerd rescan; drive that lag via `onDelete`.
  await page.route("**/api/chime-scheduler/library/*", (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    const filename = tail(route.request().url());
    cap.libraryDelete.push(filename);
    if (onDelete) onDelete(filename, snap);
    else snap.library = snap.library.filter((c) => c.filename !== filename);
    return json200(route, {});
  });

  // Library activate (POST .../activate) — "Set Active" promotes a library row
  // to the car's LockChime.wav via the gadgetd eject-handoff queue (202 queued).
  await page.route("**/api/chime-scheduler/library/*/activate", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const filename = decodeURIComponent(
      route.request().url().split("/activate")[0].split("/").pop() ?? "",
    );
    cap.libraryActivate.push(filename);
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ state: "queued", job_id: "m-act-1" }),
    });
  });

  // Library bulk delete (POST .../library/bulk-delete). Registered last so it
  // takes priority over the `library/*` glob for the batch POST. Hardware
  // coalesces the batch into ONE gadgetd handoff (202 queued); the rows leave
  // the catalog only on a LATER scannerd rescan, driven via `onBulkDelete`.
  await page.route("**/api/chime-scheduler/library/bulk-delete", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as { names: string[] };
    cap.libraryBulkDelete.push(body.names);
    if (onBulkDelete) onBulkDelete(body.names, snap);
    else snap.library = snap.library.filter((c) => !body.names.includes(c.filename));
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ state: "queued", job_id: "m-bulk-1" }),
    });
  });

  // Library rename (POST .../library/rename, body {from,to}). Registered after
  // the `library/*` glob so it wins priority for this sub-path. Mirrors webd's
  // move-style rename: enqueue the destination COPY (the new name lands in the
  // catalog) and synchronously cascade the scheduler references — the SPA then
  // file-only-deletes the source (`?cascade=false`) once the copy converges.
  await page.route("**/api/chime-scheduler/library/rename", (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as { from: string; to: string };
    cap.libraryRename.push({ from: body.from, to: body.to });
    if (onRename) {
      onRename(body.from, body.to, snap);
    } else {
      const src = snap.library.find((e) => ci(e.filename, body.from));
      if (src && !snap.library.some((e) => ci(e.filename, body.to))) {
        snap.library.push({ filename: body.to, bytes: src.bytes });
      }
      cascadeRename(body.from, body.to);
    }
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ state: "queued", job_id: `m-ren-${++seq}` }),
    });
  });

  return { snap, cap };
}

/** Navigate to /media and wait for the embedded scheduler to settle (ready). */
async function gotoScheduler(page: Page) {
  await page.goto("/media", { waitUntil: "load" });
  await expect(page.locator(".container[data-screen=media]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-scheduler]")).toBeVisible();
}

function assertCleanConsole(probe: Probe) {
  expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
  expect(probe.consoleErrors, `console error(s): ${JSON.stringify(probe.consoleErrors)}`).toEqual(
    [],
  );
  expect(
    probe.consoleWarnings,
    `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
  ).toEqual([]);
}

/** A minimal but structurally-valid PCM WAV for the library upload picker. */
function wavBuffer(dataLen = 256): Buffer {
  const buf = Buffer.alloc(44 + dataLen);
  buf.write("RIFF", 0, "ascii");
  buf.writeUInt32LE(36 + dataLen, 4);
  buf.write("WAVE", 8, "ascii");
  buf.write("fmt ", 12, "ascii");
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(44100, 24);
  buf.writeUInt32LE(88200, 28);
  buf.writeUInt16LE(2, 32);
  buf.writeUInt16LE(16, 34);
  buf.write("data", 36, "ascii");
  buf.writeUInt32LE(dataLen, 40);
  return buf;
}

test.describe("chime scheduler UAT (A3b)", () => {
  // ── Gate 1: parity + wiring + perf + screenshot ─────────────────────────
  test("parity — sections render, bundle wired, perf + screenshot captured", async ({
    page,
    probe,
  }, testInfo) => {
    await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    // App-shell chrome + media pills (chimes active), same as the Media screen.
    await assertMediaChrome(page, testInfo);
    await assertMediaPills(page, "chimes");

    // The three scheduler sections are present with their v1 titles.
    await expect(page.locator("#scheduler-section summary")).toHaveText("Chime Scheduler");
    await expect(page.locator("#groups-section summary")).toHaveText("Random Chime Groups");
    await expect(page.locator("#library-section summary")).toHaveText("Chime Library");

    // Seeded data renders: one schedule, one group, two library rows.
    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(1);
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);

    await assertWiring(page, "/media", "lock-chimes");
    await capturePerf(page, testInfo, "chime-scheduler");
    await captureScreenshot(page, testInfo, "chime-scheduler");
    assertCleanConsole(probe);
  });

  test("timezone — select renders seeded zones from GET /api/system/timezone", async ({
    page,
    probe,
  }) => {
    await installScheduler(page, populatedSnapshot());

    await gotoScheduler(page);

    const select = page.locator('[data-testid="timezone-select"]');
    await expect(select).toBeVisible();

    const options = (await select.locator("option").allTextContents()).filter(Boolean);
    expect(options).toContain("America/New_York");
    expect(options).toContain("America/Los_Angeles");
    expect(options).not.toContain("posixrules");
    expect(options).not.toContain("zone.tab");

    // Assert the raw GET contract directly rather than the select's resolved
    // value (which is tautological — the browser coerces an unmatched `current`
    // to the first option). `current` is the system's detected zone; it is
    // legitimately null on the UAT host (no timedatectl / /etc/localtime), and
    // when non-null MUST be one of the enumerated zones. Real current-zone + set
    // behavior is verified on hardware.
    const apiResponse = await page.request.get("/api/system/timezone");
    expect(apiResponse.status()).toBe(200);
    const payload = (await apiResponse.json()) as { current: string | null; zones: string[] };
    expect(Array.isArray(payload.zones)).toBe(true);
    expect(payload.zones).toContain("America/New_York");
    expect(payload.zones).not.toContain("posixrules");
    expect(payload.current === null || payload.zones.includes(payload.current)).toBe(true);

    const timezoneRequests = probe.requests.filter((r) => r.url.includes("/api/system/timezone"));
    const timezoneResponses = probe.responses.filter((r) => r.url.includes("/api/system/timezone"));
    expect(timezoneRequests.some((r) => r.method === "GET")).toBe(true);
    expect(timezoneResponses.some((r) => r.status === 200)).toBe(true);
    expect(timezoneResponses.some((r) => r.contentType.includes("application/json"))).toBe(true);

    assertCleanConsole(probe);
  });

  // ── Gate 2: honest empty states ─────────────────────────────────────────
  test("empty — schedules/groups/library show honest empty states", async ({ page, probe }) => {
    await installScheduler(page); // empty snapshot
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=schedules-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=groups-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=library-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(0);
    assertCleanConsole(probe);
  });

  // ── Gate 3: schedule type switching shows the right fields ───────────────
  test("schedule type — switching reveals the correct inputs", async ({ page, probe }) => {
    await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    // Weekly (default): days + time + chime select; no date/holiday/interval.
    await expect(page.locator("[data-testid=days-selection]")).toBeVisible();
    await expect(page.locator("[data-testid=time-selection]")).toBeVisible();
    await expect(page.locator("[data-testid=schedule-chime]")).toBeVisible();
    await expect(page.locator("[data-testid=date-selection]")).toHaveCount(0);

    // Specific Date.
    await page.locator("[data-testid=schedule-type] input[value=date]").check();
    await expect(page.locator("[data-testid=date-selection]")).toBeVisible();
    await expect(page.locator("[data-testid=days-selection]")).toHaveCount(0);
    await expect(page.locator("[data-testid=time-selection]")).toBeVisible();

    // US Holiday.
    await page.locator("[data-testid=schedule-type] input[value=holiday]").check();
    await expect(page.locator("[data-testid=holiday-selection]")).toBeVisible();
    await expect(page.locator("[data-testid=time-selection]")).toHaveCount(0);

    // Recurring Rotation: interval shows; chime select is hidden (always Random).
    await page.locator("[data-testid=schedule-type] input[value=recurring]").check();
    await expect(page.locator("[data-testid=interval-selection]")).toBeVisible();
    await expect(page.locator("[data-testid=schedule-chime]")).toHaveCount(0);
    assertCleanConsole(probe);
  });

  // ── Gate 4: add a weekly schedule → POST shape + refetch ─────────────────
  test("add schedule — weekly form POSTs the right body and the list refreshes", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await page.locator("[data-testid=schedule-name]").fill("Morning Chime");
    await page.selectOption("[data-testid=schedule-chime]", "Sparkle.wav");
    // Check Monday (first weekday checkbox).
    await page.locator("[data-testid=days-selection] input[type=checkbox]").first().check();
    await page.selectOption("[data-testid=schedule-hour]", "7");
    await page.selectOption("[data-testid=schedule-minute]", "15");
    await page.locator("[data-testid=schedule-submit]").click();

    // List grows from 1 (seed) to 2, and the new row shows the name.
    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(2);
    await expect(page.locator("[data-testid=schedule-list]")).toContainText("Morning Chime");

    expect(cap.schedulePost).toHaveLength(1);
    const body = cap.schedulePost[0] as Schedule;
    expect(body.name).toBe("Morning Chime");
    expect(body.chimeFilename).toBe("Sparkle.wav");
    expect(body.scheduleType).toBe("weekly");
    expect(body.days).toContain("Monday");
    expect(body.hour).toBe(7);
    expect(body.minute).toBe(15);
    expect(body.enabled).toBe(true);
    assertCleanConsole(probe);
  });

  // ── Gate 5: recurring schedule forces RANDOM as the chime ────────────────
  test("add schedule — recurring posts chimeFilename=RANDOM", async ({ page, probe }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await page.locator("[data-testid=schedule-name]").fill("Rotating");
    await page.locator("[data-testid=schedule-type] input[value=recurring]").check();
    await page.selectOption("[data-testid=interval-selection] select", "1hour");
    await page.locator("[data-testid=schedule-submit]").click();

    await expect(page.locator("[data-testid=schedule-list]")).toContainText("Rotating");
    expect(cap.schedulePost).toHaveLength(1);
    const body = cap.schedulePost[0] as Schedule;
    expect(body.scheduleType).toBe("recurring");
    expect(body.chimeFilename).toBe("RANDOM");
    expect(body.interval).toBe("1hour");
    assertCleanConsole(probe);
  });

  // ── Gate 6: delete a schedule ────────────────────────────────────────────
  test("delete schedule — removes the row and DELETEs the id", async ({ page, probe }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(1);
    await page.locator("[data-testid=schedule-delete]").first().click();
    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(0);
    await expect(page.locator("[data-testid=schedules-empty]")).toBeVisible();
    expect(cap.scheduleDelete).toEqual(["sch-seed"]);
    assertCleanConsole(probe);
  });

  // ── Gate 6b: edit a schedule → PUT to the id (body carries no stray id) ───
  test("edit schedule — Edit populates the form and PUTs the update", async ({ page, probe }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await page.locator("[data-testid=schedule-edit]").first().click();
    // Form populates from the stored schedule.
    await expect(page.locator("[data-testid=schedule-name]")).toHaveValue("Seed Weekly");
    await expect(page.locator("[data-testid=schedule-submit]")).toHaveText("Update Schedule");

    await page.locator("[data-testid=schedule-name]").fill("Renamed Weekly");
    await page.locator("[data-testid=schedule-submit]").click();

    await expect(page.locator("[data-testid=schedule-list]")).toContainText("Renamed Weekly");
    expect(cap.schedulePut).toHaveLength(1);
    expect(cap.schedulePut[0].id).toBe("sch-seed");
    const body = cap.schedulePut[0].body as Schedule;
    expect(body.name).toBe("Renamed Weekly");
    expect(body.scheduleType).toBe("weekly");
    assertCleanConsole(probe);
  });

  // ── Gate 7: create a group via the modal ─────────────────────────────────
  test("create group — modal POSTs name/description/chimes and the card appears", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await page.locator("[data-testid=create-group]").click();
    const modal = page.locator("[data-testid=group-modal]");
    await expect(modal).toBeVisible();

    await page.locator("[data-testid=group-name]").fill("Holiday Set");
    await page.locator("[data-testid=group-description]").fill("Festive chimes");
    // Tick the first library chime in the selector.
    await page.locator("[data-testid=group-chime-list] input[type=checkbox]").first().check();
    await page.locator("[data-testid=group-save]").click();

    await expect(modal).toHaveCount(0);
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(2);
    await expect(page.locator("[data-testid=groups-container]")).toContainText("Holiday Set");

    expect(cap.groupPost).toHaveLength(1);
    const body = cap.groupPost[0] as Group;
    expect(body.name).toBe("Holiday Set");
    expect(body.description).toBe("Festive chimes");
    expect(body.chimes).toContain("Sparkle.wav");
    assertCleanConsole(probe);
  });

  // ── Gate 7b: edit an existing group via the modal ────────────────────────
  test("edit group — modal pre-populates, PUTs the update, and the card reflects it", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    // Open the seeded group ("Night Set", chimes [Sparkle.wav]) for editing.
    await page.locator("[data-testid=group-edit]").first().click();
    const modal = page.locator("[data-testid=group-modal]");
    await expect(modal).toBeVisible();
    await expect(page.locator("#group-modal-title")).toHaveText("Edit Group");

    // The form pre-populates from the existing group.
    await expect(page.locator("[data-testid=group-name]")).toHaveValue("Night Set");
    await expect(page.locator("[data-testid=group-description]")).toHaveValue("Quiet chimes");
    await expect(page.locator("#gc-Sparkle\\.wav")).toBeChecked();
    await expect(page.locator("#gc-Chime2\\.wav")).not.toBeChecked();

    // Rename it and add the second library chime.
    await page.locator("[data-testid=group-name]").fill("Evening Set");
    await page.locator("#gc-Chime2\\.wav").check();
    await page.locator("[data-testid=group-save]").click();

    // Modal closes; the single card reflects the new name + chime count.
    await expect(modal).toHaveCount(0);
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(1);
    await expect(page.locator("[data-testid=groups-container]")).toContainText("Evening Set");
    await expect(page.locator("[data-testid=group-card] .group-meta-item")).toHaveText("2 chimes");

    // The PUT hit the right id with the merged body.
    expect(cap.groupPut).toHaveLength(1);
    expect(cap.groupPut[0].id).toBe("grp-seed");
    const body = cap.groupPut[0].body as Group;
    expect(body.name).toBe("Evening Set");
    expect(body.chimes).toContain("Sparkle.wav");
    expect(body.chimes).toContain("Chime2.wav");
    assertCleanConsole(probe);
  });

  // ── Gate 7c: delete a group via the card's Delete button ─────────────────
  test("delete group — the card's Delete button DELETEs the id and removes the card", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=group-card]")).toHaveCount(1);
    await page.locator("[data-testid=group-delete]").first().click();

    // The card disappears and the honest empty state returns.
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(0);
    await expect(page.locator("[data-testid=groups-empty]")).toBeVisible();

    expect(cap.groupDelete).toEqual(["grp-seed"]);
    assertCleanConsole(probe);
  });

  // ── Gate 8: enable random-on-boot mode ───────────────────────────────────
  test("random mode — selecting a group + Enable PUTs the right config", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=random-mode-status]")).toHaveText("Disabled");
    await page.selectOption("[data-testid=random-group-select]", "grp-seed");
    await page.locator("[data-testid=random-mode-toggle]").click();

    await expect(page.locator("[data-testid=random-mode-status]")).toHaveText("Enabled");
    expect(cap.randomMode).toHaveLength(1);
    expect(cap.randomMode[0].enabled).toBe(true);
    expect(cap.randomMode[0].groupId).toBe("grp-seed");
    assertCleanConsole(probe);
  });

  // ── Gate 9: upload a chime into the library (via the Upload New Chime panel) ─
  test("library upload — Upload New Chime adds a WAV and the table refreshes", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    let uploaded = false;
    let postUploadReads = 0;
    let uploadedBytes = 0;
    const { cap, snap } = await installScheduler(
      page,
      populatedSnapshot(),
      (entry) => {
        uploaded = true;
        uploadedBytes = entry.bytes;
        postUploadReads = 0;
      },
      (current) => {
        if (!uploaded) return;
        postUploadReads += 1;
        if (postUploadReads <= 2) {
          current.library = current.library.filter((entry) => entry.filename !== "NewChime.wav");
          return;
        }
        if (!current.library.some((entry) => entry.filename === "NewChime.wav")) {
          current.library.push({ filename: "NewChime.wav", bytes: uploadedBytes });
        }
      },
    );
    await gotoScheduler(page);

    await expect(page.locator("[data-testid~=library-row]")).toHaveCount(2);
    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "NewChime.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(512),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await page.locator("[data-testid=chime-editor-upload]").click({ force: true });

    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing “NewChime.wav”",
    );
    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-pending-status]")).toContainText("Syncing…");
    await expect(page.locator("[data-testid=library-set-active]").first()).toBeDisabled();

    await page.clock.fastForward(4000);

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(0);
    await expect(page.locator("[data-testid=library-notice]")).toContainText(
      "added to your chime library",
    );
    await expect(page.locator("[data-testid=library-table]")).toContainText("NewChime.wav");
    expect(cap.libraryPost).toEqual(["NewChime.wav"]);
    expect(snap.library.some((entry) => entry.filename === "NewChime.wav" && entry.bytes === uploadedBytes)).toBe(true);
    assertCleanConsole(probe);
  });

  test("catalog lag — pending row syncs after bounded polling and shows a notice", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const uploadBytes = 768;
    const actualUploadBytes = wavBuffer(uploadBytes).length;
    let uploaded = false;
    let postUploadReads = 0;
    const { cap } = await installScheduler(
      page,
      populatedSnapshot(),
      () => {
        uploaded = true;
        postUploadReads = 0;
      },
      (current) => {
        if (!uploaded) return;
        postUploadReads += 1;
        if (postUploadReads <= 2) {
          current.library = current.library.filter((entry) => entry.filename !== "LagChime.wav");
          return;
        }
        if (!current.library.some((entry) => entry.filename === "LagChime.wav")) {
          current.library.push({ filename: "LagChime.wav", bytes: actualUploadBytes });
        }
      },
    );
    await gotoScheduler(page);

    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "LagChime.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(uploadBytes),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await page.locator("[data-testid=chime-editor-upload]").click({ force: true });

    await expect(page.locator("[data-testid=chime-notice]")).toContainText("Upload accepted — syncing");
    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-pending-status]")).toContainText("Syncing…");
    await expect(page.locator("[data-testid=library-set-active]").first()).toBeDisabled();
    await expect(page.locator("[data-testid=library-delete]").first()).toBeDisabled();

    await page.clock.fastForward(4000);

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(0);
    await expect(page.locator("[data-testid=library-notice]")).toContainText("added to your chime library");
    expect(cap.libraryPost).toEqual(["LagChime.wav"]);
    assertCleanConsole(probe);
  });

  test("padded filename — client mirrors webd's trim so a space-padded upload still converges", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const dataLen = 640;
    const actualUploadBytes = wavBuffer(dataLen).length;
    let uploaded = false;
    let postUploadReads = 0;
    const { cap } = await installScheduler(
      page,
      populatedSnapshot(),
      () => {
        uploaded = true;
        postUploadReads = 0;
      },
      (current) => {
        if (!uploaded) return;
        postUploadReads += 1;
        if (postUploadReads <= 2) {
          current.library = current.library.filter((entry) => entry.filename !== "Padded.wav");
          return;
        }
        if (!current.library.some((entry) => entry.filename === "Padded.wav")) {
          current.library.push({ filename: "Padded.wav", bytes: actualUploadBytes });
        }
      },
    );
    await gotoScheduler(page);

    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "  Padded.wav  ",
      mimeType: "audio/wav",
      buffer: wavBuffer(dataLen),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await page.locator("[data-testid=chime-editor-upload]").click({ force: true });

    // The notice and the pending row both key off the trimmed catalog name.
    await expect(page.locator("[data-testid=chime-notice]")).toContainText(
      "Upload accepted — syncing “Padded.wav”",
    );
    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(1);

    await page.clock.fastForward(4000);

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(0);
    await expect(page.locator("[data-testid=library-notice]")).toContainText(
      "added to your chime library",
    );
    expect(cap.libraryPost).toEqual(["Padded.wav"]);
    assertCleanConsole(probe);
  });

  test("same-name reupload — stale same-name row is suppressed and the new bytes win", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const oldBytes = 2048;
    const newBytes = 4096;
    const oldUploadedBytes = wavBuffer(oldBytes).length;
    const newUploadedBytes = wavBuffer(newBytes).length;
    let uploaded = false;
    let postUploadReads = 0;
    const { snap } = await installScheduler(
      page,
      { ...populatedSnapshot(), library: [{ filename: "Sparkle.wav", bytes: oldBytes }, { filename: "Chime2.wav", bytes: 4096 }] },
      () => {
        uploaded = true;
        postUploadReads = 0;
      },
      (current) => {
        if (!uploaded) return;
        postUploadReads += 1;
        current.library = current.library.filter((entry) => entry.filename !== "Sparkle.wav");
        if (postUploadReads <= 2) {
          current.library.unshift({ filename: "Sparkle.wav", bytes: oldUploadedBytes });
          return;
        }
        current.library.unshift({ filename: "Sparkle.wav", bytes: newUploadedBytes });
      },
    );
    await gotoScheduler(page);

    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "Sparkle.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(newBytes),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await page.locator("[data-testid=chime-editor-upload]").click({ force: true });

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(1);
    await expect(page.locator("[data-testid~=library-row]")).toHaveCount(2);
    await expect(page.locator("[data-testid=library-pending-status]")).toContainText("Syncing…");
    await expect(page.locator("[data-testid=library-table]")).toContainText("Sparkle.wav");
    await expect(page.locator("[data-testid=library-table]")).not.toContainText("2 KB");

    await page.clock.fastForward(4000);

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(0);
    await expect(page.locator("[data-testid=library-notice]")).toContainText("added to your chime library");
    expect(snap.library.filter((entry) => entry.filename === "Sparkle.wav")).toHaveLength(1);
    expect(snap.library.find((entry) => entry.filename === "Sparkle.wav")?.bytes).toBe(
      newUploadedBytes,
    );
    assertCleanConsole(probe);
  });

  test("timeout — when the catalog never catches up, the row shows waiting and refresh now", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const uploadBytes = 1024;
    let uploaded = false;
    let postUploadReads = 0;
    await installScheduler(
      page,
      populatedSnapshot(),
      () => {
        uploaded = true;
        postUploadReads = 0;
      },
      (current) => {
        if (!uploaded) return;
        postUploadReads += 1;
        current.library = current.library.filter((entry) => entry.filename !== "TimeoutChime.wav");
      },
    );
    await gotoScheduler(page);

    await page.locator("[data-testid=chime-file-input]").setInputFiles({
      name: "TimeoutChime.wav",
      mimeType: "audio/wav",
      buffer: wavBuffer(uploadBytes),
    });
    await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
    await page.locator("[data-testid=chime-editor-upload]").click({ force: true });

    await expect(page.locator("[data-testid~=library-row-pending]")).toHaveCount(1);
    await page.clock.fastForward(45001);

    await expect(page.locator("[data-testid=library-pending-status]")).toContainText("Waiting for media scan…");
    await expect(page.locator("[data-testid=library-refresh-now]")).toBeVisible();
    await expect(page.locator("[data-testid=library-error]")).toHaveCount(0);
    assertCleanConsole(probe);
  });

  // ── Gate 10: delete a chime from the library ─────────────────────────────
  test("library delete — removes the row and DELETEs by filename", async ({ page, probe }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);
    const firstRow = page.locator("[data-testid=library-row]").first();
    await firstRow.locator("[data-testid=library-delete]").click();

    await expect(page.locator("[data-testid~=library-row-deleting]")).toHaveCount(1);
    expect(cap.libraryDelete).toEqual(["Sparkle.wav"]);

    await page.clock.fastForward(2001);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-notice]")).toContainText("removed");
    assertCleanConsole(probe);
  });

  test("library delete — waits for catalog absence before it disappears", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    let lagReads = 0;
    const { cap } = await installScheduler(
      page,
      {
        ...populatedSnapshot(),
        library: [
          { filename: "Sparkle.wav", bytes: 2048 },
          { filename: "Chime2.wav", bytes: 4096 },
        ],
      },
      undefined,
      (current) => {
        if (lagReads === 0) return;
        lagReads -= 1;
        if (lagReads === 0) {
          current.library = current.library.filter((entry) => entry.filename !== "Sparkle.wav");
        }
      },
      () => {
        lagReads = 3;
      },
    );
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);
    await page.locator("[data-testid=library-row]").first().locator(
      "[data-testid=library-delete]",
    ).click();

    await expect(page.locator("[data-testid~=library-row-deleting]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-delete]").first()).toContainText("Removing…");
    expect(cap.libraryDelete).toEqual(["Sparkle.wav"]);

    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-notice]")).toContainText("removed");
    await expect(page.locator("[data-testid=library-table]")).toContainText("Chime2.wav");
    assertCleanConsole(probe);
  });

  test("library delete — times out to waiting state with Refresh now", async ({ page, probe }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    await installScheduler(
      page,
      populatedSnapshot(),
      undefined,
      undefined,
      () => {
        // Never remove the row from the catalog: model a stuck rescan.
      },
    );
    await gotoScheduler(page);

    await page.locator("[data-testid=library-row]").first().locator(
      "[data-testid=library-delete]",
    ).click();

    await expect(page.locator("[data-testid=library-delete]").first()).toContainText("Removing…");
    await page.clock.fastForward(45001);
    await page.waitForTimeout(0);

    await expect(page.locator("[data-testid~=library-row-deleting]").first()).toContainText(
      "Removing — waiting for scan…",
    );
    await expect(page.locator("[data-testid=library-delete-refresh-now]")).toBeVisible();
    await expect(page.locator("[data-testid=library-error]")).toHaveCount(0);
    assertCleanConsole(probe);
  });

  test("library delete — two concurrent deletes both converge", async ({ page, probe }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);
    // Delete both rows back-to-back: each owns its own budget so neither resets
    // the other's clock, and both DELETE requests must fire exactly once.
    await page.locator("[data-testid=library-row]").nth(0).locator(
      "[data-testid=library-delete]",
    ).click();
    await page.locator("[data-testid=library-row]").nth(1).locator(
      "[data-testid=library-delete]",
    ).click();

    await expect(page.locator("[data-testid~=library-row-deleting]")).toHaveCount(2);
    expect([...cap.libraryDelete].sort()).toEqual(["Chime2.wav", "Sparkle.wav"]);

    await page.clock.fastForward(2001);

    await expect(page.locator("[data-testid=library-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=library-notice]")).toContainText("removed");
    assertCleanConsole(probe);
  });

  test("library delete — a double-click fires the DELETE only once", async ({ page, probe }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    const deleteBtn = page.locator("[data-testid=library-row]").first().locator(
      "[data-testid=library-delete]",
    );
    // Two rapid clicks: the synchronous in-flight guard must collapse them into
    // a single DELETE before the row locks.
    await deleteBtn.click();
    await deleteBtn.click({ force: true, noWaitAfter: true }).catch(() => {});

    await expect(page.locator("[data-testid~=library-row-deleting]")).toHaveCount(1);
    expect(cap.libraryDelete).toEqual(["Sparkle.wav"]);

    await page.clock.fastForward(2001);
    await expect(page.locator("[data-testid=library-row]")).toHaveCount(1);
    assertCleanConsole(probe);
  });

  test("library bulk delete — select all removes both in ONE handoff and converges", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);

    // Tick "select all" in the bulk toolbar, then Delete selected → confirm.
    await page.locator("[data-testid=library-bulk-bar] .bulk-select-all input").check();
    const delBtn = page.locator("[data-testid=library-bulk-delete-btn]");
    await expect(delBtn).toContainText("(2)");
    await delBtn.click();
    await page.locator("[data-testid=library-bulk-confirm-btn]").click();

    // Exactly ONE bulk-delete POST carrying BOTH names — not two per-row DELETEs.
    await expect.poll(() => cap.libraryBulkDelete.length).toBe(1);
    expect([...cap.libraryBulkDelete[0]].sort()).toEqual(["Chime2.wav", "Sparkle.wav"]);
    expect(cap.libraryDelete).toEqual([]);

    // Both rows show "Removing…" then converge away on the next catalog poll.
    await expect(page.locator("[data-testid~=library-row-deleting]")).toHaveCount(2);
    await page.clock.fastForward(2001);
    await expect(page.locator("[data-testid=library-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=library-notice]")).toContainText("removed");
    assertCleanConsole(probe);
  });

  test("library bulk delete — per-row checkbox selects a single chime to delete", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);

    // Tick just the first row's checkbox (its own dedicated column).
    await page
      .locator("[data-testid=library-row]")
      .first()
      .locator(".bulk-row-check")
      .check();
    await page.locator("[data-testid=library-bulk-delete-btn]").click();
    await page.locator("[data-testid=library-bulk-confirm-btn]").click();

    await expect.poll(() => cap.libraryBulkDelete.length).toBe(1);
    expect(cap.libraryBulkDelete[0]).toEqual(["Sparkle.wav"]);

    await page.clock.fastForward(2001);
    await expect(page.locator("[data-testid=library-row]")).toHaveCount(1);
    await expect(page.locator("[data-testid=library-row]")).toContainText("Chime2.wav");
    assertCleanConsole(probe);
  });

  // ── Gate 10b: Set Active promotes a library row (queued gadgetd handoff) ──
  test("library set active — posts activate by filename and shows a syncing notice", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    const firstRow = page.locator("[data-testid=library-row]").first();
    await expect(firstRow).toContainText("Sparkle.wav");
    // Each library row exposes the v1 action set: a preview player, a Download
    // link, Set Active, and Delete.
    await expect(firstRow.locator("[data-testid=library-audio]")).toHaveCount(1);
    await expect(firstRow.locator("[data-testid=library-download]")).toHaveAttribute(
      "href",
      /\/api\/chime-scheduler\/library\/Sparkle\.wav\/download$/,
    );
    await firstRow.locator("[data-testid=library-set-active]").click();

    await expect(firstRow.locator("[data-testid=library-set-active]")).toContainText("Syncing…");
    expect(cap.libraryActivate).toEqual(["Sparkle.wav"]);
    assertCleanConsole(probe);
  });

  // ── Gate 11: snapshot unavailable → honest error state ───────────────────
  test("error — a 503 snapshot read shows the scheduler-unavailable message", async ({
    page,
    probe,
  }) => {
    await page.route("**/api/chime-scheduler", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "scheduler_unavailable", message: "down" } }),
      });
    });

    await page.goto("/media", { waitUntil: "load" });
    await expect(page.locator(".container[data-screen=media]")).toBeVisible();
    await expect(page.locator("[data-testid=scheduler-error]")).toBeVisible();

    // No JS faults; the 503 is the only tolerated console *resource* error.
    expect(probe.pageErrors, `pageerror(s): ${JSON.stringify(probe.pageErrors)}`).toEqual([]);
    expect(
      probe.consoleWarnings,
      `console warning(s): ${JSON.stringify(probe.consoleWarnings)}`,
    ).toEqual([]);
    const leaked = probe.consoleErrors.filter(
      (e) => !(/Failed to load resource/i.test(e.text) && e.text.includes("status of 503")),
    );
    expect(leaked, `unexpected console error(s): ${JSON.stringify(leaked)}`).toEqual([]);
  });

  // ── Gate 12: rename a library chime — cascade + copy→remove convergence ───
  test("rename — referenced chime cascades through schedule + group and the file converges", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    // Seeded schedule + group both reference Sparkle.wav.
    await expect(page.locator("[data-testid=library-row]")).toHaveCount(2);
    await expect(page.locator("[data-testid=schedule-item]")).toContainText("Sparkle.wav");
    await expect(page.locator(".group-chimes .chime-tag")).toHaveText("Sparkle.wav");

    const sparkleRow = page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" });
    await sparkleRow.locator("[data-testid=library-rename]").click();
    const input = sparkleRow.locator("input[aria-label='Rename Sparkle.wav']");
    await expect(input).toBeVisible();
    await input.fill("Bell");
    await sparkleRow.locator("form button[type=submit]").click();

    // The rename POST carried {from,to} (with the auto-appended .wav).
    await expect.poll(() => cap.libraryRename.length).toBe(1);
    expect(cap.libraryRename[0]).toEqual({ from: "Sparkle.wav", to: "Bell.wav" });

    // Drive the copy→remove convergence polling (2s interval).
    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);

    // The library now holds the new name only; the old source is gone.
    await expect(
      page.locator("[data-testid=library-row]", { hasText: "Bell.wav" }),
    ).toHaveCount(1);
    await expect(
      page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" }),
    ).toHaveCount(0);
    await expect(page.locator("[data-testid=library-notice]")).toContainText("renamed");

    // The cascade is reflected in the UI: schedule + group now point at Bell.wav.
    await expect(page.locator("[data-testid=schedule-item]")).toContainText("Bell.wav");
    await expect(page.locator("[data-testid=schedule-item]")).not.toContainText("Sparkle.wav");
    await expect(page.locator(".group-chimes .chime-tag")).toHaveText("Bell.wav");

    // The source cleanup DELETE is file-only (cascade=false) — it must NOT
    // re-scrub the references the rename just moved. Asserted after convergence
    // so the async cleanup request is guaranteed recorded.
    await expect
      .poll(() =>
        probe.requests.some(
          (r) =>
            r.method === "DELETE" &&
            r.url.includes("/library/Sparkle.wav") &&
            r.url.includes("cascade=false"),
        ),
      )
      .toBe(true);

    assertCleanConsole(probe);
  });

  test("rename — inline editor renders and captures perf + screenshot", async ({
    page,
    probe,
  }, testInfo) => {
    await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    const sparkleRow = page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" });
    await sparkleRow.locator("[data-testid=library-rename]").click();
    await expect(sparkleRow.locator("input[aria-label='Rename Sparkle.wav']")).toBeVisible();
    await expect(sparkleRow.locator("form button[type=submit]")).toHaveText("Save");

    await capturePerf(page, testInfo, "chime-rename");
    await captureScreenshot(page, testInfo, "chime-rename");
    assertCleanConsole(probe);
  });

  test("rename — client-side validation rejects illegal characters and duplicates", async ({
    page,
    probe,
  }) => {
    const { cap } = await installScheduler(page, populatedSnapshot());
    await gotoScheduler(page);

    const sparkleRow = page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" });
    await sparkleRow.locator("[data-testid=library-rename]").click();
    const input = sparkleRow.locator("input[aria-label='Rename Sparkle.wav']");

    // Illegal character → inline error, no network call.
    await input.fill("bad/name");
    await sparkleRow.locator("form button[type=submit]").click();
    await expect(page.locator("[data-testid=library-error]")).toContainText("can’t include");
    expect(cap.libraryRename).toEqual([]);

    // Collision with an existing library name → inline error, still no call.
    await input.fill("Chime2");
    await sparkleRow.locator("form button[type=submit]").click();
    await expect(page.locator("[data-testid=library-error]")).toContainText("already exists");
    expect(cap.libraryRename).toEqual([]);

    assertCleanConsole(probe);
  });

  // ── Gate 13: delete cascade — removing a referenced chime scrubs the rest ──
  test("delete cascade — removing a referenced chime drops its schedule and empties its group", async ({
    page,
    probe,
  }) => {
    await page.clock.install({ time: new Date("2024-01-01T00:00:00Z") });
    const { cap } = await installScheduler(
      page,
      populatedSnapshot(),
      undefined,
      undefined,
      // Model webd's DEFAULT (cascade=true) delete: drop the file, delete every
      // schedule that referenced it, scrub it from groups, and delete any group
      // left empty.
      (filename, snap) => {
        const ciEq = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();
        snap.library = snap.library.filter((c) => !ciEq(c.filename, filename));
        snap.schedules = snap.schedules.filter((s) => !ciEq(s.chimeFilename, filename));
        for (const g of snap.groups) g.chimes = g.chimes.filter((c) => !ciEq(c, filename));
        snap.groups = snap.groups.filter((g) => g.chimes.length > 0);
      },
    );
    await gotoScheduler(page);

    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(1);
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(1);

    const sparkleRow = page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" });
    await sparkleRow.locator("[data-testid=library-delete]").click();
    expect(cap.libraryDelete).toEqual(["Sparkle.wav"]);

    // Converge: the row leaves the catalog and the snapshot refetch shows the
    // dependent schedule deleted and the now-empty group gone.
    await page.clock.fastForward(2001);
    await page.clock.fastForward(2001);

    await expect(
      page.locator("[data-testid=library-row]", { hasText: "Sparkle.wav" }),
    ).toHaveCount(0);
    await expect(page.locator("[data-testid=schedule-item]")).toHaveCount(0);
    await expect(page.locator("[data-testid=schedules-empty]")).toBeVisible();
    await expect(page.locator("[data-testid=group-card]")).toHaveCount(0);
    await expect(page.locator("[data-testid=groups-empty]")).toBeVisible();
    assertCleanConsole(probe);
  });
});
