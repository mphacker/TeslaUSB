/**
 * Typed client for the webd catalog API (contract D2).
 *
 * webd is **read-only** except for the operator-gated `gadgetd` eject-handoff
 * mutations: the car-visible clip delete (`DELETE /api/clips/:id?target=car`,
 * §2.3) and lock-chime management (`POST /api/chimes`, `DELETE /api/chimes/:id`,
 * §2.3.1). Every other method is a GET. Errors surface as {@link ApiError}
 * carrying the server's `{code, message}` envelope when present (and the HTTP
 * `status`, which callers use to tell a transient `409` from a terminal `422`).
 * All paths are same-origin (`/api/...`) because the bundle is served by webd
 * itself; in dev, Vite proxies `/api` to webd.
 */
import type {
  Analytics,
  ApiErrorBody,
  Chimes,
  Clip,
  DaySummary,
  EventItem,
  Page,
  Pref,
  StorageHealth,
  StorageInfo,
  SystemHealth,
  SystemMetrics,
  Trip,
  TripDetail,
} from "./types";

/** An error from a webd API call (non-2xx, network, or malformed body). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== "",
  ) as [string, string | number][];
  if (entries.length === 0) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of entries) sp.set(k, String(v));
  return `?${sp.toString()}`;
}

async function request<T>(
  method: string,
  path: string,
  signal?: AbortSignal,
  reqBody?: BodyInit,
): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      // NOTE: never set Content-Type for a FormData body — the browser must
      // supply the multipart boundary itself, so we only declare Accept.
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal,
      ...(reqBody !== undefined ? { body: reqBody } : {}),
    });
  } catch (err) {
    throw new ApiError(0, "network", (err as Error).message || "network error");
  }
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      if (!resp.ok) {
        throw new ApiError(resp.status, "http_error", `HTTP ${resp.status}`);
      }
      throw new ApiError(resp.status, "bad_json", "malformed JSON response");
    }
  }
  if (!resp.ok) {
    const env = body as ApiErrorBody | null;
    const code = env?.error?.code ?? "http_error";
    const message = env?.error?.message ?? `HTTP ${resp.status}`;
    throw new ApiError(resp.status, code, message);
  }
  return body as T;
}

function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>("GET", path, signal);
}

/** Terminal result of a successful car-delete handoff (`200 {handoff_id, state}`). */
export interface DeleteClipResult {
  handoff_id: string;
  /** Always `"done"` for a 200; any other value is an unexpected protocol state. */
  state: string;
}

/**
 * Terminal result of a successful lock-chime install/remove handoff
 * (`200 {handoff_id, state:"done"}`). Both routes block on the gadgetd
 * eject-handoff and return the terminal state synchronously, exactly like the
 * clip-delete mutation.
 */
export interface ChimeHandoffResult {
  handoff_id: string;
  state: string;
}

/** The single lock-chime slot id on the p2 MEDIA partition (B-1 is single-slot). */
export const LOCK_CHIME_ID = "LockChime";

/** Logical lock-chime size cap mirrored from webd's `CHIME_MAX_BYTES` (1 MiB). */
export const CHIME_MAX_BYTES = 1024 * 1024;

export interface EventsParams {
  after?: number;
  limit?: number;
  trip?: number;
}

export interface ClipsParams {
  after?: number;
  limit?: number;
  folder_class?: string;
}

