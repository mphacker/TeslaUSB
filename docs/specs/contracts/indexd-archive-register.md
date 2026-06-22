# Contract — `indexd` archive registration RPC (`RegisterArchivedClip`)

> Parent: [`indexd.md`](../indexd.md) · [`retentiond.md`](../retentiond.md) §3.3 ·
> [`storage.md`](../storage.md) · [`ADR-0001`](../../adr/0001-scannerd-indexd-ipc-seam.md)
> Status: **DESIGN (2026-06-19)**, amended **2026-06-22** with §9 (decodability
> gate / quarantine of undecodable archive copies), reconciled with an independent
> GPT-5.5 adversarial design review (`moov-design-review`) and two GPT-5.5 diff
> reviews (`moov-review`, `moov-review2`). Opus design, originally reconciled with
> an independent GPT-5.5 design review (`rt-register-design`). Phase-1 of the
> two-phase `retentiond serve` plan (archive-only; **no deletes**). Unblocks map
> clip playback (`webd` §4.2) with zero deploy risk.

## 1. Why

`retentiond` (Phase 1) copies **complete** `RecentClips` segments from the
read-only car volume into the Pi-side ext4 archive (`retentiond.md` §3.3, the
bounded rolling mirror). For the trip map to actually *play* those archived
segments, the catalog angle must read `view_kind='archive'`: `webd`
`open_archive_angle` returns **404 unless `view_kind='archive'`**
(`webd/src/media.rs:356-375`) and then resolves `archive_root + file_ref` inside a
path jail; `view_kind='ro_usb'` renders the "not yet archived" overlay. So
archiving is only *complete* once the catalog is updated — and that write must go
through `indexd`, the **sole SQLite writer** (`indexd.md` §2.1).

A RecentClips segment first lands via `scannerd → indexd apply` as a `clips` row
+ `angles(view_kind='ro_usb')` (`apply.rs::view_kind_for` sets `'ro_usb'` for
`RecentClips`). Registration must **promote the existing angle in place** to
`'archive'`, not invent a second clip — same `clips.id`, same `(clip_id,camera)`
angle. A separate archive clip would need an artificial `canonical_key` and would
duplicate the segment on the trip map. The cost of in-place promotion: the angle
row tracks **one** source at a time, so the car copy drops out of
`list_ro_usb_angles`. That is acceptable in Phase 1 (no car-delete planning runs);
if simultaneous car+archive source tracking is ever needed, add an
`angle_sources` table — **never** duplicate clips.

## 2. Transport — a dedicated `indexd` server socket

`indexd` is today a scannerd **client** only (`indexd/src/main.rs` connects to
`scannerd.sock`); it binds no server. Add ONE Unix-domain **listener**,
`/run/teslausb/indexd.sock`, for inbound mutations from `retentiond`:

- `0o660` socket inside the `0o750` `teslausb` runtime dir; `retentiond` is the
  only client. `indexd` remains the sole DB writer — every mutation is serialized
  through this single-threaded server onto the one write connection (no second
  writer, no `SQLITE_BUSY` races).
- Short-lived handler per connection; per-connection read/write timeouts mirror
  the scan socket.

This is the registration surface anticipated by `indexd.md` §2 (delete-state /
durability / lease columns are written by `indexd` on request) and the
governor-checkpoint IPC (`indexd.md` §2.1) — the same socket later carries the
Phase-2 delete-state and lease RPCs (out of scope here).

## 3. Wire protocol (`proto.rs`)

Mirror `scannerd::proto`: control frames are JSON with a **4-byte LE length
prefix**; `read_frame(stream, MAX_REQUEST_FRAME)` with `MAX_REQUEST_FRAME =
64 KiB` (registration payloads are small — a handful of angles). `Request` is a
serde-tagged enum so the socket can grow Phase-2 verbs without a new listener:

