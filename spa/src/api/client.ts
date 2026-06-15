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
  ChimeGroup,
  Chimes,
  Clip,
  DaySummary,
  EventItem,
  GadgetStatus,
  GroupInput,
  MediaHandoffResult,
  MediaList,
  Page,
  Pref,
  RandomMode,
  ScheduleInput,
  SchedulerSnapshot,
  StorageHealth,
  StorageInfo,
  StoredSchedule,
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
  contentType?: string,
): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      // NOTE: never set Content-Type for a FormData body — the browser must
      // supply the multipart boundary itself. A JSON body, however, needs an
      // explicit `application/json` so axum's `Json<T>` extractor accepts it.
      headers: {
        Accept: "application/json",
        ...(contentType ? { "Content-Type": contentType } : {}),
      },
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

function mediaContentUrl(path: string, version?: string | null): string {
  return `/api/media/content?path=${encodeURIComponent(path)}${version ? `&v=${encodeURIComponent(version)}` : ""}`;
}

/**
 * `POST <path>` a bulk-delete batch as `{ names: [...] }` JSON and assert the
 * gadgetd handoff reached its terminal `done` state. Shared by every media
 * category's `bulkDelete*` method — the route refactor made all five endpoints
 * (`/api/<cat>/bulk-delete`) accept the identical body and return the same
 * `MediaHandoffResult`.
 */
async function bulkDelete(
  path: string,
  names: string[],
  signal?: AbortSignal,
): Promise<MediaHandoffResult> {
  const res = await request<MediaHandoffResult>(
    "POST",
    path,
    signal,
    JSON.stringify({ names }),
    "application/json",
  );
  return assertAccepted(res, "bulk-delete");
}

/** Terminal result of a successful car-delete handoff (`200 {handoff_id, state}`). */
export interface DeleteClipResult {
  handoff_id: string;
  /** Always `"done"` for a 200; any other value is an unexpected protocol state. */
  state: string;
}

/**
 * Assert a media mutation response reached a webd-accepted state. The
 * frictionless write path means webd either applies the change synchronously
 * (`state:"done"`) or — far more often, because the car is usually connected —
 * accepts it into gadgetd's durable mutation queue and answers `202
 * {state:"queued", job_id}`. BOTH are success from the UI's point of view:
 * "done" is already-applied, "queued" is saved-and-syncing. Any other shape is
 * an unexpected protocol state. Returns the response so callers can chain.
 */
function assertAccepted<T extends { state?: string }>(
  res: T | undefined,
  verb: string,
): T {
  if (!res || (res.state !== "done" && res.state !== "queued")) {
    throw new ApiError(
      502,
      "gadgetd_protocol",
      `unexpected ${verb} state: ${res?.state ?? "<none>"}`,
    );
  }
  return res;
}

/**
 * True when a media mutation was accepted into gadgetd's durable queue
 * (`202 {state:"queued"}`) rather than applied synchronously. The UI uses this
 * to show "saved — syncing to the car" instead of "done".
 */
export function isQueued(
  res: { state?: string } | null | undefined,
): boolean {
  return res?.state === "queued";
}

/**
 * Result of a successful lock-chime install/remove. With the frictionless write
 * path the change is normally accepted into gadgetd's durable queue and
 * answered `202 {state:"queued", job_id}` (saved-and-syncing); on the rare
 * synchronous path it is `200 {handoff_id, state:"done"}` (already applied).
 */
