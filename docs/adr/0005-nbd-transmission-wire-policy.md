# ADR-0005 — NBD transmission-phase wire policies

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.5 of B-1 rewrite |
| Commit   | `f36e913` (inc-1.5 implementation) |

## Context

Phase 1.5 introduces `teslafat::nbd::transmission::run<B, S>` — the
per-connection dispatch loop that reads NBD request frames off the
wire and routes them to a `BlockBackend` from `teslausb-core`
(ADR-0004). The NBD specification leaves two important questions
to the implementation:

1. What does the server do when the client sends a request whose
   `length` field exceeds the maximum-block-size value the server
   advertised in the handshake?
2. What does the server do when the client sends a valid-sized
   WRITE request whose `(offset, length)` falls outside the
   exported size?

Both situations are client protocol violations of differing
severity, and both touch the connection's byte-stream alignment
invariant (the next request must start at the next 28-byte
boundary). The wrong answer to either question would silently
desync the connection and corrupt subsequent traffic, so each
warrants ADR-level documentation.

§A covers the oversized-request policy. §B covers the
out-of-bounds WRITE policy. Both apply to every transport — the
in-process `DuplexStream` used by tests, the per-LUN
`UnixStream` used in production (Phase 1.6), and any future
TCP transport.

## §A — Decision: oversized requests terminate the connection

```rust
if req.length > BLOCK_SIZE_MAX {
    write_error_reply(stream, NBD_EOVERFLOW, req.handle).await?;
    bail!("client sent request exceeding advertised BLOCK_SIZE_MAX");
}
```

`BLOCK_SIZE_MAX = 32 * 1024 * 1024` is the constant the handshake
advertises in its `NBD_INFO_BLOCK_SIZE` reply. A request that
exceeds it is, by construction, a client that ignored the value
the server told it.

### Alternatives considered

**A1: Drain the payload, reply EOVERFLOW, continue the loop.**
This is the "polite" choice — preserve the connection so the
client can attempt smaller requests on the same socket.

- Rejected because: a client that exceeds the advertised cap has
  already demonstrated that it is not honouring server-provided
  parameters. We have no reason to believe its next request will
  be legitimate.
- Cost of being polite: we have to allocate or stream-discard
  up to `u32::MAX` bytes (≈ 4 GiB per request) just to reach
  the next request header. Memory budget on the Pi Zero 2 W is
  512 MiB total; a 4 GiB drain is not a thing we can do.
- Cost of being polite under attack: a hostile or buggy client
  can keep us busy draining indefinitely.

**A2: Drop the request silently, do not reply, continue the loop.**

- Rejected because: leaves the client wondering what happened
  with `handle = X` (NBD has no out-of-band signalling); the
  client will eventually time out, which is strictly worse for
  diagnostics than the explicit EOVERFLOW + close.

**A3: Replace `bail!` with a custom "connection reset" sentinel
that the supervisor catches.**

- Rejected because: `Err` from `run()` is already the architecture's
  connection-closed signal (Phase 1.6 supervisor logs + drops the
  stream on `Err`). Adding a sentinel type buys nothing and
  multiplies cases.

### Consequences

- Clients that exceed `BLOCK_SIZE_MAX` get one `EOVERFLOW` reply
  and then the socket closes. They must reconnect to retry. This
  is observable behaviour; future changes to it would be
  client-visible.
- The 32 MiB cap is enforced *before* any allocation, so an
  oversize request cannot trigger an OOM kill.
- The `bail!` returns `Err` up to `run()`'s caller (Phase 1.6
  supervisor), where it will be logged at WARN with the client
  identity. Operators can grep for "BLOCK_SIZE_MAX" to find
  misbehaving clients.

## §B — Decision: out-of-bounds WRITEs drain payload before rejecting

```rust
let mut buf = vec![0u8; usize_len];
stream.read_exact(&mut buf).await?;
match backend.write(req.offset, &buf, flags).await {
    Ok(()) => write_ok_reply(stream, req.handle).await?,
    Err(e) => write_error_reply(stream, map_backend_err(&e), req.handle).await?,
}
```

The payload is always read off the wire (regardless of whether
the backend will accept it), and only then is it handed to
`backend.write`, which returns `BackendError::OutOfBounds` if
`offset + length > backend.size()`. The error maps to NBD's
`EINVAL` in the reply; the loop continues.

### Alternatives considered

**B1: Reject before reading the payload.**

```rust
// HYPOTHETICAL — not what we ship:
if req.offset.saturating_add(req.length as u64) > backend.size() {
    write_error_reply(stream, NBD_EINVAL, req.handle).await?;
    continue;
}
let mut buf = vec![0u8; usize_len];
stream.read_exact(&mut buf).await?;
// ...
```

