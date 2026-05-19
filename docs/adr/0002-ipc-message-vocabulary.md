# ADR-0002 — IPC message vocabulary: enum-tagged envelope, format-agnostic, forward-compatible

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.2 of B-1 rewrite |
| Commit   | `aa3f18c` (inc-1.2 implementation) |

## Context

Charter §"The Boundaries Are Real" (lines 443–446) makes the IPC
schema the contract between the `teslafat` daemon and any peer
that needs to query or mutate its state (`teslausb-worker`, future
`teslactl`, the Python web app). It says the schema is "versioned"
but does not prescribe a structure.

Phase 1.2 (`docs/00-PLAN.md` row 1.2) had to commit the IPC
*vocabulary* — the request/response types — independently of the
*transport* (which encoder, what framing, which socket family). The
transport lands no earlier than Phase 1.5 alongside the NBD
transmission loop. Pinning the vocabulary first lets the daemon
and worker be built and tested in isolation against a frozen
contract.

The plan's row was concrete enough about *what* to model
(versioned envelope; `STATUS` / `RETENTION_UPDATE` /
`INVALIDATE_CACHE` request/response with `serde_test` round-trip
tests) that the design space was mostly about *how*:

1. Where does the version live, and what enforces it?
2. How are enum payloads tagged on the wire?
3. Should unknown fields be rejected (strict) or ignored
   (forward-compat)?
4. What error type wraps validation failures at the boundary?

## Decision

Adopt the following vocabulary design in
`rust/crates/teslausb-core/src/ipc/messages.rs`:

1. **Versioned envelope as a generic struct.**
   `Envelope<T> { version: u8, id: u64, payload: T }` wraps every
   request and every response. `T` is parameterised so the same
   envelope type serves request and response without duplication.
   A `const fn new(id, payload)` defaults `version` to
   `PROTOCOL_VERSION` (currently `1`).

2. **Validation lives on the envelope, returns typed error.**
   `Envelope::validate(&self) -> Result<(), IpcError>` returns
   `IpcError::UnsupportedVersion { got, expected }` (via
   `thiserror`) if the wire version doesn't match the local
   `PROTOCOL_VERSION`. Call sites at the wire boundary invoke
   this immediately after deserialisation.

3. **Internally-tagged enums for `Request` and `Response`.**
   `#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]`
   on both. Wire form for a unit variant is
   `{"type": "STATUS"}`; for a struct-style variant the tag is
   inlined alongside the variant's fields.

4. **No `#[serde(deny_unknown_fields)]` on wire types.**
   Forward-compatibility within a major version: a newer server
   may add fields to a response, and an older client must ignore
   them. The `PROTOCOL_VERSION` bump is the major-break signal.
   Unknown *variants* (`{"type": "DELETE_ALL_THE_THINGS"}`) still
   fail deserialisation hard — that's a missing dispatch case,
   not an additive field.

5. **Wire format deferred.**
   This module derives `Serialize` / `Deserialize` but does NOT
   pick an encoder. The transport layer (Phase 1.5+) picks
   length-prefixed JSON / MessagePack / etc. The decision is
   deferred because no transport-layer code exists yet to consume
   the bytes.

6. **`thiserror` at the lib boundary, `anyhow` reserved for
   binaries.** Per charter §"Rust standards". Adds
   `thiserror = "1.0"` to `teslausb-core` deps.

## Consequences

### Positive

* **Single source of truth for the contract.** Both the Rust
  daemon and the future Rust worker depend on `teslausb-core`; no
  duplicated struct definitions, no drift.
* **Transport-independent.** When Phase 1.5 picks length-prefixed
  JSON (most likely), the messages module is unchanged. If a
  later phase needs MessagePack for size reasons on the
  RAM-constrained Pi Zero 2 W, again the messages module is
  unchanged.
* **`Envelope::validate` makes the version field operational.**
  Without `validate`, the `version` field would be dead data and
  clippy would flag it. With it, the boundary check is a one-line
  guard at every wire entry point.
* **Forward-compat without a "schemaful" registry.** Adding a
  field to `StatusBody` is a non-breaking change; old clients see
  the field as if it never existed. Bumping `PROTOCOL_VERSION` is
  the explicit signal that a peer needs an update.
* **`serde_test` round-trip discipline.** Token-level assertions
  catch accidental schema breakage (e.g., a field rename without
  a `#[serde(rename = "old_name")]` compat alias) at unit-test
  time, not at integration time.

### Negative

* **Internally-tagged enums serialise as `Struct` not `Map` in
  `serde_test` token streams.** This is a known
  `serde_test`-specific footgun: copy-pasting an "envelope =
  map" mental model into a token assertion fails. The fix is to
  use `Token::Struct { name, len }` not `Token::Map`. Documented
  inline in the test bodies.
* **Forward-compat tolerance is one-directional.** A *new* server
  field is silently accepted by an old client, but an *old*
  server cannot produce a field a new client requires. The new
  client must treat any new field as optional (`Option<T>` /
  `#[serde(default)]`) for as long as the major version doesn't
  bump. This is on contributors to remember.
* **No central schema registry.** The contract is "whatever
  `teslausb-core` at the linked version says". Cross-language
  consumers (e.g., the Python web app) will need a hand-rolled
  shim or a generated schema. Deferred until a Python consumer
  actually appears.