export interface ChimeHandoffResult {
  /** Present only on the synchronous `"done"` path. */
  handoff_id?: string;
  /** `"done"` (applied synchronously) or `"queued"` (accepted into the durable queue, a 202). */
  state: string;
  /** gadgetd queue entry id; present only on the `"queued"` path. */
  job_id?: string;
}

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
   * Live USB-gadget state (`GET /api/gadget/status`) from gadgetd's control
   * socket — present/bound/udc + both LUN backing files. Distinct from the
   * catalog reads above: this CAN throw {@link ApiError} (503 gadgetd-down /
   * 502 protocol) because it talks to a live daemon socket. Callers render
   * those as "USB status unavailable", never a crash.
   */
  gadgetStatus: (signal?: AbortSignal) =>
    getJson<GadgetStatus>("/api/gadget/status", signal),

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
    return assertAccepted(res, "install");
  },

  // ── Toybox media categories (GET = catalog read; POST/DELETE = gadgetd handoff) ──

  /** List installed boombox horn sounds (`GET /api/boombox`). */
  boombox: (signal?: AbortSignal) =>
    getJson<MediaList>("/api/boombox", signal),

  /** Install a boombox horn sound (`POST /api/boombox`, multipart `file`). */
  installBoombox: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "horn.wav");
    const res = await request<MediaHandoffResult>(
      "POST",
      "/api/boombox",
      signal,
      form,
    );
    return assertAccepted(res, "install");
  },

  /** Remove a boombox horn sound by file name (`DELETE /api/boombox/:name`). */
  removeBoombox: async (
    name: string,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const res = await request<MediaHandoffResult>(
      "DELETE",
      `/api/boombox/${encodeURIComponent(name)}`,
      signal,
    );
    return assertAccepted(res, "remove");
  },

  /** List installed music files (`GET /api/music`). */
  music: (signal?: AbortSignal) => getJson<MediaList>("/api/music", signal),

  /** Install a music file (`POST /api/music`, multipart `file`). */
  installMusic: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "track.mp3");
    const res = await request<MediaHandoffResult>(
      "POST",
      "/api/music",
      signal,
      form,
    );
    return assertAccepted(res, "install");
  },

  /** Remove a music file by name (`DELETE /api/music/:name`). */
  removeMusic: async (
    name: string,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const res = await request<MediaHandoffResult>(
      "DELETE",
      `/api/music/${encodeURIComponent(name)}`,
      signal,
    );
    return assertAccepted(res, "remove");
  },

  /** List installed light shows (`GET /api/lightshows`; wraps are a separate category). */
  lightshows: (signal?: AbortSignal) =>
    getJson<MediaList>("/api/lightshows", signal),

  /** Install a light show file (`POST /api/lightshows`, multipart `file`). */
  installLightshow: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "show.fseq");
    const res = await request<MediaHandoffResult>(
      "POST",
      "/api/lightshows",
      signal,
      form,
    );
    return assertAccepted(res, "install");
  },

  /** Remove a light show file by name (`DELETE /api/lightshows/:name`). */
  removeLightshow: async (
    name: string,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const res = await request<MediaHandoffResult>(
      "DELETE",
      `/api/lightshows/${encodeURIComponent(name)}`,
      signal,
    );
    return assertAccepted(res, "remove");
  },

  /** List installed license plate images (`GET /api/plates`). */
  plates: (signal?: AbortSignal) => getJson<MediaList>("/api/plates", signal),

  /** Install a license plate PNG (`POST /api/plates`, multipart `file`). */
  installPlate: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "plate.png");
    const res = await request<MediaHandoffResult>(
      "POST",
      "/api/plates",
      signal,
      form,
    );
    return assertAccepted(res, "install");
  },

  /** Remove a license plate image by name (`DELETE /api/plates/:name`). */
  removePlate: async (
    name: string,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const res = await request<MediaHandoffResult>(
      "DELETE",
      `/api/plates/${encodeURIComponent(name)}`,
      signal,
    );
    return assertAccepted(res, "remove");
  },

  /** List installed wrap images (`GET /api/wraps`). */
  wraps: (signal?: AbortSignal) => getJson<MediaList>("/api/wraps", signal),

  /** Install a wrap PNG (`POST /api/wraps`, multipart `file`). */
  installWrap: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "wrap.png");
    const res = await request<MediaHandoffResult>(
      "POST",
      "/api/wraps",
      signal,
      form,
    );
    return assertAccepted(res, "install");
  },

  /** Remove a wrap image by name (`DELETE /api/wraps/:name`). */
  removeWrap: async (
    name: string,
    signal?: AbortSignal,
  ): Promise<MediaHandoffResult> => {
    const res = await request<MediaHandoffResult>(
      "DELETE",
      `/api/wraps/${encodeURIComponent(name)}`,
      signal,
    );
    return assertAccepted(res, "remove");
  },

  /**
   * Bulk-delete media in ONE gadgetd handoff (`POST /api/<cat>/bulk-delete`,
   * body `{ names }`). A single eject/remount cycle removes the whole batch,
   * unlike calling `remove*` N times. Each `bulkDelete*` returns the terminal
   * `MediaHandoffResult` of that handoff.
   */
  bulkDeleteBoombox: (names: string[], signal?: AbortSignal) =>
    bulkDelete("/api/boombox/bulk-delete", names, signal),
  bulkDeleteMusic: (names: string[], signal?: AbortSignal) =>
    bulkDelete("/api/music/bulk-delete", names, signal),
  bulkDeleteLightshows: (names: string[], signal?: AbortSignal) =>
    bulkDelete("/api/lightshows/bulk-delete", names, signal),
  bulkDeletePlates: (names: string[], signal?: AbortSignal) =>
    bulkDelete("/api/plates/bulk-delete", names, signal),
  bulkDeleteWraps: (names: string[], signal?: AbortSignal) =>
    bulkDelete("/api/wraps/bulk-delete", names, signal),

  // ── Chime scheduler (webd proxies these to schedulerd) ──

  /**
   * Read the full scheduler snapshot (`GET /api/chime-scheduler`): schedules,
   * groups, random-on-boot mode, the chime library, and the form menus — one
   * round-trip so the page bootstraps without a waterfall. Throws {@link ApiError}
   * (503 scheduler-down / 502 protocol) when schedulerd is unreachable.
   */
  scheduler: (signal?: AbortSignal) =>
    getJson<SchedulerSnapshot>("/api/chime-scheduler", signal),

  /** Create a schedule (`POST /api/chime-scheduler/schedules`). 422 on invalid input. */
  addSchedule: (input: ScheduleInput, signal?: AbortSignal) =>
    request<StoredSchedule>(
      "POST",
      "/api/chime-scheduler/schedules",
      signal,
      JSON.stringify(input),
      "application/json",
    ),

  /** Replace a schedule by id (`PUT /api/chime-scheduler/schedules/:id`). */
  updateSchedule: (id: string, input: ScheduleInput, signal?: AbortSignal) =>
    request<StoredSchedule>(
      "PUT",
      `/api/chime-scheduler/schedules/${encodeURIComponent(id)}`,
      signal,
      JSON.stringify(input),
      "application/json",
    ),

  /** Delete a schedule by id (`DELETE /api/chime-scheduler/schedules/:id`). */
  deleteSchedule: (id: string, signal?: AbortSignal) =>
    request<{ ok?: boolean }>(
      "DELETE",
      `/api/chime-scheduler/schedules/${encodeURIComponent(id)}`,
      signal,
    ),

  /** Create a chime group (`POST /api/chime-scheduler/groups`). */
  addGroup: (input: GroupInput, signal?: AbortSignal) =>
    request<ChimeGroup>(
      "POST",
      "/api/chime-scheduler/groups",
      signal,
      JSON.stringify(input),
      "application/json",
    ),

  /** Replace a group by id (`PUT /api/chime-scheduler/groups/:id`). */
  updateGroup: (id: string, input: GroupInput, signal?: AbortSignal) =>
    request<ChimeGroup>(
      "PUT",
      `/api/chime-scheduler/groups/${encodeURIComponent(id)}`,
      signal,
      JSON.stringify(input),
      "application/json",
    ),

  /** Delete a group by id (`DELETE /api/chime-scheduler/groups/:id`). */
  deleteGroup: (id: string, signal?: AbortSignal) =>
    request<{ ok?: boolean }>(
      "DELETE",
      `/api/chime-scheduler/groups/${encodeURIComponent(id)}`,
      signal,
    ),

  /** Set the random-on-boot configuration (`PUT /api/chime-scheduler/random-mode`). */
  setRandomMode: (mode: RandomMode, signal?: AbortSignal) =>
    request<RandomMode>(
      "PUT",
      "/api/chime-scheduler/random-mode",
      signal,
      JSON.stringify(mode),
      "application/json",
    ),

  /**
   * Upload a WAV into the chime library (`POST /api/chime-scheduler/library`,
   * multipart `file`). Like every frictionless write, webd accepts the file into
   * gadgetd's durable queue and answers `202 {state:"queued", job_id}` — the
   * response carries NO filename/size, so callers must use the client-known file
   * identity (name + size) for any follow-up matching. Throws on abort or an
   * unexpected protocol state.
   */
  uploadLibraryChime: async (
    file: File | Blob,
    signal?: AbortSignal,
  ): Promise<ChimeHandoffResult> => {
    const form = new FormData();
    form.append("file", file, "name" in file ? file.name : "chime.wav");
    const res = await request<ChimeHandoffResult>(
      "POST",
      "/api/chime-scheduler/library",
      signal,
      form,
    );
    return assertAccepted(res, "upload");
  },

  /** Remove a library chime by filename (`DELETE /api/chime-scheduler/library/:filename`). */
  deleteLibraryChime: (filename: string, signal?: AbortSignal) =>
    request<{ ok?: boolean }>(
      "DELETE",
      `/api/chime-scheduler/library/${encodeURIComponent(filename)}`,
      signal,
    ),

  /**
   * Promote a library chime to the car's active `LockChime.wav`
   * (`POST /api/chime-scheduler/library/:filename/activate`). webd reads the
   * library file, re-validates the WAV, and routes it through the SAME
   * `gadgetd` eject-handoff as {@link installChime} — so it returns the same
   * `202 {state:"queued"}` / `200 {state:"done"}` shape and applies at the next
   * safe window. Throws on abort.
   */
  setActiveChime: async (
    filename: string,
    signal?: AbortSignal,
  ): Promise<ChimeHandoffResult> => {
    const res = await request<ChimeHandoffResult>(
      "POST",
      `/api/chime-scheduler/library/${encodeURIComponent(filename)}/activate`,
      signal,
    );
    return assertAccepted(res, "install");
  },

  /** Root-relative URL for inline playback of a library chime (`GET …/library/:filename/audio`). */
  libraryAudioUrl: (filename: string): string =>
    `/api/chime-scheduler/library/${encodeURIComponent(filename)}/audio`,

  /** Root-relative URL for inline playback of any media file under the MEDIA
   * partition (`GET /api/media/content?path=<rel>`). `version` (the file's
   * mtime) is appended as a cache-buster so a re-uploaded same-name file isn't
   * replayed from a stale cache. */
  mediaContentUrl,

  /** Root-relative URL for inline playback of the active lock chime
   * (`GET /api/media/content?path=LockChime.wav`). `version` (the installed
   * chime's mtime) is appended as a cache-buster so the native <audio> element
   * reloads the new bytes after an install rather than replaying a stale chime. */
  activeChimeAudioUrl: (version?: string | null): string =>
    mediaContentUrl("LockChime.wav", version),

  /** Root-relative URL to download a library chime (`GET …/library/:filename/download`). */
  libraryDownloadUrl: (filename: string): string =>
    `/api/chime-scheduler/library/${encodeURIComponent(filename)}/download`,
};

export type Api = typeof api;
