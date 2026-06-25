/** The server's standard `{error:{code,message}}` error envelope. */
export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
  };
}

/**
 * Wire DTO types for the webd read API (contract D2).
 *
 * These mirror `rust/crates/webd/src/dto.rs` field-for-field. Time fields
 * (`*_at`, `t`) are UTC epoch **seconds**; `*_ms` fields are milliseconds;
 * speeds are m/s (the SPA converts units client-side). `view_kind` is an
 * **opaque** string — do not narrow it to an enum that rejects unknowns.
 */

/** A cursor-paginated page (`{items, next_cursor, limit}`). */
export interface Page<T> {
  items: T[];
  /** Opaque cursor to echo as `cursor` for the next page, or null at the end. */
  next_cursor: string | null;
  limit: number;
}

/** `GET /api/days` entry (day DESC). */
export interface DaySummary {
  day: string;
  trip_count: number;
  event_count: number;
  distance_m: number;
}

export interface Bbox {
  min_lat: number;
  min_lon: number;
  max_lat: number;
  max_lon: number;
}

/** Polyline: array of segments, each an array of `[lat, lon]` pairs. */
export type Polyline = [number, number][][];

/** `GET /api/trips` row. */
export interface Trip {
  id: number;
  day: string;
  started_at: number;
  ended_at: number;
  bbox: Bbox | null;
  distance_m: number | null;
  point_count: number;
  polyline: Polyline;
}

export interface TripPoint {
  t: number;
  lat: number;
  lon: number;
  speed: number | null;
  heading: number | null;
}

/** `GET /api/trips/:id` (trip flattened + points). */
export interface TripDetail extends Trip {
  points: TripPoint[];
}

/** `GET /api/events` item. */
export interface EventItem {
  id: number;
  type: string;
  severity: number | null;
  t: number;
  lat: number | null;
  lon: number | null;
  clip_id: number | null;
  trip_id: number | null;
  front_frame_index: number | null;
  front_frame_offset_ms: number | null;
  description: string | null;
}

export interface EventsParams {
  cursor?: string;
  limit?: number;
  trip?: number;
}

export interface Angle {
  camera: string;
  /** Opaque passthrough (code writes "live"); do not assume an enum. */
  view_kind: string;
  offset_ms: number;
  duration_s: number | null;
  size_bytes: number | null;
}

/** `GET /api/clips` / `GET /api/clips/:id` item. */
export interface Clip {
  id: number;
  canonical_key: string;
  started_at: number;
  ended_at: number | null;
  lat?: number | null;
  lon?: number | null;
  partition: string;
  folder_class: string;
  is_sentry: boolean;
  duration_s: number | null;
  availability: string;
  angles: Angle[];
}

export interface ClipsParams {
  cursor?: string;
  limit?: number;
  folder_class?: string;
}

export interface TripsPageParams {
  cursor?: string;
  limit?: number;
}

export interface EventTypeCount {
  type: string;
  count: number;
}

export interface DayTripCount {
  day: string;
  count: number;
  distance_m: number;
}

export interface SeverityCount {
  severity: number;
  count: number;
}

export interface FolderClassStat {
  folder_class: string;
  clip_count: number;
  file_count: number;
  size_bytes: number;
}

export interface VideoStats {
  total_clips: number;
  total_files: number;
  total_bytes: number;
  by_folder_class: FolderClassStat[];
}

/** `GET /api/analytics`. */
export interface Analytics {
  total_trips: number;
  total_distance_m: number;
  total_events: number;
  events_by_type: EventTypeCount[];
  trips_by_day: DayTripCount[];
  // Extended aggregates (older webd builds may omit these — treat as absent).
  total_drive_time_s?: number;
  warning_event_count?: number;
  avg_speed_mps?: number | null;
  max_speed_mps?: number | null;
  events_by_severity?: SeverityCount[];
  video_stats?: VideoStats;
}

/** `GET /api/settings` raw pref row. */
export interface Pref {
  key: string;
  value: string;
}

