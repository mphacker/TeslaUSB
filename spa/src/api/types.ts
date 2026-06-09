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
  /** id to pass as `after` for the next page, or null at the end. */
  next_cursor: number | null;
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
  partition: string;
  folder_class: string;
  is_sentry: boolean;
  duration_s: number | null;
  availability: string;
  angles: Angle[];
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

/** `GET /api/analytics`. */
export interface Analytics {
  total_trips: number;
  total_distance_m: number;
  total_events: number;
  events_by_type: EventTypeCount[];
  trips_by_day: DayTripCount[];
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

/** Uniform error envelope (`{"error": {code, message}}`). */
export interface ApiErrorBody {
  error: { code: string; message: string };
}
