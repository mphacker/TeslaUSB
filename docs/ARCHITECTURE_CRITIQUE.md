# Architecture Critique — Video Lifecycle

> A critical, opinionated review of the current video-handling
> architecture (`docs/VIDEO_LIFECYCLE.md`) from the perspective of an
> engineer building for resource-constrained hardware. Where is the
> design wasteful? Where is I/O amplified? What would a from-scratch
> rebuild do differently?
>
> Nothing here is a change request. This is a candid assessment to
> guide future refactoring discussions.
>
> **Audit history:** Re-validated against the live source under
> `scripts/web/services/` (no time horizon assumed; only what the
> code does today). Every numbered concern below carries a verdict
> tag — ✅ Confirmed, 🔧 Corrected, ❌ Withdrawn, ➕ Augmented — and
> the original wording is preserved (with strike-through where
> appropriate) so a reader can see what changed and why.

---

## 1. ~~The single biggest waste:~~ Archiving everything before deciding it's worth keeping (PARTIALLY DONE — re-verified ✅)

> **Verdict: ✅ confirmed, partially mitigated.** Re-verification of
> `archive_worker.py:1383-1641` and `config.yaml:249` confirms the
> stationary-skip path:
> `_skip_stationary_recent_clips_enabled()` reads
> `archive.skip_stationary_recent_clips`, `_clip_has_gps_signal()`
> peeks the front-cam SEI on the source file, and a `True` peek
> result calls `archive_queue.mark_skipped_stationary` to mark the
> row `skipped_stationary` instead of copying. **The copy savings
> are real and present today.**
>
> What's still on the table:
>
> 1. The flag defaults to **`false`** in `config.yaml:249` — most
>    installs aren't getting the savings.
> 2. The peek runs **inside `process_one_claim`** at
>    `archive_worker.py:1629`, after the `archive_queue` row is
>    already inserted and claimed, so the queue INSERT, the claim
>    UPDATE, and the worker wakeup are still paid per stationary
>    clip. Relocating the peek to the producer
>    (`archive_producer._scan_once`) would cut those.

The current flow when the flag is **off** (default) is still
**read-from-USB → write-to-SD → read-from-SD → parse → decide →
maybe-keep**. We commit the most expensive operation (a full file
copy across the SDIO bus, ~30 MB per camera × 6 cameras ×
continuously) **before** we know whether the clip is useful.

Quantify it. RecentClips at 6 cameras × 1 minute clips × 60 minutes
= 360 files/hour, ~10 GB/hour. With Tesla concurrently writing those
*same files* through the gadget block layer, the SDIO bus is doing:

- Tesla → gadget image (write)
- Pi reads gadget image (read)
- Pi writes ArchivedClips (write)
- Tesla rewrites cleared RecentClips (write)

That's **3–4× I/O amplification on a bus the watchdog daemon has to
share**. Issue #104 (the May 12 watchdog reset chain documented in
`copilot-instructions.md`) is exactly this contention pattern
manifesting as a hang.

The architecture *acknowledges* this — it has `chunk_pause_seconds`,
`per_file_time_budget_seconds`, `load_pause_threshold`,
`inter_file_sleep_seconds`, a `nice -19 ionice idle` envelope, AND
a 90-second hardware watchdog with a real-time priority drop-in.
**All of these are duct-tape on a fundamentally too-greedy I/O
pattern.** The throttles are necessary precisely because we're
moving 5–10× more data than we keep.

### What should happen instead

1. **Default `archive.skip_stationary_recent_clips: true`** — the
   feature is built and tested; enable it.
2. **Producer-side peek.** Move the SEI peek to
   `archive_producer.enqueue_one()` so a stationary clip never
   becomes an `archive_queue` row in the first place. Eliminates
   the queue INSERT, the worker wake, and the claim UPDATE per
   stationary clip.

---

## 2. The waypoint-per-frame question — actually you're already doing this right (CORRECTED 🔧)

