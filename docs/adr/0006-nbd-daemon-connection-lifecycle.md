# ADR-0006 — NBD daemon connection-lifecycle policies

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.6 of B-1 rewrite |
| Commit   | `1228f41` (inc-1.6 implementation) |

## Context

Phase 1.6 lands the daemon's connection lifecycle on top of the
inc-1.3 handshake (ADR-0003) and the inc-1.5 transmission loop
(ADR-0005). The new surface in `teslafat::server` adds three
load-bearing policy choices that govern how the long-running
process treats each individual NBD client:

1. **Concurrency model.** Does one daemon process serve one
   client at a time, or does it `tokio::spawn` a task per
   accepted connection?
2. **Per-connection failure propagation.** When a client's
   handshake times out, its connection desyncs, or its
   transmission loop errors, does that error climb up to the
   accept loop and out of the daemon, or is it isolated to the
   one connection?
3. **Liveness checks.** Does the userspace daemon police
   per-request timeouts in addition to handshake timeouts, or
   does it rely on the kernel `nbd-client` for that?

All three are observable to clients: changing any of them after
ship would alter behaviour the kernel `nbd-client` can detect
(connection drops, reconnect-storm shape, request-timeout
attribution). They also all touch the daemon-vs-systemd
boundary: each interacts with `Restart=on-failure` and with the
`@.service` instanced template the same commit ships, so a
silent change to any of the three would also change the
operational shape of the running fleet.

§A covers the concurrency model. §B covers per-connection
failure propagation. §C covers the liveness-check division
between userspace and the kernel NBD client. The three are
co-located here rather than split into three single-decision
ADRs because they jointly define the daemon's "one-process,
one-client, errors stay local, kernel polices requests"
identity — splitting would lose the constraint that ties them
together.

## §A — Decision: one connection at a time per process

```rust
// rust/crates/teslafat/src/server.rs (sketch)
pub async fn serve<B: BlockBackend, F: Future<Output = ()>>(
    listener: UnixListener,
    backend: B,
    handshake_timeout: Duration,
    shutdown: F,
) -> anyhow::Result<()> {
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            biased;
            () = &mut shutdown => return Ok(()),
            accept = listener.accept() => match accept {
                Ok((stream, _)) => {
                    serve_one_connection(stream, &backend, handshake_timeout).await;
                }
                Err(e) => warn!(error = %e, "accept failed; retrying"),
            }
        }
    }
}
```

No `tokio::spawn`. The accept loop awaits `serve_one_connection`
to completion before issuing the next `accept()`. Two LUNs run
two processes (the unit file is instanced: `teslafat@0.service`,
`teslafat@1.service`); within one process at most one client
is ever active.

### Alternatives considered

**A1: Spawn a task per accepted connection.**

```rust
// HYPOTHETICAL — not what we ship:
let (stream, _) = listener.accept().await?;
let backend = backend.clone();
tokio::spawn(async move {
    serve_one_connection(stream, &backend, handshake_timeout).await;
});
```

- Rejected because: this is the only intended peer for the
  per-LUN socket is the host kernel's `nbd-client`, which owns
  the `/dev/nbdN` block device. The kernel module opens at most
  one connection per device by design (one socket fd per
  `NBD_SET_SOCK` ioctl, and the per-device thread serialises
  requests over that socket). Adding spawn buys nothing for the
  real peer; it only matters if a second client appears, and
  none can.
- Rejected because: NBD simple-reply mode (what `nbd::handshake`
  negotiates and what `nbd::transmission::run` implements)
  requires the server to reply to requests in the order
  received on a given connection. Spawning per request would
  let backend latency vary the reply order. We could instead
  spawn per *connection* (not per request) and keep
  per-connection sequencing, but see the next reason.
- Rejected because: Phase 2's `FileBackend` will hold a
  per-LUN backing file open for read-write. Two `serve_one_connection`
  futures could call `backend.write(offset, &buf)` simultaneously
  and race on the file's `pwrite` ordering. The trait
  (ADR-0004) deliberately does not require `Sync`. Single
  serialised access is the simplest correct choice.