```rust
#[derive(Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    RegisterArchivedClip(RegisterArchivedClip),
    // Phase 2 adds: delete-state, lease, checkpoint verbs (separate contract).
}

#[derive(Deserialize)]
pub struct RegisterArchivedClip {
    // --- clip identity (matches the existing scanner-produced clip) ---
    pub canonical_key: String,     // SAME key apply uses; binds to the existing clip
    pub folder_class:  String,     // origin class, e.g. "RecentClips" (NOT promoted to ArchivedClips)
    pub partition:     String,
    pub started_at:    i64,
    pub ended_at:      i64,
    pub duration_s:    Option<f64>,

    // --- the durable archive unit ---
    pub archive: ArchiveUnit,

    // --- per-camera archive angles ---
    pub angles: Vec<ArchiveAngle>, // >= 1; camera ∈ front/back/left/right/repeater set
}

pub struct ArchiveUnit {
    pub path:        String,       // archive-root-relative, DETERMINISTIC (see §6)
    pub size_bytes:  i64,
    pub file_count:  i64,
    pub archived_at: i64,
}

pub struct ArchiveAngle {
    pub camera:      String,
    pub file_ref:    String,       // archive-root-relative (view_kind discriminates — §5)
    pub offset_ms:   i64,
    pub duration_s:  Option<f64>,
    pub size_bytes:  i64,
}

#[derive(Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum Response {
    Ok { clip_id: i64, archive_item_id: i64 },
    Error { message: String },
}
```

`retentiond` sends this **after** the copy is byte-complete on the Pi (copied to a
temp name, hashed/fsync'd, atomically renamed into place) — never for a partial
file.

## 4. Indexd write — ONE transaction, single writer

On `RegisterArchivedClip`, `indexd` performs all four writes in a **single
transaction** on its sole write connection:

1. **`ensure_clip`** by `canonical_key` → `clip_id`. Reuses the existing clip;
   **does not downgrade `started_at`** (existing front-resolved value wins, like
   the scanner path) and **does not flip `folder_class` to `ArchivedClips`** — the
   segment's origin class is preserved; only the *angle* becomes archive-backed.
2. **Upsert `archive_items` by `path`** (`path` is `UNIQUE`): `folder_class`,
   `path`, `clip_id`, `size_bytes`, `file_count`, `archived_at`,
   `delete_state='LIVE'`, **`durable=0`**, `created_at`/`updated_at`. ON
   CONFLICT(path) update size/file_count/archived_at/clip_id/updated_at.
   - **`durable=0` is mandatory.** `durable=1` means *uploaded and remotely
     verified — safe to evict* (`indexd.md` §2.4; `mutations.rs:633`), NOT "exists
     on the Pi". A local archive copy is **not** durable.
3. **Insert `archive_item_clips(archive_item_id, clip_id)`** `OR IGNORE`. The
   `clip_id` back-ref on `archive_items` alone is **not** sufficient: the Phase-2
   lease path (`mutations.rs::lease_acquire_for_clip:345`) resolves a clip's
   backing items **through this join table**. Skipping it would silently break
   playback/upload leasing later.
4. **Upsert each angle as `view_kind='archive'`**, `file_ref` = the
   archive-root-relative path, via a **force-archive** upsert (§5, Guard A).

Return `Ok { clip_id, archive_item_id }`.

## 5. Precedence invariants — the recording-critical core

The car volume is rescanned continuously. Two scanner-side paths would otherwise
**destroy** the archive promotion. Both must be guarded; the guards are the heart
of this contract.

**Invariant:** *Only `retentiond`'s lifecycle (Phase 2, after marking /
quarantining / deleting the archive item) may move an angle off `'archive'` or
remove an archive-backed clip. The scanner/apply/prune path must never downgrade
or delete archive-backed catalog state.* The scanner cannot distinguish "stale
car rescan" from "archive truly gone," so it is **never** allowed to decide
`archive → ro_usb`.

