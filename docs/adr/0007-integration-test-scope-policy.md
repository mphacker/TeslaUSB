# ADR-0007 — Integration test scope: userspace wire only on dev box; kernel `nbd-client` deferred to H1

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.7 of B-1 rewrite |
| Commit   | `37fb2fb` (inc-1.7 implementation) |

## Context

Phase 1.7 was originally scoped (`docs/00-PLAN.md` row 1.7) as a
dev-box smoke test that "runs the `teslafat` binary and uses
`nbd-client` to connect to `/run/teslafat-0.sock`, reads return
all-zero from a `NullBackend`". The literal "use `nbd-client`"
reading turns out to be operationally impossible on every
environment except the Pi itself:

| Requirement of `nbd-client` | Available on dev box? | Available in CI? | Available on Pi? |
|---|---|---|---|
| Linux kernel `nbd` module loaded | Sometimes | No (generic container) | Yes |
| `/dev/nbdN` device nodes present | No (Windows dev) / sometimes (Linux dev) | No | Yes |
| `CAP_SYS_ADMIN` or root | No (regular user) | No | Yes |
| `nbd-client` binary installed | No | No | Yes (we install it) |

Even the Linux dev-box case is unreliable — running `nbd-client`
inside an unprivileged user namespace fails with `EPERM` on
`ioctl(NBD_SET_SOCK)`. Asking every contributor to run as root,
load the kernel module, and pre-create device nodes before the
test suite passes is incompatible with the charter's "tests must
be runnable by any contributor without ceremony" expectation.

The narrower problem the smoke test actually needs to solve is:
*does the `teslafat` binary correctly speak the NBD wire protocol
end-to-end?* The kernel `nbd-client` is one of *many* possible
NBD clients (the protocol is fully public). What we want to assert
is server conformance to the wire spec, not the specific behaviour
of one well-known client.

Phase 1.7 also forces a policy question that affects every future
phase: **what level of integration do `cargo test` tests cover,
and what is reserved for the hardware deploy gate H1?** Without an
answer in writing, every future increment will re-litigate this
trade-off in PR review (Phase 2 FileBackend tests, Phase 3 FAT
formatter tests, Phase 4 IPC tests, etc.).

## Decision

§A — **The smoke test speaks the NBD wire protocol directly from
the test process over `tokio::net::UnixStream`. It does NOT invoke
the kernel `nbd-client` tool.**

§B — **General policy: `cargo test` integration tests target the
binary's externally observable contracts (wire protocol, exit
codes, sentinel lines, signal handling, socket cleanup). Tests
that require root, kernel modules, special device nodes, or
hardware not present on a typical dev box are reserved for the H1
hardware deploy gate.**

## Rationale

### Why drive the wire directly (Decision §A)

1. **Tests what we ship, not what we depend on.** The teslafat
   binary's job is to produce conformant NBD wire bytes. Driving
   the wire from the test process tests exactly that. Going
   through `nbd-client` would test "does `nbd-client` accept our
   wire bytes", which is strictly weaker — a client could accept
   non-conformant bytes (via tolerant parsing) and we'd never
   know.
2. **Zero environment friction.** Any Rust developer with
   `cargo test --workspace` running on Linux or macOS sees the
   smoke tests run. No `sudo`, no `modprobe nbd`, no `mknod`. The
   charter requires gates to be runnable without ceremony; this
   is the only way to honour that requirement at the integration
   level.
3. **The wire protocol surface is already public crate API.** The
   daemon's own `nbd::handshake` and `nbd::wire` modules expose
   exactly the constants and encoders/decoders a client needs
   (`CF_FIXED_NEWSTYLE`, `IHAVEOPT`, `NBD_OPT_EXPORT_NAME`,
   `RequestHeader`, `encode_request_header`, etc.). The smoke
   tests import them directly and re-use them — eliminating
   duplicate spec-fact maintenance and pinning the public surface
   as part of every smoke run.
4. **The test stays close to what it asserts.** A failing test in
   `tests/smoke.rs` points directly at the daemon side — there is
   no "is this a kernel-client bug or a teslafat bug?" triage
   step. The captured daemon stderr lines (via
   `DaemonHandle::dump_stderr_on_failure`) sit alongside the
   panic message in test output.
5. **Speed.** A wire-direct test runs in sub-second. Spinning up
   a kernel NBD device, attaching, doing I/O, and tearing down
   takes 5–10 seconds even on warm hardware, multiplied by every
   test.

### Why kernel `nbd-client` is still valuable (the H1 case)

The kernel `nbd-client` validates a different layer:
- Does the daemon survive the kernel client's specific reconnect
  cadence (`-persist` flag)?
- Does the daemon's exported size make it through `/sys/block/nbdN/size`
  correctly so the kernel sees the right block-device geometry?
- Does the daemon cooperate with `mkfs.vfat` writing real FAT
  metadata to it?
- Does the daemon survive a `umount + unbind + rebind` cycle the
  way TeslaUSB's gadget binding does?

These are valuable assertions, but they're inherently hardware-
and-kernel-coupled. They belong at the H1 gate where we have a
real Pi, a real loaded `nbd` module, and a real USB gadget binding
in the loop.

### Why the dev-box / H1 split is general policy (Decision §B)

Phase 1.7 is the first integration test, but it will not be the
last. The same trade-off applies to:

| Future test | Userspace-only at integration | Hardware at H1 |
|---|---|---|
| FAT formatter writes correct boot sector | `mkfs.vfat` against a tempfile loop-mount equivalent (or raw byte assertions) | `mkfs.vfat` on a real `/dev/nbdN` via kernel client |
| Retention worker removes stale clips | `tempfile` directory + injected clock | Real SD card + real ArchivedClips layout |
| IPC server accepts a coordinator connection | Spawn the binary + connect from test process over Unix socket | Real teslausb-coordinator process |
| Power-cut recovery | Inject a "kill" mid-write via test harness | Pull SD card, replace, boot |