- Rejected because: native AFIT futures returned by
  `BlockBackend` methods are not `Send` (ADR-0004 §A
  consequence). `tokio::spawn` requires `Send` futures on the
  multi-thread runtime — and even on the current-thread runtime,
  the type-system requirement remains. We would have to
  introduce `LocalSet` plumbing for spawn, against zero
  observable benefit.

**A2: One LUN per process, one connection per process (what we ship).**

Accepted. Process isolation comes from systemd: two LUNs =
two instances of `teslafat@.service` = two PIDs = two
configurations, with no shared state.

**A3: Bound concurrency with a `Semaphore` (e.g. max 4).**

- Rejected because: the bound that matters is "at most 1" (the
  client cap), not a tunable. Adding a semaphore introduces
  configuration that operators must understand without
  delivering any safe-default behaviour change.

### Consequences

- A second client connecting to the socket while one is active
  blocks in `accept()` until the first finishes. Acceptable: no
  legitimate second client exists.
- Each `serve_one_connection` call has exclusive access to the
  `backend` reference. Phase 2's `FileBackend` can keep its
  backing-file handle non-`Sync` without changes to the
  serving infrastructure.
- A spawn-introducing change in a future increment would be a
  cross-cutting refactor that touches the trait bounds, the
  test harness, and the systemd unit's restart semantics. That
  shape of change is exactly what ADRs exist to make visible.
- The `biased;` in the `select!` ensures shutdown wins races
  against `accept()`: a pending accept is dropped (TCP/Unix
  RST), and the daemon exits cleanly. The alternative
  (unbiased select) would let an accept-then-shutdown race
  start a new connection that gets dropped mid-handshake.

## §B — Decision: per-connection errors never propagate to the accept loop

```rust
pub async fn serve_one_connection<S, B>(
    mut stream: S,
    backend: &B,
    handshake_timeout: Duration,
)
where
    S: AsyncRead + AsyncWrite + Unpin,
    B: BlockBackend,
{
    match timeout(handshake_timeout, handshake::run(&mut stream, backend.size())).await {
        Err(elapsed) => {
            let _: Elapsed = elapsed;  // marker-type pin
            warn!("handshake timed out");
            return;
        }
        Ok(Err(e)) => { warn!(error = %e, "handshake failed"); return; }
        Ok(Ok(())) => {}
    }
    if let Err(e) = transmission::run(&mut stream, backend).await {
        warn!(error = %e, "transmission ended with error");
    }
}
```

`serve_one_connection` returns `()`, not `Result`. Every
per-connection failure — handshake timeout, handshake protocol
error, transmission wire desync (including the inc-1.5
`bail!`-on-oversized path), backend errors that surface as
`Err` from `transmission::run` — is logged at `warn!` and
becomes a normal return. Only loop-level fatal conditions
(listener-gone, shutdown signal) can propagate out of `serve`.

### Alternatives considered

**B1: Return `Result` from `serve_one_connection` and `?` it in the loop.**

```rust
// HYPOTHETICAL — not what we ship:
loop {
    let (stream, _) = listener.accept().await?;
    serve_one_connection(stream, &backend, handshake_timeout).await?;
}
```

- Rejected because: `systemd` is configured with
  `Restart=on-failure`. If a misbehaving or malicious client
  can trigger `Err` propagation, that client can force the
  daemon to exit. systemd will restart it (good), but the
  restart cycle:
  - Drops every other client connection (there are none today;
    there could be a second LUN process tomorrow).
  - Logs a `Failed` state transition that triggers any
    `OnFailure=` units the operator wires up.
  - Counts against `StartLimitBurst`. After
    `StartLimitIntervalSec` exceeded, systemd stops restarting
    and the daemon stays down — a textbook DoS amplifier.
