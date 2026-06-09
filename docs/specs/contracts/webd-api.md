# Contract D2 — `webd` REST/SSE API + shared types

```
Contract-Version: 0.1 (DRAFT)
Server:  webd               (axum/tokio; serves the SPA + the API)
Client:  SPA                (~14 parity screens)
Binds:   5.1 (defines, contract-first) → 5.2–5.x (SPA screens fan out)
```

**Derives from:** [`webd.md §1–§5`](../webd.md) ·
[`spa.md §3,§4`](../spa.md) · [`storage.md §6`](../storage.md) ·
[`indexd-schema.md` (D1)](./indexd-schema.md) ·
[`single-writer-lease.md` (D3)](./single-writer-lease.md) ·
[`SPEC.md §7`](../SPEC.md) · [`tasks.md` 5.1](../../tasks/tasks.md).

> Per [`plan.md §8`](../../tasks/plan.md) this API shape is **fixed first
> (contract-first)** so the independent SPA screens can fan out against it. It is a
> **read API over `indexd`'s SQLite** plus a small set of **mutations that route
> through the `gadgetd` eject-handoff** and **config forwards** to
> `retentiond`/`uploadd`/`wifid`.

---

## 1. Conventions

- **Base path** `/api`. **JSON** request/response (`Content-Type: application/json`),
  except media streaming (`video/mp4` + range) and export (`application/zip`).
- **Trust model: unchanged.** No app-level login (today's Flask app uses cloud
  OAuth only, no `login_required`) — preserved, **not** silently changed
  ([`webd.md §3.1`](../webd.md), [`SPEC.md §7`](../SPEC.md)). `webd` binds to the
  **LAN/AP interface only**; mutations are unauthenticated on the trusted segment by
  design. Adding/removing auth is **ASK FIRST**.
- **Errors** use a uniform envelope: `{"error": {"code": "<machine>", "message":
  "<human>"}}` with appropriate HTTP status (`400` validation, `404` not found,
  `409` handoff refused/busy, `503` service unavailable, `507` storage exhausted).
- **Times** are UTC epoch seconds (matching D1; unit conversion/civil-date display is
  client-side — `spa.md §4` speed-unit toggle, day nav).
- **Units** unit-neutral on the wire (speed in m/s); the SPA converts per the
  speed-unit pref ([`spa.md §3`](../spa.md)).
- **Validation is `webd`'s job**, before any handoff — path-traversal, file-type,
  size — because `gadgetd` executes what it's given
  ([`webd.md §3.1`](../webd.md)).
- **SSE** for progress/streaming events (proposed primary; `webd.md §6` allows
  long-poll — see OQ).

---

## 2. Endpoint catalog (parity map → ~14 screens)

Maps each existing Flask blueprint/screen ([`webd.md §3`](../webd.md),
[`spa.md §3`](../spa.md)) to its new route. "Reads D1" = served from `indexd`'s
SQLite ([D1](./indexd-schema.md)).

### 2.1 Read endpoints (from SQLite, read-only)

| Method · Route | Screen | Returns (shape sketch) | Reads |
|---|---|---|---|
| `GET /api/overview` | Home / media hub | counts, recent events, feature availability, health summary | D1 + service status |
| `GET /api/days` | Trip map day-nav | `[{day, trip_count, event_count, distance_m}]` | `trips`, `events` |
| `GET /api/trips?day=YYYY-MM-DD` | Trip map | `[{id, day, started_at, ended_at, bbox, distance_m, polyline|point_ref}]` | `trips`(+`trip_points`) |
| `GET /api/trips/:id/route` | Trip map route | ordered points / simplified polyline | `trip_points` |
| `GET /api/events?day=…&trip=…` | Event bubbles | `[{id, type, severity, t, lat, lon, clip_id, front_frame_offset_ms}]` | `events` |
| `GET /api/clips?…` | All-clips list | `[{id, started_at, folder_class, is_sentry, duration_s, availability, angles:[camera]}]` | `clips`,`angles` |
| `GET /api/clips/:id` | Event player | clip + its angle set + linked event/jump-offset (`front_frame_offset_ms`) | `clips`,`angles`,`events` |
| `GET /api/clips/:id/telemetry` | Video overlay HUD | SEI/telemetry samples synced to playback (speed/heading/etc. over time) for the **client-side** HUD ([`spa.md §2,§3`](../spa.md), [`webd.md §4`](../webd.md)) | `indexd` (or sidecar) |
| `GET /api/chimes`, `/api/lightshows`, `/api/boombox`, `/api/music`, `/api/plates`, `/api/wraps` | Media managers (list/detail) | installed items + assignable state (parity `GET` halves of the blueprints, [`webd.md §3`](../webd.md)) | media staging + D1 |
| `GET /api/jobs/failed` | Failed jobs | snapshot list of failed/retryable jobs (parity `failed_jobs.html`, [`spa.md §3`](../spa.md)) | `webd` jobs + `uploadd` |
| `GET /api/analytics` | Analytics | chart datasets (parity with `analytics.py`) | D1 aggregates |
| `GET /api/storage` | Storage settings | reserves/quotas/policy + per-FS free bytes+inodes | `prefs`, `retentiond` |
| `GET /api/storage/health` | Storage health | full `StorageHealth` (§4): governor tier, per-FS free **bytes+inodes**, `disk.img` logical-vs-alloc (sparse warn), archive breakdown by class, WAL/staging/thumb/log usage, **pinned/leased/reclaimable** bytes, next candidate classes, undurable-sacrifice flag, paused writers, last eviction, two distinct signals | `retentiond` + `archive_items`/`leases` ([`storage.md §6`](../storage.md)) |
| `GET /api/system/health` | System health | uptime, mem, service states, gadget bound/UDC, write-heartbeat | services |
| `GET /api/settings` | Settings | all editable prefs/thresholds | `prefs` |