> **Verdict: 🔧 corrected.** The "1 sample/sec" claim holds
> (`config.yaml:101` — `mapping.sample_rate: 30`), but the original
> "telemetry columns are loaded only when a user clicks a specific
> waypoint" assertion is **wrong**:
> `mapping_queries.query_trip_route` (lines 302-306) and
> `query_events` (lines 523-577) both `SELECT` the entire telemetry
> payload — `steering_angle`, `brake_applied`, `gear`,
> `autopilot_state`, `acceleration_x/y/z`, `blinker_on_left/right` —
> for **every** waypoint they return, not on click. They run on
> every trip-detail and per-event render. The "dead weight on disk
> 99.9% of the time" framing was wrong; the real framing is "loaded
> on every render even when the polyline only needs lat/lon".

You'd ask: do we really need a waypoint per frame? Good news: **the
indexer is already at 1 sample/second**, not per-frame.

`config.yaml`:

```yaml
mapping:
  sample_rate: 30   # Extract every Nth frame (30 = ~1/sec at 30fps)
```

So a 60-second clip = 60 waypoints, not 1800. A 2-hour drive =
~7,200 waypoints. Annual heavy use ≈ 2.5M rows. SQLite handles this
fine — the issue is not row count.

**However**, the *cost per waypoint* is still bloated. Every row
carries the full telemetry payload (`steering_angle`,
`blinker_on_left/right`, `brake_applied`, `gear`, `autopilot_state`,
`acceleration_x/y/z`) and the bulk SELECTs in `mapping_queries.py`
(`query_trip_route`, `query_events`) **read those columns on every
render**. The map polyline only needs `lat`, `lon`, `speed_mps`,
`heading`, but every trip-route render pulls the full row from disk.

That's:

- More bytes per row → bigger SQLite pages → more page-cache
  pressure → more SD reads
- Fatter SELECTs → more bytes serialized to JSON → larger HTTP
  responses → slower mobile rendering
- Larger WAL deltas per insert → more frequent auto-checkpoints
- Slower `VACUUM` and slower migration backups

### What should happen instead

Two-table split:

- **`waypoints`** — `id, trip_id, ts, lat, lon, speed, heading`
  only. The map polyline + click-to-seek path. Hot, small, indexed.
- **`waypoint_telemetry`** — full payload, joined on demand. Cold
  storage.

Or even better: **only persist full telemetry at "interesting"
frames** (event detected, gear change, autopilot transition, harsh
dynamics). The rest carries lat/lon/speed/heading and that's it.
You'd cut the waypoint table size by 60–80% with zero UI impact.

Counter-argument: "but if I add a new event detector later I want
to rerun against history." Answer: re-parsing 30-day-retained clips
from `~/ArchivedClips` takes a couple hours of nice-19 work; you
don't need every byte of SEI sitting in SQLite forever. Optimize
for the 99% read path, not the 0.01% reprocess path.

---

## 3. The double-enqueue path into `indexing_queue` is unnecessary (CORRECTED 🔧)

> **Verdict: 🔧 corrected.** The original claim that the
> `register_archive_callback` watch is the redundant one is **wrong**.
> Re-verification of `file_watcher_service._notify_callbacks`
> (lines 332-369) and `_classify_paths` shows that the four
> callbacks are routed by source path:
>
> - **RO USB mount** → `register_archive_callback` only → enqueues
>   into `archive_queue` (NOT `indexing_queue`)
> - **`ArchivedClips`** → `register_callback` only → enqueues into
>   `indexing_queue` (and wakes the cloud archive worker)
> - **`event.json` arrivals** → `register_event_json_callback` only
>   → enqueues into `live_event_queue`
> - **deletes** → `register_delete_callback` only → calls
>   `purge_deleted_videos`
>
> So `register_archive_callback` is NOT redundant — it serves a
> different queue (`archive_queue`) and a different source path
> (RO USB).
>
> The actual redundancy is between `register_callback` (when an
> ArchivedClips file appears via inotify) and the archive worker's
> own `_enqueue_indexed` call (`archive_worker.py:1746`, immediately
> after a successful copy). Both enqueue the SAME path into
> `indexing_queue`. The `INSERT OR IGNORE` on the queue's UNIQUE
> constraint hides the duplication, but the work is paid twice per
> clip.