- Rejected because: the alternative ("misbehave a client to
  take down the daemon") is exactly the threat model the
  systemd hardening profile (`CapabilityBoundingSet=`,
  `RestrictAddressFamilies=AF_UNIX`, etc.) is designed to
  reduce. Putting a client-controlled error path on the
  daemon-exit fast path defeats that hardening.

**B2: Return `Result` but `?`-only "fatal" errors, log+continue on "recoverable" ones.**

- Rejected because: it requires classifying every error as
  fatal or recoverable, and that classification has to stay
  correct as new error variants land. Forgetting to mark a
  new error variant as recoverable is a silent regression to
  B1. The contract "no error propagates from a connection" is
  much harder to break by accident.

**B3: Return `()` (what we ship), errors via tracing.**

Accepted. The tracing layer captures every error with full
context (`error.cause`, `error.backtrace` where available),
and operators have one place to look (`journalctl -u
teslafat@*.service`) for client misbehaviour.

### Consequences

- A misbehaving client can spam `warn!` lines in the journal
  but cannot exit the daemon. The journal rate-limit
  (`RateLimitInterval`, `RateLimitBurst` in journald defaults)
  bounds the storage cost; the daemon-availability cost is
  zero.
- The signature `serve_one_connection -> ()` is a load-bearing
  contract. A future contributor who changes it to `Result`
  must justify why daemon-exit-on-client-error is the right
  semantics. The accept loop's call site is the single
  enforcement point.
- Tests for the error paths assert on the side-effect side
  (was the warn line emitted? did the stream close?) not on
  a `Result::Err` return — because there is no `Result::Err`
  return to assert on.
- Phase 1.7's smoke test does NOT need a "did the daemon
  survive a bad handshake?" assertion separate from the
  happy-path assertion: the daemon's survival is part of the
  type signature.

## §C — Decision: handshake timeout is the only liveness check

The daemon enforces `handshake_timeout_seconds` (default 10 s,
range [1, 600] s) via `tokio::time::timeout` wrapping
`handshake::run`. After handshake completes, the transmission
loop is **untimed** — there is no per-request timeout,
no per-connection idle timeout, no overall connection-lifetime
cap.

```rust
// in serve_one_connection
match timeout(handshake_timeout, handshake::run(...)).await { ... }
// then:
if let Err(e) = transmission::run(...).await { ... }
//             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ no timeout wrap
```

### Alternatives considered

**C1: Add per-request timeout (`tokio::time::timeout` per command).**

- Rejected because: the kernel `nbd-client` already polices
  request liveness via `/sys/block/nbdN/queue/io_timeout`
  (default 30 s, operator-tunable). Adding a userspace
  per-request timeout would duplicate that policy in a place
  the operator cannot tune.
- Rejected because: legitimate large WRITEs on the Pi
  Zero 2 W can take seconds under SD-card pressure (the
  archive subsystem documents this extensively; see the
  copilot-instructions.md "task_coordinator max_hold" near-miss
  discussion). A userspace per-request timeout would
  preemptively kill those WRITEs and cause data loss
  precisely when the device is most loaded.
- Rejected because: per-request timeouts add a yield point
  to every command path. Since each command also calls into
  the backend (which holds no async-scoped resources beyond
  its own scope), adding a timeout wrapper means adding a
  cancellation safety analysis for every backend method.
  Cost without benefit.

**C2: Add per-connection idle timeout (e.g. drop after 60 s of silence).**

- Rejected because: `nbd-client` may legitimately go idle.
  The kernel block layer issues requests only when userspace
  reads/writes the device; a mounted-but-quiet filesystem
  produces no requests. Dropping the connection would force
  a reconnect each time the FS goes idle, defeating the
  one-connection-per-LUN model.

**C3: Add overall connection-lifetime cap (e.g. drop after 24 h).**

- Rejected because: there is no operational reason to drop a
  healthy long-lived connection. The systemd `RuntimeMaxSec`
  directive exists for processes that need this; we don't.

**C4: Handshake timeout only (what we ship).**

Accepted. The handshake is the one phase where (a) the daemon
is allocating resources for a not-yet-validated peer, (b) a
half-open connection has no other liveness signal (no
requests yet), and (c) a stuck handshake holds the
single-client slot of §A indefinitely. The default 10 s is
generous enough to cover slow-but-honest peers (TLS
handshakes complete in well under that even on the Pi); the
[1, 600] s range covers everything from aggressive (test
fixtures) to permissive (cross-WAN debugging).

### Consequences

- Operators tune request-level liveness via
  `/sys/block/nbdN/queue/io_timeout` on the consumer side,
  not via daemon config. This is documented as a deliberate
  split.
- A future requirement for daemon-side per-request timeouts
  (e.g. "kill any read that takes > 5 s") would be a
  user-visible behaviour change requiring an ADR amendment.
- The `Elapsed` marker-type pin (`let _: Elapsed = elapsed;`)
  in `serve_one_connection` is a load-bearing import: it
  proves at compile time that the only timeout in the
  function is the handshake timeout. If a contributor later
  adds a second `timeout()` call, the marker would silently
  cover both — at which point we'd want a second marker per
  timeout site. Phase 1.6 has one site, so one marker.
- Tests verify the timeout fires (`serve_one_connection_returns_when_handshake_times_out`)
  using a stream that accepts the client option request but
  never finishes the option negotiation. The transmission
  loop is tested separately with no timing assertions, which
  documents that the transmission loop intentionally has no
  timing contract.

## Consequences (combined)

- The daemon's identity is "one process, one LUN, one client
  at a time, errors stay local, kernel owns request liveness."
  This identity is jointly enforced by §A (no spawn) + §B
  (no error propagation) + §C (no transmission timeouts).
  Changing any one of them in isolation breaks the joint
  contract.
- The `@.service` systemd unit's `Restart=on-failure` +
  `StartLimitBurst` configuration is calibrated against the
  §B promise that per-connection errors don't reach the
  daemon-exit path. A change to §B would require a
  corresponding change to the unit's restart policy (e.g.
  switch to `Restart=always` to absorb client-caused exits).