- Rejected because: NBD has no per-request framing other than the
  28-byte header + (for WRITE) `length` payload bytes. If we skip
  the payload read, the next 28 bytes the server reads are *not*
  the next request header — they are the middle of the WRITE
  payload the client already sent. Every subsequent request on
  that connection is desynced and decoded as garbage. From the
  client's perspective: every request after the OOB one mysteriously
  fails or times out, and the cause is invisible from packet
  captures unless you correlate against the byte counter.
- The wire invariant is: **server must consume `length` payload
  bytes after every WRITE header**, no exceptions. Anything else
  desyncs the stream.

**B2: Reply EINVAL, then bail (close the connection).**

- Rejected because: OOB writes are a *valid* client behaviour
  during legitimate boundary probing. Clients (especially the
  Linux `nbd-client` kernel module under unusual filesystem
  load) sometimes send a WRITE that extends one byte past EOF.
  Dropping the connection on each one would force a reconnect
  storm. Reply + continue is the correct compromise.

**B3: Reply EINVAL and silently discard the payload bytes via
`tokio::io::copy(stream.take(length), tokio::io::sink())`.**

- Equivalent to what we ship, but spends an extra heap alloc on
  the `Take` wrapper. `read_exact(&mut buf)` is simpler and
  reuses the same allocation we would have needed for the
  successful path anyway.

### Consequences

- A WRITE allocates `length` bytes (up to 32 MiB) even if the
  range is OOB. Acceptable: §A caps `length` at 32 MiB; the
  allocation is bounded.
- The wire invariant "server consumes exactly `length` payload
  bytes after every WRITE header" is documented as a load-bearing
  contract. Any future contributor refactoring `handle_write`
  must preserve it.
- The `BackendError::OutOfBounds` -> `NBD_EINVAL` mapping in
  `map_backend_err` is the single chokepoint where this policy
  is enforced; OOB checks happen inside the backend, not in the
  transmission loop, so a new backend implementation cannot
  accidentally skip them.

## Consequences (combined)

- Inc-1.6 (`teslafat@.service` systemd wiring) inherits both
  policies via `run()`; no per-LUN configuration knob exposes
  either. If a future operator requirement demands looser
  oversize handling, the change is local to §A.
- The `refuse_oversized` helper and `handle_write`'s
  drain-then-reject ordering are covered by named tests
  (`oversized_read_replies_eoverflow_and_terminates_loop`,
  `out_of_bounds_write_drains_payload_then_replies_einval`). A
  future contributor that "optimises" either path by skipping
  the drain or the bail would break the named test, not just
  some incidental property check.
- Both policies are observably tied to handshake behaviour
  (§A) and backend `size()` (§B). Changing either is a wire-
  protocol-visible event, so future revisions of this ADR are
  the appropriate place to document such changes — not silent
  source edits.

## Out of scope

- **READ out-of-bounds** is also handled (via `backend.read`
  returning `OutOfBounds`), but READ has no payload to drain,
  so there is no analogue to §B's invariant. Behaviour is
  "reply EINVAL, continue loop." Documented in
  `out_of_bounds_read_replies_einval_with_no_payload`.
- **Pipelining policy** (in-order vs out-of-order reply
  delivery): the loop is single-threaded sequential per
  connection. NBD spec permits out-of-order replies (each
  carries the request's `handle`), but no client we need to
  support requires that. If a future client does, replies
  become handles-routed independently of request order — a
  Phase 2+ optimisation, ADR'd when needed.
- **DISC has no reply** is a spec mandate, not a project
  decision; no ADR.
- **In-test concurrency uses `tokio::join!`, not `tokio::spawn`**
  is a consequence of ADR-0004 §A (native AFIT futures are
  not `Send`), captured in the `drive()` helper's inline
  comment in `transmission.rs`. Not re-litigated here.

## Revisit criteria

This ADR should be revisited if any of the following becomes true:

- The Pi Zero 2 W is replaced with a host that has > 4 GiB RAM
  available to the per-LUN worker, and a "drain oversized
  requests" policy becomes practical.
- A real-world NBD client emerges that legitimately sends
  oversized requests during a recoverable workflow (e.g. one
  that issues a sized probe). At which point §A's choice of
  "close on violation" should be re-examined against the
  cost of the resulting reconnect.
- The backend gains a `discard` method, prompting reconsideration
  of how OOB TRIM is handled (currently TRIM is a no-op success;
  with a real discard, the OOB-rejection logic would mirror §B).
