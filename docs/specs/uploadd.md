# uploadd — durable, resumable, throttled cloud-upload daemon

Status: **NOT FROZEN — reconciled design draft with OPEN ITEMS** (cloud-sync
buildout, 2026-07). A first draft was frozen-reviewed twice (Tier-3 adversarial);
the second pass **overturned several code-level premises** the draft relied on
(see `files/cloud-p0-review-reconciliation.md`, cycle 2). Those premise errors are
corrected below; the remaining wire-level details are marked **OPEN** and are to
be finalized in their implementing phase against the compiler — **not** pre-frozen
here. Do not treat any table below as a frozen contract until its OPEN items close.

> **⚠️ Corrected premises (verified against code, 2026-07):**
> - The archive driver is **Phase-1 `RecentClips`-only** (`archive_driver.rs`
>   hardcodes `folder_class: "RecentClips"`); it does **not** register Sentry/Saved
>   event directories. Cloud-uploading *events* is therefore **gated on an
>   archive-registration path for those folders that does not exist yet** (§2).
> - The durable queue identity is a **single `ArchiveItemId(i64)`**
>   (`queue.rs` — `QueueItem.id`, `UploadQueue` dedups/`get`/`select_next` on it).
>   Modeling per-child objects requires a **queue-identity type change** in P1/P3;
>   it is not an existing capability (§3.1).
> - retentiond publishes storage pressure as a **file**
>   (`/run/teslausb/retentiond.governor.json`, `{uploads_allowed: bool}`) — **no
>   socket, no `seq`, no action** (§6.2).
> - The upload **lease is boot-scoped monotonic ms** (`leases.boot_id` +
>   `expires_mono_ms`), **not** unix-seconds wall-clock (RTC-less target) — see the
>   schema contract.
>
> **OPEN ITEMS (finalize in-phase, not here):** exact throttle-action precedence
> ordering (`gate()` is link-first today); the cancellable rclone child-supervisor
> + thread-safe lease seam; the safe-open **FD-handoff** mechanism that closes the
> openat2→pass-pathname-to-rclone TOCTOU (§4/§6.3).

