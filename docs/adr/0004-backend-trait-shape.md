# ADR-0004 — `BlockBackend` trait shape (native AFIT) and `WriteFlags` newtype

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.4 of B-1 rewrite |
| Commit   | `8f98f43` (inc-1.4 implementation) |

## Context

Phase 1.4 introduces `teslausb-core::backend` — the dependency-
inversion seam between the NBD transmission loop (Phase 1.5) and
whatever concrete storage backs the export (initially a regular
file on the Pi's SD card via `teslafat::backend::FileBackend`,
later possibly a striped layout or an over-IPC proxy backend).

Two API-shape decisions had to be locked in. Both meet the
charter's ≥1-trigger ADR threshold (`docs/03-CODE-QUALITY-CHARTER.md`
§"ADRs"), and both are tightly coupled to `teslausb-core::backend`'s
public surface — so this ADR co-locates them, following the
ADR-0003 template of grouping two related API-shape decisions in
one document instead of fragmenting them into ADR-0004a /
ADR-0004b.

§A covers the trait's async machinery. §B covers the flag-passing
convention on `BlockBackend::write`. Both are visible to every
backend implementor and every transmission-loop caller for the
remainder of Phase 1+.

## §A — Decision: native `async fn in trait` (no `async-trait` dep)

The trait is declared as

```rust
#[allow(async_fn_in_trait)]
pub trait BlockBackend {
    fn size(&self) -> u64;
    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()>;
    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()>;
    async fn flush(&self) -> BackendResult<()>;
}
```

using native `async fn in trait` (stabilised in Rust 1.75) rather
than the `async-trait` crate macro.

### Consequence: no `dyn BlockBackend`

Native AFIT does not yet support `dyn` dispatch. The transmission
loop in Phase 1.5 will be generic over `<B: BlockBackend>`; the
binary chooses the backend impl at startup and instantiates one
generic transmission-loop type per impl. There will never be a
`Box<dyn BlockBackend>` or a `Vec<Box<dyn BlockBackend>>` in this
codebase as long as the trait stays AFIT.

This matches the parameterisation already used in
`teslafat::nbd::handshake::run<S: AsyncRead + AsyncWrite + Unpin>`
(see ADR-0003 §A) and the
`tokio::net::UnixStream` / `tokio::io::DuplexStream` parallel
testing path the handshake unit tests use.

### Alternatives considered

1. **`async-trait` crate macro.** Boxes every returned future
   (`Box<dyn Future>`). Enables `dyn BlockBackend`. Adds a
   transitive macro dep to `teslausb-core` — a crate whose
   charter description (`Cargo.toml` lines 11-14) explicitly
   limits runtime deps to `serde + thiserror`. Per-call
   allocation overhead is small in absolute terms but
   philosophically wrong for the hot read/write path that the
   NBD protocol exercises at ~thousands of ops/sec sustained.
   **Rejected** — the dyn-dispatch capability is not needed and
   the dep is at the wrong layer.
2. **`-> impl Future<Output = ...>` (RPITIT, stable in 1.75).**
   Semantically equivalent to AFIT — the compiler desugars AFIT
   to exactly this. More verbose at every method signature, no
   functional difference. **Rejected** — pure stylistic loss
   over the `async fn` keyword form.
3. **Sync trait + `tokio::task::spawn_blocking` in the
   transmission loop.** Keeps `teslausb-core` runtime-free in
   the strictest sense. Forces every backend method call to
   incur a `spawn_blocking` thread-pool round-trip (≥ µs latency
   per call). Mixes sync and async paradigms in the
   transmission loop. **Rejected** — paradigm mixing and the
   per-call latency hit dominate the no-dep benefit.
4. **Embassy `embedded-hal-async`-style traits.** Built for
   `no_std` embedded targets, irrelevant for a daemon running on
   a full Linux kernel. **Rejected** — wrong ecosystem.
5. **Sync trait, blocking I/O, no async anywhere.** Requires the
   transmission loop and IPC server to coexist on threads
   instead of tasks — exactly the architecture ADR-0003 §A
   rejected for the daemon. **Rejected** — internally
   inconsistent with the runtime decision.

### Trigger criteria fired

- Criterion 2 (affects > 1 module): the trait will be
  implemented in `teslafat::backend::FileBackend` (Phase 1.5),
  consumed by `teslafat::nbd::transmission` (Phase 1.5), and
  has reference implementations (`NullBackend`, `MockBackend`)
  in `teslausb-core::backend::mock`.
- Criterion 4 (forward-compat surface): adding `dyn`
  dispatchability later would require either `async-trait` or
  a parallel `dyn`-friendly trait — both source-breaking
  changes. Future contributors must see this decision before
  reaching for `Box<dyn BlockBackend>`.
- Criterion 5 (non-obvious vs alternatives): `async-trait` is
  the established convention in every tokio tutorial and on the
  async-book site. Native AFIT is the newer, less-documented
  path. Documenting the why prevents drive-by "fixes" that
  re-introduce the dep.

