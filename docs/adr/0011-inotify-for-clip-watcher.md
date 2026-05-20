# ADR-0011 — `inotify` for the clip watcher

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-20 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 4b.2 (indexer) |
| Triggers | Charter §"ADR discipline" — new third-party dep |

## Context

Phase 4b.2b adds the clip-watcher half of the indexer. The
watcher is the one component that decides *when* the SEI
walker (4b.1z) runs against a clip. It must:

* Notice a new `*.mp4` file in `RecentClips`, `SavedClips`,
  or `SentryClips` within seconds (Tesla writes one clip per
  minute per camera; lag adds to the cleanup-worker's
  reaction-to-pressure budget).
* Notice the file is **fully written** before walking it.
  Tesla writes the clip, then `close()`s — walking on the
  first `IN_MODIFY` would race against an incomplete `mdat`
  box.
* Survive directory renames (Tesla doesn't, but defensive
  code is cheap and `inotify` event types collapse the cases).
* Cost ~zero when idle (the Pi Zero 2 W is RAM- and
  CPU-constrained).
* Be testable from a tempdir without touching `/srv`.

## Options considered

### Option A — Periodic directory scan (no new dep)

* **Pros:** Zero dep. Trivially portable.
* **Cons:** Either the poll interval is long (laggy
  indexing → cleanup decisions stale → free-space pressure
  spikes) or it is short (constant `readdir` of a tree that
  can hold thousands of clips → constant CPU + thrashed
  page cache). The Pi has plenty of better things to do.
  Also: a `readdir` mid-write can hand us a half-written
  file; we'd still need a "stable size for N seconds" check
  before walking, which is just inotify-with-extra-steps.

### Option B — `notify` crate

* **Pros:** Cross-platform abstraction (inotify on Linux,
  FSEvents on macOS, ReadDirectoryChangesW on Windows).
  Already widely used.
* **Cons:** The cross-platform abstraction hides exactly
  the Linux-specific event we care about
  (`IN_CLOSE_WRITE`). On the `RecommendedWatcher` API we'd
  get a coarse `Event { kind: Modify(_), .. }` and would
  have to debounce ourselves. The B-1 worker is
  Linux-only by design (it runs inside the gadget config);
  the portability is unused.
* **Cons:** Pulls a backend selector + multiple OS bindings
  even on Linux. Bigger surface than we need.

### Option C — `inotify` crate (selected)

* **Pros:**
  * Direct binding to Linux `inotify(7)`. Exposes the exact
    `IN_CLOSE_WRITE | IN_MOVED_TO` mask the watcher needs.
    `IN_CLOSE_WRITE` fires once when Tesla closes the file
    after the final write — eliminating the "is it done?"
    race. `IN_MOVED_TO` covers any future
    write-tmp-then-rename pattern.
  * Pure FFI binding, no driver picker. Smaller binary.
  * Synchronous file-descriptor API (`Inotify::read_events`)
    that maps cleanly to tokio's `AsyncFd` for an `async`
    stream wrapper. The worker's tokio runtime already
    needs `AsyncFd` for the NBD socket; this reuses the
    same pattern.
  * The maintainer is the rust-lang/inotify-rs team; the
    crate has been at 0.10 since 2023 with no breaking
    changes — stable enough.
* **Cons:**
  * Linux-only. Accepted: the worker only runs inside the
    Pi gadget.
  * `inotify_init1(IN_NONBLOCK)` opens a real fd; tests
    must run in a tempdir on Linux to exercise the wire
    path. We can unit-test the pure-logic event-classifier
    (path-to-bucket mapping, mp4-extension filter,
    debounce window) without inotify at all, and ship a
    `#[cfg(target_os = "linux")]` integration test that
    drives a real `Inotify` against a tempdir.

## Decision

Adopt **`inotify = "0.10"`** for the `teslausb-worker::watcher`
module. Event mask: `IN_CLOSE_WRITE | IN_MOVED_TO`. The
watcher emits a typed `WatchEvent { bucket, path }` stream
that the indexer drains.

## Rust integration shape

* New dep on `teslausb-worker` only (`Cargo.toml`):
  ```toml
  inotify = "0.10"
  ```
  `teslausb-core` does not get this dep — the watcher is a
  Layer-3 adapter.
* `teslausb_worker::watcher::ClipWatcher`:
  * `ClipWatcher::new(roots: &BucketRoots) -> Result<Self>`
    — adds a watch on each of the three bucket roots.
  * `next_event(&mut self) -> Option<WatchEvent>` — blocking
    read. The tokio adapter (`tokio::task::spawn_blocking`)
    moves the blocking call off the runtime.
* `WatchEvent`:
  * `bucket: Bucket` (the indexer store's enum)
  * `path: PathBuf` — absolute path to the clip file
  * `kind: WatchKind` — `Created` (CLOSE_WRITE) or `Moved`
    (MOVED_TO). Both trigger an index; the kind is kept for
    log clarity.
* Filters live in pure-logic helpers (`is_indexable(path) ->
  bool`, `event_to_bucket(path, &BucketRoots) -> Option<Bucket>`)
  so the unit tests can exercise them without an `Inotify` fd.

## Indexer service shape (Phase 4b.2b)

`teslausb_worker::indexer::Indexer`:

* `Indexer::new(config, store, walker)` wires the store
  from 4b.2a and the SEI walker from 4b.1z.
* `Indexer::bootstrap()` — one-shot at startup: walks the
  three bucket trees, indexes any `*.mp4` the store does
  not already `knows_clip()`. Recovers from "worker was
  down while Tesla wrote clips".
* `Indexer::run(events: impl Stream<Item = WatchEvent>)` —
  main loop. For each event, debounces against the
  config-driven window, dedups in-flight paths, walks the
  clip, persists via `store.record_clip`. Per-clip parse
  failures log at WARN and continue (one bad clip must not
  stall the indexer).

## Charter compliance

* Layering: `inotify` is Layer-3, lives in worker only.
* No shortcuts: typed errors via thiserror; debounce window
  named (`config.indexer.debounce_ms`), not a magic literal.
* Pure logic is separated from I/O: bucket-mapping and
  extension-filter unit-tested without an inotify fd.
* ADR documented (this file).

## Consequences

* The watcher is Linux-only at compile time. Non-Linux
  builds (developer workstation tests) compile the
  `watcher` module's pure helpers but `cfg`-gate the
  `ClipWatcher` struct itself. The host-OS unit-test suite
  still exercises ~all of the watcher's logic; the
  inotify-fd integration test is `#[cfg(target_os = "linux")]`.
* If we ever need to support a non-Linux platform (we don't
  plan to), the `watcher` module becomes the only file to
  re-implement.
