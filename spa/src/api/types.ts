/** The server's standard `{error:{code,message}}` error envelope. */
export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
  };
}

export type CloudCredentialProvider = "drive" | "onedrive" | "dropbox";

export type CloudCredentialState =
  | "not_configured"
  | "configured"
  | "unreadable";

export interface CloudCredentialsResponse {
  state: CloudCredentialState;
  provider: CloudCredentialProvider | null;
  updated_at: number | null;
}

export interface CloudStatusResponse {
  configured: boolean;
  provider_type: string | null;
  uploader_state: string;
  sync_now_state: string;
}

export interface SaveCloudCredentialsRequest {
  provider: CloudCredentialProvider;
  token: string;
}

export interface CloudQueueItem {
  archive_item_id: number;
  child_key: string;
  category: string;
  seq: number;
  total_bytes: number;
  bytes_uploaded: number;
  state: string;
  attempts: number;
  not_before: number | null;
  last_error_class: string | null;
  upload_set_id: string | null;
}

export interface CloudQueuePageResponse {
  items: CloudQueueItem[];
  next_cursor: string | null;
  limit: number;
}

export interface CloudHistoryItem {
  id: number;
  completion_seq: number;
  archive_item_id: number;
  child_key: string;
  outcome: string;
  size_bytes: number;
  at: number;
  error_class: string | null;
}

export interface CloudHistoryPageResponse {
  items: CloudHistoryItem[];
  next_cursor: string | null;
  limit: number;
}

export interface FailedUploadHistoryItem {
  archive_item_id: number;
  child_key: string;
  size_bytes: number;
  at: number;
  error_class: string | null;
  upload_set_id?: string | null;
}

export interface FailedUploadHistoryPageResponse {
  items: FailedUploadHistoryItem[];
  next_cursor: string | null;
  limit: number;
}

export interface FailedJob {
  job_id: number;
  kind: string;
  state: string;
  progress: number | null;
  detail?: string;
  handoff_id?: string;
}

export interface FailedJobsResponse {
  jobs: FailedJob[];
}

export interface FailedUploadRetryRequest {
  archive_item_id: number;
  child_key: string;
  upload_set_id?: string | null;
  requestId: string;
  idempotencyKey: string;
  requestHash?: string;
}

export interface FailedUploadRetryResponse {
  status: "accepted" | "replay" | "conflict" | "refused";
  jobId: string;
  requestId: string;
  state: string;
  detail?: string;
}

export interface ArchiveDeleteMutationResponse {
  status: "accepted" | "replay" | "conflict" | "refused";
  target: "archive" | string;
  jobId: string;
  requestId: string;
  state: string;
  detail?: string;
  statusUrl?: string;
}

export interface ArchiveDeleteStatusResponse {
  jobId: string;
  requestId: string;
  state: string;
  responseStatus?: string | null;
  responseCode?: number | null;
  detail?: string | null;
}

/** `GET /api/fsck/status` read-only maintenance snapshot. */
export interface FsckStatusResponse {
  running: boolean;
  partition: "part1" | "part2" | "part3" | null;
  mode: "quick" | "repair" | null;
  progress: string | null;
  start_time: string | null;
  result:
    | "healthy"
    | "repaired"
    | "recording"
    | "errors"
    | "failed"
    | "timeout"
    | "cancelled"
    | "never_checked"
    | null;
  details: string | null;
  duration: number | null;
  error: string | null;
}

/** One validated history row from `GET /api/fsck/history`. */
export interface FsckHistoryEntry {
  timestamp: string;
  partition: "part1" | "part2" | "part3";
  mode: "quick" | "repair";
  result: "healthy" | "repaired" | "recording" | "errors" | "failed" | "timeout" | "cancelled";
  details: string;
  duration_seconds: number;
}

/** `GET /api/fsck/last-check/:partition` read-only summary. */
export interface FsckLastCheckResponse {
  timestamp: string | null;
  result: "healthy" | "repaired" | "recording" | "errors" | "failed" | "timeout" | "cancelled" | "never_checked";
  details?: string;
  age_hours?: number;
}

/** `GET /api/index/status` read-only catalog diagnostics. */
export interface IndexStatusResponse {
  schema_version: number;
  trip_count: number;
  event_count: number;
  clip_count: number;
  waypoint_count: number;
}

