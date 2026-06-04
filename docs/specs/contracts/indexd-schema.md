# Contract D1 — `indexd` SQLite schema

```
Contract-Version: 0.1 (DRAFT — PROVISIONAL)
Owner (writer):   indexd            (sole SQLite writer + migrator + checkpointer)
Readers:          webd, retentiond, uploadd
Binds tasks:      4.2 (defines) → 5.1, 6.1, 6.3 (consume)
```

> **PROVISIONAL:** shape is stable and bindable now, but specific columns/values may
> be amended by the storage-governor calibration (delete / fsync / WAL latencies,
> [`storage.md §7`](../storage.md)) before this schema is **ratified at Phase 4**
> (the `indexd` build). See [`README.md` maturity table](./README.md).

**Derives from:** [`indexd.md §2,§4`](../indexd.md) ·
[`storage.md §4.1,§5,§6`](../storage.md) · [`SPEC.md §6,§6.1,§7`](../SPEC.md) ·
[`uploadd.md §2`](../uploadd.md) · [`webd.md §2`](../webd.md) ·
task cards [`tasks.md` 4.2](../../tasks/tasks.md).

> This is the **derived, rebuildable side-state** the UI reads. It lives in WAL
> mode on the **Pi-side ext4** filesystem (`/var/lib/teslausb/index.sqlite3`),
> **never** inside the car's `disk.img` LUN or on the Tesla volume
> ([`indexd.md §1`](../indexd.md), [`SPEC.md §6.1,§7`](../SPEC.md)). It can be
> dropped and regenerated from the media at any time
> ([`indexd.md §2.6`](../indexd.md)).

---

## 1. Scope & rules this schema encodes

- **Single writer.** Only `indexd` writes/migrates/checkpoints
  ([`indexd.md §2.1`](../indexd.md), [`storage.md §5.2`](../storage.md)). `webd`,
  `retentiond` and `uploadd` are **readers**; their *mutations* (leases,
  delete-state, durability) are requested **via `indexd`** — see
  [D3](./single-writer-lease.md), not direct DB writes.
- **WAL.** `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA
  foreign_keys=ON;`. `indexd` performs WAL checkpoint/truncate on `retentiond`'s
  request ([`storage.md §5.2`](../storage.md)).
- **Idempotent / incremental.** Re-indexing a seen clip must not duplicate; prune
  removes rows for clips that no longer exist
  ([`indexd.md §2.5`](../indexd.md)). Encoded via natural unique keys
  (`clips.canonical_key`, `angles(clip_id,camera)`) and `INSERT … ON CONFLICT`.
- **Time is UTC epoch seconds (INTEGER).** Absolute recording time derives from the
  MP4 `mvhd.creation_time` (GPS-derived) with the Tesla filename only as a fallback
  — the Pi has no RTC and the car clock drifts (this is the documented clock-skew
  rule; see `.github/copilot-instructions.md` and `scannerd`). `indexd` stores the
  resolved instant; it does not re-derive it.
- **Rebuildable — with a caveat (see §6).** The *derived* model (clips/angles/
  trips/trip_points/events) is fully rebuildable from the media
  ([`indexd.md §5 acceptance`](../indexd.md)). But this DB also holds **durable
  control/policy state that is NOT derivable from video** — `archive_items.pinned`,
  `durable`, `delete_state`, `suppress_until`, the `eviction_tombstones`, and
  `prefs`. A naïve "drop and rebuild" would lose pins, re-import evicted clips, and
  re-upload already-durable copies. §6 separates the two and gives the rebuild
  recovery sources. (Flagged by both contract reviews.)

---

## 2. DDL (proposed v1)