**Guard A — no scanner downgrade of an `archive` angle.** Today
`ingest.rs::upsert_angle:151` ON CONFLICT **unconditionally** overwrites
`view_kind` + `file_ref`, so the next `RecentClips` rescan (which computes
`ro_usb`) would clobber playback. Split the two writers:
- **apply path** (`scannerd → indexd`) uses an **archive-preserving** upsert:
  when the existing row has `view_kind='archive'`, the incoming `ro_usb` write
  **keeps** the existing `view_kind` + `file_ref` (it MAY still refresh
  offset/duration/size). i.e. `ON CONFLICT … DO UPDATE SET view_kind = CASE WHEN
  angles.view_kind='archive' THEN angles.view_kind ELSE excluded.view_kind END`
  (and the same guard on `file_ref`).
- **registration path** uses a **force-archive** upsert that always sets
  `view_kind='archive'` + the archive `file_ref`.

**Guard B — no prune of archive-backed clips.** A *complete* scan calls
`prune_missing_clips` (`apply.rs:225`), which `DELETE`s every clip whose
`canonical_key` is absent from the car volume (`ingest.rs:227`) — cascading its
`angles`. `RecentClips` **rotates off the car by design**, so an archived segment
*will* vanish from `present_keys` and, unguarded, the prune deletes the very clip
+ angle `webd` plays from — the archive copy survives on disk (FK `ON DELETE SET
NULL`) but becomes invisible. **Fix `prune_missing_clips`**: a clip is stale only
if its key is absent **AND** it has no archive-backed catalog state — exclude any
clip that has an `angles` row with `view_kind='archive'` **or** a non-`DELETED`
`archive_items` row. (Phase-2 deletion clears the archive backing first, after
which a future prune may reclaim the clip normally.)

## 6. Idempotency

Re-registration (re-archive, retry, restart mid-batch) must converge with no
duplicates. It does iff the archive `path` is **deterministic** for a given
segment:
- `clips.canonical_key` converges (`UNIQUE`).
- `angles UNIQUE(clip_id,camera)` converges → force-archive upsert is a no-op on
  resend.
- `archive_items.path UNIQUE` → upsert-by-path is a no-op on resend.
- `archive_item_clips` PK `(archive_item_id, clip_id)` → `OR IGNORE` is a no-op.

**Pitfall:** a *changed* archive path for the same segment creates a duplicate
`archive_items` row. `retentiond` MUST use a stable archive layout (path derived
from canonical identity, not wall-clock or attempt counter).

## 7. Out of scope (explicit)

- **No deletes of anything** — no car-delete handoff, no governor/eviction, no
  lease acquisition. That is Phase 2 (gated; separate contract).
- **`webd` is unchanged.** `file_ref` stays polymorphic with `view_kind` as the
  discriminator (`ro_usb` = volume-root-relative; `archive` =
  archive-root-relative); `webd/src/media.rs` already resolves both.
  `archive_items.path` remains retention/lifecycle metadata, **not** the playback
  lookup path.
- **Which segments / when / the copy itself** — that is the `retentiond`
  archive-store + driver lanes (`p1-archive-store`, `p1-archive-driver`), not this
  RPC.

## 8. Acceptance criteria

- [ ] `RegisterArchivedClip` over `/run/teslausb/indexd.sock` (scannerd-style
      framing) writes, in one transaction: `ensure_clip` (no `started_at` /
      `folder_class` downgrade) + `archive_items`(`LIVE`,`durable=0`) +
      `archive_item_clips` + angle(s) `view_kind='archive'`; returns
      `{clip_id, archive_item_id}`.
- [ ] **Guard A:** after registration, a subsequent `RecentClips` apply of the
      same `canonical_key` (which computes `ro_usb`) **leaves** the angle at
      `view_kind='archive'` with the archive `file_ref` (regression test on the
      apply-path upsert).
- [ ] **Guard B:** a *complete* scan whose `present_keys` **omits** an archived
      segment's key does **not** delete that clip/angle; `webd` still resolves the
      archive angle. A clip with neither archive angle nor live `archive_items`
      still prunes.
