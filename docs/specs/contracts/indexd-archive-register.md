# Contract — `indexd` archive registration RPC (`RegisterArchivedClip`)

> Parent: [`indexd.md`](../indexd.md) · [`retentiond.md`](../retentiond.md) §3.3 ·
> [`storage.md`](../storage.md) · [`ADR-0001`](../../adr/0001-scannerd-indexd-ipc-seam.md)
> Status: **DESIGN (2026-06-19)**. Opus design, reconciled with an independent
> GPT-5.5 design review (`rt-register-design`). Phase-1 of the two-phase
> `retentiond serve` plan (archive-only; **no deletes**). Unblocks map clip
> playback (`webd` §4.2) with zero deploy risk.

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