/** `GET /api/index/lifecycle` read-only index lifecycle diagnostics. */
export interface IndexLifecycleResponse {
  schema_version: number;
  lifecycle_state: "empty" | "healthy" | "stale" | "error";
  stale_clip_count: number;
  front_parse_total: number;
  front_parse_error_count: number;
  front_parse_stale_count: number;
  front_parse_retry_pending_count: number;
  front_parse_missing_count: number;
  front_parse_missing_keys: string[];
  last_derived_at: number | null;
  last_front_parse_attempt_at: number | null;
}

/** `GET /api/index/driving-stats` read-only mapping aggregates. */
export interface IndexDrivingStatsResponse {
  total_trips: number;
  total_distance_m: number;
  total_drive_time_s: number;
  warning_event_count: number;
  sentry_event_count: number;
  avg_speed_mps: number | null;
  max_speed_mps: number | null;
}

export interface IndexEventTypeCount {
  type: string;
  count: number;
}

export interface IndexEventChartDayPoint {
  day: string;
  count: number;
  sentry_count: number;
  warning_count: number;
}

/** `GET /api/index/event-chart` read-only event distribution. */
export interface IndexEventChartResponse {
  total_events: number;
  by_type: IndexEventTypeCount[];
  by_day: IndexEventChartDayPoint[];
}

/** `GET /api/retention/status` read-only governor state. */
export interface RetentionCleanupHistoryEntry {
  at: number;
  items: number;
  bytes_freed: number;
}

export interface RetentionExclusionReason {
  reason: string;
  count: number;
  size_bytes: number;
}

export interface RetentionExclusionReport {
  sample_limit: number;
  sample_size: number;
  sample_truncated: boolean;
  reasons: RetentionExclusionReason[];
}

export interface RetentionOperatorSignal {
  status: string;
  free_frac: number | null;
  target_exit_frac: number | null;
  pressure_below_target_exit: boolean | null;
  retention_non_progress: boolean | null;
  no_eligible_candidates: boolean | null;
  stop_indicates_no_progress: boolean | null;
}

export interface RetentionStatusResponse {
  governor: Record<string, unknown> | null;
  candidate_count: number | null;
  candidate_count_truncated: boolean;
  estimated_reclaimable_bytes: number | null;
  estimated_reclaimable_bytes_truncated: boolean;
  operator_signal?: RetentionOperatorSignal | null;
  exclusion_report?: RetentionExclusionReport | null;
  recent_cleanup: RetentionCleanupHistoryEntry[] | null;
  recent_cleanup_truncated: boolean;
  cloud_durability_required: boolean;
  cloud_durability_disclosure: string;
}

export interface RetentionCandidate {
  id: number;
  size_bytes: number;
  archived_at: number;
  folder_class: string;
}

export interface RetentionPreviewResponse {
  status: "ready" | "unavailable";
  items: RetentionCandidate[];
}

/** Read-only typed retention policy snapshot (`GET /api/retention/policy`). */
export interface RetentionPolicySnapshot {
  effective_mode: string;
  target_exit_frac: number;
  target_free_frac: number | null;
  recency_floor_secs: number;
  per_cycle_evict_bytes: number;
  per_cycle_evict_count: number;
  per_cycle_wall_ms: number | null;
  source: string;
}

