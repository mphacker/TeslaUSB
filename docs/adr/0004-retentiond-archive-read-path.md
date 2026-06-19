# ADR 0004 — `retentiond` archives via `scannerd` `ReadFile` + read-only `indexd` catalog; never mounts `teslacam.img`

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** operator; Opus (orchestrator) reconciled with two independent GPT-5.5 adversarial reviews
- **Scope:** `retentiond`, `scannerd`, `indexd`, `docs/specs/{retentiond,scannerd}.md`, `docs/specs/contracts/scannerd-readfile.md`
- **Supersedes:** the committed Phase-1 `retentiond` "live seams" that read the
  car volume through a **mounted** `--source-root` (`LiveRecentDirReader` =
  `fs::read_dir`, `LiveArchiveStore` = `File::open`). Those violate ADR-0003.

## Context

The committed Phase-1 archive driver (`retentiond/src/archive_driver.rs` core +
`live.rs` seams + `main.rs serve --source-root`) reads RecentClips off the car
volume by **mounting `teslacam.img`** read-only and using `std::fs`
(`live.rs:200` literally documents `source_root` as "a read-only exFAT **mount**
of the car-visible volume").

This is forbidden:

- **ADR-0003 #1 invariant:** the Pi must **never mount `teslacam.img`** — the car
  writes it continuously, so any kernel mount (even RO) is cache-incoherent and
  risks the recording path. ADR-0003 explicitly **rejected** a RO loop-mount of
  `teslacam.img`. teslacam reads go via raw `pread` only.
- **`retentiond.md` §6/§7:** archive copy uses the **raw read path**, never
  mounts the Tesla FS, and `retentiond` does **no raw parsing/indexing**
  (it *consumes* `scannerd`/`indexd` outputs).

So `retentiond` may neither mount the volume **nor** parse exFAT itself. The pure
decision core (`archive_recent_once`), the `ArchiveStore`/`RegisterClient`
contracts, and the `indexd` register server are sound; only the **source-read
wiring** is wrong. The safe byte-source — a `scannerd` raw `ReadFile` socket —
was specified in `contracts/scannerd-readfile.md` but marked *deferred*.

Two independent GPT-5.5 reviews (an architecture-defect review and a design
second-opinion) both confirmed the defect and converged on the same fix.

## Decision

`retentiond`'s archive loop reads the car volume **only** through two existing,
mount-free seams:

1. **Inventory ← read-only `indexd` SQLite catalog.** `retentiond` opens
   `indexd`'s SQLite DB **read-only** (`SQLITE_OPEN_READ_ONLY` +
   `SQLITE_OPEN_NO_MUTEX`, `busy_timeout`, `pragma query_only`, WAL) — the
   identical pattern `webd` uses (`webd/src/catalog.rs`). It selects archive
   candidates with SQL, not a directory scan:

   ```sql
   SELECT c.id, c.canonical_key, c.partition, c.folder_class,
          c.started_at, c.ended_at, c.duration_s
   FROM clips c
   WHERE c.folder_class = 'RecentClips'
     AND c.availability = 'present'
     AND EXISTS (SELECT 1 FROM angles a
                 WHERE a.clip_id = c.id AND a.view_kind = 'ro_usb')
     AND NOT EXISTS (SELECT 1 FROM angles a
                     WHERE a.clip_id = c.id AND a.view_kind = 'archive')
     AND NOT EXISTS (SELECT 1 FROM archive_items ai
                     WHERE ai.clip_id = c.id AND ai.delete_state <> 'DELETED')
     AND NOT EXISTS (SELECT 1 FROM archive_item_clips aic
                     JOIN archive_items ai ON ai.id = aic.archive_item_id
                     WHERE aic.clip_id = c.id AND ai.delete_state <> 'DELETED')
   ORDER BY c.started_at ASC;
   ```

   Per-candidate angles (`camera`, `file_ref`, `view_kind`, `offset_ms`,
   `duration_s`, `size_bytes`) come from the `angles` table. The car-volume
   source path is `angles.file_ref` (volume-root-relative, e.g.
   `TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4`); the slot is
   `clips.partition` (`slot0`). No new `indexd` RPC is added.

2. **Bytes ← `scannerd` `ReadFile` read socket.** `scannerd` gains a **second,
   dedicated** Unix socket `/run/teslausb/scannerd-read.sock` (separate from the
   single-client scan socket `scannerd.sock`) implementing the
   `contracts/scannerd-readfile.md` protocol: stateless, concurrent, positioned
   `pread` over `Volume::read_file_range` — **no mount, no write, no eject**.
   `retentiond` is its first client (the archive copy); `webd`'s live-clip map
   fallback is a later client of the same socket.

The corrected Phase-1 pipeline:

