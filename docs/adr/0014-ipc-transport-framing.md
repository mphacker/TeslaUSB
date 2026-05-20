# ADR-0014: IPC transport framing — newline-delimited JSON

* Status: Accepted
* Date: 2026-05-20
* Authors: B-1 working group
* Decider: project owner
* Supersedes: (none)
* Superseded by: (none)
* Related: ADR-0002 (IPC vocabulary), ADR-0001 (TOML config)

## Context

`teslausb-core` (`src/ipc/messages.rs`) pinned the wire **vocabulary**
in Phase 1 (Envelope + Request + Response + StatusBody + ...) but
deliberately left the **framing** unspecified — the encoder choice
and byte-level delimiter were called out as a Phase 1.5 transport
decision. The relevant comment in `messages.rs`:

> Format-agnostic: every type derives `serde::Serialize` and
> `serde::Deserialize` but the encoder choice (JSON, length-
> prefixed, `MessagePack`, etc.) is a transport-layer decision that
> lives outside this crate.

Phase 5.5 is the first concrete consumer of the framing: the
Python control-plane client (`web/teslausb_web/services/
teslafat_client.py`) must speak the same byte layout the Rust
daemon will speak. We must decide before that client lands.

## Decision

**Newline-delimited JSON over `AF_UNIX` SOCK_STREAM sockets.**

Concretely:

1. Encoder: `serde_json::to_writer` on Rust;
   `json.dumps(..., separators=(",", ":"))` on Python — both
   produce compact JSON with no embedded line breaks (compact-
   mode `serde_json` and Python's `dumps` both serialise newlines
   in string values as the escape `\n`, never as a raw `0x0a`,
   so the framing delimiter is unambiguous).
2. Delimiter: a single `\n` (`0x0a`) byte appended to every
   message. The peer reads up to and including the first `\n`,
   strips it, and deserialises the remainder as the Envelope.
3. One JSON Envelope per line. No partial reads — clients buffer
   until they see `\n`. (Both sides MUST tolerate short reads on
   the socket and re-issue `read` until the delimiter arrives.)
4. Maximum message length: **64 KiB** per envelope. Both sides
   refuse to process a line longer than this without seeing the
   delimiter and close the connection with a clear log line.
5. UTF-8 only. JSON guarantees this; both encoders do too.

## Rationale

* **Debuggable.** An operator with `socat - UNIX-CONNECT:/var/run/
  teslafat-lun0.sock` can issue requests by hand and read
  responses with eyes. Length-prefixed binary requires a custom
  client.
* **No escape worries.** Compact-mode `serde_json` and Python
  `json.dumps` both escape internal newlines to `\n`, so a single
  `0x0a` byte is unambiguously a frame boundary.
* **Stdlib only on the Python side.** No `framed-msgpack` or
  similar dependency; `socket.recv` + `bytes.find(b"\n")` is the
  entire reader. Charter §3 prefers stdlib over a third-party
  framing crate when correctness is equivalent.
* **Cheap on the Rust side.** `tokio_util::codec::LinesCodec`
  with `max_length = 65_536` is one type and one `.framed(...)`
  call. No hand-rolled state machine.
* **Symmetric with the worker DB IPC.** The other Rust↔Python
  surface in B-1 (worker → Flask, Phase 4b) already uses NDJSON
  over Unix socket for the same reasons. Using the same framing
  for the daemon control socket means both ends of the Python
  side can share a transport class.
* **64 KiB cap matches expected payload sizes.** Largest envelope
  is a `RetentionUpdate` batch; even 1 000 clips at ~120 B per
  entry is 120 KB — but the worker enforces a per-batch cap of
  256 entries (≈ 30 KB), so 64 KiB is 2× headroom. Bigger
  batches indicate a worker bug; failing the read closed is the
  desired loud-failure mode.

## Alternatives considered

### 4-byte big-endian length prefix + raw JSON

The naive choice. Rejected because:
* Not socat-debuggable.
* Requires a 4-byte read-then-buffer-N-bytes state machine on the
  Python side that adds 30+ LOC and a class for stream state.
* Saves ~3 bytes per message vs. NDJSON — irrelevant at <1 req/s
  control traffic.
* No real upside given JSON itself is already text.

### MessagePack (length-prefixed or framed)

Rejected because:
* Pulls a Python dep (`msgpack`). Charter §3 disfavours
  third-party deps when stdlib + JSON is sufficient.
* Loses debuggability.
* The vocabulary types in `messages.rs` already serialise cleanly
  to JSON; switching encoder doesn't help compactness for our
  message shape.

### Raw socket, ad-hoc framing

Rejected — that's a per-message state machine, not a framing
contract.

## Consequences

### Positive

* Operator can hand-test the daemon by hand.
* Python client is ~40 LOC of transport vs. ~80 for length-prefix.
* Both sides have a single line-codec library call.

### Negative

* JSON parse cost is non-zero. Mitigation: control-plane traffic
  is < 1 req/s in steady state; the JSON parser is not on the
  hot path.
* Strings with raw `0x0a` bytes would break framing. Mitigation:
  JSON forbids unescaped control characters in strings; both
  encoders refuse to emit them.

### Neutral

* 64 KiB cap is a hard limit. If a future feature wants larger
  envelopes, the cap is bumped via this ADR (not silently).

## Implementation notes

* **Rust side** (Phase 1.5/5 follow-up):
  * Use `tokio_util::codec::LinesCodec::new_with_max_length(65_536)`.
  * On `LinesCodecError::MaxLineLengthExceeded`, log
    `warn!(reason="oversize_envelope")` and close the connection.
* **Python side** (Phase 5.5, this ADR's first consumer):
  * `socket.socket(AF_UNIX, SOCK_STREAM)`; manual buffered read
    until `b"\n"`.
  * Cap accumulated buffer at 65_536 bytes; raise `IpcFramingError`
    and close the socket if the cap is hit.
* **No `keepalive`, no heartbeats** at this layer. Connections
  are short-lived (open → request → response → close) per
  request, matching the lock-chimes "set active" UX expectation
  of < 200 ms perceived latency.

## Revisit triggers

* If we ever ship a streaming progress endpoint (large
  cleanup-preview), revisit and consider chunked NDJSON or
  Server-Sent-Events style.
* If we measure parser cost > 5% of control-plane CPU, revisit
  and consider a binary alternative.