Without a stated policy, every PR risks proposing the hardware-
coupled variant (because it feels "more real") and getting stuck
on the inability to run it. The policy says: *if the userspace
variant can pin the contract you care about, ship the userspace
variant in `tests/`. If only the hardware variant can pin it, write
it as an H1 manual test in `docs/V2_HARDWARE_VALIDATION.md`'s
equivalent for this rewrite (TBD location).*

This policy does NOT mean "skip hardware testing". It means
"separate the two so each runs at the right time".

### Why not just gate the `nbd-client` test on a feature flag?

Considered: `#[cfg(feature = "kernel-nbd-tests")]` with the test
present but off-by-default. Rejected for two reasons:

1. **Off-by-default tests rot.** A gated test that nobody runs
   silently breaks the moment the surface it exercises changes.
   The charter explicitly disallows gates that aren't part of the
   default `cargo test` run for exactly this reason. (See the
   `tests-must-run-on-every-PR` discussion in PROGRESS session
   logs around inc-1.3.)
2. **It still doesn't solve the environment problem.** Enabling
   the feature on a dev box that lacks the `nbd` module just
   produces a confusing runtime panic instead of a compile-time
   skip. Better to be explicit: this category of test lives at H1,
   not in `cargo test`.

### Alternatives considered and rejected

- **`losetup`-based loopback NBD using `nbd-server` in-process.**
  Out of scope — `nbd-server` is a different binary, doesn't share
  the teslafat wire implementation, and adds a dependency for a
  test we can write in 766 LOC of pure Rust.
- **A mocked `nbd-client` stub.** Loses the value of "the wire
  bytes are real". Inferior to driving the actual wire from a
  real `UnixStream`.
- **Spawning `nbd-client` inside a privileged container in CI.**
  Project policy is no GitHub Actions for B-1 (per user
  instruction "prefer to not rely on github actions for now").
  Dev-box gates are the contract; CI is not the gate.

## Consequences

### Positive

- The 6 smoke tests in `rust/crates/teslafat/tests/smoke.rs` run
  on any Linux or macOS dev box with `cargo test`, zero ceremony.
  On Windows the file compiles to an empty test binary (the
  `#[cfg(unix)]` gate) so the suite is still buildable for
  Windows-resident reviewers.
- Future integration tests have a clear pattern to copy: spawn
  the binary via `env!("CARGO_BIN_EXE_<name>")`, drive its public
  contract from a test process, capture stderr, dump on failure.
- The H1 hardware checklist (to be defined in the H1 phase) has a
  clear input: every smoke test in `tests/` is a wire-level
  assertion; every H1 test is a hardware-level assertion. No
  overlap, no ambiguity.

### Negative

- The kernel `nbd-client` integration is not exercised until H1.
  If we ship a wire-conformant daemon that nonetheless trips up
  the kernel client (e.g. a slow flush-reply pattern the kernel
  doesn't like), we find out at H1 deploy time, not at PR time.
  Mitigation: H1 hardware gate is part of the Phase 1 close-out;
  Phase 2 starts only after H1 passes.
- The dev-box / H1 policy is binding for every future increment.
  We've signed up to maintain a separate H1 manual checklist as
  the surface area grows. Accepted as a known cost.

### Neutral

- The smoke harness re-uses production constants (`CF_FIXED_NEWSTYLE`,
  `NBD_REQUEST_MAGIC`, etc.) directly. If we ever decide to renumber
  any of these (we won't, they're spec constants), the smoke tests
  will auto-update. This is a feature, not a bug — the alternative
  would be to hand-paste byte values and risk drift.
- `CF_NO_ZEROES` is set in the smoke handshake (server replies with
  the compact 10-byte export reply, no 124-byte legacy pad). This
  is a smoke-test ergonomics choice, not a protocol-policy choice;
  the daemon must support both shapes either way (covered by the
  existing `legacy_export_name_*` unit tests in
  `nbd::handshake::tests`).

## Compliance plan

- Inc-1.7 implementation (commit `37fb2fb`) ships the 6
  wire-direct smoke tests under `rust/crates/teslafat/tests/smoke.rs`.
- Inc-1.7 review-fix (this ADR + PROGRESS + PLAN updates) amends
  PLAN row 1.7's "`nbd-client` connects to..." wording to reflect
  the userspace-wire reality. The original PLAN intent (validate
  the daemon end-to-end on the dev box) is fully met by the
  shipped tests; only the choice of client is amended.
- H1 hardware checklist (to be authored as part of Phase 1
  close-out, after this ADR lands) will include the kernel
  `nbd-client` test as a named manual step.
- Future Phase 2+ PLAN rows that describe integration tests will
  explicitly state "(dev-box wire-level)" or "(H1 manual)" per
  this policy. New integration tests that fall outside the
  dev-box-friendly envelope require a new ADR amendment to this
  one.

## References

- `docs/00-PLAN.md` row 1.7 (the original "use nbd-client" wording
  that this ADR explicitly amends)
- `docs/00-PLAN.md` H1 phase (the hardware deploy gate where the
  kernel `nbd-client` test belongs)
- `rust/crates/teslafat/tests/smoke.rs` (the inc-1.7 deliverable)
- ADR-0006 §B (per-connection error isolation contract, end-to-end
  validated by smoke test #3)
- NBD protocol spec
  (https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md)
  — the authoritative reference the wire constants come from