* **`u8` for `PROTOCOL_VERSION` caps at 255 major bumps.**
  Acceptable: at one bump per year that's 2.5 centuries; if we
  ever need more, the bump itself is a breaking change anyway.

### Neutral

* **Validation is opt-in.** `Envelope::validate` must be called by
  the wire-boundary code; it is not enforced by `Deserialize`.
  This is intentional: the boundary may want to construct an
  envelope for testing without paying the validation cost. The
  Phase 1.5 transport will call `validate` immediately after
  every receive.

## Alternatives Considered

### Untagged enums (`#[serde(untagged)]`)

* Smaller wire form (no `"type"` field).
* Deserialisation tries each variant in order until one matches —
  silently accepts the *first* variant that happens to deserialise,
  which is a footgun for ambiguous payloads (`StatusBody` and
  `RetentionFailure` both have string-and-number fields). Tagged
  is more explicit and produces better error messages.

### Externally-tagged enums (default serde behaviour)

* Wire form `{"STATUS": {}}` — variant name as the *key*.
* Awkward for the empty `Status` request (`{"STATUS": null}` or
  `{"STATUS": {}}`?). Internally-tagged is more uniform across
  unit, struct, and tuple variants.

### Adjacently-tagged (`#[serde(tag = "type", content = "data")]`)

* Wire form `{"type": "STATUS", "data": {}}`.
* Extra nesting for no semantic gain. Internally-tagged is
  flatter on the wire.

### Separate types for each message instead of an enum

* `StatusRequest`, `StatusResponse`, `RetentionUpdateRequest`,
  etc., dispatched by reading the `"type"` field manually.
* Loses serde's automatic dispatch. The boundary code has to
  hand-roll a router. Charter §"Anti-patterns" forbids reinventing
  what a well-maintained crate already provides.

### Strict schema (`#[serde(deny_unknown_fields)]` on wire types)

* Catches typos in hand-written clients early.
* Defeats the entire purpose of the versioned envelope: every new
  optional field becomes a breaking change. Versioning is *for*
  forward-compat; strict schema is *against* it. Pick one.

### `anyhow::Error` instead of `IpcError`

* Less boilerplate for the validate function.
* Violates charter §"Rust standards" — `anyhow` at lib boundary
  loses the typed match the consumer needs (`match err {
  IpcError::UnsupportedVersion { got, expected } => log!(...) }`).

## Compliance

Charter §"ADRs" (`docs/03-CODE-QUALITY-CHARTER.md` lines 477–485)
mandates an ADR for decisions that meet ≥ 1 of:

* Affects > 1 module — **yes** (the IPC vocabulary is consumed by
  `teslafat`, `teslausb-worker`, and any future `teslactl` or
  Python shim).
* Locks in a third-party dependency — **yes** (`thiserror = "1.0"`
  is new in this increment).
* Changes a protocol or schema — **yes, explicitly** (this *is*
  the IPC schema the charter §"The Boundaries Are Real" refers to).
* Performance/correctness trade-off — partial (forward-compat
  tolerance is a correctness *choice*; standard serde derives).
* Contested in review — no.

Three of five criteria fire → ADR mandatory. This is that ADR.

## Implementation Reference

* Types + helpers + tests:
  `rust/crates/teslausb-core/src/ipc/messages.rs`
* Re-exports: `rust/crates/teslausb-core/src/ipc/mod.rs`
* Module declaration: `rust/crates/teslausb-core/src/lib.rs`
* Tests (16 unit tests in `mod tests`):
  - PROTOCOL_VERSION value pin
  - `Envelope::new` + `::validate` happy and unhappy paths
  - `IpcError` display formatting
  - `serde_test::assert_tokens` for `StatusBody`,
    `RetentionUpdate` (with `Extend` variant), `Request::Status`
  - `serde_json` end-to-end round-trips for every `Response`
    variant
  - Full `Envelope<Request>` end-to-end via JSON
  - Forward-compat: unknown future field silently ignored
  - Defensive: unknown enum variant tag rejected at parse time

## Follow-Up Work

* **Phase 1.5 (NBD transmission loop + transport layer):** picks
  the wire encoder (length-prefixed JSON is the default
  expectation) and wires `Envelope::validate` as the first call
  after every receive. The encoder choice may warrant its own
  ADR if the trade-offs are non-trivial (size vs. debuggability).
* **Phase 4b+ (worker):** when the worker speaks
  `RetentionUpdate` for real, add an integration test that
  exercises a full request/response cycle through a `socketpair`
  fixture. The vocabulary itself is unit-testable; the
  *protocol behaviour* needs a transport.
* **Phase 5 (Python web app):** if the web app ever needs to
  speak IPC directly (rather than always going through the
  worker), add a generated schema or a hand-rolled Python
  dataclass mirror. Skipped now because the web app's only IPC
  consumer in the plan is the worker.
* **Reintroduce `IpcConfig` in `teslafat/src/config.rs`** when a
  real consumer exists (Phase 1.5+ socket binding). Not done now
  because the field would be dead data and clippy would re-flag
  it (the same reason the field was removed in inc-1.1).