### 2.2 Streaming / export (with playback lease — D3)

| Method · Route | Behavior |
|---|---|
| `GET /api/clips/:id/stream?camera=front` | HTTP **range requests** to the `<video>` element (`webd.md §2.3`, ref `video_service/_range.py`); **no transcoding** — stream as stored (H.264 plays natively); codec fallback = "download to view" edge-case guard. **Holds a playback lease (TTL + heartbeat)** on the item while streaming ([D3 §2.2](./single-writer-lease.md)). |
| `GET /api/clips/:id/export.zip` | Zip/download export (ref `_zip.py`); holds the lease while exporting. |

### 2.3 Mutations

Two distinct authorities — **do not conflate them** (both contract reviews):

- **Car-visible (p1/p2) changes route through the `gadgetd` eject-handoff** — clip
  delete on the Tesla volume, media install/remove. Validate → `gadgetd` handoff →
  progress → never write the Tesla FS directly ([`webd.md §2.4`](../webd.md),
  [`gadgetd.md §4`](../gadgetd.md)). A refused handoff (car mid-save) → friendly
  retry (`409`).
- **Pi-side archive deletes are NOT a handoff** — they go to `retentiond`, the
  **sole deleter** of archive files, via its crash-safe protocol
  ([`storage.md §5`](../storage.md), [D3 §4](./single-writer-lease.md)). `gadgetd`
  never touches the Pi-side archive.

| Method · Route | Op | Authority |
|---|---|---|
| `DELETE /api/clips/:id?target=car` | delete the car-visible copy (angle group = one clip — `spa.md §4`) | `gadgetd` handoff |
| `DELETE /api/clips/:id?target=archive` | delete the Pi-side archived copy | `retentiond` delete protocol |
| `DELETE /api/clips/:id?target=both` | coordinated: car handoff + archive delete | both (sequenced) |
| `POST /api/chimes` · `DELETE /api/chimes/:id` | install/remove lock chime (+ scheduler) | `gadgetd` handoff |
| `POST /api/lightshows` · `DELETE …/:id` | install/remove light show | `gadgetd` handoff |
| `POST /api/boombox` · `DELETE …/:id` | upload/trim/assign boombox | `gadgetd` handoff |
| `POST /api/music` · `DELETE …/:id` | manage music | `gadgetd` handoff |
| `POST /api/plates` · `DELETE …/:id` | manage license plates | `gadgetd` handoff |
| `POST /api/wraps` · `DELETE …/:id` | manage wraps | `gadgetd` handoff |

> **Default `target`.** If omitted, propose `target=car` for parity with today's
> "delete this clip" (the user means the drive they see). Confirm at freeze (OQ).

**Per-mutation payload → `gadgetd` op.** `webd` validates (path-traversal,
file-type, size) then calls `gadgetd`'s `request_mutation(partition, op, payload)`
([`gadgetd.md §4`](../gadgetd.md)). Indicative op/payload map:

| Route | `partition` | `op` | `payload` |
|---|---|---|---|
| `DELETE /api/clips/:id?target=car` | p1 | `delete_clip` | `{clip_path or event_folder}` |
| `POST /api/chimes` | p2 | `install_chime` | `{filename, bytes_ref}` (validated WAV — v1 `lock_chime_service` rules) |
| `POST /api/lightshows` | p2 | `install_lightshow` | `{name, files_ref}` |
| `POST /api/boombox`/`music` | p2 | `install_audio` | `{slot, filename, bytes_ref}` |
| `POST /api/plates`/`wraps` | p2 | `install_asset` | `{kind, filename, bytes_ref}` |