export const api = {
  days: (signal?: AbortSignal) => getJson<DaySummary[]>("/api/days", signal),

  trips: (day?: string, signal?: AbortSignal) =>
    getJson<Trip[]>(`/api/trips${qs({ day })}`, signal),

  trip: (id: number, signal?: AbortSignal) =>
    getJson<TripDetail>(`/api/trips/${id}`, signal),

  events: (params: EventsParams = {}, signal?: AbortSignal) =>
    getJson<Page<EventItem>>(`/api/events${qs({ ...params })}`, signal),

  clips: (params: ClipsParams = {}, signal?: AbortSignal) =>
    getJson<Page<Clip>>(`/api/clips${qs({ ...params })}`, signal),

  clip: (id: number, signal?: AbortSignal) =>
    getJson<Clip>(`/api/clips/${id}`, signal),

  analytics: (signal?: AbortSignal) =>
    getJson<Analytics>("/api/analytics", signal),

  settings: (signal?: AbortSignal) => getJson<Pref[]>("/api/settings", signal),

  // Device-status reads (webd 5.1d). All read-only; handlers never 5xx and
  // degrade to unknown/null when a subsystem can't be probed honestly.
  systemHealth: (signal?: AbortSignal) =>
    getJson<SystemHealth>("/api/system/health", signal),

  systemMetrics: (signal?: AbortSignal) =>
    getJson<SystemMetrics>("/api/system/metrics", signal),

  storage: (signal?: AbortSignal) => getJson<StorageInfo>("/api/storage", signal),

  storageHealth: (signal?: AbortSignal) =>
    getJson<StorageHealth>("/api/storage/health", signal),

  /**
   * Read which lock chime is installed on the p2 MEDIA partition
   * (`GET /api/chimes`). Read-only: routed through the scannerd→indexd→webd
   * catalog, NOT the gadgetd eject-handoff. `installed` is null when nothing is
   * installed or the catalog predates the media schema (webd never 5xx's here).
   */
  chimes: (signal?: AbortSignal) => getJson<Chimes>("/api/chimes", signal),

  // Media URL builders (webd 5.1b). These return same-origin URLs assigned to
  // a native `<video src>` / download anchor; the browser does the byte-range
  // streaming. They are NOT fetched through getJson (the bytes aren't JSON).
  streamUrl: (id: number, camera?: string) =>
    `/api/clips/${id}/stream${qs({ camera })}`,

  exportUrl: (id: number) => `/api/clips/${id}/export`,

  downloadUrl: (id: number, camera: string) =>
    `/api/clips/${id}/angles/${encodeURIComponent(camera)}/download`,

  /**
   * Delete a clip's car-visible (Tesla USB) copy via the `gadgetd` eject-handoff
   * (contract §2.3, the **only** mutation webd exposes). `target=car` is baked in
   * so a caller can never omit it — the server 400-rejects an absent target (no
   * destructive default), and `car` is the only implemented target (archive/both
   * → 501). webd blocks on gadgetd and returns the **terminal** state
   * synchronously: a `200 {handoff_id, state:"done"}` means the delete completed.
   * Any other 2xx shape is treated as an unexpected protocol state (not a
   * completed delete). Transient refusals surface as `ApiError` with `status 409`
   * (retryable); validation refusals as `422` (terminal). Throws on abort.
   */
  deleteClip: async (
    id: number,
    signal?: AbortSignal,
  ): Promise<DeleteClipResult> => {
    const res = await request<DeleteClipResult>(
      "DELETE",
      `/api/clips/${id}?target=car`,
      signal,
    );
    if (!res || res.state !== "done") {
      throw new ApiError(
        502,
        "gadgetd_protocol",
        `unexpected delete state: ${res?.state ?? "<none>"}`,
      );
    }
    return res;
  },

  /**
   * Install (or replace) the lock chime on the p2 MEDIA partition by POSTing a
   * finished WAV as multipart `file` (contract §2.3.1). webd validates the WAV
   * (RIFF/WAVE, PCM 16-bit, mono/stereo, 44.1/48 kHz, ≤1 MiB) and routes the
   * staged file through the `gadgetd` eject-handoff that momentarily ejects the
   * USB drive from the live vehicle, returning the **terminal** state
   * synchronously: `200 {handoff_id, state:"done"}`. Any other 2xx shape is an
   * unexpected protocol state. Transient refusals surface as `ApiError` with
   * `status 409` (`handoff_busy`, retryable) or `503` (`gadgetd_unavailable`);
   * validation refusals as `422` (`invalid_wav`/`chime_too_large`) and `400`
   * (`upload_required`/`duplicate_field`/`invalid_multipart`); failures as `502`
   * (`handoff_failed`) / `500` (`critical_fault`/`staging_failed`). Throws on abort.
   */
  installChime: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<ChimeHandoffResult> => {
    const form = new FormData();
    // The field name MUST be `file`; preserve a filename so webd's multipart
    // reader sees a file part (not a plain text field).
    form.append("file", file, "name" in file ? file.name : "LockChime.wav");
    const res = await request<ChimeHandoffResult>(
      "POST",
      "/api/chimes",
      signal,
      form,
    );
    if (!res || res.state !== "done") {
      throw new ApiError(
        502,
        "gadgetd_protocol",
        `unexpected install state: ${res?.state ?? "<none>"}`,
      );
    }
    return res;
  },

  /**
   * Remove the installed lock chime via the same `gadgetd` eject-handoff
   * (`DELETE /api/chimes/LockChime`, contract §2.3.1). The id is baked in
   * because B-1 is single-slot — any other id is a `404`. Idempotent: removing
   * when nothing is installed still returns `200 {handoff_id, state:"done"}`.
   * Same terminal/transient status mapping as {@link installChime}. Throws on abort.
   */
  removeChime: async (signal?: AbortSignal): Promise<ChimeHandoffResult> => {
    const res = await request<ChimeHandoffResult>(
      "DELETE",
      `/api/chimes/${LOCK_CHIME_ID}`,
      signal,
    );
    if (!res || res.state !== "done") {
      throw new ApiError(
        502,
        "gadgetd_protocol",
        `unexpected remove state: ${res?.state ?? "<none>"}`,
      );
    }
    return res;
  },
};

export type Api = typeof api;