- [ ] **Idempotency:** sending the same `RegisterArchivedClip` twice yields
      identical row counts (no duplicate `clips`/`angles`/`archive_items`/
      `archive_item_clips`).
- [ ] **`durable=0`** on the registered `archive_items` row (never `1`).
- [ ] Socket is `0o660` in the `0o750` runtime dir; oversized/garbage frames are
      rejected within `MAX_REQUEST_FRAME` without writing partial state.
- [ ] `webd` `open_archive_angle` serves bytes for the registered angle end-to-end
      against a fixture (the playback unblock this contract exists for).
- [x] **Decodability gate (§9):** after a copy lands, an angle that is not a
      container-complete MP4 (missing `ftyp`/`moov`/`mdat`, unparseable `mdhd`, or
      any top-level box whose declared extent overruns EOF / is malformed) routes
      the whole clip to quarantine via `RegisterQuarantinedArchive`, never to a
      playable `archive` angle (probe unit tests in `retentiond/src/probe.rs`).
- [x] **Zero clip loss on scan failure:** a probe failure or probe I/O error on an
      already-landed file routes to quarantine (bytes **kept**); only a *copy*
      failure runs `remove_dest` (driver tests in `retentiond/src/archive_driver.rs`).
- [x] **Fail-closed across deploy ordering:** `RegisterQuarantinedArchive` is a
      distinct `cmd`; an `indexd` lacking the verb returns `Error`, so `retentiond`
      defers to its pending queue rather than publishing a bad `archive` angle.
      Quarantine pendings are **not** poison-dropped (held until `indexd` accepts).
- [x] **Quarantine ingest:** `archive_items.delete_state='QUARANTINED'`,
      `durable=0`, `archive_item_clips` linked, angles **not** promoted (stay
      `ro_usb` → `webd` 404). Idempotent; an incoming `QUARANTINED` never relabels
      an existing `LIVE` row (`ingest.rs` `ON CONFLICT` CASE guard + tests).

## 9. Decodability gate — quarantine of undecodable archive copies (amendment 2026-06-22)

A force-published `archive` angle whose bytes are not a decodable MP4 makes `webd`
serve `206` on every camera while the browser never decodes (`readyState=0`). The
gate stops a not-container-complete copy from ever becoming a playable angle, while
**keeping the bytes** (zero clip loss) and **never re-archiving it in a loop**.

