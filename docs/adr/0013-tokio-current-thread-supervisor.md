# ADR-0013 — Tokio current-thread runtime for the worker supervisor

**Status:** Accepted
**Date:** 2026-05-19
**Phase:** 4b.4 (worker supervisor)

## Context

Phase 4b.4 lands the real `teslausb-worker::main` supervisor that
replaces the Phase 0.2 placeholder. The supervisor must:

1. Run a **bootstrap pass** of the indexer at startup (walks
   `RecentClips`/`SavedClips`/`SentryClips`, records every clip the
   store has not yet seen).
2. Run the **inotify clip watcher** on a long-lived blocking thread
   (`Watcher::next_batch` blocks on the kernel `read(2)` against the
   inotify FD) and feed events back into the indexer.
3. Run the **cleanup sweep** on a periodic ticker (default 5 min,
   configurable).
4. Handle **SIGTERM + SIGINT** for clean shutdown so systemd
   `Restart=on-failure` plus normal stops work correctly.
5. Surface unrecoverable errors as a non-zero exit code so systemd
   restarts the service.

The supervisor is the **only** async surface in `teslausb-worker`.
The indexer, watcher, store, and cleanup modules are all
synchronous (they own blocking I/O — SQLite + `std::fs` + inotify
— that doesn't compose with async runtimes anyway).

## Options considered

### Option A — `std::thread` + `mpsc` only

Spawn raw OS threads for the watcher loop and the cleanup ticker;
join on a `JoinHandle<()>` mpsc shutdown channel.

* **Pros:** No new dependencies. Simple.
* **Cons:** Signal handling on Unix from a non-async context
  forces either `signal-hook` (another dep) or a `signalfd` raw
  syscall via `nix`/`rustix`. Joining multiple worker threads with
  prompt SIGTERM response is fiddly (must use `pthread_kill` or
  a poll loop with `EINTR`). The "select over signal +
  ticker + channel" idiom is the exact pain tokio's `select!`
  removes.

### Option B — `tokio` current-thread runtime + `spawn_blocking`

`#[tokio::main(flavor = "current_thread")]` gives a single-threaded
async reactor. The two blocking surfaces (`Watcher::next_batch`
and the indexer's `record_clip` SQLite transaction) live on a
dedicated `spawn_blocking` thread; the cleanup ticker is
`tokio::time::interval`; signals are `tokio::signal::ctrl_c()` and
`tokio::signal::unix::signal(SignalKind::terminate())`. The
top-level `tokio::select!` over `{signal, watcher events, cleanup
tick}` is one short macro.

* **Pros:** `tokio::signal` is the standard tool for the job;
  `select!` makes the supervisor trivial to read and to test;
  `spawn_blocking` is the canonical bridge from sync I/O to the
  reactor. Current-thread flavor keeps the runtime overhead tiny
  (one thread + one blocking-pool thread).
* **Cons:** New top-level dependency. Adds `tokio` to the
  worker's transitive closure.

### Option C — `async-std` or `smol`

Same shape as Option B with a different runtime.

* **Pros:** Smaller than tokio.
* **Cons:** Smaller ecosystem; `signal` handling is less battle-
  tested. We already use tokio elsewhere in the project plan
  (Phase 1 `teslafat`'s NBD server runs on tokio). Standardising
  on one runtime avoids two reactors in one process tree.

## Decision

**Option B — tokio current-thread, no `multi_thread` flavor.**

The supervisor's hot path is "wait on one of three things";
that is exactly what `tokio::select!` is for. `current_thread`
keeps the threadcount predictable (one reactor thread + one
blocking-pool thread for the watcher + occasional blocking-pool
threads for short-lived `spawn_blocking` indexer work). Matches
the charter's "Pick the Hard Right" — Option A would be more
code, more dependencies of its own (`signal-hook` at minimum),
and worse readability for the same behaviour.

## Consequences

* `tokio` is now a direct dependency of `teslausb-worker`.
  Features pinned to the minimum we use: `rt`, `macros`,
  `signal`, `time`, `sync`. **No `rt-multi-thread`.**
* The supervisor lives in `src/supervisor.rs` (testable pure-logic
  helpers + the async `run` orchestrator). `src/main.rs` shrinks
  to clap parsing + tracing init + `run` call.
* All blocking I/O in indexer/watcher/cleanup stays synchronous;
  the supervisor wraps the calls in `spawn_blocking` where needed.
* `cleanup_interval` is floored to 5 s by a pure-logic helper so
  a typo in `interval_seconds = 0` cannot pin a CPU. (Config
  already rejects literal `0`, but the floor is defence in depth.)
* Signal handling is Linux-only at runtime via
  `tokio::signal::unix`; on Windows the supervisor falls back to
  `ctrl_c()` only. Dev-workstation tests for the supervisor's
  pure-logic helpers run on all platforms; the full
  `run`-with-fakes integration test is `#[cfg(target_os = "linux")]`.

## References

* `tokio::signal::unix` —
  <https://docs.rs/tokio/1/tokio/signal/unix/index.html>
* `tokio::task::spawn_blocking` —
  <https://docs.rs/tokio/1/tokio/task/fn.spawn_blocking.html>
* `tokio::time::interval` —
  <https://docs.rs/tokio/1/tokio/time/fn.interval.html>
* Phase 4b.4 in `docs/00-PLAN.md`.
