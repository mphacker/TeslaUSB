# ADR-0003 — Async runtime (tokio, current-thread) and `teslafat` crate shape (lib + bin)

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.3 of B-1 rewrite |
| Commit   | `2c1a56f` (inc-1.3 implementation) |

## Context

Phase 1.3 ports the NBD newstyle handshake from the preserved
draft `teslafat/src/nbd/handshake.rs` into the active workspace at
`rust/crates/teslafat/src/nbd/handshake.rs`. Two architectural
decisions had to be locked in to make the port work without
violating the code-quality charter:

1. **An async runtime had to be chosen.** The handshake speaks
   over a `tokio::net::UnixStream` to the kernel `nbd-client`. The
   draft already used `tokio`, but it was a draft — not a
   commitment. Phase 1.5 (transmission loop), Phase 1.6 (systemd
   unit, signal handling), and Phase 1.7 (IPC server) will all
   touch the runtime. Adding the runtime in 1.3 commits the daemon
   to its async ecosystem for the rest of Phase 1+.
2. **The `teslafat` crate had to be reshaped from pure bin to
   lib + bin.** The pure-core / thin-shell decomposition the
   charter mandates (§"Best architecture") means
   `nbd::handshake::*` has lots of `pub` items that the binary's
   `main.rs` doesn't call yet — the transmission loop in Phase 1.5
   will be the first consumer. With `teslafat` as a pure binary,
   the workspace's `dead_code` lint (warn → deny under `-D warnings`)
   correctly fires on every `pub` item that isn't reachable from
   `main`. The lib + bin split exposes the protocol as a real
   public surface that tests + future modules can depend on.

Both decisions met ≥ 3 of the 5 charter ADR-trigger criteria, so
they belong here.

## Decision

### §A — Async runtime: tokio, current-thread, minimal features

Add `tokio = "1.40"` to `rust/crates/teslafat/Cargo.toml` with:

```toml
tokio = { version = "1.40", default-features = false, features = ["rt", "net", "io-util", "macros"] }
```

* **`default-features = false`** keeps the binary small. The
  default feature set pulls in `time`, `signal`, `process`,
  `sync`, `parking_lot`, etc. — none needed by Phase 1.3.
* **`rt`** is the current-thread runtime (not `rt-multi-thread`).
* **`net`** brings `tokio::net::UnixStream` for the production
  socket binding (used in Phase 1.6 wiring; declared now so the
  Phase 1.6 dep delta is a one-liner).
* **`io-util`** brings `AsyncReadExt` / `AsyncWriteExt` (which
  the handshake uses) and `tokio::io::duplex` (which the unit
  tests use).
* **`macros`** brings `#[tokio::main]` (Phase 1.6) and
  `#[tokio::test]` (Phase 1.3 unit tests).

No `time`, no `signal`, no `process` — they get added in their
respective phases with a one-line feature delta.

### §B — `teslafat` crate shape: lib + bin

Add `rust/crates/teslafat/src/lib.rs` that declares:

```rust
pub mod config;
pub mod nbd;
```

Update `rust/crates/teslafat/src/main.rs` to consume the lib:

```rust
use teslafat::config::Config;
```

Both targets share the same crate name (`teslafat`). Cargo
produces both `libteslafat.rlib` and the `teslafat` binary;
`tests/sentinel.rs` (Phase 1.1 integration test) is unaffected
because it spawns the binary via `assert_cmd`.

## Alternatives considered

### §A alternatives

1. **`async-std`.** Smaller and simpler than tokio, but with a
   much smaller ecosystem and an unclear maintenance future. The
   `nbd`/`fatfs`/`fuser` ecosystem we'll integrate with in later
   phases all assume tokio. Rejected.
2. **`smol`.** Minimalist async runtime; appealing on a Pi Zero
   2 W. But the `rclone`-rs / `notify`-async ecosystem the Python
   web app will eventually want to share types with also assumes
   tokio. Rejected.
3. **`std::thread` + `std::sync::Mutex`.** Genuinely viable for
   ≤ 3 concurrent connections — the daemon doesn't have enough
   parallelism to need async at all. **Rejected** because the
   NBD transmission loop (Phase 1.5) does benefit from async
   read/write multiplexing on a single connection (interleaved
   READ + FLUSH + WRITE), and the IPC server (Phase 1.7) will
   benefit from non-blocking signal handling. Keeping the daemon
   one paradigm is simpler than mixing sync I/O for the handshake
   with async I/O for the transmission loop.
4. **`embassy`.** No-std async runtime intended for microcontroller
   targets. The Pi Zero 2 W runs full Linux; embassy would be a
   massive lift. Rejected.
5. **`tokio` with `rt-multi-thread`.** Spawns one thread per CPU
   core (4 on the Pi Zero 2 W) for the runtime. Useful for
   CPU-bound workloads — but the daemon is I/O-bound and serves
   ≤ 3 connections concurrently. Multi-thread would burn an
   additional ~200 KB RAM per worker thread + steal cores from
   the Python web app and the archive worker. **Rejected** for
   the Pi Zero 2 W constraint; revisit if a beefier target is
   ever supported.

### §B alternatives