/** One `{severity, message}` row of `GET /api/system/health`. */
export interface HealthBlock {
  /** `ok | warn | error | unknown`. */
  severity: string;
  message: string;
}

/** `GET /api/system/health`. Subsystems webd cannot probe are omitted. */
export interface SystemHealth {
  overall: string;
  subsystems: Record<string, HealthBlock>;
}

/** Load averages (1/5/15 min). */
export interface LoadAvg {
  one: number;
  five: number;
  fifteen: number;
}

/** A memory or swap tile. */
export interface MemStat {
  total_bytes: number;
  available_bytes: number;
  used_pct: number;
}

/** `GET /api/system/metrics`. Fields webd cannot read honestly are null. */
export interface SystemMetrics {
  uptime_s: number | null;
  load: LoadAvg | null;
  mem: MemStat | null;
  swap: MemStat | null;
  /** SoC temperature in °C (one decimal), or null when no sensor is exposed. */
  cpu_temp_c: number | null;
  updated_at: number | null;
}

/** One filesystem of `GET /api/storage`. */
export interface FilesystemEntry {
  mount: string;
  device: string;
  fstype: string;
  free_bytes: number;
  total_bytes: number;
  free_inodes: number;
  total_inodes: number;
}

/** `GET /api/storage`. `governor` is null until retentiond is wired in. */
export interface StorageInfo {
  filesystems: FilesystemEntry[];
  governor: unknown | null;
}

/** `GET /api/storage/health`. Wear telemetry is null (SD cards expose none). */
export interface StorageHealth {
  severity: string;
  summary: string;
  device: string | null;
  fstype: string | null;
  mount: string | null;
  used_bytes: number | null;
  total_bytes: number | null;
  fs_errors: number | null;
  io_errors_24h: number | null;
  trim: string | null;
}

/**
 * `GET /api/gadget/status`. Live USB-gadget state read from gadgetd's control
 * socket — present/bound/udc plus the two LUN backing files and the last
 * handoff result. Unlike the catalog reads, this CAN fail with 503 (gadgetd
 * down / socket absent) or 502 (unparseable reply); callers should render an
 * ApiError as "USB status unavailable" rather than treat it as an app crash.
 */
export interface GadgetStatus {
  present: boolean;
  bound: boolean;
  bound_udc: string | null;
  udc_state: string | null;
  lun_file: string | null;
  media_lun_file: string | null;
  handoff_active: boolean;
  /** Queued / in-flight media mutations (null when gadgetd omits the counts). */
  pending_mutations: number | null;
  applying_mutations: number | null;
  /** RO media-mount health (the `/api/media/content` read seam). `mounted` is
   * null when an older gadgetd doesn't report it; `error` is the last mount
   * failure reason or null when healthy. */
  media_ro_mounted: boolean | null;
  media_ro_path: string | null;
  media_ro_error: string | null;
  /**
   * i2 auto-reenum (chime apply). `chime_reenum_pending` is true while gadgetd
   * still owes the parked car a USB re-enumeration after a `LockChime.wav`
   * install — the SPA shows its "syncing to the car, keep the doors closed"
   * overlay until this flips false. Defaults to false on an older gadgetd (no
   * overlay). `last_reenum` is the terminal result of the most recent
   * re-enumeration (or null before any has run).
   */
  chime_reenum_pending: boolean;
  last_reenum: ReenumResult | null;
  last_handoff_id: string | null;
  last_result: unknown | null;
}

/**
 * Terminal result of a USB re-enumeration, as reported by gadgetd's
 * `last_reenum`. `result` is `"done"` on success; `disconnect_ms` is how long
 * the gadget was detached; `reason` tags what triggered it (e.g.
 * `"chime_apply"`). All but `result` are best-effort and may be absent.
 */
export interface ReenumResult {
  result: string;
  disconnect_ms?: number | null;
  reason?: string | null;
  detail?: string | null;
}

/**
 * One file inventoried on the MEDIA (p2) partition for the toybox categories
 * (Boombox, Music, LightShows, LicensePlates, Wraps). Mirrors `MediaItemDto`
 * in `rust/crates/webd/src/dto.rs`. `modified` is a best-effort naive-local
 * `YYYY-MM-DDThh:mm:ss` string (null when the exFAT timestamp couldn't be
 * decoded). `rel_path` doubles as the delete id.
 */