- Phase 2's `FileBackend` design (next phase) inherits the §A
  serialisation guarantee. The backend trait (ADR-0004) is
  not required to be `Sync` precisely because of §A.
- Phase 1.7's smoke test exercises §A (one connection
  succeeds), §B (daemon survives a deliberate-bad-handshake
  client and accepts the next one), and §C (handshake
  timeout fires when the test client stalls). All three are
  named test assertions.

## Out of scope

- **Systemd unit hardening profile** (`CapabilityBoundingSet=`,
  `ProtectSystem=strict`, `RestrictAddressFamilies=AF_UNIX`,
  `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`).
  Documented inline in `units/teslafat@.service` with comments per
  directive. The choices are stock systemd hardening; defer
  ADR-0007 until a hardening choice is contested or relaxed.
- **`--check-config` CLI flag.** Operational; self-documenting
  via `--help`; referenced from the unit's `ExecStartPre=` and
  from `tests/sentinel.rs`. Removing it would break both
  call sites loudly. No ADR.
- **`backend::ZeroBackend` module location** (`teslafat::backend`
  vs `teslausb-core::backend::mock`). Explained inline in
  `backend.rs` module header. The decision avoids expanding
  the inc-1.4 `teslausb-core` surface and avoids the
  `NullBackend::new(size: usize) -> Vec<u8>` allocation
  hazard for daemon-scale sizes. No ADR.
- **Socket cleanup on shutdown.** Best-effort: `prepare_listener`
  unlinks any stale socket file before bind, and `unix_serve`
  best-effort removes the socket on clean shutdown. The
  contract "stale socket on disk is OK, we'll unlink on the
  next start" is documented in `prepare_listener`. No ADR.

## Revisit criteria

This ADR should be revisited if any of the following becomes
true:

- A real second NBD client appears on a per-LUN socket (e.g.
  a future "snapshot" subsystem that wants concurrent
  read-only access via a second connection). §A would need
  a per-LUN configuration switch.
- A client failure mode emerges that operators *want* to
  surface as a daemon exit (e.g. "if the kernel `nbd-client`
  disconnects unexpectedly, restart the daemon to reset
  shared kernel state"). §B would need a narrowly-scoped
  exception path.
- The kernel `nbd-client`'s per-request timeout proves
  insufficient in some workflow (e.g. operators routinely
  setting `io_timeout=0` for debug builds). §C would need
  a userspace per-request timeout as a belt-and-braces
  safety net.