The file watcher exposes **four** callback types:

```
register_callback              — new mp4 in ARCHIVE_DIR (ArchivedClips)
register_event_json_callback   — new event.json
register_delete_callback       — file delete
register_archive_callback      — new mp4 on RO USB mount (TeslaCam)
```

Each has a single subscriber and a single destination queue
(see web_control.py lines 154-289 for the wiring), so the four are
not, on their own, mutually redundant.

The redundancy lives elsewhere: every successfully-archived clip is
enqueued into `indexing_queue` **twice**:

1. Inside `archive_worker.process_one_claim` after the copy
   succeeds, via `_enqueue_indexed` (line 1746).
2. From the watcher's `register_callback` subscription
   (`web_control._on_new_videos`) when inotify fires on the new
   ArchivedClips file.

What the duplicate enqueue costs:

- One extra `enqueue_many_for_indexing` call per clip
- One INSERT-OR-IGNORE round-trip to SQLite (a write, an fsync) —
  **not** free even when it no-ops on UNIQUE conflict
- Extra wake of the indexing worker

It exists as defense-in-depth ("what if the archive worker's
`_enqueue_indexed` fails silently?"). But the failure mode is
detectable (`_enqueue_indexed` only catches `Exception`, logs it,
and continues) and rare; the cost is paid every clip. **Trade
certainty for I/O.** Drop one of the two enqueues — preferably the
inotify path, since the worker has stronger guarantees about when
the file is fully written — and let the daily stale-scan catch any
drift.

---

## 4. The four-queue, two-database design is over-engineered (CORRECTED 🔧)

> **Verdict: 🔧 corrected.** Two factual errors in the original:
>
> 1. `archive_queue` lives in **`geodata.db`**, not
>    `cloud_sync.db` — see `archive_queue.py:5` ("Lives in
>    `geodata.db` alongside") and the schema in
>    `mapping_migrations.py:195`. The "even though it has nothing
>    to do with cloud" jab does not apply.
> 2. The actual database split is cleaner than originally framed:
>    - `geodata.db` → on-device queues + map data
>      (`archive_queue`, `indexing_queue`, `indexed_files`,
>      `trips`, `waypoints`, `detected_events`).
>    - `cloud_sync.db` → cloud-related state
>      (`cloud_synced_files`, `cloud_sync_sessions`,
>      `live_event_queue`).
>
>    That split is defensible (cloud state can be backed up /
>    restored independently of geo data), so the "bad split" claim
>    is weaker than written. The four-queue / per-clip-fsync
>    complaint still stands.

You have:

- `archive_queue` (in `geodata.db`)
- `indexing_queue` (in `geodata.db`)
- `cloud_synced_files` (in `cloud_sync.db`)
- `live_event_queue` (in `cloud_sync.db`)

Plus:

- `indexed_files` booking table (`geodata.db`)
- `cloud_sync_sessions` audit log (`cloud_sync.db`)
- Schema migrations spread across two DBs

Each clip's lifecycle touches **all four queues** in sequence.
That's:

- Clip arrives → `archive_queue` INSERT (1 fsync)
- Worker claims → UPDATE (1 fsync)
- Copy succeeds → UPDATE (1 fsync)
- Worker enqueues for indexing → `indexing_queue` INSERT (1 fsync)
- Indexer claims → UPDATE (1 fsync)
- Indexer commits waypoints + events + indexed_files row → at least
  1 fsync, often 3
- Cloud producer enqueues → `cloud_synced_files` INSERT (1 fsync)
- Cloud worker claims → UPDATE (1 fsync)
- Cloud worker marks synced → UPDATE (1 fsync)

**~10 fsyncs per clip just for bookkeeping**, before counting the
actual data writes. On an SD card, fsync is the most expensive
operation (it forces flash-controller commits). At 6 clips/minute
that's 60 fsyncs/minute of pure ceremony. (The exact count depends
on `synchronous=NORMAL` batching — `mapping_migrations.py:279`
configures NORMAL not FULL — but the order of magnitude holds.)

WAL mode helps somewhat (writes batched into the WAL), but each
commit still needs a sync.

### What should happen instead

**One queue, polymorphic.** A single `pipeline_queue` table with
`stage` column (`archive_pending`, `archive_done`, `index_pending`,
`index_done`, `cloud_pending`, `cloud_done`, `terminal`). Workers
SELECT WHERE stage matches their concern. State transitions are
single-row UPDATEs in one DB. Cuts the fsync count by 3–4×.

**One database** (optional). The "shared backup" rationale for
splitting is real (cloud state can roll forward independently of
geo data), but a `pipeline_queue` redesign collapses much of the
need anyway: the only cloud-specific state left would be
`cloud_synced_files` (final upload audit) and `cloud_sync_sessions`
(history), both of which could live in their own attached DB
without doubling queue infrastructure.

---

## 5. Two cloud uploaders (cloud_archive + LES) is a complexity tax (CONFIRMED ✅)

> **Verdict: ✅ confirmed.** All claimed coordination mechanisms
> exist in the live code:
> - `cloud_archive_service.py:2065-2088, 2274-2276` calls
>   `has_ready_live_event_work(db_path)` between cloud files (the
>   "yield to LES" inter-file check).
> - `task_coordinator.py` provides the `'cloud_sync'` /
>   `'live_event_sync'` mutual-exclusion keys both services
>   acquire.
> - `helpers/refresh_cloud_token.py` (the NM dispatcher) wakes LES
>   first and waits for its drain before triggering cloud_archive.
> - LES has its own queue (`live_event_queue` in
>   `live_event_sync_service.py:124`), its own worker thread, its
>   own retry backoff schedule, its own daily cap, its own
>   webhook.

The architecture has TWO cloud uploaders, with elaborate
coordination:

- `task_coordinator` lock to serialize them
- `has_ready_live_event_work()` polled between every cloud_archive
  file
- NM dispatcher waits up to 10 min for LES to drain before kicking
  cloud_archive
- LES has its own queue, its own worker, its own retry backoff, its
  own daily cap, its own webhook
- They share rclone helpers but not state

Why do they exist as two systems? Because LES needs **immediate**
upload of events, and cloud_archive needs **bulk catch-up**. Two
different priorities, two different cadences.

### What should happen instead

**One worker, priority-aware queue.** Single thread, single
`pipeline_queue.priority` column:

| Priority | Item type                                 | Retry policy           |
|---------:|-------------------------------------------|------------------------|
| 0        | Event (SentryClips/SavedClips), <5min old | LES backoff            |
| 1        | Event, older                              | LES backoff            |
| 2        | Geolocated trip clip                      | Standard backoff       |
| 3        | Other (opt-in)                            | Standard backoff       |

`SELECT … ORDER BY priority, enqueued_at LIMIT 1` does what the
entire LES coordination machinery does today. Eliminates the
inter-file LES poll (a SELECT per cloud file), the wake endpoint,
the 10-minute drain wait, and an entire service module.

The "LES doesn't import cv2/PIL/numpy and stays under 25 MB RSS"
rationale only matters because LES is a separate process/thread.
In a unified design the worker shares the existing service's
footprint.

---

## 6. ~~Polling fallback runs unconditionally~~ — Withdrawn (already conditional)

> **Withdrawn after re-verification.**
> `file_watcher_service._watcher_loop` only enters polling mode
> after inotify setup fails ("Falling back to polling mode" log,
> `_status['mode'] = 'polling'`). When inotify is healthy the
> 5-minute sweep does not run. My original claim was wrong.

---

## 7. Boot catch-up scan rescans everything (REDUCED scope ✅)

> **Verdict: ✅ confirmed at reduced scope.**
> `mapping_service.boot_catchup_scan` (line 1898) walks **only
> `~/ArchivedClips`** — its docstring at line 1902-1905 explicitly
> notes that the RO USB mount is no longer scanned and that the
> `archive_producer` thread handles enqueueing from the RO side.
> The high-water-mark improvement still applies but the original
> "rescans everything including the USB mount" framing is no longer
> accurate.

`mapping_service.boot_catchup_scan()` runs at every gadget_web
start and walks `~/ArchivedClips`, then bulk-INSERTs into
`indexing_queue` (deduped by canonical key against `indexed_files`).

For an installation with 10,000 archived clips, that's 10,000
stat()s + 10,000 INSERT-or-IGNORE. After a restart it's almost
entirely no-ops because everything is already in `indexed_files`.

### What should happen instead

Persist a **high-water-mark** (max mtime seen, or
last_boot_completed_at) in a small JSON file or SQLite KV row. On
next boot, only scan files newer than the watermark. Write the new
watermark when the catch-up completes. Eliminates O(everything)
boot work; reduces to O(new files since last boot).

---

## 8. ~~We index from SD but copy ALL six cameras~~ — Not a real issue

> **Withdrawn.** All six camera angles are a product requirement.
> Users browse the map, click a point on the route, and expect to be
> able to switch between front / rear / left / right / pillar views
> in the overlay player. The "skip the non-front cameras" suggestion
> would have broken that core flow.
>
> The keep/skip decision still belongs at the clip-folder level
> (covered by #1: skip the entire stationary-parked clip group when
> there's no motion and no event) — but if we keep a clip we keep
> all six cameras for it.

---

## 9. rclone subprocess per file is high per-file overhead (CONFIRMED ✅)

> **Verdict: ✅ confirmed.**
> `cloud_archive_service.upload_path_via_rclone` (line 1623) and
> the inner `rclone copy` invocation use `--transfers", "1"`
> (line 1618) per file, and the in-memory profile (line 1934)
> uses `--transfers", "1", "--checkers", "1"`. Each file pays
> the full subprocess + provider auth + TLS overhead; nothing in
> the current code amortizes that cost across files.

Each cloud upload is a fresh `rclone copy <one_file>` subprocess.
That's:

- Process exec
- Config file read
- Provider auth handshake
- TLS connect
- File transfer
- Process teardown

For small `event.json` files (<1 KB) the overhead dominates the
actual transfer by orders of magnitude. The single-rclone-at-a-time
constraint means you can't even amortize TLS across files.

### What should happen instead

`rclone copy --transfers=N <directory>` against batches. Or use
rclone's `serve restic`/mount mode (long-lived process, queue files
via stdin). Either way the per-file overhead drops dramatically.
The "one rclone subprocess at a time" rule that exists for
SDIO-bus reasons can become "one rclone subprocess at a time, but
it copies batches" — same bus pressure, less per-file ceremony.

---

## 10. The task_coordinator's per-file LES poll (CONFIRMED ✅)

> **Verdict: ✅ confirmed.**
> `cloud_archive_service.py:2065, 2088, 2276` show
> `has_ready_live_event_work(db_path)` being called between cloud
> uploads (around the inter-file boundary). It is indeed a small
> indexed `SELECT` but it fires on every cloud-uploaded file.

Between every cloud_archive file, the worker calls
`has_ready_live_event_work()` which is a SQLite
`SELECT 1 FROM live_event_queue WHERE status='pending' LIMIT 1`.
This is "O(1) indexed" and "sub-millisecond" per the docs — but it
happens at the inter-file boundary of a *cloud upload*, which is
where you'd otherwise be doing useful work or sleeping. Poll N
times per file, N×files times per session.

Single-priority-queue design (#5) makes this entire mechanism
vanish.

---

## 11. ~~Atomic copy temp file in the same directory~~ — Materially corrected (CORRECTED 🔧)

> **Verdict: 🔧 corrected — both factual claims were wrong.**
>
> 1. The temp suffix is **`.partial`**, not `.tmp` —
>    `archive_worker._atomic_copy` (line 832) writes to
>    `dest_path + '.partial'`.
> 2. The "scan every event folder for `.tmp` files" recovery cost
>    the original critique invokes is **already paid**, and
>    automatically: `archive_worker._sweep_partial_orphans`
>    (line 580) runs once at worker startup, walks
>    `~/ArchivedClips`, removes any orphaned `.partial` files left
>    by a prior crash, and skips the `.dead_letter` diagnostic dir.
>    Per its docstring (line 590-599), the sweep completes BEFORE
>    the worker begins claiming rows, and only one worker exists
>    at a time, so it cannot race a live writer.
>
> The remaining substantive critique — "the partial file lives in
> the destination directory and is visible to filesystem traversal
> during the copy" — still applies, but the post-crash recovery
> story is already handled. Staging in a single
> `.staging/` directory would still be a tidy improvement (one
> `rm -rf .staging/*` on boot is cheaper than walking every event
> dir), but it's a smaller win than originally claimed.

`archive_worker._atomic_copy(src, dest)` writes to
`dest_path + '.partial'`, fsyncs, then `os.replace`s into place.
Standard pattern. The `.partial` file lives in the destination
directory (the parent of `dest_path`), which means **the SD card
filesystem traversal sees half-written files** during the copy.
That is mitigated by:

- `_verify_destination_complete(partial)` — verifies ftyp+moov
  before the rename, so a corrupted partial never becomes a
  visible "complete" file.
- `_sweep_partial_orphans(archive_root)` at worker startup — the
  recovery sweep that was missing from the original critique.

### What should still happen

Stage in a single `~/ArchivedClips/.staging/` directory, then
rename to final location across the same filesystem (rename is
atomic on the same FS). Recovery becomes "blow away `.staging/*`
on boot" — one `os.scandir`, not a full `os.walk` of the whole
archive tree. Smaller incremental win than the original critique
implied; not a correctness fix.

---

## 12. Daily stale scan walks every indexed_files row (CONFIRMED ✅)

> **Verdict: ✅ confirmed.** `mapping_service.py:1987` (the comment
> immediately above the daily scan code path) literally says
> "every `indexed_files` row, `os.path.isfile` checks each, and
> removes...". `start_daily_stale_scan` at line 2173 schedules
> the recurring sweep.

`start_daily_stale_scan()` does an `os.path.isfile()` on every row
in `indexed_files`. With 10,000 rows that's 10,000 stat() syscalls
every 24 hours, all hitting the SD card. The doc calls this
"cheap"; at scale it isn't.

### What should happen instead

Inotify already tells you when a file is deleted
(`register_delete_callback`). A row should become stale only when:

- The delete callback fires and we missed it (inotify gap), or
- Someone manually rm'd the file outside our control

The first is rare; the second is rarer. Run the full scan
**monthly**, not daily, and rely on the delete callback for normal
operation. Or run it only when free space is low and you're about
to make retention decisions anyway.

---

## 13. WAL files don't get explicit idle-time checkpoints (REDUCED scope ✅)

> **Verdict: ✅ confirmed at reduced scope.**
> `mapping_migrations._init_db` (lines 277-286) sets
> `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`,
> `busy_timeout=15000`, `cache_size=-2000`, `mmap_size=0`,
> `temp_store=MEMORY`, `journal_size_limit=4194304` (caps WAL at
> 4 MB), and `wal_autocheckpoint=200` (every ~800 KB — 5× more
> aggressive than SQLite's 1000-page default). A search for
> explicit `wal_checkpoint(...)` calls returned no hits — there is
> no idle-time checkpoint thread today.

The remaining issue is that auto-checkpoints **still fire during a
write transaction** — when the next writer crosses the 200-page
mark. Under sustained queue churn the checkpoints land in the
middle of archive copies, not in idle time.

### What should happen instead

Run `PRAGMA wal_checkpoint(TRUNCATE)` from a low-priority idle
thread when no worker holds the task_coordinator lock. Pre-empts
the auto-checkpoint, lands the cost in idle time. Less aggressive
than dropping the autocheckpoint threshold further; just shifts
*when* the checkpoint runs.

---

## 14. The mode-switch model leaks complexity into every workflow (CONFIRMED ✅)

> **Verdict: ✅ confirmed.** `partition_mount_service.quick_edit_part2`
> exists at line 381. The 120-second stale-lock window comes from
> `LOCK_MAX_AGE` and the file-age check at lines 130-146; the
> "ensure cleanup on every code path" rule manifests as the deeply-
> nested `with _acquire_lock(timeout=timeout):` block around every
> mount/loop/LUN operation (lines 446-660). Every blueprint that
> writes to LUN1 routes through a service that wraps this. The
> original 10-step description is approximately correct — the
> actual code path is "clear LUN1 backing → unmount RO → detach
> loops → recreate writable loop → mount RW → run callback → sync
> → unmount → detach → recreate RO loop → remount RO → restore
> LUN1 backing → optional gadget rebind".

`quick_edit_part2` exists because we want to be in "present mode"
(RO mounts, gadget bound) but occasionally need to write to LUN1
(chime upload, light-show install). The mechanism is:

1. Clear LUN1 backing
2. Unmount RO
3. Detach old loops
4. Create RW loop
5. Mount RW
6. Do work
7. Sync, unmount, detach
8. Recreate RO loop, remount RO
9. Restore LUN1 backing
10. (Probably rebind gadget to invalidate Tesla's cache)

That's 10 steps, with a 120-second stale lock, an "ensure cleanup
on every code path" rule, and the whole thing happens while Tesla
might be writing to LUN0. Every blueprint that writes to LUN1 has
to remember to use the service that wraps this.

### What should happen instead

Honestly: this is hard to fix without rearchitecting the whole
gadget setup. But it's worth flagging as the **#1 source of latent
bugs** in the codebase. A simpler model would be: present LUN1
read-write to the Pi via a shared filesystem (not the gadget), and
present a separate read-only snapshot to Tesla. The LWN articles on
`overlayfs` over USB gadget describe how this can work. It would
eliminate `quick_edit_part2` entirely.

---

## 15. The archive worker and indexer should be one worker (CONFIRMED ✅)

> **Verdict: ✅ confirmed.** Two distinct workers exist — see
> `archive_worker.py` (separate `_run_worker_loop` and
> `process_one_claim`) and `indexing_worker.py` (separate
> `_run_worker_loop` and `process_claimed_item`). The archive
> worker's `_enqueue_indexed` (line 939) hands the dest path off
> to `indexing_queue_service.enqueue_for_indexing`; the indexer
> later opens that same SD-card path from scratch. Both workers
> independently call `_apply_low_priority` (`SCHED_IDLE` +
> `ionice -c 3`) per-thread.

A clip's path is:

1. Archive worker: open file, copy bytes to SD, fsync, close,
   enqueue indexing
2. Time passes (inter-file sleep, queue wakeup)
3. Indexer: open file from SD, mmap, parse SEI, parse mvhd, write
   rows, close

In step 1, the file is **open and the data is in the page cache**.
We then close it, sleep, and reopen it from a different worker,
paying the SD-card read cost a second time (page cache may have
been evicted under memory pressure on a 512 MB device).

### What should happen instead

Inline the SEI parse into the archive copy. The archive worker
reads bytes anyway — it can hand them to a streaming SEI parser. By
the time the file lands on SD, the indexing transaction is already
prepared. One queue removed, one worker thread removed, one
redundant disk read removed.

The objection "but the indexer does CPU-heavy work and we want to
throttle it separately" is real — but the indexer already runs at
nice-19, and the archive worker also throttles. Combining them
just changes which knob you turn.

---

## Where the I/O actually goes (per RecentClips clip, ~30 MB front cam)

| Operation                                                | Bytes moved | Necessary? |
|----------------------------------------------------------|-------------|------------|
| Tesla writes to gadget image (USB block layer)           | 30 MB       | Yes        |
| Pi reads gadget image for archive copy                   | 30 MB       | **Maybe** (#1: skip path exists, default off) |
| Pi writes to SD card (`~/ArchivedClips/.../front.mp4`)   | 30 MB       | **Maybe** (#1: skip path exists, default off) |
| Pi reads SD card for indexer parse                       | ~200 KB (mmap, paged) | Yes — but could be inline (#15) |
| SQLite fsyncs for queue bookkeeping                      | ~10 fsyncs  | **Reducible** — overcomplicated (#4) |
| SQLite fsyncs for waypoint inserts                       | 1–3 fsyncs  | Yes        |
| Polling fallback walks tree                              | (none unless inotify fails) | **n/a** — withdrawn (#6) |
| Boot catch-up walks ArchivedClips                        | many stat() | **Reducible** with a high-water-mark (#7) |
| Daily stale-scan stat()s every indexed_files row         | 10K stat()  | **Reducible** to monthly (#12) |
| Pi reads SD for cloud upload (if kept)                   | 30 MB       | Yes        |
| Inter-file `has_ready_live_event_work` SELECT            | ~1 SELECT/file | **Reducible** if LES merges into one queue (#10) |
| rclone subprocess per file overhead                      | TLS handshake | **Reducible** (#9) |
| WAL auto-checkpoint during writes                        | random      | **Reducible** with explicit idle checkpoint (#13) |

---

## What I'd do if rebuilding from scratch

1. **Decide first, copy second.** Peek SEI on the RO mount,
   classify (event / driving / stationary), then copy only what
   you'll keep. Stationary parked + no event → skip the whole
   six-camera folder. The skip path already exists (#1) but is
   off by default and runs post-claim — flip the default and move
   the peek to the producer.
2. **One pipeline_queue, one DB.** Stage column drives state
   transitions; one priority-aware worker drains it. Cuts fsyncs
   by 3–4×.
3. **One cloud worker, prioritized.** Delete the
   `live_event_sync_service` entirely; LES becomes "rows with
   priority 0–1 in pipeline_queue."
4. **Hot/cold split waypoints.** Tiny `waypoints_hot` (lat/lon/speed
   /heading) for the map polyline; `waypoints_cold` joined on
   demand for the inspect-this-frame view. Today
   `mapping_queries.query_trip_route` reads the full telemetry
   payload on every render even though the polyline only needs
   four columns.
5. **Parse during copy.** Merge archive_worker and indexer; SEI
   parse runs against the bytes already in flight.
6. **Drop the duplicate indexing-queue enqueue.** Either
   `_enqueue_indexed` (worker direct call) OR the inotify-on-
   ArchivedClips `_on_new_videos` callback — not both.
7. (n/a — polling fallback already conditional; #6 withdrawn.)
8. **High-water-mark boot catch-up.** Don't restat the entire
   ArchivedClips tree on every restart.
9. **Stale-scan monthly, not daily.** Inotify delete callback
   handles the common case.
10. **Batched rclone.** `--transfers=N` against staged batches, not
    one subprocess per file.
11. **Explicit WAL checkpoints from idle.** Pre-empt the
    writer-stalling auto-checkpoints.
12. **Stage to `.staging/`, then rename across same FS.** Cleaner
    crash recovery.

The current architecture works because of heroic throttling and a
90-second hardware watchdog timeout. Rebuilt around the principles
above, it would do the same job with ~5× less SD-card wear, ~3×
fewer fsyncs, no need for the load-pause guard or the chunk-pause
throttle, and a much smaller blast radius for any single component
failure.

The Pi Zero 2 W is not the constraint here. The architecture is.

---

## Source files

This critique references but does not propose changes to:

- `scripts/web/services/file_watcher_service.py`
- `scripts/web/services/archive_worker.py`,
  `archive_producer.py`, `archive_queue.py`, `archive_watchdog.py`
- `scripts/web/services/indexing_worker.py`,
  `indexing_queue_service.py`
- `scripts/web/services/mapping_service.py`,
  `mapping_queries.py`, `mapping_migrations.py`
- `scripts/web/services/cloud_archive_service.py`
- `scripts/web/services/live_event_sync_service.py`
- `scripts/web/services/task_coordinator.py`
- `scripts/web/services/sei_parser.py`
- `scripts/web/services/partition_mount_service.py`
  (`quick_edit_part2`)
- `config.yaml` (`mapping.sample_rate`,
  `archive.*`, `cloud_archive.*`, `live_event_sync.*`)