1. **Keep `teslafat` as a pure bin, suppress `dead_code` on the
   `nbd` mod with `#[allow(dead_code)]`.** The inc-1.1 review
   explicitly *removed* a preemptive `#[allow]` for being
   dishonest scaffolding. Repeating the pattern in inc-1.3 would
   undermine the charter discipline that made the review work in
   the first place. Rejected.
2. **Keep `teslafat` as a pure bin, wire `nbd::handshake::run`
   into `main.rs` behind a never-taken branch.** Honest from
   the compiler's perspective (the code is reachable) but
   dishonest from a reader's perspective (the call is a lie —
   it's there to satisfy the linter, not to do work). Rejected.
3. **Move `nbd::handshake` into `teslausb-core` as a pure-types
   crate consumer.** The handshake is I/O-bound async code; it
   doesn't belong in the pure-types crate that the Python web
   app may eventually consume bindings against. Rejected on
   layering grounds.
4. **Split `teslafat` into `teslafat-core` (lib) + `teslafat`
   (bin) as separate workspace crates.** Cleaner ownership story
   (lib has no `main.rs`, bin has only `main.rs`), but doubles
   the Cargo.toml maintenance burden and pollutes the workspace
   crate list. **Rejected** because Cargo's single-crate lib+bin
   pattern is the idiomatic Rust answer for exactly this
   situation.
5. **Lib + bin with the same name** (CHOSEN). Standard
   Rust pattern, idiomatic, zero ceremony, and tests get access
   to the lib's public surface for free.

## Consequences

### Positive

* **Phase 1.5 transmission loop is unblocked.** It can depend on
  `teslafat::nbd::handshake::run` directly without spawning a
  binary; tests can compose handshake + transmission against the
  same `tokio::io::DuplexStream` pair.
* **Phase 1.7 IPC server gets a free runtime.** It just adds
  `tokio::net::UnixListener` + `tokio::select!` against the
  existing runtime — no new dep churn.
* **Tests are honest.** Unit tests live with the code they test
  and exercise the real production code path (the pure helpers
  AND the async orchestrator), not a separately maintained
  stub.
* **Future `teslactl` (a CLI hitting the IPC socket) can depend
  on `teslafat`'s lib for the IPC types** without re-implementing
  serialization — both the daemon and the CLI share one schema.

### Negative

* **Tokio dep is a ~3 MB binary-size hit** (release build,
  stripped, with LTO). On the 32 GB SD card that's irrelevant;
  on RAM it's negligible because the runtime is mmap'd.
* **Current-thread runtime is single-threaded.** If a single
  handshake blocks the executor (e.g. a poorly-bounded
  `read_exact`), the IPC server stops responding. **Mitigation:**
  Phase 1.6 wraps every handshake call in `tokio::time::timeout`
  (see follow-up TODO in `nbd/mod.rs`).
* **`Cargo.lock` churned by 61 lines** for tokio's transitive
  deps (`bytes`, `mio`, `pin-project-lite`, `signal-hook-registry`,
  `socket2`). All standard, all well-maintained — but a 61-line
  Cargo.lock delta is a real audit-trail cost. Unavoidable.
* **Lib + bin shape sets a precedent** for `teslausb-worker` and
  any future binary crate that needs testable internals. The
  precedent is good (charter-aligned), but it does mean Phase 2+
  reviewers should default to lib+bin when there's a real public
  surface.

### Neutral

* **`tokio` is in the plan implicitly** (the draft used it, the
  plan said "1:1 port"). Pinning it via this ADR upgrades that
  implicit choice to an explicit one.
* **Single-thread runtime can be upgraded to multi-thread** in
  a future ADR by changing one feature flag if hardware ever
  warrants it (more cores, less RAM pressure). No code changes
  required — `#[tokio::main]` honors whichever runtime is
  configured.

## Forward-looking notes

* **Phase 1.5 transmission loop** will use `tokio::select!` over
  the NBD socket read + a shutdown signal channel. The
  current-thread runtime handles `select!` correctly.
* **Phase 1.6 wiring** needs to add `tokio` features `time`
  (for `timeout`) and `signal` (for `SIGTERM` graceful
  shutdown). A one-line feature delta, not a new ADR trigger.
* **Phase 2 `teslausb-worker`** will likely adopt the same
  runtime + crate-shape pattern. If it does, this ADR provides
  the rationale; the worker's increment doesn't need its own
  ADR for the same decision.

## References

* Charter §"Best architecture" (lines 388–410) — pure core,
  thin I/O shell.
* Charter §"The Boundaries Are Real" (lines 443–446) — daemon
  and web app are separate processes.
* Charter §"Lints" (lines 213–218) — `dead_code` warn, escalated
  to deny via `-D warnings`.
* Charter §"ADRs" (lines 477–485) — 5 trigger criteria.
* Tokio runtime overview:
  <https://docs.rs/tokio/latest/tokio/runtime/index.html>
* `tokio::io::duplex` for in-memory async stream testing:
  <https://docs.rs/tokio/latest/tokio/io/fn.duplex.html>
* ADR-0001 — config format (TOML); ADR-0002 — IPC vocabulary.