A car-handoff mutation returns `{handoff_id}`; progress is observed via
`GET /api/jobs` (SSE) or polled via **`GET /api/handoff/:id`** →
`{handoff_id, state, detail}` where `state ∈ queued|ejecting|mounted|applying|
representing|done|refused|failed` ([`gadgetd.md §4`](../gadgetd.md)). Mutations are
**serialized** by `gadgetd` (never two concurrent handoffs).

### 2.4 Config forwards (validate + forward; `webd` does not own the policy)

| Method · Route | Forwards to |
|---|---|
| `GET/POST /api/cloud/*` (provider/browse/queue/sync) | `uploadd` ([`webd.md §3`](../webd.md)) |
| `PUT /api/settings` (retention reserves/quotas/value-weights) | `retentiond` |
| `GET/POST /api/wifi` (STA/AP config) | `wifid` (secrets never echoed — [`webd.md §3.1`](../webd.md)) |
| `GET /portal` | captive-portal entry for AP onboarding ([`webd.md §2.7`](../webd.md)) |

### 2.5 Progress streams (SSE)

| Route | Events |
|---|---|
| `GET /api/jobs` (SSE) | `index_progress`, `handoff_status`, `upload_queue`, `job_status` |

---

## 3. SSE event catalog (proposed)

`text/event-stream`; each event has a named `event:` + JSON `data:`.

| `event:` | `data` shape | Source |
|---|---|---|
| `index_progress` | `{active_file, queue_depth, last_outcome}` | `indexd` status |
| `handoff_status` | `{handoff_id, state, detail}` where state ∈ queued/ejecting/mounted/applying/representing/done/refused/failed ([`gadgetd.md §4`](../gadgetd.md)) | `gadgetd` |
| `upload_queue` | `{queued, in_progress, done, failed, current?}` | `uploadd` |
| `job_status` | `{job_id, kind, state, progress}` — **realized** (`webd`): `state ∈ running/done/failed/refused/busy`; `progress` is `number|null` (always present; `1.0` on success, else `null` for start/end-granular jobs); plus optional `detail` (string, on failure/refusal) and `handoff_id` (string, when the job drove a `gadgetd` handoff). `job_id` is process-monotonic. | `webd` jobs |

> The index banner truth rule (`active_file != null`, not queue depth) is a v1
> lesson preserved in `.github/copilot-instructions.md`; `index_progress` carries
> `active_file` so the SPA follows it.

---

## 4. Shared Rust types proposal (`teslausb-core::contracts`)

A single shared-DTO module so `webd` handlers and the contract/integration tests
bind to one source of truth (illustrative; **no `.rs`/`Cargo` edits** from this
lane — integrator wires it). `serde`-derived.