## §B — Decision: `WriteFlags(u32)` newtype with manual bit ops

The flag-passing type on `BlockBackend::write` is declared as

```rust
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct WriteFlags(u32);

impl WriteFlags {
    pub const NONE: Self = Self(0);
    pub const FUA: Self  = Self(1 << 0);
    // contains / is_empty / bits / from_bits_truncate
    // + BitOr / BitOrAssign / BitAnd / Display
}
```

with bitwise plumbing hand-written instead of generated via the
`bitflags!` macro.

### Consequence: tiny, no-dep surface that scales by hand-editing

Phase 1.4 has exactly one flag (`FUA`). Adding `PRE_FLUSH` or
`NO_HOLE` later means adding `pub const X: Self = Self(1 << n);`
+ extending the `Display` and `ALL_KNOWN_BITS` match — five lines
per flag. If a third or fourth flag arrives we will revisit;
`bitflags!` becomes more attractive once the bookkeeping is
non-trivial.

### Alternatives considered

1. **`bitflags = "2.6"` crate.** The universal idiom; generates
   `BitOr` / `BitAnd` / `contains` / `from_bits_truncate` /
   `Display` from a macro. Adds a runtime dep to a crate that
   otherwise has only `serde + thiserror`. For a single-flag
   surface the macro savings are real but the dep cost is
   visible. **Deferred** — adopt when ≥ 2 flags exist.
2. **`fua: bool` field on `write`.** Simplest possible API. Does
   not scale: adding `PRE_FLUSH` would require a breaking change
   to every implementor and caller. The bit-pattern field also
   matches the NBD wire convention exactly, which means the
   transmission loop can pass through `WriteFlags::from_bits_truncate(wire_flags)`
   with no mapping table. **Rejected** — does not scale.
3. **Separate `Write` / `WriteFua` trait methods.** Doubles the
   trait surface per flag. Combinatorial explosion at the third
   flag. **Rejected** — surface explosion.
4. **Public `u32` flag arg with constants in a `flags` module.**
   Loses type safety — any `u32` is callable. **Rejected** —
   surface a bare integer and the typestate is gone.
5. **`enum WriteFlags { None, Fua, ... }`.** Cannot represent
   combinations (`FUA | PRE_FLUSH`) — would require
   `Vec<WriteFlags>` or `BTreeSet<WriteFlags>` for combinations.
   **Rejected** — bit flags by definition compose.

### Trigger criterion fired

- Criterion 5 (non-obvious vs alternatives): `bitflags` is the
  obvious idiom; picking a newtype with manual ops is the
  unusual choice and deserves a written record of when to
  revisit.

The other four trigger criteria are not strongly hit (the type
is internal to `teslausb-core` for now, the bit pattern matches
the wire so there is no semantic-conversion correctness risk,
etc.) but criterion 5 alone is sufficient under the charter.

## FUA contract (informational, not a separate decision)

The Forced Unit Access semantics — captured in the
`backend.rs` module docstring and in three named tests
(`fua_contract_plain_write_is_not_durable_on_its_own`,
`fua_contract_fua_write_is_durable_immediately`,
`fua_contract_flush_after_plain_write_makes_durable`) — are part
of the trait's API contract and are tested via `MockBackend::observed_any_durability()`.
A backend impl that does not honour FUA will fail those tests.
This is not a separate ADR-able decision; it is the trait's
documented behaviour.

## References

- `docs/03-CODE-QUALITY-CHARTER.md` §"Best Architecture Practices"
  — domain-core layering rules.
- `docs/03-CODE-QUALITY-CHARTER.md` §"ADRs" — 5 trigger criteria.
- ADR-0003 — async runtime and crate shape (sibling decision —
  this ADR builds on the tokio-current-thread + lib+bin
  architecture).
- `rust/crates/teslausb-core/src/backend.rs` — the implementation
  this ADR documents.
- NBD newstyle protocol command flags: `NBD_CMD_FLAG_FUA = 1 << 0`,
  `NBD_CMD_FLAG_NO_HOLE = 1 << 1`, `NBD_CMD_FLAG_DF = 1 << 2`,
  `NBD_CMD_FLAG_REQ_ONE = 1 << 3`, `NBD_CMD_FLAG_FAST_ZERO = 1 << 4`.

## Consequences

- The transmission loop in Phase 1.5 will be a generic
  `Transmission<B: BlockBackend>` — no trait objects.
- Adding `dyn BlockBackend` later requires either (a)
  re-introducing `async-trait` as a Phase X cost-benefit
  re-litigation, or (b) waiting for native AFIT to gain dyn
  support (RFC #3668 work in progress as of Rust 1.85).
- Adding a second `WriteFlags` flag is a five-line change;
  adding a third or fourth justifies revisiting the `bitflags`
  dep decision.
- The FUA contract test naming convention (`fua_contract_*`)
  must be preserved by future backend impls in `teslafat` so a
  grep for that prefix lists the entire contract surface in one
  shot.
