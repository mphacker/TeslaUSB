import { test as base, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const ARTIFACTS = resolve(HERE, "artifacts");

export interface UatState {
  baseURL: string;
  pid: number;
  seedDb: string;
  dist: string;
  buildId: string;
  jsAsset: string;
  cssAsset: string;
}

/** Per-run handshake written by global-setup (built bundle id, hashed assets). */
export function loadState(): UatState {
  return JSON.parse(readFileSync(resolve(ARTIFACTS, "uat-state.json"), "utf8"));
}

/** Captures everything the UAT gates assert against: console, page errors,
 *  and every network request/response the page made. Listeners attach before
 *  the test body navigates, so nothing is missed. */
export interface Probe {
  consoleErrors: { text: string; location: string }[];
  consoleWarnings: { text: string; location: string }[];
  pageErrors: string[];
  requests: { method: string; url: string }[];
  responses: { url: string; status: number; contentType: string }[];
  failedRequests: { url: string; failure: string }[];
}

export const GADGET_STATUS_OK = {
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

export const test = base.extend<{ probe: Probe }>({
  probe: async ({ page }, use) => {
    const probe: Probe = {
      consoleErrors: [],
      consoleWarnings: [],
      pageErrors: [],
      requests: [],
      responses: [],
      failedRequests: [],
    };
    page.on("console", (m) => {
      const loc = m.location();
      const where = loc ? `${loc.url}:${loc.lineNumber}` : "";
      if (m.type() === "error") probe.consoleErrors.push({ text: m.text(), location: where });
      else if (m.type() === "warning")
        probe.consoleWarnings.push({ text: m.text(), location: where });
    });
    page.on("pageerror", (e) => probe.pageErrors.push(e.message ?? String(e)));
    page.on("request", (r) => probe.requests.push({ method: r.method(), url: r.url() }));
    page.on("response", (r) =>
      probe.responses.push({
        url: r.url(),
        status: r.status(),
        contentType: r.headers()["content-type"] ?? "",
      }),
    );
    page.on("requestfailed", (r) =>
      probe.failedRequests.push({
        url: r.url(),
        failure: r.failure()?.errorText ?? "unknown",
      }),
    );
    await page.route("**/api/gadget/status", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(GADGET_STATUS_OK),
      }),
    );
    await page.route("**/api/cloud", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          configured: false,
          provider_type: null,
          uploader_state: "unknown",
          sync_now_state: "unavailable",
        }),
      }),
    );
    await page.route("**/api/cloud/queue*", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null, limit: 16 }),
      }),
    );
    await page.route("**/api/cloud/history*", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], next_cursor: null, limit: 16 }),
      }),
    );
    await page.route("**/api/retention/status", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          governor: null,
          candidate_count: 0,
          candidate_count_truncated: false,
          estimated_reclaimable_bytes: 0,
          estimated_reclaimable_bytes_truncated: false,
          operator_signal: {
            status: "unavailable",
            free_frac: null,
            target_exit_frac: null,
            pressure_below_target_exit: null,
            retention_non_progress: null,
            no_eligible_candidates: null,
            stop_indicates_no_progress: null,
          },
          exclusion_report: null,
          recent_cleanup: [],
          recent_cleanup_truncated: false,
          cloud_durability_required: false,
          cloud_durability_disclosure: "Cloud durability is not required.",
        }),
      }),
    );
    await page.route("**/api/retention/policy", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "unavailable", snapshot: null }),
      }),
    );
    await use(probe);
  },
});

export { expect };

/** Read APIs the settings-dashboard screen is permitted to call. webd is
 *  read-only; anything outside this set (or any non-GET) is a hard failure.
 *  The dashboard reads /api/settings (config-form bindings), /api/clips (Video
 *  Indexer enrichment), the three read-only device-status probes (5.1d), and
 *  the read-only Wi-Fi
 *  probes (`/api/wifi/status`, `/api/wifi/networks`).
 *  The full catalog client is exercised separately by api-client.spec.ts. */
export const ALLOWED_API = new Set([
  "/api/settings",
  "/api/clips",
  "/api/system/health",
  "/api/system/metrics",
  "/api/storage/health",
  "/api/fsck/status",
  "/api/fsck/history",
  "/api/fsck/last-check/1",
  "/api/fsck/last-check/2",
  "/api/fsck/last-check/3",
  "/api/wifi/status",
  "/api/wifi/networks",
  "/api/gadget/status",
  "/api/gadget/mode-status",
]);