> read-only `indexd` candidate query → for each angle, loop `ReadFile` windows
> from `scannerd-read.sock` (capture `ClipIdentity` on the first chunk, echo it
> thereafter; on `Changed` abort the clip) → write + `fsync` + atomic-rename into
> the Pi-side archive, hashing the destination bytes → `RegisterArchivedClip` to
> `indexd` (flips the angle `ro_usb` → `archive`).

The `ClipIdentity` fence **is** the spec's "re-validate the source after the
copy": if the car replaced the clip mid-copy, the identity no longer matches and
the clip is skipped — never archived as stitched bytes.

### Pinned wire contract (both crates must match byte-for-byte)

`scannerd-read.sock` framing = the workspace standard: a 4-byte little-endian
length prefix then a JSON payload (`MAX_REQUEST_FRAME = 65536`); the read
response is a JSON **header** frame followed, on `ok`, by a `u32`-LE-prefixed raw
byte **tail**. `MAX_READ_LEN = 8 MiB (8388608)`.

`scannerd` (server, in `scannerd/proto.rs`) and `retentiond` (client, its own
matching serde types — the workspace's deliberate no-shared-proto-crate pattern,
exactly as `register_client.rs` mirrors the indexd register types) **must each
ship a unit test asserting these exact JSON forms**:

- **Request** (bare struct, `snake_case`):
  ```json
  {"path":"TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4","offset":0,"len":8388608,"handle":null}
  ```
  with an echoed handle:
  ```json
  {"path":"...","offset":8388608,"len":8388608,"handle":{"first_cluster":1234,"total_size":2097152,"name_hash":3735928559}}
  ```
- **Response header** (internally tagged `"status"`, `snake_case`):
  ```json
  {"status":"ok","identity":{"first_cluster":1234,"total_size":2097152,"name_hash":3735928559},"readable_size":2097152,"eof":true,"byte_len":1048576}
  {"status":"changed"}
  {"status":"not_found"}
  {"status":"out_of_range"}
  {"status":"error","message":"..."}
  ```
- `ClipIdentity` = `{first_cluster:u32, total_size:u64, name_hash:u32}`.

## Salvage map

| Survives as-is | Reworked | Retired (dead) |
|---|---|---|
| `register_client.rs` (`RegisterClient`/`UnixRegisterClient`); `indexd` register DB transaction; `archive.rs` `ArchiveStore` trait + `run_verified_pass` concept; the pure `archive_recent_once` orchestration shape | `archive_recent_once` inputs (candidate clips, not mounted-dir facts); `LiveArchiveStore` (keep dest temp/`fsync`/atomic-rename + dest hashing; replace **source** reads with the `ReadFile` client); `main.rs serve` flags (drop `--source-root`; add `--indexd-db` + `--scannerd-read-socket`) | `LiveRecentDirReader`; most of `recent_facts.rs` (it re-derived stability over a mounted listing — `scannerd` already gates stability and `indexd` already records it) |

## Consequences

- **No mount of `teslacam.img`, ever** — the #1 invariant holds; the archive loop
  is decoupled from the gadget state.
- `scannerd` gains a second socket + the `ReadFile` handler (the previously
  deferred contract; now on the critical path).
- `retentiond` gains a `rusqlite` (bundled, read-only) dependency and a
  `scannerd-read.sock` client; it keeps zero exFAT parsing.
- **Hardware staging of synthetic footage still requires bringing the USB gadget
  down first** (write `teslacam.img` only with the car detached) — orthogonal to
  this ADR but reconfirmed by the same reviews.
- Two pre-existing nits to fold in opportunistically (not Phase-1 blockers): the
  `indexd` register server's hard-coded 6-camera whitelist (`server.rs:188`)
  should accept `scannerd`'s verbatim camera labels; the volatile in-memory
  register-retry queue may drop after 5 attempts.

## Alternatives considered

- **RO loop-mount of `teslacam.img` for `retentiond`** (the committed approach).
  Rejected by ADR-0003 (cache-incoherent under the car's writes) — the defect
  this ADR fixes.
- **`retentiond` opens `teslacam.img` and parses exFAT itself (raw `pread`, no
  mount).** Rejected: duplicates `scannerd`'s parser and violates
  `retentiond.md` §7 ("no raw parsing/indexing"). `scannerd` already holds the
  read-only image fd and the exFAT primitives.
- **A new `indexd` query RPC for candidates.** Rejected as needless coupling:
  `webd` already proves read-only direct SQLite over WAL is safe and supported.
- **`retentiond` consumes `scannerd`'s `Scan` stream directly.** Rejected: the
  scan socket is single-client and owned by `indexd` for its lifetime; a second
  consumer would head-of-line block. The catalog already has the facts.