export interface MediaItem {
  name: string;
  rel_path: string;
  size_bytes: number;
  modified: string | null;
}

/**
 * Generic list response for a toybox media category
 * (`GET /api/boombox`, `/api/music`, `/api/lightshows`, `/api/plates`, `/api/wraps`).
 * `items` is empty when nothing is installed — never absent.
 */
export interface MediaList {
  items: MediaItem[];
}

/** Terminal result of a successful media install/remove handoff. */
export interface MediaHandoffResult {
  /** Present only on the synchronous `"done"` path (a completed handoff). */
  handoff_id?: string;
  /** `"done"` (applied synchronously) or `"queued"` (accepted into the durable queue, a 202). */
  state: string;
  /** gadgetd queue entry id; present only on the `"queued"` path. */
  job_id?: string;
}

/**
 * The installed lock chime (`GET /api/chimes` → `installed`), mirrors
 * `InstalledChimeDto` in `rust/crates/webd/src/dto.rs`. `modified` is a
 * best-effort naive-local `YYYY-MM-DDThh:mm:ss` string (null when the exFAT
 * timestamp couldn't be decoded).
 */
export interface InstalledChime {
  name: string;
  rel_path: string;
  size_bytes: number;
  modified: string | null;
}

/**
 * `GET /api/chimes`. `installed` is null when no chime is on the p2 MEDIA
 * partition OR the catalog predates the media schema (webd degrades to null
 * rather than erroring).
 */
export interface Chimes {
  installed: InstalledChime | null;
}

/** `GET /api/system/timezone`. */
export interface TimezoneInfo {
  current: string | null;
  zones: string[];
}

// ── Chime scheduler (GET/POST/PUT/DELETE /api/chime-scheduler/*) ──
// webd is a pure proxy to schedulerd; these mirror `schedulerd::model`
// (camelCase JSON). The SPA forwards inputs verbatim and renders the snapshot.

/** A schedule's trigger kind — matches schedulerd's `scheduleType` tokens. */
export type ScheduleType = "weekly" | "date" | "holiday" | "recurring";

/** A schedule definition (request body for create/update). */
export interface ScheduleInput {
  name: string;
  chimeFilename: string;
  scheduleType: ScheduleType;
  /** Weekday names (weekly). */
  days?: string[];
  /** Calendar month 1–12 (date). */
  month?: number | null;
  /** Day-of-month 1–31 (date). */
  day?: number | null;
  /** Holiday label (holiday). */
  holiday?: string | null;
  /** Interval token (recurring). */
  interval?: string | null;
  /** Trigger hour 0–23 (weekly/date). */
  hour?: number | null;
  /** Trigger minute 0–59 (weekly/date). */
  minute?: number | null;
  enabled: boolean;
}

/** A persisted schedule: an input plus its server-assigned id. */
export type StoredSchedule = ScheduleInput & { id: string };

/** A named group of chimes for scoped random selection. */
export interface ChimeGroup {
  id: string;
  name: string;
  description: string;
  chimes: string[];
}

/** Group create/update body (no id). */
export interface GroupInput {
  name: string;
  description: string;
  chimes: string[];
}

/** Random-on-boot configuration. */
export interface RandomMode {
  enabled: boolean;
  groupId?: string | null;
}

/** One file in the chime library. */
export interface LibraryEntry {
  filename: string;
  bytes: number;
}

/** Form menu metadata derived from the engine's source-of-truth lists. */
export interface SchedulerMenus {
  holidays: string[];
  intervals: string[];
  weekdays: string[];
}

/** `GET /api/chime-scheduler` — the full scheduler snapshot in one round-trip. */
export interface SchedulerSnapshot {
  schedules: StoredSchedule[];
  groups: ChimeGroup[];
  randomMode: RandomMode;
  library: LibraryEntry[];
  menus: SchedulerMenus;
}