```rust
// teslausb-core::contracts::api  (doc-only proposal)
// Time/unit convention (annotated per field): *_at = unix epoch SECONDS (UTC, wall);
// *_ms = milliseconds; speed = m/s (client converts); day = local civil 'YYYY-MM-DD'.
pub struct DaySummary   { pub day: String, pub trip_count: u32, pub event_count: u32, pub distance_m: f64 }
pub struct TripDto      { pub id: i64, pub day: String, pub started_at: i64 /*s*/, pub ended_at: i64 /*s*/,
                          pub bbox: Bbox, pub distance_m: f64 }
pub struct Bbox         { pub min_lat: f64, pub min_lon: f64, pub max_lat: f64, pub max_lon: f64 }
pub struct EventDto     { pub id: i64, pub r#type: String, pub severity: Option<i32>,
                          pub t: i64 /*s*/, pub lat: Option<f64>, pub lon: Option<f64>,
                          pub clip_id: Option<i64>, pub front_frame_offset_ms: Option<i64> }
pub struct ClipDto      { pub id: i64, pub started_at: i64 /*s*/, pub folder_class: String,
                          pub is_sentry: bool, pub duration_s: Option<f64>,
                          pub availability: String, pub angles: Vec<AngleDto> }
pub struct AngleDto     { pub camera: String, pub duration_s: Option<f64> }

// Video HUD telemetry (client renders the overlay; webd never transcodes)
pub struct ClipTelemetry { pub clip_id: i64, pub samples: Vec<TelemetrySample> }
pub struct TelemetrySample { pub t_ms: i64 /*offset into clip*/, pub speed: Option<f64> /*m/s*/,
                          pub heading: Option<f64>, pub lat: Option<f64>, pub lon: Option<f64> }

// Storage health — full per storage.md §6 (both reviews: prior shape too thin)
pub struct StorageHealth {
    pub car_writeable:      bool,            // "TeslaCam USB: OK / Not OK" (the invariant signal)
    pub archive_tier:       String,          // Healthy|Low|Critical|Emergency|Exhausted (distinct signal)
    pub per_fs:             Vec<FsFree>,     // root + data (collapsed if same st_dev)
    pub disk_img_logical_bytes:   u64,
    pub disk_img_allocated_bytes: u64,       // < logical ⇒ sparse-image warning
    pub archive_by_class:   Vec<ClassUsage>, // SentryClips/SavedClips/RecentClips/Track/thumb/cache/staging
    pub wal_bytes:          u64,
    pub log_bytes:          u64,
    pub pinned_bytes:       u64,
    pub leased_bytes:       u64,
    pub reclaimable_bytes:  u64,
    pub next_candidate_classes: Vec<String>, // what eviction would target next
    pub sacrificing_undurable:  bool,        // is undurable footage being sacrificed?
    pub paused_writers:     Vec<String>,     // which optional writers are stopped
    pub last_eviction:      Option<EvictionSummary>,
}
pub struct FsFree       { pub mount: String, pub free_bytes: u64, pub total_bytes: u64,
                          pub free_inodes: u64, pub total_inodes: u64, pub reserve_breached: bool }
pub struct ClassUsage   { pub class: String, pub bytes: u64, pub file_count: u64 }
pub struct EvictionSummary { pub at: i64 /*s*/, pub what: String, pub why: String, pub bytes_freed: u64 }

pub enum HandoffState   { Queued, Ejecting, Mounted, Applying, Representing, Done, Refused, Failed }
pub struct HandoffStatus{ pub handoff_id: String, pub state: HandoffState, pub detail: Option<String> }
pub struct ApiError     { pub code: String, pub message: String }
```

These reuse D3's `LeaseKind`/`DeleteState` and D4's `ThrottleState` where the
storage/cloud screens surface them.

---

## 5. Acceptance hooks (from [`webd.md §5`](../webd.md))

- Every §2 screen's data is reachable (parity checklist).
- Range playback works within the memory cap; export works; codec-fallback path
  present.
- Mutations always route through the handoff + report progress; a refused handoff
  surfaces a friendly retry (`409`).
- Secrets (`0600`, root) read via the owning service, never echoed to the SPA or
  placed in the bundle ([`webd.md §3.1`](../webd.md)).
- Playwright proves the served HTML loads the expected JS, interactive < ~2 s on the
  Pi, zero console/pageerror ([`spa.md §5`](../spa.md), [`SPEC.md §8`](../SPEC.md)).

---

## 6. OPEN QUESTIONS

1. **(OQ-6) SSE vs. long-poll.** [`webd.md §6`](../webd.md) allows either. Proposed:
   **SSE primary** for `/api/jobs` (one stream, all progress events), with a poll
   fallback (`GET /api/handoff/:id`) for environments where SSE is awkward. Confirm.
2. **Pagination / windowing.** `/api/clips` and `/api/events` over a large archive
   can be big. Propose cursor pagination (`?after=<id>&limit=`) — confirm whether the
   parity UI ever needs full-list semantics.
3. **Media-manager payloads.** The `POST /api/{chimes,boombox,…}` bodies carry file
   uploads (audio for chimes/boombox); confirm multipart vs. base64-JSON and the
   trimmer hand-off (`spa.md §2` `lamejs` is client-side, so the server likely
   receives a finished WAV/MP3 — confirm validation rules mirror v1
   `lock_chime_service`).
4. **`/api/overview` composition.** Exact tiles/counts to match `index.html` parity —
   reconcile against the Phase 0 parity baseline capture.
5. **Clip id vs. archive_item id in URLs.** Ties to [D3 OQ-1](./single-writer-lease.md):
   `/api/clips/:id` uses `clips.id`; the playback lease subject resolves (recommended)
   to **all backing `archive_items`** via `acquire_for_clip` ([D3 §2.1](./single-writer-lease.md)).
6. **Delete `target` default.** Proposed `target=car` (parity with today's "delete
   this clip"). Confirm — and confirm `target=both` sequencing (car handoff then
   archive delete, or parallel).
7. **Clip telemetry source.** `GET /api/clips/:id/telemetry` — does `indexd` persist
   the per-sample telemetry track (a compacted SEI sample stream, [D1 OQ-2](./indexd-schema.md)),
   or does `webd` re-extract on demand from the mp4 (no transcode, just SEI parse)?
   The HUD parity (`spa.md §2`) needs one answer.