**Where it runs.** In `retentiond::archive_driver::archive_recent_once`, **after**
`store.copy_and_hash_dest(...)` returns `Ok` (bytes landed + fsync'd) and **before**
registration. It is *not* inside `copy_and_hash_dest` (whose `Err` path deletes the
temp file). A probe I/O/parse/limit error is **not** a copy failure → it routes to
quarantine (keep bytes), never `remove_dest`. `remove_dest` remains reserved for a
*copy* failure only.

**Container-complete probe** (`retentiond::probe::probe_file_playability`, mirrors
`scannerd::mp4probe::probe_mp4.complete`, reusing `teslausb_core::sei::mp4`): top-level
`ftyp` **and** `mdat` present, **and** a top-level `moov` whose `moov/trak/mdia/mdhd`
parses with `timescale > 0`. **Memory-bounded** for the Pi Zero 2 W: it seek-walks
top-level box headers (handling 32-bit size, `size==0` to-EOF, `size==1` 64-bit
largesize, `size<header` malformed); any top-level box whose declared extent overruns
EOF **or** whose header is malformed (truncated 64-bit, `size<header`, overflow) ⇒
`Unplayable`; it reads only the bounded `moov` body (cap `MAX_MOOV_BODY`) and **never**
reads `mdat` into memory.

**Granularity = whole-clip.** If *any* angle is not playable, the whole clip is
quarantined. The demonstrated failure mode is all-angles-bad synthetic stubs, where
whole-clip quarantine loses nothing. Per-angle partial archive is **FU-3**.

**Distinct RPC verb `RegisterQuarantinedArchive`** (a separate `cmd`, *not* a
defaulted bool — a defaulted bool would be silently ignored by an old `indexd` and
fail **open**). It carries the same payload as `RegisterArchivedClip`. An `indexd`
without the variant rejects the unknown `cmd` → `retentiond` fails **closed**
(enqueues pending; never publishes a bad angle). **Binding deploy order: `indexd`
before `retentiond`.**

**Quarantine ingest** (`indexd::db::ingest::register_quarantined_clip`, one
transaction): `ensure_clip` (unchanged); upsert `archive_items` by `path` with
`delete_state='QUARANTINED'`, `durable=0`; `INSERT OR IGNORE archive_item_clips`;
**skip** angle promotion (angles stay `ro_usb` → `webd open_archive_angle` 404 by its
`view_kind != 'archive'` gate). The `ON CONFLICT(path)` `delete_state` update is
guarded so an incoming `QUARANTINED` **never** relabels an existing `LIVE` row.
Idempotent (re-send = no-op).

**Re-archive suppression.** Once committed, the candidate SELECT
(`retentiond/src/candidates.rs`) excludes any clip with a non-`DELETED`
`archive_items` row, so a `QUARANTINED` clip is never re-selected. While a quarantine
registration is pending-retry, the driver's in-memory `canonical_key` dedupe skips
re-copying it. Quarantine pendings are **not** poison-dropped (LIVE pendings still
are), so under a deploy-order violation the clip fails closed **and holds** (no
re-copy churn) until `indexd` accepts; `MAX_PENDING` eviction remains the memory bound.

**Preserved invariants.** Guard A / Guard B (§5) unchanged: the gate only quarantines
*fresh* candidates that have no existing `archive` angle, so it never downgrades one;
Guard B prune-protects the quarantined clip (it persists as an unplayable `ro_usb`
entry, never deleted — `retentiond`'s deleter/governor leaves `QUARANTINED` untouched).

### 9.1 Follow-ups (filed, out of scope for this amendment)

- **FU-1 — remediate already-published bad angles.** Clips force-promoted to
  `archive` *before* this gate stay playable-but-broken until a narrow Guard-A
  exception + re-validation driver downgrades them. (Live clip-5 synthetic stubs;
  superseded at C3, low urgency.)
- **FU-2 — finalization-aware archiving / explicit un-quarantine.** Gate candidate
  selection on `mp4probe.complete` and allow re-archival when source bytes change, so
  a merely-not-yet-finalized segment isn't permanently quarantined. The legitimate
  `QUARANTINED → LIVE` transition is owned here (the ingest does **not** guard that
  reverse direction today; it is unreachable in the single-slot pipeline — see FU-6).
- **FU-3 — per-angle partial archive** (archive the good angles, quarantine only the
  bad ones) instead of whole-clip quarantine.
- **FU-4 — quarantined-byte accounting metric** (surface bytes held in quarantine;
  never auto-delete).
- **FU-5 — strict nested-box extent validation** in the MP4 probe (reject a child
  box whose declared extent exceeds its parent), applied **consistently** across
  `scannerd::mp4probe` and `retentiond::probe` so they don't diverge. Today both
  reuse `teslausb_core::sei::mp4::find_box*`, which clamps child extents to the parent
  range; a deliberately-malformed-but-fully-present `moov` is a theoretical gap not
  reachable by the truncation failure mode this gate targets.
- **FU-6 — slot-aware archive path** (pre-existing). `archive_item_path_for_candidate`
  derives `RecentClips/{date}/{timestamp}` from the `canonical_key` timestamp only,
  dropping the `slot:` prefix. Two clips on different slots sharing a timestamp would
  collide on one archive path (overwriting the Pi-side copy and, via the `clip_id`-keyed
  — not path-keyed — candidate SELECT, making the FU-2 reverse transition reachable).
  Unreachable in the shipped single-slot RecentClips topology and **never** loses
  source footage (the car volume is read-only), but the path scheme should include the
  slot. Predates this gate (affected the LIVE path equally).