> Indicative per [`indexd.md §4`](../indexd.md) ("finalize during build with the
> existing DB as the behavioral reference"). Column *intent* is contractual; exact
> spelling is reconciled at freeze.

```sql
-- ── schema versioning ────────────────────────────────────────────────
CREATE TABLE schema_version (
    version     INTEGER NOT NULL,          -- monotonic; current = MAX(version)
    applied_at  INTEGER NOT NULL,          -- unix epoch seconds (UTC)
    note        TEXT
);
-- v1 seed row inserted by the initial migration.

-- ── clips: a recording session (a group of camera angles) ────────────
CREATE TABLE clips (
    id             INTEGER PRIMARY KEY,
    canonical_key  TEXT    NOT NULL UNIQUE, -- dedup key (mapping_service.canonical_key analogue)
    started_at     INTEGER NOT NULL,        -- UTC epoch s (resolved, mvhd-first)
    ended_at       INTEGER,                 -- nullable until known
    partition      TEXT    NOT NULL,        -- 'p1' (TeslaCam) | 'p2' (media); car-visible source
    folder_class   TEXT    NOT NULL,        -- SentryClips|SavedClips|RecentClips|TeslaTrackMode|ArchivedClips
    is_sentry      INTEGER NOT NULL DEFAULT 0,
    duration_s     REAL,                    -- best-known clip duration
    availability   TEXT    NOT NULL DEFAULT 'present', -- present|archived|missing
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- ── angles: one camera file within a clip ────────────────────────────
CREATE TABLE angles (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera      TEXT    NOT NULL,           -- front|back|left_repeater|right_repeater|left_pillar|right_pillar
    file_ref    TEXT    NOT NULL,           -- see "file identity" note below
    view_kind   TEXT    NOT NULL DEFAULT 'archive', -- 'archive' (Pi-side path) | 'ro_usb' (live RO view, rotatable)
    offset_ms   INTEGER NOT NULL DEFAULT 0, -- start offset of this angle vs clip start
    duration_s  REAL,
    size_bytes  INTEGER,
    UNIQUE (clip_id, camera)
);

-- ── trips: per-day driving segments ──────────────────────────────────
CREATE TABLE trips (
    id           INTEGER PRIMARY KEY,
    day          TEXT    NOT NULL,          -- local civil date 'YYYY-MM-DD' (UI day-nav key)
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER NOT NULL,
    bbox_min_lat REAL, bbox_min_lon REAL,
    bbox_max_lat REAL, bbox_max_lon REAL,
    distance_m   REAL,
    point_count  INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX idx_trips_day        ON trips(day);
CREATE INDEX idx_trips_started_at ON trips(started_at);

-- ── trip_points: the GPS polyline (OPEN: rows vs blob, see §5) ────────
CREATE TABLE trip_points (
    trip_id  INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,              -- order within trip
    t        INTEGER NOT NULL,              -- UTC epoch s
    lat      REAL    NOT NULL,
    lon      REAL    NOT NULL,
    speed    REAL,                          -- stored unit-neutral (m/s); UI converts
    heading  REAL,
    PRIMARY KEY (trip_id, seq)
);

-- ── events: honk / Sentry / hard-brake / hard-accel bubbles ──────────
CREATE TABLE events (
    id                 INTEGER PRIMARY KEY,
    trip_id            INTEGER REFERENCES trips(id) ON DELETE SET NULL,
    clip_id            INTEGER REFERENCES clips(id) ON DELETE SET NULL,
    type               TEXT    NOT NULL,    -- honk|sentry|hard_brake|hard_accel|...
    severity           INTEGER,             -- indexd-derived ordinal
    t                  INTEGER NOT NULL,    -- UTC epoch s
    lat                REAL, lon REAL,      -- nullable: stationary Sentry may lack geo
    front_frame_offset INTEGER,             -- ms into front-cam to jump to (front_frame_offset_ms)
    created_at         INTEGER NOT NULL
);
CREATE INDEX idx_events_trip ON events(trip_id);
CREATE INDEX idx_events_clip ON events(clip_id);
CREATE INDEX idx_events_t    ON events(t);

-- ── archive_items: the retention/value/durability/delete unit ────────
-- Eviction granularity is the *unit* (storage.md §4.2): whole event folder for
-- SavedClips/SentryClips/TeslaTrackMode, per-segment for RecentClips mirror, etc.
CREATE TABLE archive_items (
    id             INTEGER PRIMARY KEY,
    folder_class   TEXT    NOT NULL,        -- SentryClips|SavedClips|TeslaTrackMode|RecentClips|thumbnail|cache|staging|temp
    path           TEXT    NOT NULL UNIQUE, -- Pi-side archive path (NOT inside disk.img)
    clip_id        INTEGER REFERENCES clips(id) ON DELETE SET NULL, -- link to playable clip if any
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    file_count     INTEGER NOT NULL DEFAULT 1, -- for inode-pressure pivot (storage.md §4.3)
    archived_at    INTEGER NOT NULL,        -- start of grace window
    -- delete-state machine (storage.md §5.1):
    delete_state   TEXT    NOT NULL DEFAULT 'LIVE'
                   CHECK (delete_state IN
                     ('LIVE','DELETE_CLAIMED','DELETING','DELETED',
                      'DELETE_FAILED','QUARANTINED')),
    delete_gen     TEXT,                    -- 128-bit random token for trash naming (no wall-clock)
    bytes_freed    INTEGER,                 -- set when DELETED
    -- durability + value signals (storage.md §4.1/§4.3):
    durable        INTEGER NOT NULL DEFAULT 0, -- 1 = UPLOADED_VERIFIED (remote-verified)
    pinned         INTEGER NOT NULL DEFAULT 0, -- pin/favorite/keep
    user_disposable INTEGER NOT NULL DEFAULT 0,
    has_event_json INTEGER NOT NULL DEFAULT 0,
    has_geo        INTEGER NOT NULL DEFAULT 0,
    event_severity INTEGER,                 -- mirrors strongest related event
    sentry_flood   INTEGER NOT NULL DEFAULT 0,
    value_score    INTEGER,                 -- OPEN §5: cached-derived vs computed by retentiond
    suppress_until INTEGER,                 -- anti-thrash tombstone horizon (storage.md §5.3)
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX idx_archive_state    ON archive_items(delete_state);
CREATE INDEX idx_archive_class    ON archive_items(folder_class);
CREATE INDEX idx_archive_value    ON archive_items(delete_state, value_score);  -- governor candidate scan
CREATE INDEX idx_archive_suppress ON archive_items(suppress_until);
-- Compound index for the index-driven governor candidate query (storage.md §3,§4)
-- so eviction never degrades into a full-table scan under pressure:
CREATE INDEX idx_archive_candidate
    ON archive_items(folder_class, durable, delete_state, pinned, value_score);

-- ── archive_item_clips: many-to-many (a whole event folder = one archive_item
--    that backs several one-minute clips / angle files) ─────────────────
-- Resolves a playback/export request on a clip to ALL archive_items that must be
-- lease-protected before streaming, and lets the governor know which clips a unit
-- backs. (Both contract reviews: a single archive_items.clip_id FK is too weak.)
CREATE TABLE archive_item_clips (
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    clip_id         INTEGER NOT NULL REFERENCES clips(id)         ON DELETE CASCADE,
    PRIMARY KEY (archive_item_id, clip_id)
);
CREATE INDEX idx_aic_clip ON archive_item_clips(clip_id);

-- ── eviction_tombstones: anti-thrash record (storage.md §5.3) ─────────
-- Required so an evicted item is not re-imported/re-fetched below its horizon.
-- NOT rebuildable from media — this is durable control state (see §6).
CREATE TABLE eviction_tombstones (
    id             INTEGER PRIMARY KEY,
    source_path    TEXT    NOT NULL,        -- identity of the evicted item
    folder_class   TEXT    NOT NULL,
    size_bytes     INTEGER,                 -- mtime/size/hash where known (identity)
    mtime          INTEGER,
    content_hash   TEXT,
    reason         TEXT    NOT NULL,        -- why evicted
    delete_gen     TEXT    NOT NULL,        -- the 128-bit gen from the delete that created it
    durable_at_evict INTEGER NOT NULL DEFAULT 0, -- was a verified durable copy present?
    suppress_until INTEGER NOT NULL,        -- don't re-import before this horizon
    created_at     INTEGER NOT NULL
);
CREATE INDEX idx_tombstone_path     ON eviction_tombstones(source_path);
CREATE INDEX idx_tombstone_suppress ON eviction_tombstones(suppress_until);

-- ── leases: shape owned here, protocol owned by D3 ───────────────────
-- Canonical leasable/evictable subject is an ARCHIVE_ITEM. A webd playback/export
-- request on a clip acquires leases on EVERY backing archive_item (join via
-- archive_item_clips). Non-archived live/RO clips are NOT retention-leasable — the
-- car may rotate them; see D3 §3 + OQ-1.
-- Deadlines are BOOT-SCOPED MONOTONIC (the Pi has no RTC; a wall-clock jump must not
-- pin a lease forever or reap a live one — storage.md §5.1 same rationale).
CREATE TABLE leases (
    id              INTEGER PRIMARY KEY,
    archive_item_id INTEGER NOT NULL REFERENCES archive_items(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('upload','playback')),
    holder          TEXT    NOT NULL,       -- service+instance, e.g. 'uploadd', 'webd:<conn>'
    gen             TEXT    NOT NULL,       -- 128-bit token to detect stale renew/release
    boot_id         TEXT    NOT NULL,       -- indexd boot token; a different boot_id ⇒ stale ⇒ reaped at startup
    acquired_wall   INTEGER,                -- best-effort wall time, DIAGNOSTICS ONLY (may be wrong)
    expires_mono_ms INTEGER NOT NULL,       -- monotonic deadline (CLOCK_MONOTONIC ms) within this boot
    preempt_req     INTEGER NOT NULL DEFAULT 0 -- governor asked holder to release early (D3 §5)
);
CREATE INDEX idx_leases_item   ON leases(archive_item_id);
CREATE INDEX idx_leases_expiry ON leases(boot_id, expires_mono_ms);   -- TTL sweep

-- ── prefs/settings: UI + policy knobs ────────────────────────────────
CREATE TABLE prefs (
    key   TEXT PRIMARY KEY,                 -- e.g. 'map.view', 'units.speed', 'thresholds.hard_brake',
    value TEXT NOT NULL                     --      'storage.reserves', 'value.weights' (JSON values)
);
```

---

## 3. Read shapes the consumers depend on

| Consumer | Reads | Used for |
|----------|-------|----------|
| `webd` `/api/days`,`/api/trips`,`/api/events` | `trips`, `trip_points`, `events` | trip map + bubbles ([`webd.md §3`](../webd.md), [`spa.md §3`](../spa.md)) |
| `webd` `/api/clips/:id` + stream | `clips`, `angles` | event player ([`webd.md §3`](../webd.md)) |
| `webd` `/api/storage*` | `archive_items` aggregates, `leases` | storage health UI ([`storage.md §6`](../storage.md)) |
| `retentiond` governor | `archive_items` (state, value signals), `leases` | value-scored eviction ([`storage.md §4`](../storage.md)) |
| `uploadd` | `archive_items` (durable flag target), `leases` | mark durable, hold upload lease ([`uploadd.md §2`](../uploadd.md)) |

D2 ([`webd-api.md`](./webd-api.md)) maps these rows to JSON DTOs; D3
([`single-writer-lease.md`](./single-writer-lease.md)) governs every write to
`leases` and `archive_items.delete_state`/`durable`.

### 3.1 File identity (what `angles.file_ref` / `archive_items.path` mean)

A parallel builder needs to know *exactly* what identifies a media file, because
stability gating (`scannerd`), playback/export (`webd`), durability verification
(`uploadd`) and deletion (`retentiond`) all key off it. Contract:

- `archive_items.path` is a **Pi-side archive path** on the ext4 data filesystem
  (`/srv/teslausb/archive/…`), outside `disk.img` — the canonical deletion/upload
  identity. This is what `retentiond` renames-to-trash and `uploadd` reads.
- `angles.file_ref` is resolvable to a concrete mp4; `angles.view_kind` says which
  view: `'archive'` (a Pi-side path, the durable/playable source) or `'ro_usb'`
  (the live read-only USB view, which Tesla may rotate at any time — never
  retention-leasable, never an upload source per
  [`uploadd.md §3`](../uploadd.md)).
- Cross-view dedup uses `clips.canonical_key` (the SD-card and RO-USB views of the
  same recording resolve to one key), mirroring v1's `mapping_service.canonical_key`.

> **OPEN (OQ-6):** whether `file_ref` should additionally carry a content identity
> (size+mtime, or a hash) for archive-verification/anti-thrash, vs. relying on path
> + `canonical_key`. `scannerd`/`retentiond` builders should confirm.

---

## 4. Migration story

- **Forward-only, versioned, idempotent** ([`indexd.md §2.1,§6`](../indexd.md)).
  `indexd` is the sole migrator. On open: read `MAX(schema_version.version)`, apply
  each numbered migration `> current` in a transaction, append a `schema_version`
  row per step.
- **No down-migrations.** A schema the binary doesn't understand (version newer than
  the binary) is a hard, logged error — `indexd` does not guess.