Concept source: v1
[`CLOUD_ARCHIVE.md`](https://github.com/mphacker/TeslaUSB/blob/main/docs/CLOUD_ARCHIVE.md).
Ported idiomatically to Rust — **no Python**.

---

## 1. Purpose & scope

`uploadd` copies dashcam events off the Pi to an operator-configured cloud
remote — automatically, in priority order, without knocking WiFi offline, and
resumably across crashes. It is the B-1 home for v1's Cloud Archive worker.

**In scope:** *what* to upload, *in what order*, staying under the WiFi TX cap,
resuming without duplication, holding an upload lease so the space governor can't
evict a file mid-transfer, integrity-verifying each transfer, and flagging
durability so retention can delete the car-side copy.

**Out of scope (owned elsewhere):** producing the archive (`retentiond`
`archive_driver`); deleting anything (`retentiond` is the sole deleter —
`single-writer-lease.md` §4; uploadd has **no delete seam**); deriving the WiFi
cap/link state (`wifid`, D4); storage backpressure (`retentiond` storage plane);
credential encryption (`cloud-provider-creds.md`); persistence (indexd is the
sole SQLite writer — `indexd-cloud-schema.md`).

## 2. The unit of work: archive item + child objects (D2) — **gated on event registration**

An archived clip/event is a **group of camera-angle files** (plus, for real
Tesla events, an `event.json`), registered as **one `archive_items` row**. The
Phase-1 `archive_driver.rs` proves this shape for `RecentClips` (one
`ArchiveRegistration` with N `angles`), but it **hardcodes
`folder_class: "RecentClips"` and does not register Sentry/Saved event
directories**. So:

- **PREREQUISITE (not owned by cloud-sync):** uploading Sentry/Saved *events*
  requires an archive-registration path that catalogs those folders (with their
  angle files + `event.json`) as `archive_items`. Until that exists, cloud-sync
  can only target what indexd actually holds (RecentClips-class archives). This
  gate is tracked in `plan.md`; do not assume Sentry/Saved archive rows exist.
- A single `rclone copyto` + a single hash cannot upload/verify a whole group, so
  the model is **parent `archive_items` row + child objects** (one child = one
  angle file). The parent's `durable` flag flips **only when every child is
  verified** (§6.6 / D5).
- **Queue identity is the child, not the parent (D7, FROZEN).**
  `queue.rs::QueueItem.id` is a single `ArchiveItemId(i64)` and `UploadQueue`
  dedups/selects on it, so N children of one parent collapse to one item — a bug
  for events. P3 rekeys the queue on the **database primary key
  `QueueKey(destination_id, remote_key)`** (mirroring `cloud_upload_queue`), and
  adds `archive_item_id` + `child_key` as carried fields. The **queue identity and
  the lease subject are deliberately different**: children are selected/retried
  independently by `QueueKey`, but the delete-blocker **lease is acquired on the
  parent `archive_item_id`** (§6.6). A per-parent-held lease is impossible because
  the priority queue interleaves children of different parents, so the lease is
  acquired/released **per child transfer**; the brief release→re-acquire window is
  an accepted best-effort gap (the lease is eviction-avoidance, not a transaction —
  idempotent retry re-uploads if the item survives).
- **The queue must be seeded; `cloud_candidates` does not discover.**
  `cloud_candidates` SELECTs `FROM cloud_upload_queue` — it is a **folder-filtered
  ready-view over the already-seeded queue**, not catalog discovery (an empty queue
  yields no candidates). Queue rows are seeded from the archive catalog by
  **(a)** the event-registration/finalize path (new events, when `auto_sync` + the
  folder category are enabled) and **(b)** a **catalog-discovery RPC** for
  pre-existing / toggle-on backfill (scans `archive_items` children `LEFT JOIN`
  the queue for not-yet-queued, not-durable, category-enabled items). uploadd never
  raw-scans the filesystem — a scan cannot derive `archive_item_id`. indexd returns
  source paths + a reusable local content hash; the **backend-specific**
  verification hash is derived by uploadd after it knows the provider (D3).

## 3. The core (host-testable, pure)

The core library `uploadd::*` decides policy behind traits (seams); real side
effects live in the gated binary (§7). Seams: `source::ArchiveSource`,
`transfer::Uploader` (Rust-uploader path only — see §5), `rclone::CommandRunner`
(the chosen path), `lease::LeaseClient`, `queue::QueueStore`,
`durability::DurabilityClient`, `throttle::ThrottleSource`, `time::Clock`/`Waiter`.
Both `engine::UploadEngine` (chunked) and `rclone::RcloneUploadEngine`
(whole-file) satisfy the **per-item** `serve::UploadProcessor` contract, and
`serve::Scheduler` drives either unchanged.

### 3.1 The durable child-object queue (state machine)

`queue.rs::UploadState`: `Queued → InProgress → Done` (terminal), with
`InProgress → Failed(reason, reset_offset)` and retry `Failed → InProgress`.

`QueueItem` (one child object): `key` (`QueueKey(destination_id, remote_key)` — the
database PK, D7), `archive_item_id` (the parent event, the lease subject),
`child_key`, `source_rel`, `category`, `seq` (FIFO tiebreak), `total_bytes`,
`verify` (`VerifySpec`, §3.3), `state`, `bytes_uploaded`, `attempts`, `not_before`,
`last_error`.

- **Idempotent enqueue** dedupes on `QueueKey` (D7). Re-offering the same key is a
  no-op. Readiness (`select_next`) also honors the persisted `not_before` backoff.
- **Resume granularity is backend-dependent (D1):** the state machine *models* a
  byte checkpoint, but the **rclone backend resumes at whole-file granularity**
  (`bytes_uploaded ∈ {0, total}`; `copyto` overwrites, so a restart re-copies the
  whole file — safe, non-duplicating). Sub-file checkpointing is reserved for a
  future Rust uploader.
- **Completion is terminal**; **`Failed` is retryable** while
  `attempts < max_attempts` **and** its persisted **not-before** backoff has
  elapsed (M2).

### 3.2 Priority order

`PriorityPolicy` ranks `UploadCategory`: **`EventSentry` → `Trip` → `Bulk`**,
FIFO by `seq`. The candidate step (§6) computes v1's order
`folder_index * 1000 + content_score` (folder axis dominates; oldest-event-first
within a folder), then maps **folder-class → category** per the pinned mapping in
`indexd-cloud-schema.md` §4 so the pure policy reproduces it. Priority reorder
changes only `seq`/ordering — **never** `remote_key` derivation (D7).

### 3.3 Transfer & integrity (D1, D3)

The chosen backend is **rclone whole-file** (§5). Integrity is **capability-based
per backend (D3)** — there is no universal SHA-256. The core carries a
**`VerifySpec` per child** (replacing the sha256-only `ContentHash([u8;32])`, which
`verify_digest` currently compares directly and is silently wrong for md5/sha1
backends):

- **`VerifySpec::Native { alg, expected }`** — the backend exposes a usable hash
  (e.g. S3/B2/Drive). uploadd computes the local `expected` in **`alg`**, fetches
  the remote hash via `rclone hashsum <alg>`, and requires an exact match. It then
  commits the **actual** value + alg. If the requested `alg` cannot be produced,
  it **fails** (never opportunistically commits a different-alg hash).
- **`VerifySpec::CopyIntegrity`** (`verify_alg = "none"`) — the remote exposes **no
  usable hash** (some WebDAV/SFTP/SMB/FTP configs, per `cloud-provider-creds.md`
  §8). Durability = a `copyto` that exits 0 **plus** an explicit remote-size
  confirmation; commit records `hash=""`, `hash_alg="none"`. This is a first-class
  supported path, **not** a rejection.
- A full `--download` re-verify is **only** used when explicitly enabled and its
  2× bandwidth is calibrated and throttled — never silently.

The per-row `expected`/`verify_alg` are persisted by indexd and **must be surfaced
on `cloud_queue_load`** so verification survives a restart, and
`cloud_upload_commit` **validates the supplied hash/alg against the queued
expectation** (P1.5, §6.1). The per-backend capability + durability criterion are
frozen in `cloud-provider-creds.md` §8.

## 4. Hard invariants

1. **Source only from the archive — enforced, not lexical (D10).** Beyond
   `ArchiveRoot::resolve`'s textual guard, the live `ArchiveSource` opens through
   a root-directory FD with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
   RESOLVE_NO_MAGICLINKS)`. Because `rclone copyto` re-opens its source **by
   pathname** (a TOCTOU hole — a symlink swapped after `resolve` would redirect the
   read), rclone is **not** handed the archive pathname: uploadd holds the
   `openat2` FD (no `CLOEXEC`) and passes **`/proc/self/fd/N`** as the copyto
   source, which the inherited-FD rclone child re-opens to the exact inode uploadd
   opened — closing the TOCTOU and making `ArchiveSource` the sole opener on the
   rclone path. The root is validated absolute once at startup.
2. **Never deletes Pi-side files.** No delete seam exists.
3. **Never exceeds the WiFi TX cap.** The rclone path bounds rate with
   `rclone --bwlimit` seeded from the wifid cap (the "belt"); the kernel `tc` cap
   is the "braces". Uploads **stop entirely** when the throttle gate says pause
   (§6.2). *(The D4 `max_chunk_bytes` per-write ceiling is **not** enforceable by
   rclone and applies only to the Rust-uploader `Pacer` path — D1.)*
4. **Never reboots/restarts anything.** Failures retry in-queue with backoff.

## 5. Backend decision — rclone, **PROVISIONAL, locked at Phase 8 (D1)**

`rclone::RcloneUploadEngine` implements `UploadProcessor` directly: whole-file
`rclone copyto` (paced by `--bwlimit`) + capability-based verify (§3.3), behind
the `CommandRunner` subprocess seam. Chosen for provider breadth + v1 parity.
The `transfer::Uploader` chunked seam is preserved for a future Rust uploader but
is **not** on the rclone path.

The decision is **provisional until the Phase 8 WiFi TX-cap spike proves** rclone
(with `--bwlimit`, `--buffer-size 0`, one transfer) stays under the measured
BCM43436 SDIO-deadlock threshold on the Pi Zero 2 W. If it cannot, the fallback
is the in-process Rust uploader (which *can* honor `max_chunk_bytes`). Do not
mark the backend frozen before Phase 8.

## 6. Live-wiring contract (`uploadd serve` — the P3 deliverable)

Today `main.rs serve` prints + exits `FAILURE`. P3 provides the **live seam
implementations** and runs `serve::Scheduler` + `RcloneUploadEngine`.

### 6.0 Read-only control channel (implemented first)
Before any cloud mutation verbs, `uploadd` now exposes a minimal read-only Unix
socket status verb for `webd`:

- socket: `/run/teslausb/uploadd.sock` (configurable),
- framing: 4-byte LE length + JSON (64 KiB cap),
- timeout: 15 s read/write,
- request: `{"cmd":"get_status"}`,
- response: `{"status":"uploadd_status","configured", "provider_type",
  "uploader_state","sync_now_state":"unsupported"}`.

This channel is intentionally non-mutating and never returns secrets.
`sync_now`/other control mutations are intentionally deferred until webd
auth+CSRF exists and mutations have durable accepted/job semantics.

### 6.1 Persistence — indexd RPC clients
Client seams over the indexd control socket (framed-JSON `{"cmd":…}`, mirror
`webd::indexd_client::UnixIndexdClient`): **`QueueStore`** (`cloud_queue_load` +
`cloud_queue_upsert`), **`LeaseClient`** (`upload_lease_acquire/renew/release`),
**`CommitClient`** (`cloud_upload_commit`), **`FailClient`** (`cloud_upload_fail`),
**`ConfigClient`** (`cloud_config_get`), and **`DiscoverClient`** (the catalog seed,
§6.4). The verbs exist (P1, indexd migration v6); envelopes/error-mapping/TTL
units/pagination are frozen in `indexd-cloud-schema.md` §3. **`DurabilityClient` is
NOT wired** — `cloud_upload_commit` returns `durable_parent` and flips
`archive_items.durable` atomically on the last child, so it is the single
durability authority (§6.6). **P1.5 amendments (this phase reopens indexd):**
`cloud_queue_load` must also surface `expected_hash` + `verify_alg` (they exist as
columns but are write-only today, so restart verification is impossible), and
`cloud_upload_commit` must validate the supplied `hash`/`hash_alg` against the
queued expectation and compare replays per-alg (not sha256-only).

> **⚠️ v7 SUPERSESSION (FROZEN — `indexd-cloud-schema.md` §7):** the sealed-upload-set
> durability protocol **removes** commit's durability authority. `cloud_upload_commit`
> **no longer flips `archive_items.durable`** (`maybe_flip_parent_durable` is deleted);
> it keeps returning `durable_parent` for wire-compat but **always `false`** (deprecated).
> Durability is owned solely by **`cloud_finalize_parent_upload`** (§6.8). uploadd gains
> two seams — **`PrepareClient`** (`cloud_prepare_parent_upload`) and **`FinalizeClient`**
> (`cloud_finalize_parent_upload`) — and `CommitClient`/`FailClient` calls now **carry the
> `upload_set_id`** (queue PK alone is unsafe: a superseding prepare can retag a
> `remote_key`). The old per-child `DurabilityClient` is retired.

### 6.2 Throttle — two planes, freshness (D4)
`ThrottleSource` combines the **wifid link plane** (`WifiThrottle`) and the
**retentiond storage plane** (`StoragePressure`); `ThrottleSnapshot::gate()` is
**link-first (FROZEN)** — pause if `!wifi.uploads_allowed || wifi.max_tx == 0`,
then storage, else `Run`. `DrainNoNew` means finish the in-flight child and take no
new work; on the whole-file rclone path `pause_at_checkpoint` collapses to
`abort_resume_later` (kill the child, resume whole-file later).

- **Transport (FROZEN):**
  - *wifi plane* — poll `{"cmd":"get_ap_status"}` on `/run/teslausb/wifid.sock`
    (request/response, same framing); read `.throttle` (a `ThrottleState` carrying
    `seq` + the full body). **No new `get_throttle` verb** and no file.
  - *storage plane* — retentiond publishes `/run/teslausb/retentiond.governor.json`.
    Today `GovernorStatus` carries **no** `uploads_allowed`/`seq` (the decision
    `tier < Emergency` is derived internally and never serialized). **P3d enriches
    it to v2** (`uploads_allowed` + `seq` + `interval_secs` + `publisher_instance`,
    keeping `updated_at`), populated from the **final** `GovernorAssessment` — note
    retentiond assesses *before* archive/drain but writes status *after* drain, so
    publishing the pre-drain decision would be a bug. uploadd maps
    `uploads_allowed=false → DrainNoNew`.
- **Freshness — consumer-monotonic (D4):** measure age by the **consumer's
  monotonic clock since it last observed a new publication** (not
  `now_epoch − updated_at`; the Pi is RTC-less). Staleness window =
  `max(3 × interval_secs, 60 s)` (default interval 20 s). A **lower** `seq` is
  rejected; a publisher restart carries `publisher_instance` so a reset `seq` is
  not mistaken for fresh. Fail **closed** (`uploads_allowed=false`) when the last
  good receipt exceeds the window, or either plane is unreadable/malformed. Never
  authorize an upload before a fresh *allowed* state (`WifiThrottle::closed()` is
  the boot default).
- **Decode fix (FROZEN):** uploadd's `throttle::PauseReason` must gain
  **`ApConcurrent`** (wifid emits `ap_concurrent`; today uploadd cannot deserialize
  it). Actions: `run`, `drain_no_new`, `abort_resume_later`, `pause_at_checkpoint`.

### 6.3 Archive reads — `ArchiveSource`
`size`/`read_chunk` over ext4, accepting only `ArchivePath`, via the D10 `openat2`
safe open. Used to compute a child's `VerifySpec::Native` `expected` at candidate
time — **reuse the hash retentiond already computed at archive-copy time** where
available (M3) rather than re-reading. For the transfer, uploadd opens the file via
the same safe `openat2` and hands rclone **`/proc/self/fd/N`** (§4 invariant 1),
so rclone never re-resolves the pathname.

### 6.4 Candidate discovery → dedup → enqueue
On a timer, on WiFi-connect, and on a `sync-now` signal, in two steps:

1. **Seed** the queue from the catalog via `DiscoverClient` (`cloud_discover`):
   indexd returns the **parent `archive_items`** that still need syncing —
   `durable = 0`, `delete_state = 'LIVE'`, and `folder_class` in the
   **config-enabled** set (`SentryClips`/`SavedClips`, plus `RecentClips` when
   `recent_enabled`; **`ArchivedClips` is a retentiond *destination*, not a source
   `folder_class`**, so it is never a key). It does **not** enumerate children —
   indexd has no FS access. For each returned parent, uploadd uses `ArchiveSource`
   (§6.3, `openat2`) to enumerate the child objects, compute each child's
   `content_sha256` (dedup key) + `VerifySpec` `expected` at candidate time, and
   idempotently `cloud_queue_upsert` one `queued` row per child. New-event seeding
   also happens server-side in the P0.5 finalize path (§2); `cloud_discover` is the
   backfill / toggle-on path. Re-emitting an already-seeded parent is harmless — the
   per-child upsert dedups.
2. **Drain:** request the ready-view (`cloud_candidates`, paginated) in priority
   order and transfer each child (§6.6).

Dedup on upsert: match by `QueueKey(destination_id, remote_key)` + `content_sha256`
/ size (D7) — skip only on a hash/size match; a differing-hash collision on the
same key **parks** for operator inspection, never overwrites. Config
(`cloud_config_get`) supplies folders/priority/reserve/retry/toggles.

### 6.5 Reserve gate (M6)
Before starting a new event, if `reserve_gb` is set, require
`remote_free − reserve_gb ≥ next_event_size` (via `rclone about`). When the
backend doesn't support `rclone about`, skip the check and log once; do not block.

### 6.6 Loop & atomic completion (D5, M2, M3)
Per child, a **single cancellable supervisor loop** (not a separate renewal
thread) owns the transfer: throttle-gate → resolve safe `ArchivePath` + `openat2`
FD → acquire the parent upload lease → **spawn `rclone copyto` in its own process
group** (`--bwlimit`, `--retries 1`, `--buffer-size 0`, `/proc/self/fd/N` source,
bounded output, timeout) → **poll the child + throttle + lease-renewal every
250–500 ms**. On a cap *reduction*, `abort_resume_later`, or a **failed renew**:
`SIGTERM` the group, grace, `SIGKILL`, then `wait`/reap (never signal after
`try_wait` reports exit). Supervise the `rclone hashsum` check the same way. Then
verify per §3.3, do a **final synchronous lease renew immediately before commit**,
`cloud_upload_commit` **within the renewed TTL**, and release the lease. On
failure: `cloud_upload_fail` (persist attempts + capped-exponential **not-before**
backoff with jitter; auth/config errors classified terminal) → retry per policy →
push a **sanitized** failure to webd's JobHub (M5).

**Atomic completion (D5):** a verified child is committed with **one idempotent
`cloud_upload_commit` transaction** (keyed by `attempt_id` + `QueueKey`) that
records the dedup row, appends history, completes the queue row, and — **when all
of the parent's children are Done** — sets `archive_items.durable=1`. Never mark
the parent durable on a single child. **Two failure races (FROZEN):** if the
**final renew fails before commit**, do **not** commit — leave the verified remote
object as an orphan and retry idempotently later; if the **commit call times out**,
do **not** record a failure — replay the **same `attempt_id`** (the server may have
committed), which the idempotent transaction absorbs.

> **⚠️ v7 SUPERSESSION (FROZEN — §6.8, `indexd-cloud-schema.md` §7):** the
> "when all children Done, commit sets `durable=1`" clause above is **retired** — it
> is the footage-loss bug (a COUNT over the mutable queue flips durable before the
> full immutable set is proven backed up). Under v7, commit **never** touches
> `durable`; the parent goes durable only via `cloud_finalize_parent_upload` proving
> the sealed set COMPLETE (§6.8). The idempotency + two failure races above still hold
> for the per-child commit.

### 6.7 Enablement gate
`serve` going live is gated on the **Phase 8 TX-cap spike** (§5) and the security
prerequisites (webd auth — D8). On enablement, `uploadd.service` moves
`TESLAUSB_STAGED_SERVICES` → `TESLAUSB_APP_SERVICES`, gains `LoadCredential=`
(`cloud-provider-creds.md` §5), and the **resource bounds are mandatory (M3):**
`Nice=19`, `IOSchedulingClass=idle`, one transfer/checker, bounded subprocess
output, timeouts, and cgroup memory/IO/CPU limits — `OOMScoreAdjust=900` is a
tiebreaker, not a bound.

### 6.8 v7 sealed-upload-set sequencing (FROZEN — `indexd-cloud-schema.md` §7)
Durability is now proven per **parent**, bound to an **immutable generation**, not
inferred from a per-child count. For each discoverable parent (`cloud_discover` now
surfaces `manifest_digest` + `path`; a parent is a candidate only when `LIVE`,
`durable=0`, `manifest_digest != NULL`):
1. **Acquire + continuously renew one parent upload lease** (existing `LeaseClient`,
   `kind='upload'`, boot-scoped monotonic TTL).
2. **Enumerate + hash the complete immutable generation** (every child object —
   clips *and* sidecars). Missing/extra children here are the whole hazard, so the
   enumeration must be exhaustive and stable.
3. **`cloud_prepare_parent_upload(archive_item_id, destination_id,
   source_manifest_digest, children[])`** → `{upload_set_id}`. indexd **reconstructs**
   the FNV manifest digest from the child array and requires triple-equality with the
   parent's stored `manifest_digest` (an omitted child is rejected), then seals the set
   + tags the queue rows atomically. Idempotent on the request digest (safe to replay
   after a crash). A different current set is superseded; digest-less parents (no
   event registration yet) are **rejected — fail closed**, never force-durable.
4. **Drain only the queue rows belonging to that `upload_set_id`** under
   throttle/lease (the §6.6 supervisor loop is unchanged per child).
5. **Commit each verified child** with **both `upload_set_id` and the queue key**
   (`cloud_upload_commit`); commit never flips durable. `KeepExisting`/retry may mark a
   row `done` only on **matching synced evidence** (hash+size+alg+verify), never
   unconditionally. Sealed children may not use `verify_alg='none'`.
6. **`cloud_finalize_parent_upload(upload_set_id)`** once all sealed children commit.
   indexd flips `durable=1` **iff** the COMPLETE predicate holds (sole current set,
   digest matches, count identity over members+queue, every child `done` and its
   `content_sha256` matches the sealed member). **Retry finalize after a reboot** until
   it returns durable or a deterministic supersession/rejection. Release the lease.
If the event **grows mid-upload**, P0.5's `finalize_event_archive` writes a new
generation (new digest) and marks the old set superseded → a late
`cloud_finalize_parent_upload` on the stale set correctly **refuses** to flip. This is
why durability binds to the immutable generation, not the mutable source folder.

**Scope (B1):** these seams + the sequencing are built and fully tested now against
indexd v7. In production, parents carry a `manifest_digest` only once P0.5's
event-arm daemon populates it; until then event parents are digest-less and step 3
fails closed (no durable, no eviction) — intended, not a regression.


- **Backend:** rclone, provisional/Phase-8-locked (§5).
- **Persistence home:** indexd migration v6 (+ P1.5 amendments, §6.1), not a
  separate `cloud_sync.db`.
- **Self-pace (rclone path):** `rclone --bwlimit` (not the in-process token
  bucket, which is the Rust-uploader path only).
- **Unit of work:** child object; parent-event durability (D2).
- **Queue identity (D7):** `QueueKey(destination_id, remote_key)` (the DB PK) —
  distinct from the lease subject (parent `archive_item_id`), acquired/released per
  child.
- **Enqueue source:** finalize seeds new events + a `DiscoverClient` catalog scan
  for backfill; `cloud_candidates` is a ready-view, not discovery (§6.4).
- **Integrity:** per-child `VerifySpec` (`Native{alg,expected}` | `CopyIntegrity`);
  commit records the actual backend hash+alg (§3.3).
- **Storage plane:** governor.json v2 published from retentiond's final assessment;
  consumer-monotonic freshness, fail-closed (§6.2).
- **Cancellation/renewal:** a single cancellable supervisor loop (process-group
  kill; final sync renew before commit; commit-timeout replays `attempt_id`),
  superseding the earlier "background renewal thread" (§6.6).
- **Durability authority:** `cloud_upload_commit` (`DurabilityClient` not wired).
- **Lease timing:** TUNABLE config (§6.6).
