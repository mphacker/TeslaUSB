import { expect, test, type Probe } from "./helpers";
import type { Page, Route } from "@playwright/test";

function jsonRoute(route: Route, status: number, body: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function buildWav({
  durationSeconds,
  channels = 2,
  sampleRate = 44_100,
}: {
  durationSeconds: number;
  channels?: number;
  sampleRate?: number;
}) {
  const frames = Math.max(1, Math.floor(durationSeconds * sampleRate));
  const dataLen = frames * channels * 2;
  const buffer = Buffer.alloc(44 + dataLen);
  buffer.write("RIFF", 0, "ascii");
  buffer.writeUInt32LE(36 + dataLen, 4);
  buffer.write("WAVE", 8, "ascii");
  buffer.write("fmt ", 12, "ascii");
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(channels, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * channels * 2, 28);
  buffer.writeUInt16LE(channels * 2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36, "ascii");
  buffer.writeUInt32LE(dataLen, 40);
  let offset = 44;
  for (let i = 0; i < frames; i++) {
    const wave = Math.sin((i / sampleRate) * Math.PI * 2 * 440);
    for (let ch = 0; ch < channels; ch++) {
      const sample = ch === 0 ? wave : -wave * 0.8;
      const int16 = Math.max(-32768, Math.min(32767, Math.round(sample * 10_000)));
      buffer.writeInt16LE(int16, offset);
      offset += 2;
    }
  }
  return buffer;
}

async function gotoMedia(page: Page) {
  await page.goto("/media", { waitUntil: "load" });
  await expect(page.locator(".container[data-screen=media]")).toBeVisible();
  await page.waitForFunction(() => {
    const hooks = (
      window as unknown as { __TESLAUSB_MEDIA_HOOKS__?: { screen: string } }
    ).__TESLAUSB_MEDIA_HOOKS__;
    return hooks?.screen === "lock-chimes";
  });
}

async function installRoutes(
  page: Page,
  onUpload: (multipartBody: Buffer) => void = () => {},
) {
  await page.route("**/api/chimes", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return jsonRoute(route, 200, { installed: null });
  });
  await page.route("**/api/chime-scheduler", (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return jsonRoute(route, 200, {
      schedules: [],
      groups: [],
      randomMode: { enabled: false },
      library: [],
      menus: {
        holidays: [],
        intervals: ["on_boot"],
        weekdays: ["Monday"],
      },
    });
  });
  await page.route("**/api/chime-scheduler/library", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataBuffer();
    onUpload(body ?? Buffer.alloc(0));
    return jsonRoute(route, 202, { state: "queued", job_id: "job-editor" });
  });
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

test("single-file selection opens editor, supports trim controls, and uploads WAV once", async ({
  page,
  probe,
}) => {
  let uploadCount = 0;
  let postedBody = Buffer.alloc(0);
  await installRoutes(page, (body) => {
    uploadCount += 1;
    postedBody = body;
  });

  await gotoMedia(page);
  await page.locator("[data-testid=chime-file-input]").setInputFiles({
    name: "LongSource.wav",
    mimeType: "audio/wav",
    buffer: buildWav({ durationSeconds: 8, channels: 2 }),
  });

  await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-editor-waveform]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-stat-status]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-trim-start]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-trim-end]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-normalize]")).toBeChecked();
  await expect(page.locator("[data-testid=chime-normalize-preset]")).toHaveValue("1");
  await expect(page.locator("[data-testid=chime-editor-upload]")).toBeVisible();

  const endSlider = page.locator("[data-testid=chime-trim-end]");
  await endSlider.evaluate((node) => {
    const input = node as HTMLInputElement;
    input.value = input.max;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator("[data-testid=chime-stat-status]")).toContainText("Over limit");

  await page.locator("[data-testid=chime-editor-autofit]").click();
  await expect(page.locator("[data-testid=chime-stat-status]")).toHaveText(/Ready/);
  await expect(page.locator("[data-testid=chime-stat-size]")).toContainText(/KiB|MiB|B/);

  await page.locator("[data-testid=chime-normalize-preset]").evaluate((node) => {
    const input = node as HTMLInputElement;
    input.value = "3";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator(".chime-editor-preset-details")).toContainText("Maximum");

  // V1-parity: four loudness tick labels under the slider, active one highlighted.
  const ticks = page.locator(".chime-editor-preset-tick");
  await expect(ticks).toHaveCount(4);
  await expect(ticks).toHaveText(["Broadcast", "Streaming", "Loud", "Maximum"]);
  await expect(page.locator(".chime-editor-preset-tick.active")).toHaveText("Maximum");

  await page.locator("[data-testid=chime-editor-filename]").fill("bad/name");
  await page
    .locator("[data-testid=chime-editor-upload]")
    .click({ force: true });
  await expect(page.locator(".chime-upload-status.fatal")).toContainText(
    "Path separators are not allowed",
  );

  await page.locator("[data-testid=chime-editor-filename]").fill("Edited Chime");
  const uploadButton = page.locator("[data-testid=chime-editor-upload]");
  await page
    .locator("[data-testid=chime-editor-upload]")
    .click({ force: true });
  await expect(uploadButton).toBeDisabled();
  await expect(page.locator("[data-testid=chime-notice]")).toHaveText(
    "Upload accepted — syncing “Edited Chime.wav” to your chime library below…",
  );

  expect(uploadCount, "double-submit should be prevented").toBe(1);
  expect(postedBody.toString("latin1")).toContain('filename="Edited Chime.wav"');
  expect(postedBody.includes(Buffer.from("RIFF", "ascii"))).toBe(true);
  assertCleanConsole(probe);
});

test("two-file selection keeps batch list and no editor; switching 1↔multi tears down cleanly", async ({
  page,
  probe,
}) => {
  await installRoutes(page);
  await gotoMedia(page);

  await page.locator("[data-testid=chime-file-input]").setInputFiles([
    {
      name: "First.wav",
      mimeType: "audio/wav",
      buffer: buildWav({ durationSeconds: 2 }),
    },
    {
      name: "Second.wav",
      mimeType: "audio/wav",
      buffer: buildWav({ durationSeconds: 2 }),
    },
  ]);

  await expect(page.locator("[data-testid=chime-upload-staged]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-staged-row]")).toHaveCount(2);
  await expect(page.locator("[data-testid=chime-editor]")).toHaveCount(0);

  await page.locator("[data-testid=chime-file-input]").setInputFiles({
    name: "Single.wav",
    mimeType: "audio/wav",
    buffer: buildWav({ durationSeconds: 3 }),
  });
  await expect(page.locator("[data-testid=chime-editor]")).toBeVisible();
  await expect(page.locator("[data-testid=chime-editor-waveform]")).toBeVisible();
  await page.locator("[data-testid=chime-editor-cancel]").click({ force: true });
  await expect(page.locator("[data-testid=chime-editor]")).toHaveCount(0);
  await expect(page.locator("[data-testid=chime-upload-staged]")).toHaveCount(0);

  assertCleanConsole(probe);
});