- **Rebuild beats migrate when cheap — for the derived tables only.** The derived
  model (clips/angles/trips/trip_points/events) can be dropped+rebuilt from media if
  a migration is risky. The **durable control state** (§5) must be *preserved or
  recovered*, never silently dropped.
- **Backups before destructive steps** mirror the v1 behavior (`_backup_db` +
  retention) referenced in `.github/copilot-instructions.md`.

---

## 5. Rebuildable (derived) vs. durable (control) state

Both contract reviews flagged that "fully rebuildable" overclaims. The schema holds
two kinds of state; a rebuild must treat them differently:

| Kind | Tables / columns | On rebuild |
|------|------------------|-----------|
| **Derived** (from media) | `clips`, `angles`, `trips`, `trip_points`, `events`, `archive_item_clips` | regenerate by re-scanning + re-indexing |
| **Durable control / policy** | `archive_items.{pinned,durable,delete_state,suppress_until,value_score}`, `eviction_tombstones`, `prefs` | **preserve** across rebuild; if truly lost, recover from the sources below |

Recovery sources if control state is lost:
- `durable` ← re-verify against the cloud remote (`uploadd` reconcile) before any
  eviction trusts it ([`uploadd.md §2`](../uploadd.md)).
- `pinned` / `prefs` ← only the user has these; **losing them is real data loss** —
  back them up, don't drop them.