export interface RetentionPolicyResponse {
  status: "ready" | "unavailable";
  snapshot: RetentionPolicySnapshot | null;
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

export interface EventDetailClip {
  id: number;
  canonical_key: string;
  folder_class: string;
  is_sentry: boolean;
  started_at: number;
  ended_at: number | null;
}

export interface EventDetailSentry {
  bucket: string;
  timestamp_utc: number;
  reason: string | null;
  city: string | null;
  camera: string | null;
}

/** `GET /api/events/:id/detail` (event row + optional clip/sentry context). */
export interface EventDetail extends EventItem {
  clip: EventDetailClip | null;
  sentry: EventDetailSentry | null;
}

export interface EventsParams {
  cursor?: string;
  limit?: number;
  trip?: number;
  /** Civil day (`YYYY-MM-DD`) for standalone pinned events. */
  day?: string;
  /** Day-bucketing timezone (IANA name or "UTC"); only meaningful with day. */
  tz?: string;
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

/** `GET /api/clips/:id/waypoints` item. */
export interface ClipWaypoint {
  seq: number;
  t: number;
  lat: number;
  lon: number;
  has_gps_fix: boolean;
}

export interface ClipsParams {
  cursor?: string;
  limit?: number;
  folder_class?: string;
}

export interface TripsPageParams {
  cursor?: string;
  limit?: number;
  playable?: boolean;
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

/** `GET /api/recording/encryption` — dashcam-encryption detection. */
export interface EncryptionStatus {
  encrypting: boolean;
  latest_encrypted_at: number | null;
  latest_plain_at: number | null;
  encrypted_clip_count: number;
}

/** `GET /api/settings` raw pref row. */
export interface Pref {
  key: string;
  value: string;
}

/** Source state for one bounded advanced-setting value. */
export type AdvancedSettingSourceStatus =
  | "stored"
  | "default_missing"
  | "default_invalid";

/** Validation shape for one advanced-setting value. */
export type AdvancedSettingValidation =
  | { kind: "enum"; allowed: string[] }
  | { kind: "integer_range"; min: number; max: number }
  | { kind: "timezone_or_auto" };

/** One read-only advanced-setting snapshot row. */
export interface AdvancedSetting {
  key: string;
  label: string;
  description: string;
  value: string;
  default_value: string;
  source_status: AdvancedSettingSourceStatus;
  validation: AdvancedSettingValidation;
}

/** `GET /api/settings/advanced` bounded read-only setting/status visibility. */
export interface AdvancedSettingsResponse {
  items: AdvancedSetting[];
}

/** One `{severity, message}` row of `GET /api/system/health`. */
export interface HealthBlock {
  /** `ok | warn | error | unknown`. */
  severity: string;
  message: string;
  details?: string[];
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

/** Aggregate CPU time counters (`/proc/stat`), for client-side utilization deltas. */
export interface CpuTimes {
  total: number;
  idle: number;
}

/** Cumulative block-device byte counters (`/proc/diskstats`), for client-side throughput deltas. */
export interface DiskIo {
  read_bytes: number;
  write_bytes: number;
}

/** `GET /api/system/metrics`. Fields webd cannot read honestly are null. */
export interface SystemMetrics {
  /** Host name (`/proc/sys/kernel/hostname`), or null. */
  hostname: string | null;
  /** Primary IPv4 address (default-route source), or null. */
  ip_address: string | null;
  /** Hardware platform string (device-tree model), or null. */
  platform: string | null;
  uptime_s: number | null;
  load: LoadAvg | null;
  mem: MemStat | null;
  swap: MemStat | null;
  /** SoC temperature in °C (one decimal), or null when no sensor is exposed. */
  cpu_temp_c: number | null;
  /** Aggregate CPU time counters, or null. Utilization is a delta across polls. */
  cpu_times: CpuTimes | null;
  /** SD-card cumulative byte counters, or null. Throughput is a delta across polls. */
  sd_io: DiskIo | null;
  updated_at: number | null;
}

/** `GET /api/wifi/status`. */
export interface WifiStatus {
  connected: boolean;
  ssid: string | null;
  signal: number | null;
  security: string | null;
  ip: string | null;
  iface: string | null;
}

/** One discovered Wi-Fi network (`GET /api/wifi/networks`). */
export interface WifiNetwork {
  ssid: string;
  signal: number;
  security: string;
  saved: boolean;
  active: boolean;
  protected: boolean;
}

/** `GET /api/wifi/networks` and `POST /api/wifi/scan`. */
export interface WifiNetworksResponse {
  networks: WifiNetwork[];
}

/** One configured Wi-Fi profile (`GET /api/wifi/saved`), highest priority first. */
export interface SavedWifiNetwork {
  ssid: string;
  priority: number;
  autoconnect: boolean;
  active: boolean;
}
/** `GET /api/wifi/saved`. */
export interface SavedWifiResponse {
  networks: SavedWifiNetwork[];
}

/** `POST /api/wifi/connect`. */
export interface WifiConnectRequest {
  ssid: string;
  psk?: string;
}

/** Response of `POST /api/wifi/connect`. */
export interface WifiConnectResponse {
  connected: boolean;
  ssid: string;
  ip: string | null;
  autoconnect: boolean;
}

/** `POST /api/wifi/forget`. */
export interface WifiForgetRequest {
  ssid: string;
}

/** Response of `POST /api/wifi/forget`. */
export interface WifiForgetResponse {
  forgotten: boolean;
  count: number;
}

/** `POST /api/wifi/select` — activate an already-saved network now. */
export interface WifiSelectRequest {
  ssid: string;
}

/** Response of `POST /api/wifi/select`. */
export interface WifiSelectResponse {
  connected: boolean;
  ssid: string;
  ip: string | null;
}

/** `POST /api/wifi/priority` body — all saved ssids, highest priority first. */
export interface WifiPriorityRequest {
  order: string[];
}
/** `POST /api/wifi/priority` response. */
export interface WifiPriorityResponse {
  ok: boolean;
  count: number;
}

/** Access-point mode (`/api/wifi/ap`). */
export type ApMode = "auto" | "force_on" | "force_off";

/** The `.ap` object inside `GET /api/wifi/ap`. */
export interface ApInfo {
  mode: ApMode;
  active: boolean;
  ssid: string | null;
  client_count: number;
  ip: string | null;
}

/** `GET /api/wifi/ap` — wifid status; the SPA consumes `.ap`. */
export interface WifiApResponse {
  ap: ApInfo;
}

/** `POST /api/wifi/ap/mode`. */
export interface WifiApModeRequest {
  mode: ApMode;
}

/** `POST /api/wifi/ap/config`. */
export interface WifiApConfigRequest {
  ssid: string;
  passphrase: string;
}

/** Response of the AP mutation endpoints. */
export interface WifiApOkResponse {
  ok: boolean;
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

/**
 * One of the two USB drives the Tesla sees, from `GET /api/storage` `volumes[]`.
 * Mirrors `UsbVolumeDto` in `rust/crates/webd/src/sysinfo.rs`. Byte fields are
 * `null` when their source is unavailable (scannerd's exFAT reader down for the
 * dashcam volume, or the media mount absent) — never fabricated. `stable` is
 * false when the figure was measured while the volume was being written (a live
 * recording drive), so the SPA can label it approximate.
 */
export interface UsbVolumeInfo {
  /** `"dashcam"` (TESLACAM) | `"media"` (MEDIA). Opaque — do not narrow. */
  role: string;
  /** `"TESLACAM"` | `"MEDIA"`. */
  label: string;
  /** Always `"exfat"`. */
  fstype: string;
  total_bytes: number | null;
  free_bytes: number | null;
  used_bytes: number | null;
  /** `"bitmap"` (dashcam allocation-bitmap popcount) | `"statvfs"` (media). */
  source: string;
  stable: boolean;
}

/** Live retention-governor headroom (`retentiond.governor.json`, schema 1).
 *  Null when the governor is not reporting (file absent/inert). */
export interface GovernorInfo {
  schema: number;
  updated_at: number;
  /** "armed" (real deletion) | "dryrun" (projection only). */
  mode: string;
  drain_only: boolean;
  free_bytes: number;
  total_bytes: number;
  target_free_frac: number;
  target_exit_frac: number;
  recency_floor_secs: number;
  last_stop: string;
  last_bytes_freed: number;
  last_items: number;
}

/** Quarantined-data accounting from `GET /api/storage`.
 *  Quarantined archive items are genuinely-corrupt clips the device keeps and
 *  never auto-deletes (they may still self-heal). Mirrors `QuarantinedDto` in
 *  `rust/crates/webd/src/sysinfo.rs`. */
export interface QuarantinedInfo {
  /** Number of quarantined archive items. */
  count: number;
  /** Their total on-card footprint in bytes. */
  bytes: number;
}

/** `GET /api/storage`. */
export interface StorageInfo {
  filesystems: FilesystemEntry[];
  /** The two USB drives (TESLACAM + MEDIA); empty on an older webd. */
  volumes: UsbVolumeInfo[];
  governor: GovernorInfo | null;
  /** Quarantined-data accounting; null when the catalog read failed, absent on
   *  an older webd. */
  quarantined?: QuarantinedInfo | null;
}

/** `GET /api/storage/health`. Wear telemetry is best-effort read-only probes. */
export interface StorageHealth {
  severity: string;
  summary: string;
  device: string | null;
  fstype: string | null;
  mount: string | null;
  used_bytes: number | null;
  total_bytes: number | null;
  fs_errors: number | null;
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

/** Read-only banner severity/info from `GET /api/gadget/mode-status`. */
export interface GadgetStatusBanner {
  level: "info" | "warning";
  code: string;
  message: string;
}

export interface GadgetModeStatus {
  mode: "presented" | "syncing" | "degraded" | "unavailable";
  present: boolean;
  bound: boolean;
  bound_udc: string | null;
  udc_state: string | null;
  handoff: {
    state: "active" | "idle";
    active: boolean;
    pending_mutations: number | null;
    applying_mutations: number | null;
    last_handoff_id: string | null;
    last_result: string | null;
  };
  lun: {
    teslacam: { image: string | null; loaded: boolean | null };
    media: { image: string | null; loaded: boolean | null };
  };
  recovery: {
    chime_reenum_pending: boolean;
    last_reenum: ReenumResult | null;
    media_ro_mounted: boolean | null;
    media_ro_path: string | null;
    media_ro_error: string | null;
  };
  banner: GadgetStatusBanner | null;
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