- `delete_state` / trash ← the startup FS↔DB reconciliation matrix
  ([D3 §4.1](./single-writer-lease.md), [`storage.md §5.1`](../storage.md)).
- `eviction_tombstones` ← if lost, the worst case is a one-time re-import of an
  evicted clip; still preferable to keep.

---

## 6. OPEN QUESTIONS (do not invent — integrator/operator to resolve)

1. **(OQ-1) Lease subject identity — RECOMMENDED RESOLUTION pending freeze.** Both
   contract reviews converged: make **`archive_item` the only leasable/evictable
   subject**; a playback/export request on a clip leases **all** backing
   `archive_items` (via `archive_item_clips`); **non-archived live/RO clips are not
   retention-leasable** (the car may rotate them — best-effort playback only). The
   schema above now encodes this (`leases.archive_item_id`, `archive_item_clips`).
   **Operator/integrator to ratify** before freeze; recorded here because it was an
   `indexd.md §4` ambiguity, not a free invention.
2. **`trip_points` storage — rows vs. compacted polyline blob.**
   [`indexd.md §2`](../indexd.md) says "sei_samples **or** compacted track". Per-row
   (above) is queryable but can be 10³–10⁴ rows/trip; a compacted `trips.polyline`
   blob (RDP-simplified, like v1's `_simplify_polyline_rdp`) is far smaller and
   matches the map render path, at the cost of point-level SQL. **Pick one (or both:
   rows + cached simplified blob) — affects DB size, WAL growth, `/api/trips` shape.**
3. **`value_score`: stored vs. derived.** [`storage.md §4`](../storage.md) says
   `indexd` persists the *signals* and `retentiond` *computes* the score. The
   `value_score` column is an optional **cache** of `retentiond`'s last computation
   (handy for `webd`'s storage UI). **Confirm whether the cache is wanted.**
4. **Timestamp encoding.** Proposed INTEGER epoch-seconds UTC (wall) for `*_at`,
   INTEGER ms for `*_offset`/`expires_mono_ms`. The v1 DB used ISO-8601 TEXT in
   places. **Confirm INTEGER vs TEXT** before readers hard-code parsing. (Lease
   deadlines are deliberately **monotonic ms**, not wall time — see §2 `leases` and
   [D3 §4.2](./single-writer-lease.md): the Pi has no RTC.)
5. **`prefs` typing.** Single key/value table (JSON values) vs. typed columns per
   policy group. Proposed key/value; confirm.
6. **(OQ-6) File content identity** — see §3.1: add size/mtime/hash to `angles` /
   `archive_items`, or rely on path + `canonical_key`?

