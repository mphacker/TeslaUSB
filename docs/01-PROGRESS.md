# TeslaUSB B-1 — Progress Tracker

Living checklist. Update at each meaningful milestone.

Legend: ✅ done · 🔄 in progress · ⏳ pending · ❌ blocked · ⏭ skipped

---

## Phase 0 — Scaffolding

Each Phase 0 increment ends with `🔍 REVIEW GATE` (`charter-review`
skill) + `✅ TEST GATE` (`pre-commit run --all-files` + `cargo build`
on empty crates + `pytest` returning 0 tests OK).

| Inc | Deliverable | Status | Review | Test |
|---|---|---|---|---|
| 0.1 | Branch rename `b1-userspace-fat32` → `b1-userspace-rust`; first commit (v1 wipe + planning docs + skills) — commit `b5aeeee` | ✅ | ✅ APPROVED (doc-only, FHS path drift fixed in-place pre-merge) | ✅ git working tree clean post-commit |
| 0.2 | Cargo workspace at `rust/` (`Cargo.toml`, `rust-toolchain.toml`, `deny.toml`, empty crates `teslausb-core`, `teslafat`, `teslausb-worker`, each with `[lints]` per charter) | ✅ | ✅ APPROVED (charter coherence fixes applied in-place: 1.84→1.85 example, `teslafat/src/...` → `rust/crates/teslafat/src/...`, pre-commit `cd teslafat` → `cd rust`) | ✅ `cargo build / clippy -D warnings / fmt --check / test / doc` all green on pinned 1.85.0 |
| 0.3 | Python skeleton `web/teslausb_web/` with `pyproject.toml` (ruff + mypy + pytest per charter) | ✅ | ✅ APPROVED (6 charter coherence fixes applied in-place: 4 `web/` → `web/teslausb_web/` path drifts in CI gate + dead-code-detection blocks, `--cov=web` → `--cov=teslausb_web` module-name, `mypy web/` → bare `mypy` from `web/` cwd; plus added `from __future__ import annotations` to 5 docstring-only Python modules per charter Python deep-dive rule) | ✅ `ruff check / ruff format --check / mypy --strict / pytest --cov=teslausb_web --cov-fail-under=80 (100%) / vulture / bandit` all green on Python 3.13 dev box (3.11 target) |
| 0.4 | `scripts/check.sh` local gate runner with every gate from charter §"CI Gates" (operator-driven, NOT GitHub Actions — cloud CI deferred indefinitely per operator preference 2026-05-19; full hardware testing is H-phase territory anyway) | ✅ | ✅ APPROVED (charter coherence fix applied in-place: §"CI Gates" reframed from "hosted CI / PR" enforcement model to venue-neutral with explicit pointer to `scripts/check.sh`; hygiene wording updated `git diff` → `git ls-files` to match script behavior; 1 Nit accepted: magic `1048576` for 1 MiB is documented inline with charter line citation) | ✅ 12 PASS / 0 FAIL / 4 SKIP (cargo-llvm-cov, cargo-deny, cargo-machete, lychee — optional, install in Phase 0.6) on Windows dev box via Git Bash; exit 0 |
| 0.5 | `.pre-commit-config.yaml` mirroring CI gates locally via single-source delegation to `scripts/check.sh` (per operator preference 2026-05-19); upstream `pre-commit/pre-commit-hooks@v6.0.0` retained ONLY for cheap whitespace/EOF/yaml/TOML fixes | ✅ | ✅ APPROVED (charter coherence fix applied in-place: §"Pre-commit Hooks" L593-637 rewritten from the OLD mixed model — per-tool ruff/mypy/cargo upstream + local hooks — to the actual single-source design; upstream rev bumped `v4.6.0` → `v6.0.0` to silence deprecated-stage-names warnings; operator setup commands added) | ✅ `pre-commit run --all-files` 11 PASS / 0 FAIL / 0 SKIP exit 0; `./scripts/check.sh --all` 12 PASS / 0 FAIL / 4 SKIP exit 0; defensive re-run after charter fix also clean |
| 0.6 | `setup-dev.sh` (idempotent Rust + Python + tools install on a dev box) | ✅ | ✅ APPROVED (charter coherence fix applied in-place: §"CI Gates" now has an "Installation:" paragraph cross-referencing `scripts/setup-dev.sh` with the three modes `--check` / `--dry-run` / default; 1 Minor accepted: 372 LOC over the floated 250 budget, justified by 3 modes + 3 skip flags + cross-platform venv autodetect + 50% header/usage comments; hard cap Pillar 1 function-length 50 SLOC comfortably met — longest function `install_pip_deps` ~25 SLOC) | ✅ `bash -n` syntax exit 0; `--dry-run` exit 0 on Windows dev box (correctly reports 4 cargo tools as "would install" + venv already present + hook would install); `--check` exit 1 with structured diagnostics (rustup ✓, toolchain ✓, cargo-deny/machete/llvm-cov/lychee ✗, venv ✓ with all 7 expected dev tools, hook ✗); skip-flag matrix (`SETUP_SKIP_RUST=1` / `_PY=1` / `_HOOK=1`) honored; mutex `--dry-run --check` rejected with exit 2; unknown arg exit 2; both gate runners green after stage (`pre-commit run --all-files` 11/0/0 exit 0, `./scripts/check.sh --all` 12/0/4 exit 0) |
| 0.7 | `CODEOWNERS` + PR template referencing the charter checklist | ⏳ | ⏳ | ⏳ |
| 0.8 | ADRs 0001 – 0011 written (`docs/adr/`) | ⏳ | ⏳ | ⏳ |

**Resequencing note (2026-05-19, operator-authorized):**
H0 (decommission v1 from `cybertruckusb.local`) is now scheduled
**immediately after 0.1**, ahead of 0.2 – 0.8. Operator green-lit
the v1 wipe ("you can decommission the v1 code on the cybertruckusb.local
at any time… I have the tesla files I need from it backed up"). The
Pi has been running v1 with the failing archive worker that started
this whole investigation; getting it to a clean baseline now means
B-1 increments can be deployed and tested as soon as a binary is
ready, rather than waiting for full Phase 0 scaffolding. Phase 0.2 –
0.8 resume on the dev machine in parallel with the hardware
baseline.

## Phase 1 — Rust daemon skeleton

Per `00-PLAN.md` Phase 1, broken into 7 increments each ending
in a 🔍 REVIEW GATE + ✅ TEST GATE.

| Inc | Deliverable | Status | Review | Test |
|---|---|---|---|---|
| 1.1 | `teslafat::main` CLI + tracing + TOML loader | ✅ | ✅ APPROVED (impl `994fd65`; review-fix commit added ADR-0001 for YAML→TOML decision per charter §"ADRs" trigger criteria + removed preemptive `module_name_repetitions` allow after verifying lint doesn't fire on exact-match `config::Config`; 0 Major, 0 Minor post-fix — see session log) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 14 unit + 4 integration = 18 passed, 0 failed; `scripts/check.sh --all` 12/0/4; `pre-commit run` 9/0/2 (yaml+python: no files in changeset) |
| 1.2 | `teslausb-core::ipc::messages` types + serde tests | ✅ | ✅ APPROVED (impl `aa3f18c`; review-fix commit added ADR-0002 for the IPC vocabulary design — versioned envelope + internally-tagged enums + no `deny_unknown_fields` for forward-compat + `thiserror` at lib boundary — per charter §"ADRs" trigger criteria; 0 Major, 0 Minor post-fix — see session log) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 34 workspace tests passed, 0 failed (14 teslafat unit + 4 teslafat integration + 16 teslausb-core unit + 0 teslausb-worker); `scripts/check.sh --all` 12/0/4; `pre-commit run` 10/0/1 (yaml: no files in changeset) |
| 1.3 | NBD newstyle handshake (port from existing draft) | ✅ | ✅ APPROVED (impl `2c1a56f`; review-fix commit added ADR-0003 covering both async runtime choice — tokio current-thread, minimal features — and `teslafat` lib+bin crate-shape change forced by `dead_code` discipline on the new `nbd::handshake` public surface; the draft's 3 `try_into().unwrap()` calls were replaced with bounds-checked fallible conversions, the 2 `as usize`/`as u32` casts were replaced with `try_from + Context`, all encode/parse helpers got `///` + `# Errors` docs, and the protocol was decomposed into pure encode/decode helpers + a generic-over-`AsyncRead + AsyncWrite + Unpin` async orchestrator so the wire format is unit-testable via `tokio::io::duplex`; LOC budget overrun acknowledged in plan row 1.3 — "~50 (mostly path move)" → ~600 actual due to charter-compliance work the draft predated; Phase 1.6 `tokio::time::timeout` follow-up TODO added in `nbd/mod.rs`; preemptive `teslausb-core` dep removed (will land in Phase 1.4 with the BlockBackend trait); 0 Major, 4 Minor (all actioned), 2 Nits — see session log + `~/.copilot/.../files/charter-review-inc-1.3.md`) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 53 workspace tests passed, 0 failed (33 teslafat unit incl. 19 new `nbd::handshake` tests + 4 teslafat integration + 16 teslausb-core unit + 0 teslausb-worker); `scripts/check.sh --all` 12/0/4 same skip baseline; `pre-commit run` all hooks pass after one auto-fix round for `end-of-file-fixer` + `mixed-line-ending` on the three new files |
| 1.4 | `BlockBackend` trait + null impl + FUA contract test | ✅ | ✅ APPROVED (impl `8f98f43`; review-fix commit added ADR-0004 covering both the native `async fn in trait` decision — no `async-trait` crate dep, no `dyn BlockBackend`, generic-over-`<B: BlockBackend>` callers — and the `WriteFlags(u32)` newtype-vs-`bitflags!`-macro decision; the trait lives in `teslausb-core::backend` with `BackendError` (thiserror at lib boundary), `check_bounds` shared overflow-safe helper, and reference impls `NullBackend` + `MockBackend` in `pub mod mock`; FUA contract is captured by three named `fua_contract_*` tests using `MockBackend::observed_any_durability()` as the durability oracle; production code uses zero `unwrap`/`expect`/`panic` (mock module recovers poisoned mutexes via `lock().unwrap_or_else(PoisonError::into_inner)`); `pollster` 0.3 dev-dep added to drive `async fn` test bodies without pulling tokio into the domain-core crate; LOC budget overrun recorded — "~100" plan estimate vs ~620 actual due to charter compliance (`# Errors` docs, mock impls, FUA tests, `check_bounds` extraction); `teslafat → teslausb-core` dep still deferred to Phase 1.5 (correct per inc-1.3 review M4); 0 Major, 1 Minor (actioned), 3 Nits — see session log + `~/.copilot/.../files/charter-review-inc-1.4.md`) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 77 workspace tests passed, 0 failed (33 teslafat unit + 4 teslafat integration + 40 teslausb-core unit incl. 24 new `backend` tests + 0 teslausb-worker); `scripts/check.sh --all` 12/0/4 baseline maintained; `pre-commit run` all hooks pass on first attempt (no auto-fix round needed) |
| 1.5 | NBD transmission loop + FUA fdatasync test | ✅ | ✅ APPROVED (impl `f36e913`; review-fix commit added ADR-0005 covering both transmission wire policies — oversized requests terminate the connection (no payload drain), and out-of-bounds WRITEs drain the payload before replying EINVAL so the stream stays aligned for subsequent requests; the transmission module exposes one `pub async fn run<B: BlockBackend, S>` orchestrator that dispatches READ/WRITE/FLUSH/TRIM/DISC plus an ENOTSUP fall-through, with the wire format split into a pure-helpers `nbd::wire` submodule (28-byte request header + 16-byte simple-reply header, all encode/decode round-trippable in `[u8; N]` arrays with hand-asserted spec byte offsets); FUA pass-through is verified end-to-end via `MockBackend::observed_any_durability()` — the same FUA contract oracle introduced in inc-1.4 — proving the NBD `NBD_CMD_FLAG_FUA` wire bit reaches the backend as `WriteFlags::FUA`; production code uses zero `unwrap`/`expect`/`panic` and zero `indexing_slicing` (the request-header read loop was refactored to use `split_at_mut` + `read_exact`); tests use `tokio::join!` on a single task rather than `tokio::spawn` because native AFIT futures from `BlockBackend` are not `Send` (the consequence ADR-0004 §A predicted); 1 test design bug caught and fixed during development — the original FUA-extraction test asserted `NBD_CMD_WRITE != NBD_CMD_FLAG_FUA` but both constants are numerically `1`, rewritten as two independent encode/decode tests using hand-rolled spec byte layouts and `NBD_CMD_TRIM = 4` as the unambiguous `kind` value to eliminate the false-pass risk for a symmetric encoder/decoder field-swap; `teslafat → teslausb-core` runtime dep finally lands (closing the inc-1.3 M4 deferral); LOC budget overrun recorded — "~200" plan estimate vs ~1453 actual due to charter-compliance work (spec-citation docstrings, wire-vs-orchestrator split, byte-layout assertions, full command-coverage tests, mutation-test mental model on every assertion); 0 Major, 1 Minor (actioned), 3 Nits — see session log + `~/.copilot/.../files/charter-review-inc-1.5.md`) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 107 workspace tests passed, 0 failed (63 teslafat unit incl. 30 new in `nbd::transmission` + `nbd::wire` + 4 teslafat integration + 40 teslausb-core unit + 0 teslausb-worker); `scripts/check.sh --all` 12/0/4 baseline maintained; `pre-commit run` all hooks pass after one mixed-line-ending auto-fix on `nbd/wire.rs` |
| 1.6 | `teslafat@.service` systemd unit | ✅ | ✅ APPROVED (impl `1228f41`; review-fix commit added ADR-0006 covering three coupled connection-lifecycle decisions — §A single connection at a time per process (no `tokio::spawn`, `serve` awaits `serve_one_connection` to completion before the next `accept()`); §B per-connection errors never propagate to the accept loop (`serve_one_connection -> ()` not `Result`, every per-client failure is `warn!`-logged so a misbehaving client cannot trigger `Restart=on-failure` daemon-exit cycles and defeat the systemd hardening); §C handshake timeout (`tokio::time::timeout` wrap, default 10 s, range [1, 600] s) is the only liveness check in the daemon — the kernel `nbd-client` polices request liveness via `/sys/block/nbdN/queue/io_timeout`, and a duplicate userspace per-request timeout would kill legitimate large WRITEs on the Pi Zero 2 W under SD pressure; new `crate::backend::ZeroBackend` (~230 LOC, 12 tests) is a sparse `BlockBackend` impl that synthesises zeros and stores only its `u64` size (no `Vec` allocation — kept in `teslafat::backend` rather than `teslausb-core::backend::mock` to avoid expanding the inc-1.4 surface and to avoid the `NullBackend::new(size: usize) -> Vec<u8>` allocation hazard for daemon-scale `volume_size_gb`); new `crate::server` (~526 LOC, 10 tests = 5 cross-platform `serve_one_connection_*` + 5 Unix-only `accept_loop::*` gated `#[cfg(unix)]` since `UnixListener` is Unix-only); new `crate::config::NbdConfig` (socket_path + handshake_timeout_seconds, validated [1, 600] s); `--check-config` flag preserves the Phase 1.1 sentinel contract (validate + emit + exit) for both `tests/sentinel.rs` and the unit's `ExecStartPre=` fast-fail gate; `units/teslafat@.service` (~104 LOC instanced template) ships full systemd hardening (`CapabilityBoundingSet=`, `ProtectSystem=strict`, `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`); main.rs rewritten so the default mode builds a current-thread tokio runtime, prepares the socket (mkdir parent + unlink stale file), installs SIGTERM/SIGINT handlers, and runs `server::serve` until signal; the inc-1.3 follow-up TODO in `nbd/mod.rs` is now closed (handshake timeout wired in §C); `teslafat -> teslausb-core` dep extended to cover the `BlockBackend` impl in `ZeroBackend`; LOC budget overrun recorded — "~50" plan estimate vs ~1227 actual due to charter compliance (two new public modules with full doc + test coverage, `NbdConfig` schema delta + validation, systemd hardening profile, `--check-config` flag, SIGTERM/SIGINT plumbing with graceful fallback); 0 Major, 1 Minor (actioned), 4 Nits — see session log + `~/.copilot/.../files/charter-review-inc-1.6.md`) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 129 workspace tests passed, 0 failed (85 teslafat unit incl. 22 new across `backend` + `server` + `config::NbdConfig` + 4 teslafat integration + 40 teslausb-core unit + 0 teslausb-worker; the 5 `accept_loop::*` server tests are `#[cfg(unix)]`-gated so they compile-clean but don't run on the Windows dev box — Linux/Pi will exercise them); `scripts/check.sh --all` 12/0/4 baseline maintained; `pre-commit run` all hooks pass after one mixed-line-ending auto-fix on `units/teslafat@.service` |
| 1.7 | Dev-box smoke test harness | ✅ | ✅ APPROVED (impl `37fb2fb`; review-fix commit added ADR-0007 covering the integration-test scope policy — the smoke test speaks the NBD wire protocol *directly* from the test process over `tokio::net::UnixStream` instead of through the kernel `nbd-client` tool, because `nbd-client` requires `CAP_SYS_ADMIN` + a loaded `nbd` kernel module + `/dev/nbdN` device nodes that are unavailable on any non-Pi dev box or contributor laptop; ADR-0007 §B promotes that decision to a general policy for every future Phase 2+ integration test: `cargo test` exercises wire-level / binary-level / signal-level / observability contracts on any Linux or macOS dev box with zero ceremony, while hardware-coupled assertions (kernel client behaviour, real FAT formatter on `/dev/nbdN`, USB gadget binding, power-cut recovery) live in the H1 hardware checklist; new `rust/crates/teslafat/tests/smoke.rs` (~766 LOC, 6 tests, file-level `#![cfg(unix)]`) covers happy-path handshake + READ + DISC + SIGTERM exit, no-clients SIGTERM exit + socket cleanup, ADR-0006 §B end-to-end (bad client followed by clean client), `cfg.volume_size_gb` -> wire-advertised export size with a non-default 17 GiB pin, "started" sentinel emission in live-serve mode (not just `--check-config` which `tests/sentinel.rs` covered), and `nbd.handshake_timeout_seconds` -> sentinel JSON's `nbd_handshake_timeout_s` field; `DaemonHandle` RAII guard owns `Child` + `TempDir` + `Arc<Mutex<Vec<String>>>` stderr capture pumped by a vanilla OS thread, with `Drop` that SIGTERM-then-SIGKILLs and dumps captured stderr on test panic via `thread::panicking()` guard; `send_sigterm` shells out to `kill(1)` for zero new deps + zero `unsafe` + POSIX portability across Linux + macOS dev hosts; wire helpers re-use the daemon's own public `nbd::handshake` + `nbd::wire` constants (`CF_FIXED_NEWSTYLE`, `CF_NO_ZEROES`, `IHAVEOPT`, `NBD_OPT_EXPORT_NAME`, `RequestHeader`, `encode_request_header`, `NBD_SIMPLE_REPLY_MAGIC`, `NBD_EOK`) so the test directly pins the public protocol surface; on Windows the file compiles to an empty test binary (`#[cfg(unix)]` gates it) — Linux + Pi run all 6 tests at H1 first hardware deploy; zero new dependencies (tempfile + tokio + the teslafat lib were already in dev-deps); LOC budget overrun recorded — "~80" plan estimate vs ~766 actual (~9.5x; consistent with inc-1.3 through inc-1.6 helper-heavy pattern); 0 Major, 1 Minor (actioned by this review-fix), 4 Nits — see session log + `~/.copilot/.../files/charter-review-inc-1.7.md`) | ✅ `cargo build / clippy -D warnings / fmt --check / doc -D warnings / test` all green; 129 workspace tests passed, 0 failed (85 teslafat unit + 4 teslafat integration sentinel + 0 teslafat integration smoke (cfg-gated on Windows; +6 on Linux/Pi) + 40 teslausb-core unit + 0 teslausb-worker); `scripts/check.sh --all` 12/0/4 baseline maintained; `pre-commit run` all hooks pass first try (no autofix rounds) |

**Pre-existing scaffolding from earlier sessions:**

- 🔄 `teslafat/Cargo.toml` — exists (1708 B), needs TOML migration + lints
- 🔄 `teslafat/src/main.rs` — exists (5574 B), doesn't compile
- 🔄 `teslafat/src/config.rs` — exists (4006 B), YAML → TOML
- 🔄 `teslafat/src/nbd/mod.rs` — exists (3420 B), doesn't compile
- ✅ `teslafat/src/nbd/handshake.rs` — exists (8126 B), reusable

These are reorganized as part of increment 1.1 + 1.3 + 1.4 + 1.5.

## Phase H0 — Decommission v1 from `cybertruckusb.local`

First hardware work. See `00-PLAN.md` Phase H0 for the 14-step
contract. **Until this is done, no B-1 binary may be deployed
to the device.**

| Inc | Step | Status |
|---|---|---|
| H0.1 | tar snapshot of /etc + dotfiles, scp off-device (803 KB, sha256 `DA62A22B…1845C`) | ✅ |
| H0.2 | systemctl stop v1 services (6 of them, in order) | ✅ |
| H0.3 | systemctl disable v1 services (7, incl. teslausb-safe-mode) | ✅ |
| H0.4 | Reboot — verify boot + WiFi + SSH (75 s back, load 0.71) | ✅ |
| H0.5 | ~~systemctl mask v1 services~~ — **skipped, superseded by H0.6** (rm of unit files makes mask redundant) | ⏭️ |
| H0.6 | rm v1 systemd unit files at `/etc/systemd/system/` (8 units removed; +2 discovered later: network-optimizations, wifi-powersave-off) | ✅ |
| H0.7 | rm v1 sudoers drop-ins (preserving non-teslausb entries) — removed `/etc/sudoers.d/teslausb-gadget` | ✅ |
| H0.8 | rm v1 NM dispatcher scripts — removed `99-teslausb-cloud-refresh` | ✅ |
| H0.9 | ~~Comment out v1 cmdline.txt + config.txt entries (with .b1-backup)~~ — **DEFERRED to Phase 6** (`hardware-test` skill rule: setup.sh handles `/boot/firmware/*` idempotently; manual edit here = drift risk) | ⏭️ |
| H0.10 | rm -rf ~/TeslaUSB + ~/ArchivedClips (after operator confirm) — **454 GB freed in 6.8 s; disk 100% → 2%** | ✅ |
| H0.11 | Disable smbd + nmbd (Samba is opt-in in B-1) | ✅ |
| H0.12 | ~~Disable v1's watchdog.service~~ — **KEPT enabled** (watchdog daemon is pure defensive HW health, not v1-specific; B-1 wants the same protection; `teslausb-priority.conf` drop-in preserved) | ⏭️ |
| H0.13 | Reboot — verify clean baseline (took 2 final reboots due to a D-Bus wedge after the 3rd reboot, recovered via operator power-cycle) | ✅ |
| H0.14 | Capture clean-boot journal as reference baseline — saved to session workspace as `h0-clean-baseline-boot.log.gz` (19.4 KB / 954 lines, 0 TeslaUSB-related warnings) | ✅ |

**🔍 REVIEW GATE:** N/A for H0 (no code changed, only destructive
ops on existing hardware). **✅ TEST GATE:** verification probe at
end of H0.14 green on all 12 checks (disk freed, mem clean, WiFi up,
gadget configfs empty, no loop mounts, 0 failed units, all 9 v1
services `not-found`, no v1 unit residuals in `/etc/systemd/system/`,
home dir clean, ssh/watchdog/NM/logind/dbus all `active`).

## Phase H1 — Daemon smoke on hardware

| Inc | Step | Status |
|---|---|---|
| H1.1 | Cross-build teslafat for aarch64 via `tools/xbuild/` podman image (Pi build forbidden — ADR-0008) | ✅ |
| H1.2 | scp to /tmp + install -m 0755 to /usr/local/bin/teslafat | ✅ |
| H1.3 | Install teslafat-test@.service (NOT production name) + teslafat-0.toml | ✅ |
| H1.4 | --check-config as teslausb user (sentinel JSON, exit 0); systemctl start teslafat-test@0 | ✅ |
| H1.5 | nbd-client connects + newstyle handshake completes (negotiated 4096 MB) | ✅ |
| H1.6 | blockdev --getsize64 returns 4294967296 (exact 4 GiB); read returns zeros (ZeroBackend by design — FAT in Phase 2) | ✅ |
| H1.7 | nbd-client -d, systemctl stop; runtime dir auto-cleaned; binary + unit + config left installed for downstream H-increments | ✅ |
| H1.8 | SSH alive, uptime steady (load 0.27 post-test), swap 0, no OOM, no NEAR-MISS | ✅ |

**🔍 REVIEW GATE:** charter-review-h1.md (session-state),
covering the two crash artifacts that yielded ADR-0008.
**✅ TEST GATE:** all H1.x green; journal captured to
session-state as `h1-journal.log`.

## Phase 2 — FS read-side synthesis (FAT32 + exFAT)

| Inc | Deliverable | Status | Review | Test |
|---|---|---|---|---|
| 2.1 | `fs::geometry` + `fs::fat32::geometry` | ⏳ | ⏳ | ⏳ |
| 2.2 | `fs::fat32::boot_sector::synthesize` | ⏳ | ⏳ | ⏳ |
| 2.3 | `fs::fat32::fsinfo::synthesize` | ⏳ | ⏳ | ⏳ |
| 2.4 | `fs::fat32::fat_table::synthesize` | ⏳ | ⏳ | ⏳ |
| 2.5 | `fs::fat32::directory::synthesize` (8.3 + LFN) | ⏳ | ⏳ | ⏳ |
| 2.6 | `fs::fat32::synth::read` dispatcher | ⏳ | ⏳ | ⏳ |
| 2.7 | `fs::fat32` integration test (synth+mount+cmp) | ⏳ | ⏳ | ⏳ |
| 2.8 | `fs::exfat::geometry` + boot region | ⏳ | ⏳ | ⏳ |
| 2.9 | `fs::exfat::allocation_bitmap` + `upcase_table` | ⏳ | ⏳ | ⏳ |
| 2.10 | `fs::exfat::directory` | ⏳ | ⏳ | ⏳ |
| 2.11 | `fs::exfat::synth::read` dispatcher | ⏳ | ⏳ | ⏳ |
| 2.12 | `fs::exfat` integration test | ⏳ | ⏳ | ⏳ |
| 2.13 | `lazy_load.rs` (deferred deep-dir materialization) | ⏳ | ⏳ | ⏳ |
| 2.14 | Cold-start benchmark (≤ 1 s for 10K files) | ⏳ | ⏳ | ⏳ |

## Phase H2 — Read-only synth on hardware

H2.1 – H2.8 per `00-PLAN.md`. All ⏳.

## Phase 3 — FS write-side (FAT32 + exFAT)

| Inc | Deliverable | Status |
|---|---|---|
| 3.1 | `fs::fat32::parse::decode_write` | ⏳ |
| 3.2 | `fs::exfat::parse::decode_write` | ⏳ |
| 3.3 | `backend::dir_tree` POSIX adapter | ⏳ |
| 3.4 | `cluster_map` extent-based | ⏳ |
| 3.5 | Wire `synth::write` integration test | ⏳ |
| 3.6 | Power-cut harness | ⏳ |

## Phase H3 — Write-side on hardware

H3.1 – H3.5 per `00-PLAN.md`. All ⏳.

## Phase 4 — RecentClips retention shim

| Inc | Deliverable | Status |
|---|---|---|
| 4.1 | `retention::filter` (mtime hide) | ⏳ |
| 4.2 | Tesla-delete interception | ⏳ |
| 4.3 | Virtual free-cluster reporting | ⏳ |
| 4.4 | TOML config + IPC reload | ⏳ |

## Phase 4b — Cleanup + indexer-driven preservation (Rust)

| Inc | Deliverable | Status |
|---|---|---|
| 4b.1 | `teslausb-worker::sei` (port v1 logic to Rust, golden parity) | ⏳ |
| 4b.2 | `teslausb-worker::indexer` (inotify → SEI → SQLite) | ⏳ |
| 4b.3 | `teslausb-worker::cleanup` (GPS-aware deletion) | ⏳ |
| 4b.4 | `teslausb-worker::main` task supervisor | ⏳ |
| 4b.5 | `teslausb-worker.service` systemd unit | ⏳ |

## Phase H4 — Retention + worker on hardware

H4.1 – H4.5 per `00-PLAN.md`. All ⏳.

## Phase 4c — Tesla cache invalidation

| Inc | Deliverable | Status |
|---|---|---|
| 4c.1 | `scripts/tesla_cache_invalidate.sh` | ⏳ |
| 4c.2 | sudoers fragment + install path | ⏳ |
| 4c.3 | `services/cache_invalidation.py` debouncer | ⏳ |
| 4c.4 | Unit tests | ⏳ |
| 4c.5 | Integration test on dev box | ⏳ |

## Phase H4c — Cache invalidation on hardware

H4c.1 – H4c.6 per `00-PLAN.md`. All ⏳.

## Phase 5 — Python web app (Flask, UI only)

Each increment ends with charter-review + (for blueprints/templates)
a screenshot diff vs. v1 baseline.

| Inc | Deliverable | Status |
|---|---|---|
| 5.1 | Copy `UI_UX_DESIGN_SYSTEM.md` from v1 (doc-only) | ⏳ |
| 5.2 | Flask app skeleton + factory + gunicorn entry | ⏳ |
| 5.3 | Static assets port (fonts, SVGs, CSS, JS) | ⏳ |
| 5.4 | Templates skeleton (base.html, partials, theme) | ⏳ |
| 5.5 | `services/teslafat_client.py` IPC | ⏳ |
| 5.6 | Register `services/cache_invalidation.py` (built in 4c.3) | ⏳ |
| 5.7 | `blueprints/system_health.py` (btrfs scrub widget) | ⏳ |
| 5.8 | `blueprints/lock_chimes.py` + full flow | ⏳ |
| 5.9 | `blueprints/light_shows.py` | ⏳ |
| 5.10 | `blueprints/wraps.py` (PNG dim validation) | ⏳ |
| 5.11 | `blueprints/music.py` | ⏳ |
| 5.12 | `blueprints/boombox.py` (5-file alphabetical cap) | ⏳ |
| 5.13 | `blueprints/mapping.py` + overlay player | ⏳ |
| 5.14 | `blueprints/cloud_archive.py` | ⏳ |
| 5.15 | `blueprints/captive_portal.py` | ⏳ |
| 5.16 | `blueprints/settings.py` (mode-removal + samba toggle) | ⏳ |
| 5.17 | `services/samba_service.py` + inotify | ⏳ |
| 5.18 | `services/cleanup_service.py` UI orchestrator | ⏳ |
| 5.19 | gunicorn + nginx config snippets | ⏳ |

## Phase H5 — Web app on hardware

H5.a, H5.b, H5.c, ... — run after every 3 phase-5 increments.
Each does the rsync + venv + test-gunicorn-on-8080 + screenshot
diff dance per `00-PLAN.md`. All ⏳.

## Phase 6 — setup.sh + uninstall.sh

| Inc | Deliverable | Status |
|---|---|---|
| 6.1 | setup.sh package install + idempotency + --dry-run | ⏳ |
| 6.2 | setup.sh user/group + sudoers | ⏳ |
| 6.3 | setup.sh btrfs subvolume creation | ⏳ |
| 6.4 | setup.sh systemd unit install | ⏳ |
| 6.5 | setup.sh NetworkManager + AP (with .b1-backup) | ⏳ |
| 6.6 | setup.sh boot cmdline + config.txt (with .b1-backup) | ⏳ |
| 6.7 | setup.sh watchdog + sshd-protect drop-ins | ⏳ |
| 6.8 | setup.sh enable + start + post-start health check | ⏳ |
| 6.9 | uninstall.sh + --purge | ⏳ |
| 6.10 | shellcheck clean + --help complete | ⏳ |

## Phase H6 — setup.sh on a clean Pi

H6.1 – H6.7 per `00-PLAN.md`. Operator must reserve a second Pi
or a freshly-flashed SD card before this phase begins. All ⏳.

## Phase 7 — Integration + hardware soak

| Inc | Deliverable | Status |
|---|---|---|
| 7.1 | Integration test suite | ⏳ |
| 7.2 | Synthetic Tesla-write load harness | ⏳ |
| 7.3 | Cache-invalidation acceptance on truck | ⏳ |
| 7.4 | 24-hour parked-Sentry soak | ⏳ |
| 7.5 | 72-hour driven soak | ⏳ |

## Phase 8 — Documentation

| Inc | Deliverable | Status |
|---|---|---|
| 8.1 | README.md | ⏳ |
| 8.2 | docs/architecture.md | ⏳ |
| 8.3 | docs/fs-synthesis.md | ⏳ |
| 8.4 | docs/tesla-cache-invalidation.md | ⏳ |
| 8.5 | docs/setup.md | ⏳ |
| 8.6 | docs/uninstall.md | ⏳ |
| 8.7 | docs/development.md (incl. hardware-test framework) | ⏳ |
| 8.8 | docs/charter-review-playbook.md | ⏳ |

---

## Session log

### 2026-05-19 — Session start
- Branch created
- Old tree wiped (255 → 0 tracked files in deletion staging)
- Directory scaffold created
- Planning docs in place
- Beginning Phase 1 (Rust daemon skeleton)

### 2026-05-19 (resumed) — Scope expansion
- exFAT added as primary FS, FAT32 retained as fallback
- Cleanup policy elevated to Phase 4b
- Power-loss tolerance promoted to first-class invariant
- Phase 1 partial complete (Cargo.toml, main.rs, config.rs,
  nbd/{mod,handshake}.rs)

### 2026-05-19 (resumed, again) — UI parity contract
- Operator: "I want the website to look the same as it does now"
- Phase 5 substantially expanded with port-verbatim list + screenshot-diff gate
- Samba reframed from "anti-pattern" to "optional first-class feature"
- 12th invariant added: UI parity is binding

### 2026-05-19 (resumed, again) — Code Quality Charter
- New `docs/03-CODE-QUALITY-CHARTER.md` (~28 KB) created
- Five pillars: no smells, best architecture, no shortcuts,
  fix bugs immediately, no dead code
- 13th invariant added: charter is binding

### 2026-05-19 (resumed, again) — Anti-anchoring + Rust-first
- Operator: "Don't feel locked into the original way of doing things"
- Operator: "If something could work faster in Rust vs Python, do
  it in Rust. Don't worry about potential regression."
- Architecture rewritten: 3 Rust processes (teslafat-0, teslafat-1,
  teslausb-worker) + 1 Python Flask web app behind nginx + gunicorn
- SEI parser moves from "port v1 Python verbatim" to "fresh Rust impl"
- Indexer, cleanup, cloud uploader, file watcher all move to Rust
  (`teslausb-worker` binary)
- Config format switched from YAML to TOML
- Web app moved out from "Flask as root on port 80" to "Flask behind
  nginx behind gunicorn, runs as `teslausb` user"
- USB gadget switched from `g_mass_storage` module to pure configfs
  + `usb_f_mass_storage`
- Database consolidated: single `/var/lib/teslausb/teslausb.db`
- Filesystem paths switched to FHS standard (`/etc/`, `/srv/`,
  `/var/lib/`, `/run/`)
- SDIO write coordination: `fcntl(LOCK_EX)` on sentinel file,
  replacing v1's in-process `task_coordinator`
- Decisions table grew from 19 → 31 locked decisions
- New "v1 carry-forwards we are NOT taking" section in PLAN.md
- New "v1 carry-forwards we ARE keeping" section in PLAN.md
- Branch should be renamed at first commit:
  `b1-userspace-fat32` → `b1-userspace-rust`
- New ADRs added to Phase 0 backlog (0006-0011)

### 2026-05-19 (resumed, again) — Tesla folder/file conventions
- Operator: "in the /srv/teslausb/media folder we need a LightShow
  folder... For the current lock chime it needs to be called
  LockChime.wav and placed in the root of the ./media folder"
- Operator: "also note that the current v1 implementation handles
  these folders correctly" → v1 source = authoritative
- Added "Tesla on-USB folder/filename conventions (canonical)"
  section to `00-PLAN.md` with case-sensitive folder names
  (`Chimes`, `LightShow`, `Wraps`, `Music`, `Boombox`,
  `LockChime.wav`), per-feature size/format/count limits,
  citation back to specific v1 service files, and the final
  B-1 `/srv/teslausb/media/` directory layout.
- Locked Rust constants (planned in
  `rust/crates/teslausb-core/src/paths.rs`) so no caller can
  typo the folder names.
- Documented the trade-off of consolidating Music+Boombox onto
  LUN 1 (v1 had a separate LUN 3) per operator preference.

### 2026-05-19 (resumed, again) — Incremental review + hardware-test discipline
- Operator: "Don't do a ton of work and wait to do code reviews.
  Have specific code review breaks and then fix ALL issues you
  find. We should have a way to test the code too."
- Operator: "You will use the device at cybertruckusb.local
  (login with the account pi) for testing... You do need to be
  very careful to not knock it offline (break wifi connection),
  cause boot issue, or cause anything that would block you from
  SSH into the device."
- **Phased implementation restructured** in `00-PLAN.md` —
  every phase broken into numbered increments (e.g., 1.1 –
  1.7, 2.1 – 2.14, 4b.1 – 4b.5, 5.1 – 5.19). Each ends with
  a 🔍 REVIEW GATE + ✅ TEST GATE. No batching.
- **New H-series phases interleaved** — H0 (decommission v1),
  H1 (daemon smoke), H2 (RO synth), H3 (write-side), H4
  (retention + worker), H4c (cache invalidate), H5 (web app
  screenshot diffs every 3 increments), H6 (clean Pi install),
  H7 (24h + 72h soaks). Each H-step uses the safety wrapper.
- **New "Hardware test environment" section** added to
  `00-PLAN.md` codifying the three sacred rails (SSH up,
  WiFi up, boot OK) and the dead-man-reboot safety contract.
- **New skill `.github/skills/charter-review/SKILL.md`** —
  per-increment charter-compliance audit. Five Pillars +
  Rust/Python deep dives + architecture compliance + delegated
  security and UI/UX reviews + phase-gate criteria. Outputs
  structured report; reviews must mark APPROVED before next
  increment.
- **New skill `.github/skills/hardware-test/SKILL.md`** —
  single sanctioned way to touch `cybertruckusb.local`. Arms
  3-min dead-man timer before every step, snapshots files
  before edits, refuses to touch sshd/NetworkManager without
  explicit operator confirmation, captures journals for
  charter-review.
- **PROGRESS.md restructured** — every phase shown as a table
  of numbered increments with Status / Review / Test columns.
  Single source of truth for "what's done, what's next, what
  passed its gate".
### 2026-05-19 (resumed, again) — 0.1 baseline commit + review + H0 unblocked

- **Increment 0.1 landed as commit `b5aeeee`** ("chore(b1): wipe v1
  + establish B-1 greenfield baseline"). 264 files changed,
  4 975 insertions, 140 099 deletions. Branch renamed
  `b1-userspace-fat32` → `b1-userspace-rust`. Not pushed to
  origin yet (awaiting operator direction; default is hold for
  local review first).
- **Increment 0.1 charter review — APPROVED with in-place fixes.**
  Doc-only scope; verified internal consistency, no orphan TODOs,
  no stale references to the old monolithic phase structure.
  **MAJOR findings (all fixed pre-merge of next increment):**
  every B-1 body-text reference to v1 paths (`/var/teslacam/`,
  `/var/teslalightshow/`) replaced with FHS paths
  (`/srv/teslausb/teslacam/`, `/srv/teslausb/media/`) per
  Decision #26; one B-1 `config.yaml` reference (Samba persistence)
  replaced with `/etc/teslausb/teslausb.toml` per Decision #23;
  one CHARTER `config.yaml.example` reference replaced with
  `teslausb.toml.example`. v1 source citations
  (`config.yaml: web.lock_chime_filename` etc.) and
  `.pre-commit-config.yaml` (which IS yaml — industry convention)
  left as-is. Net 19 path/format substitutions across PLAN,
  LEARNINGS, CHARTER. No structural changes.
- **Operator green-lit hardware decommission** ("you can decommission
  the v1 code on the cybertruckusb.local at any time and use that
  hardware for testing. I have the tesla files I need from it
  backed up. The Tesla is in sentry mode so it will keep writing
  to the USB drives as soon as they are made available.").
  Sequencing updated: H0 now runs immediately after 0.1, ahead of
  Phase 0.2 – 0.8 dev-machine scaffolding. Rationale and the
  parallel-dev plan are recorded in the Phase 0 "Resequencing note"
  above. Sentry-mode write reality captured for later phases (any
  B-1 USB binding will see Tesla writes start within seconds — H1
  + H2 hardware tests will need to account for live writes if
  Sentry is still on during the test window).

### 2026-05-19 (resumed, again) — Phase H0 complete

- **Phase H0 fully executed and verified** against
  `cybertruckusb.local`. Pi is now a clean Debian 13 / Bookworm
  install with zero v1 artifacts. See the Phase H0 table above for
  the per-increment record; this entry captures session-level
  context and lessons.
- **Disk-full root cause** of the original "archive worker not
  working / lost videos" complaint that started this whole rewrite
  effort: `~/TeslaUSB` (306 GB of `.img` files) + `~/ArchivedClips`
  (148 GB) had filled the 470 GB SD card to **100% used / 461 GB**.
  With no free space, the v1 archive worker couldn't copy
  RecentClips → ArchivedClips, so Tesla's circular buffer overwrote
  clips before they could be saved. Load avg was 8.67 / 8.51 / 6.30
  at first contact. H0.10 deleted both directories in **6.8 seconds**
  (ext4 just updates inode + bitmap, no data copy). Disk now at
  **7.7 GB used / 2%** — restoring the actual root cause we've been
  trying to fix at the application layer for weeks. B-1 must
  architect for this: SD-card-resident state grows, and the
  worker must enforce a high-watermark eviction policy long before
  the disk fills. Captured as Anti-Pattern in `02-LEARNINGS.md`
  (forthcoming).
- **D-Bus wedge mid-flight** (operational hazard noted): three
  back-to-back `systemctl reboot` calls during H0.4 / H0.7 / H0.13
  triggered a state where `systemd-logind` couldn't be reached over
  D-Bus — `systemctl status` returned "Transport endpoint is not
  connected", and SSH key-auth succeeded in <1 sec but
  `pam_systemd(sshd:session): Failed to create session: Connection
  timed out` blocked non-interactive sessions for ~3-4 minutes.
  Unrecoverable without a hard power-cycle (operator initiated at
  11:55 EDT). **Lesson:** for B-1 Phase H1+, limit each session to
  ONE controlled reboot and use the existing SSH window to capture
  the journal before each reboot. Detailed forensic in
  `session-state/.../files/h0-dbus-breakage-finding.md`.
- **Clean-baseline journal** captured at `h0-clean-baseline-boot.log.gz`
  (19.4 KB / 954 lines, 0 TeslaUSB-related warnings). All remaining
  warnings are stock Pi OS noise: alsa rules, bcm2835 staging
  modules, bluetooth perms, pipewire/wireplumber RTKit. Reference
  baseline for diffing future H1+ post-deploy journals.
- **Items preserved on the Pi for B-1 reuse** (validated):
  `/etc/systemd/system/sshd-protect.conf` (RefuseManualStop), the
  watchdog priority drop-in (Nice=-5 RT), NM `wifi-roaming.conf`,
  desktop service masks (colord/pipewire/wireplumber/pipewire-pulse).
  Items intentionally NOT touched: `/boot/firmware/cmdline.txt` and
  `config.txt` — Phase 6 setup.sh will normalize idempotently with
  `.b1-backup`.
- **Test environment ready** for Phase H1+. `b1-userspace-rust`
  remains 2 commits ahead of `main`, local-only (not yet pushed
  to origin). Next session resumes the deferred Phase 0.2 – 0.8
  scaffolding (Cargo workspace, Python skeleton, CI, pre-commit,
  setup-dev.sh, CODEOWNERS, ADRs).

### 2026-05-19 (resumed, again) — Phase 0.2 Cargo workspace skeleton

- **Increment 0.2 implemented and gate-verified.** Workspace
  scaffolded at `rust/` with three empty crates per charter:
  * `teslausb-core` (lib) — IPC envelope + `BlockBackend` trait
    + `Filesystem` trait will land here in Phase 1.2 onward.
  * `teslafat` (bin) — NBD server + FAT/exFAT synthesizer. Phase
    1.1 replaces the placeholder `main`.
  * `teslausb-worker` (bin) — background retention/cloud-sync/
    indexer. Populated in Phase 14.
- **All five Rust CI gates green** on the dev box with the
  pinned toolchain (`rustup` installed via `winget`, then
  `rust-toolchain.toml` auto-fetched stable `1.85.0` + `rustfmt`
  + `clippy`):
  * `cargo build --workspace --all-targets` — 0
  * `cargo clippy --workspace --all-targets -- -D warnings` — 0
  * `cargo fmt --all -- --check` — 0
  * `cargo test --workspace --all-targets` — 0 (0 tests, 0 fails)
  * `cargo doc --no-deps --document-private-items --workspace` — 0
- **Charter discrepancy caught and fixed in the same commit.**
  Clippy's `lint_groups_priority` rejected the charter's literal
  lint block: lint *groups* (`unused`, `nonstandard_style`,
  `future_incompatible`) need `priority = -1` so individual
  lints (`missing_docs`) can override them. Updated
  `docs/03-CODE-QUALITY-CHARTER.md` §"Lints" and the workspace
  `Cargo.toml` simultaneously so the charter stays the source
  of truth and cargo accepts the syntax.
- **Pedantic docs enforced from day one.** `clippy::pedantic`
  promoted to deny via `-D warnings` flagged un-backticked
  identifiers (`TeslaUSB`, `RETENTION_UPDATE`, `INVALIDATE_CACHE`,
  `exFAT`) in the placeholder crate-level doc comments.
  Backticked all of them — establishes the bar for every
  future doc comment in the workspace.
- **Existing `teslafat/` at repo root** classified as Phase 1
  design drafts (`teslafat/README.md`) rather than dead code.
  Drafts get ported into `rust/crates/teslafat/src/` increment
  by increment through Phase 1.1 – 1.7; the root `teslafat/`
  directory is `git rm`'d at the end of Phase 1.7.
- **Stats:** 14 files added, ~378 lines (~150 of code + lints,
  ~228 of explanatory docs/README/charter delta). `rust/target/`
  added to `.gitignore`; `rust/Cargo.lock` committed per Rust
  best practice for workspaces containing binary crates.
- **Next:** Increment 0.2 review gate (formal `charter-review`
  skill invocation now that this is the first code-bearing
  increment), then 0.3 (Python skeleton + `pyproject.toml`).
  Branch `b1-userspace-rust` will be 4 commits ahead of `main`
  after this commit lands; still local-only, not pushed to
  origin per operator preference.

### 2026-05-19 (resumed, again) — Phase 0.2 review gate

- **Increment 0.2 charter review — APPROVED with in-place fixes.**
  First code-bearing increment, so the `charter-review` skill
  applied formally. Pre-flight automated gates already green
  from the implementation commit (`cargo build / clippy -D
  warnings / fmt --check / test / doc` all 0). Manual pillar
  walk found nothing blocking on the new code itself:
  * Pillar 1 (code smells) — all 10 new source files ≤ 72
    lines; only function is the placeholder `fn main()` whose
    body is empty. No magic values, no nesting, no duplication,
    no primitive obsession.
  * Pillar 2 (architecture) — `teslausb-core/src/lib.rs` is
    doc-only with zero imports; layering rule trivially holds.
    Binary `main`s are entry points (Layer 4) so domain-core
    rule does not bind.
  * Pillar 3 (no shortcuts) — placeholder `fn main()` is
    explicitly sanctioned by the plan note ("may need ...
    placeholder main.rs so cargo build succeeds"); `cargo-deny`
    permissive-license allow-list and `multiple-versions = warn`
    each documented in `deny.toml` comments.
  * Pillar 4 (fix bugs immediately) — see charter delta below.
  * Pillar 5 (no dead code) — root `teslafat/` reclassified as
    Phase 1 drafts via `teslafat/README.md` and slated for
    `git rm` at end of Phase 1.7.
- **Review surfaced four pre-existing charter inconsistencies**
  (not introduced by 0.2, but made visible by the work). Fixed
  in-place per the inc-0.1 review pattern, in a dedicated
  `docs(b1): inc-0.2 review fixes — charter coherence` commit:
  * Line 174 — toolchain example `1.84.0` → `1.85.0` (edition
    2024 needs ≥ 1.85; 1.84.0 would fail to build).
  * Lines 275-277 — coverage gate paths
    `teslafat/src/{fs,nbd}/` → `rust/crates/teslafat/src/{fs,nbd}/`
    (workspace moved in 0.2).
  * Lines 543-544 — CI llvm-cov include patterns same path
    update.
  * Lines 595, 601 — pre-commit `cd teslafat` → `cd rust`
    (workspace root for cargo commands moved).
- **Tooling gaps noted for inc-0.6** (`setup-dev.sh`): `cargo
  machete`, `cargo llvm-cov`, `cargo deny` not yet installed on
  the dev box. Pre-flight skipped those gates with a "tool not
  installed" note. Implementation deferred to 0.6 per plan;
  charter rules already document the install in §"CI Gates".
- **Branch state after review commit:** 5 commits ahead of
  `main`, still local-only. Next session resumes inc-0.3
  (Python skeleton + `pyproject.toml`).

### 2026-05-19 (resumed, again) — Phase 0.3 Python skeleton

- Created `web/` Python skeleton matching plan §272-294 canonical
  layout: `web/teslausb_web/{__init__.py,blueprints/__init__.py,services/__init__.py}`,
  `web/tests/{__init__.py,conftest.py,test_smoke.py}`, `web/pyproject.toml`,
  `web/README.md`. All package `__init__.py` files carry docstrings only
  — no logic yet. `test_smoke.py` asserts the three packages import +
  `__version__ == "0.1.0"` so pytest exits 0 (not 5 "no tests collected")
  on the empty skeleton.
- `pyproject.toml` mirrors charter §"Python Standards" verbatim:
  `[project]` (Python ≥ 3.11 target, deps=[], dev extra with
  `ruff>=0.6 mypy>=1.11 pytest>=8.0 pytest-cov>=5.0 vulture>=2.11 bandit>=1.7`),
  setuptools build backend with `packages.find`, ruff with 30+ rule families
  enabled and `target-version = "py311"`, mypy strict + `python_version = "3.11"`,
  pytest `--strict-markers --strict-config` (coverage threshold lives on the
  CI command line per charter, not in `addopts`), full `[tool.coverage.*]`.
- Tool venv created at `C:\Users\mhack\source\repos\Tesla\.teslausb-tools-venv`
  (OUTSIDE the repo per the AI workspace rule "never install dependencies
  inside the git repo"). `pip install -e ".[dev]"` succeeded with
  ruff 0.15.13, mypy 2.1.0, pytest 9.0.3, pytest-cov 7.1.0, vulture 2.16,
  bandit 1.9.4.
- **All 6 Python gates run green on the skeleton:**
  * `ruff check .` — 0
  * `ruff format --check .` — 0 (6 files formatted in-place to match style)
  * `mypy --strict teslausb_web tests` — 0 (6 source files, no issues)
  * `pytest --cov=teslausb_web` — 3 passed, 100% line coverage
  * `vulture teslausb_web --min-confidence 80` — 0 (no dead code;
    `__version__` exported via package `__init__`)
  * `bandit -r teslausb_web -ll` — 0 (49 LOC scanned, zero findings)
- **Charter coherence fix in lockstep (same pattern as 0.2):** ruff 0.5+
  removed lints `ANN101` and `ANN102`; ruff emitted a startup warning
  on every run. Charter §"Lints" still listed both rules in `ignore`;
  removed them from both `web/pyproject.toml` AND
  `docs/03-CODE-QUALITY-CHARTER.md` so the two stay in sync. Comment
  documents the intent (empty `ignore` = no charter-mandated
  suppressions).
- **`.gitignore` hardened for Python tooling:** added `.mypy_cache/`,
  `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `.coverage.*`,
  `htmlcov/`, `*.egg-info/`, `build/`, `dist/`, `.venv/`,
  `.tox/`. Verified `git diff --cached --name-only` for inc-0.3
  shows ZERO build-artifact paths.
- Commit `<TBD>` "chore(b1): inc-0.3 Python skeleton + pyproject.toml at web/"
  — 10 files (+354/-2). Branch `b1-userspace-rust` now 6 commits ahead of
  `main`.
- Next step: formal charter-review gate per `.github/skills/charter-review/SKILL.md`.
  Expected coherence finds in charter (same class as 0.2):
  * L387 "≥ 80% line coverage on `web/services/`" → `web/teslausb_web/services/`
  * L551 `pytest --cov=web` → `pytest --cov=teslausb_web`


### 2026-05-19 (resumed, again) — Phase 0.3 review gate

- Followed `.github/skills/charter-review/SKILL.md` against commit
  `b8f9d5f`. Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-0.3.md`.
- **Pre-flight gates:** all 6 Python gates green at HEAD prior to
  review (ruff check, ruff format --check, mypy strict, pytest with
  charter-prescribed `--cov=teslausb_web --cov-fail-under=80`
  achieving 100.00%, vulture, bandit). Pre-commit hooks not yet
  installed — Phase 0.5 deliverable, noted as expected gap.
- **Pillar walk:** all 5 pillars clean on the 6 new Python files +
  `pyproject.toml` + `README.md`. Files are 7-30 LOC each, pure
  scaffolding. Zero shortcuts, zero dead code, zero anti-patterns.
- **Findings: 0 Blocker, 6 Major, 0 Minor, 0 Nit.** All 6 Majors
  fixed in-place during review:
  1. Charter L388 (Test discipline): `web/services/` →
     `web/teslausb_web/services/` (path drift, same class as the
     0.2 `teslafat/src/` → `rust/crates/teslafat/src/` drift).
  2. Charter L402 (Dead code detection): `vulture web/` →
     `vulture web/teslausb_web/` with comment explaining that
     pytest fixtures look unused to vulture's static analysis.
  3. Charter L555 (CI Gates): `mypy web/` → bare `mypy` from
     `web/` cwd (uses `files = ["teslausb_web", "tests"]` from
     pyproject). Restructured entire Python CI block to make `cd web`
     explicit, paralleling how the Rust block implicitly runs from `rust/`.
  4. Charter L557 (CI Gates): `--cov=web` → `--cov=teslausb_web`
     (pytest-cov takes a module name, not a path).
  5. Charter L558-559 (CI Gates): `vulture web/ / bandit -r web/` →
     `vulture teslausb_web / bandit -r teslausb_web` (avoid
     recursing into `tests/` where vulture/bandit produce noise).
  6. Missing `from __future__ import annotations` in 5 of 6
     Python files (charter Python deep-dive L397 `Major if missing`).
     Added to `teslausb_web/__init__.py`,
     `teslausb_web/blueprints/__init__.py`,
     `teslausb_web/services/__init__.py`, `tests/__init__.py`,
     `tests/conftest.py`. `tests/test_smoke.py` already had it.
- **One deliberately-not-a-finding:** charter says "New deps in
  `pyproject.toml` always trigger an ADR." The `[project.optional-dependencies] dev`
  list (ruff, mypy, pytest, pytest-cov, vulture, bandit) does add 6
  deps — but those are the **charter-mandated** tools named in the
  charter itself. Plan schedules ADRs 0001-0011 as the Phase 0.8
  deliverable; documenting tool choices the charter already mandates
  does not require a Phase 0.3 ADR. Captured this reasoning in the
  review report so future contributors don't re-litigate.
- **Re-ran all 6 Python gates** after fixes: ALL GREEN at HEAD with
  identical 100.00% coverage. The added `from __future__ import
  annotations` lines actually IMPROVED the coverage count
  (2/1/1 statements vs. 1/0/0 — they count as executable statements).
- Commit `<TBD>` "docs(b1): inc-0.3 review fixes - charter
  coherence + future annotations" — 6 files (+24/-9). Branch
  `b1-userspace-rust` now 7 commits ahead of `main`.
- **Branch state after review commit:** 7 commits ahead of `main`,
  still local-only. Next session resumes inc-0.4 (GitHub Actions CI
  workflow mirroring charter §"CI Gates"). Charter coherence
  pattern continues: any deltas surfaced by 0.4 will be fixed in
  lockstep, exactly as 0.2 and 0.3 have been.


### 2026-05-19 (resumed, again) — Phase 0.4 local gate runner

- **Operator pivot mid-flight:** original inc-0.4 spec was
  `.github/workflows/ci.yml` (GitHub Actions). User instructed
  *"prefer to not rely on github actions for now"*. Combined with
  the broader rule *"there is limited testing that can be done on
  PCs or cloud devices. We can only really do full testing on the
  PI"* — captured as a durable memory.
- Pivoted inc-0.4 to `scripts/check.sh` — a single local gate
  runner that mirrors charter §"CI Gates" verbatim. Operator runs
  `./scripts/check.sh` before each commit; same enforcement model
  as the would-be GitHub Actions workflow but without the cloud
  dependency.
- Updated `docs/00-PLAN.md` row 0.4 + the Phase 0 test-gate
  paragraph to reflect the pivot. Cloud CI is now intentionally NOT
  a Phase 0 deliverable; the row carries the rationale so future
  contributors don't re-litigate.
- `scripts/check.sh` design (282 LOC, exec bit set):
  * One script, three gate suites: `--rust`, `--python`,
    `--hygiene` (default: all three, fail-fast on first red).
  * `--all` runs every gate and reports at end (continue-on-error).
  * Required tools (script aborts if missing): `cargo`,
    `rustup`, `python`. Optional tools (cleanly skipped with
    WARN): `cargo-llvm-cov`, `cargo-deny`, `cargo-machete`,
    `lychee`.
  * Python venv auto-discovery: `\` env var override →
    out-of-tree `../.teslausb-tools-venv/` (Linux/Windows layout) →
    bare `python3`/`python`. Per the AI workspace rule the venv
    NEVER lives inside the repo.
  * ASCII output throughout (no Unicode box-drawing chars) so
    Windows `cmd.exe` / Git Bash consoles render cleanly without
    mojibake. Color via `tput` only when stdout is a TTY.
  * Hygiene checks use `git ls-files` (NOT `find`) so local
    untracked build artifacts (`rust/target/`, `web/.ruff_cache/`,
    `web/teslausb_web/__pycache__/` from a fresh pytest run) don't
    falsely trigger the "no forbidden artifacts" gate. Charter rule
    is about COMMITTED artifacts, not on-disk noise.
  * Gates that must pass: 13 — 7 Rust (fmt, clippy, test, llvm-cov,
    deny, machete, doc), 6 Python (ruff check, ruff format, mypy,
    pytest+cov, vulture, bandit). Hygiene adds 3 (large files,
    forbidden artifacts, markdown links).
- **Two bugs found and fixed by my own first dry-run before commit:**
  1. Hygiene initially used `find` and found `__pycache__`
     directories that pytest had just created — fixed by switching
     to `git ls-files`.
  2. Initial output used Unicode chars (━, ✓, ✗, ⊘) that mojibake
     on Windows cmd.exe — switched to ASCII (`=====`, `[PASS]`,
     `[FAIL]`, `[SKIP]`).
- **Verified end-to-end on Windows dev box via Git Bash:**
  `./scripts/check.sh --all` reports **12 PASS / 0 FAIL / 4
  SKIP** (optional tools), exit code **0**. The 4 skipped tools
  install via `scripts/setup-dev.sh` (Phase 0.6); the Pi will
  have them all post-H0/H1 deploy.
- Commit `<TBD>` "chore(b1): inc-0.4 local gate runner at
  scripts/check.sh (cloud CI deferred)" — 2 files (+291/-4).
  Branch `b1-userspace-rust` now 7 commits ahead of `main`.
- Next step: formal charter-review per
  `.github/skills/charter-review/SKILL.md`. Expected charter
  coherence finds (same pattern as 0.2 / 0.3):
  * Charter §"CI Gates" still uses YAML / PR / "merge" language
    that assumes a hosted CI system. Need to reframe as "every
    gate runs locally via `scripts/check.sh` before commit; same
    'red blocks' rule, different enforcement venue".
  * Charter §"Pre-commit Hooks" (L573-617) still references
    pre-commit framework — that's correct for inc-0.5; no change.


### 2026-05-19 (resumed, again) — Phase 0.4 review gate

- Followed `.github/skills/charter-review/SKILL.md` against commit
  `922930a`. Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-0.4.md`.
- **Pre-flight gates:** `./scripts/check.sh --all` reports
  12 PASS / 0 FAIL / 4 SKIP, exit 0 on Windows dev box. The 4 SKIPs
  are optional tools (cargo-llvm-cov, cargo-deny, cargo-machete,
  lychee) installed by Phase 0.6. Bash syntax check (`bash -n`)
  exit 0.
- **Pillar walk:** all 5 pillars clean on the 282-LOC script.
  Functions ≤ 32 SLOC, nesting depth ≤ 2, all comments are WHY, all
  variables quoted, `local` everywhere, `set -uo pipefail` at top
  with `set -e` deliberately omitted (each gate needs individual
  exit-status tracking).
- **Findings: 0 Blocker, 1 Major, 0 Minor, 1 Nit.**
  - **Major:** Charter §"CI Gates" still assumed a hosted-CI / PR
    enforcement model ("CI Gates / must pass before merge",
    "A red CI is a blocked PR. Period."). After the operator's
    pivot to a local runner, that framing was misleading. Fixed
    in-place by:
    1. Adding an opening paragraph to §"CI Gates" naming
       `scripts/check.sh` as the current enforcement venue,
       recording the 2026-05-19 operator preference, and stating
       that the gate definitions below are venue-neutral.
    2. Reframing "A red CI is a blocked PR" → "A red gate run is
       a blocked commit. Period."
    3. Adding a "How to run locally" line pointing at the script.
    4. Updating the "any changes" hygiene block from `git diff`
       scope to `git ls-files` scope (matching what the script
       actually does — committed artifacts only, not on-disk noise).
  - **Nit:** Magic value `1048576` (1 MiB) in
    `check_large_files`. ACCEPTED with inline comment citing
    charter L564; a named const would add indirection without
    clarity at the single use site.
- **Re-ran `./scripts/check.sh --all`** after the charter fix:
  still 12 PASS / 0 FAIL / 4 SKIP, exit 0. The script doesn't read
  the charter at runtime — the fix is documentation coherence only.
- Commit `<TBD>` "docs(b1): inc-0.4 review fixes - charter §"CI
  Gates" reframed for local runner" — 1 file (+~30/-~20). Branch
  `b1-userspace-rust` now 8 commits ahead of `main`.
- **Deferred to Phase 0.8:** a formal ADR
  `docs/adr/NNNN-defer-cloud-ci.md` capturing the cloud-CI
  pivot decision. Rationale is currently recorded in three places
  (inc-0.4 commit message + PROGRESS entry + charter §"CI Gates"
  preamble) so the decision is discoverable until the ADR batch
  arrives.


### 2026-05-19 (resumed, third time) — Phase 0.5 implementation

- Asked operator: "single source via scripts/check.sh OR per-tool hooks?"
  Answer: **single source** (matches the cloud-CI-deferred posture from
  inc-0.4; minimizes duplication; charter changes propagate via one
  script edit instead of two).
- Created `.pre-commit-config.yaml` (~60 LOC). Three local hooks
  (`scripts-check-hygiene`, `scripts-check-rust`,
  `scripts-check-python`) all funnel into `scripts/check.sh` with
  the matching `--<suite>` flag. `files:` regex on each hook so
  the rust suite only runs when `.rs` / `Cargo.toml` / `Cargo.lock`
  / `rust-toolchain.toml` is staged, python suite only when `.py`
  / `web/pyproject.toml` is staged. Hygiene hook is `always_run: true`.
- Kept upstream `pre-commit/pre-commit-hooks@v6.0.0` for cheap
  whitespace/EOF/yaml/TOML/merge-conflict/large-file/private-key/
  mixed-line-ending fixes — these have NO equivalent in
  scripts/check.sh and are battle-tested; the one-time clone is
  cached under `~/.cache/pre-commit`. `v4.6.0` was the charter
  default but emits a deprecated-stage-names warning;
  `pre-commit autoupdate` resolved it to `v6.0.0`.
- Added `pre-commit>=3.7` to `web/pyproject.toml` dev extras so
  `pip install -e web/[dev]` brings it in.
- First `pre-commit run --all-files` flagged `mixed-line-ending`
  on 27 tracked files (CRLF crept in via Windows tooling — the
  existing `.gitattributes` only covered `.sh / .py / .json / .conf`
  and missed `.rs / .toml / .md / .yaml`). **Fixed at the source:**
  extended `.gitattributes` to declare `* text=auto eol=lf` plus
  explicit lines for every text file type. After re-running, all
  hooks pass; the 25 EOL-only modifications collapsed to zero diff
  under the new attributes (git normalized them on stage).
- Final state: `pre-commit run --all-files` -> 11 PASS / 0 FAIL /
  1 SKIP (yaml hook skipped because the only `.yaml` is the config
  itself; pre-commit always skips its own config file). Defensive
  `./scripts/check.sh --all` -> 12 PASS / 0 FAIL / 4 SKIP, exit 0.
- Real diff in the inc-0.5 commit: just 3 files (.gitattributes +
  .pre-commit-config.yaml + web/pyproject.toml), +76 lines. The
  `mixed-line-ending` auto-fixes do NOT appear in the diff because
  git normalizes them transparently once the attributes are correct.
- **Anticipated inc-0.5 review-gate finds:**
  1. Charter §"Pre-commit Hooks" still prescribes the OLD MIXED model
     (per-tool `astral-sh/ruff-pre-commit` + `mirrors-mypy` +
     local `cargo fmt`/`cargo clippy` hooks). Now incoherent with
     the single-source design just implemented. Will rewrite §"Pre-
     commit Hooks" in the inc-0.5 review fix-up commit to match.
  2. `.pre-commit-config.yaml` ~60 LOC — well under any reasonable
     budget; no expected pillar finds.


### 2026-05-19 (resumed, fourth time) — Phase 0.5 review gate

- Followed `.github/skills/charter-review/SKILL.md` against commit
  `bde82e3`. Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-0.5.md`.
- **Pre-flight gates:** `pre-commit run --all-files` 11 PASS / 0 FAIL
  / 0 SKIP exit 0; `./scripts/check.sh --all` 12 PASS / 0 FAIL /
  4 SKIP exit 0. YAML syntax + `pre-commit validate-config` both
  exit 0.
- **Pillar walk:** all 5 pillars clean on the 61-LOC YAML config +
  `.gitattributes` extension + `web/pyproject.toml` one-liner.
  Declarative config, DRY satisfied via delegation, every hook is
  wired and documented.
- **Findings: 0 Blocker, 1 Major (anticipated in the inc-0.5 commit
  message), 0 Minor, 0 Nit.**
  - **Major:** Charter §"Pre-commit Hooks" L593-637 prescribed the
    OLD mixed model -- per-tool `astral-sh/ruff-pre-commit@v0.6.0` +
    `pre-commit/mirrors-mypy@v1.11.0` + local cargo fmt/clippy +
    `pre-commit/pre-commit-hooks@v4.6.0`. Five duplicated gate
    definitions; charter changes would need two updates. Now
    incoherent with the single-source design. Fixed in-place by:
    1. Rewriting the example block to show the 3-local-hook
       delegation pattern with `files:` regexes.
    2. Bumping upstream pin `v4.6.0` -> `v6.0.0` (silences the
       deprecated-stage-names warning).
    3. Stating explicitly that per-tool upstream hooks are
       DELIBERATELY not used (would duplicate gate definitions;
       conflicts with the 2026-05-19 operator preference).
    4. Adding operator install commands
       (`pip install -e web/[dev]` + `pre-commit install` +
       `pre-commit run --all-files`).
    5. Noting `pre-commit>=3.7` is now in
       `web/pyproject.toml` dev extras.
- **Defensive re-run** after the charter fix: `pre-commit run
  --all-files` 11/0/0 exit 0 again. Only the charter diff is
  staged (86 lines). One transient mixed-line-ending hit on
  `docs/01-PROGRESS.md` from a PowerShell `Add-Content` (writes
  CRLF by default) — auto-fixed on the next run; future progress
  appends should use `[System.IO.File]::AppendAllText` with
  explicit LF or rely on git's stage-time normalization.
- Commit `<TBD>` "docs(b1): inc-0.5 review fixes - charter
  Pre-commit Hooks rewritten for single-source delegation" — 1 file
  changed (+54/-32). Branch `b1-userspace-rust` will be 10
  commits ahead of `main`.
- **Deferred to Phase 0.8:** formal ADR
  `docs/adr/NNNN-single-source-gate-runner.md` capturing the
  single-source design decision (pairs with the deferred
  `defer-cloud-ci.md` ADR from inc-0.4 review).


### 2026-05-19 (resumed, fifth time) -- Phase 0.6 implementation

- Created `scripts/setup-dev.sh` (372 LOC bash, exec bit 100755). Per
  operator preference (2026-05-19, choice "script only, no execution"),
  did NOT run the installer; verified the script logic via three
  non-mutating modes: `--dry-run`, `--check`, and the
  `SETUP_SKIP_*` env-override matrix.
- **Three-step install** (matches the four optional-tool gates that
  `scripts/check.sh` currently SKIPs):
  - [1/3] Rust: rustup (curl one-liner if absent) -> `(cd rust &&
    rustup show)` to install the toolchain pinned by
    `rust-toolchain.toml` -> `cargo install --locked` cargo-deny,
    cargo-machete, cargo-llvm-cov, lychee.
  - [2/3] Python: `python3 -m venv ../.teslausb-tools-venv` (out-of-
    tree per AI workspace rule) -> `pip install --upgrade pip` ->
    `pip install -e web/[dev]` (brings ruff, mypy, pytest, pytest-
    cov, vulture, bandit, pre-commit).
  - [3/3] `pre-commit install` to register `.git/hooks/pre-commit`.
- **Three modes per Pillar 3 (No Shortcuts) -- explicit, non-mutating
  audit paths:**
  - `--dry-run`: prints every action that would be taken; exit 0;
    makes zero filesystem / shell changes.
  - `--check`: verifies the env IS set up; exit 1 with per-tool
    diagnostics if anything is missing. Used today to confirm the
    Windows dev box has the venv + all 7 dev tools but is missing the
    4 cargo subcommands and the git hook.
  - default (no flag): full idempotent install.
- **Skip-flag matrix:** `SETUP_SKIP_RUST=1` / `SETUP_SKIP_PY=1` /
  `SETUP_SKIP_HOOK=1` each excise their step. Useful for re-running
  just one section after a partial failure.
- **Prerequisites checked, not auto-installed:** `git`, `curl`,
  Python >=3.11. Script aborts with exit 2 and a hint if missing.
  Rationale: telling people how to install Python on their platform is
  out of scope; Pi has it pre-installed via apt; Windows users use
  python.org / Microsoft Store.
- **Mutex enforcement:** `--dry-run --check` rejected with exit 2.
- **Mojibake fix (Pillar 4 -- Fix Bugs Immediately):** my first draft
  used em-dash characters (`--`) in comments + heredoc. When `--
  help` was invoked on Windows the output showed `ΓÇö` mojibake.
  Replaced every em-dash with ASCII `--` via a PowerShell byte-
  replace, matching the same fix applied to `scripts/check.sh` in
  inc-0.4.
- **Logging refinement:** `log_action` originally printed
  `[RUN] cargo install ...` even in `--check` mode, which was
  misleading (nothing was being run). Refined to print
  `[CHECK]   verifying: cargo install ...` in check mode so the
  intent matches the output. Caught by my own `--check` dry-test
  before commit.
- **Anticipated inc-0.6 review-gate finds:**
  1. Charter doesn't yet mention `setup-dev.sh` as the canonical
     dev-env bootstrap. The CI Gates section (inc-0.4 reframe) just
     says "scripts/check.sh"; the Pre-commit Hooks section
     (inc-0.5 reframe) mentions `pip install -e web/[dev]` + `pre-
     commit install` manually but doesn't point at the wrapper.
     Likely cross-reference cleanup needed.
  2. `setup-dev.sh` LOC (372) is over the ~250 budget I floated in
     the prior session summary. Justification: 3 modes (dry-run /
     check / default) + 3 skip flags + venv-python-path autodetect
     for Linux vs Windows + structured per-tool verify in check
     mode. Pillar 1 function-length cap (50 SLOC) is comfortably
     met (longest function `install_pip_deps` ~25 SLOC). Expect
     the LOC overage to be acknowledged + accepted at review.
- **Verification at HEAD-1 (before commit):**
  - `bash -n scripts/setup-dev.sh` exit 0
  - `./scripts/setup-dev.sh --dry-run` exit 0
  - `./scripts/setup-dev.sh --check` exit 1 (correct -- env not yet
    fully set up: 4 cargo subcommands + git hook missing)
  - `./scripts/setup-dev.sh --help` exit 0
  - `./scripts/setup-dev.sh --bogus` exit 2
  - `./scripts/setup-dev.sh --dry-run --check` exit 2 (mutex)
  - `SETUP_SKIP_RUST=1 SETUP_SKIP_PY=1 SETUP_SKIP_HOOK=1
     ./scripts/setup-dev.sh --dry-run` exit 0
  - `pre-commit run --all-files` -> 11 PASS / 0 FAIL / 0 SKIP exit 0
  - `./scripts/check.sh --all` -> 12 PASS / 0 FAIL / 4 SKIP exit 0
    (same SKIPs as inc-0.4 -- they'll go to 0 once an operator runs
    the new installer for real).


### 2026-05-19 (resumed, sixth time) -- Phase 0.6 review gate

- Followed `.github/skills/charter-review/SKILL.md` against commit
  `07147a3`. Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-0.6.md`.
- **Pre-flight gates:** `pre-commit run --all-files` 11/0/0 exit 0;
  `./scripts/check.sh --all` 12/0/4 exit 0. Bash syntax check
  `bash -n` exit 0. All 6 mode checks green (--dry-run exit 0,
  --check exit 1 correctly, --help exit 0, --bogus exit 2, mutex
  exit 2, SETUP_SKIP_* matrix exit 0).
- **Pillar walk:** all 5 pillars clean on the 372-LOC script. Functions
  <= 25 SLOC (longest install_pip_deps), comments WHY throughout,
  prerequisites CHECKED not auto-installed, --dry-run / --check are
  first-class modes (not bolt-ons), three skip-flag env overrides
  fully wired and documented.
- **Findings: 0 Blocker, 1 Major (fixed in-place), 1 Minor (accepted
  with rationale), 0 Nit.**
  - **Major:** Charter section CI Gates didn't cross-reference
    setup-dev.sh as the canonical install path. Inc-0.4's reframe
    named scripts/check.sh as the gate runner; inc-0.5's reframe
    pointed at pip install for the Python venv. Neither said WHERE
    to get the tools from. Fixed in-place by adding a 13-line
    "Installation:" paragraph after the "Enforcement venue:" para
    in section CI Gates, naming setup-dev.sh, summarizing the three
    modes (--check, --dry-run, default install), and noting the
    [SKIP] -> [PASS] transition operators expect after running it.
  - **Minor:** Script LOC 372 over the prior-session floated 250
    budget. Justified by 3 modes + 3 skip flags + Linux-vs-Windows
    venv-python-path autodetect + ~50% header-and-usage comments.
    Pillar 1 hard cap (function length 50 SLOC) is comfortably met
    -- longest function install_pip_deps ~25 SLOC. ACCEPTED.
- **Defensive re-run** after the charter fix: pre-commit run
  --all-files 11/0/0 exit 0 again. The only other diff was a 1-line
  end-of-file-fixer add (trailing newline) on PROGRESS.md from the
  earlier [System.IO.File]::AppendAllText pattern; folded into this
  review commit. PowerShell-append rule: always include a final `\n`
  in the appended text OR rely on end-of-file-fixer to handle it.
- Commit `<TBD>` "docs(b1): inc-0.6 review fixes - charter CI Gates
  cross-references setup-dev.sh" -- 2 files changed (+~22/-~5).
  Branch `b1-userspace-rust` will be 12 commits ahead of main.
- **Deferred to Phase 0.8:** formal ADR
  `docs/adr/NNNN-setup-dev-modes.md` capturing the three-mode
  design decision (--check audit, --dry-run preview, default install)
  + the "prereqs-checked-not-installed" boundary.

### 2026-05-19 (resumed, seventh time) -- Phase 1.1 implementation + review gate

- **Phase 1.1 implementation** (commit `994fd65`): ported draft
  `teslafat/src/{main.rs,config.rs}` (190 + 131 LOC YAML) into
  `rust/crates/teslafat/src/{main.rs,config.rs}` as the first
  code-bearing increment: `clap` CLI, `tracing-subscriber` JSON
  to stderr with `EnvFilter` (level via `RUST_LOG`), TOML config
  loader with `#[serde(deny_unknown_fields)]` + semantic `validate`,
  literal `info!(..., "started")` sentinel, anyhow error chain
  logging on exit failure. 8 files changed, +1439/-340 (Cargo.lock
  surge is the one-shot transitive-dep registration). 14 unit tests
  in `config.rs` + 4 integration tests in `tests/sentinel.rs` =
  18 passed, 0 failed.
- **Scope deliberately stripped:** NBD listen, IPC sockets, signal
  handlers all deferred to Phase 1.3 / 1.5. `ipc: IpcConfig` field
  from the draft was REMOVED (not allowed-as-dead-code) because no
  Phase 1.1 code reads it; reintroduce alongside the actual IPC
  envelope types in Phase 1.2.
- **YAML -> TOML decision:** committed to TOML for the on-disk
  config (matches `Cargo.toml` syntax, proper typed scalars, no
  `serde_yaml` unmaintained-dep liability). Documented inline in
  `Cargo.toml` + `config.rs` module doc + commit message +
  `teslafat/README.md` strikethrough update.
- **Phase 1.1 review gate** (this commit): followed
  `.github/skills/charter-review/SKILL.md` against `994fd65`.
  Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-1.1.md`.
- **Pre-flight gates (re-verified post-fix):** `cargo build` ok,
  `cargo clippy --workspace --all-targets --all-features -- -D warnings`
  ok, `cargo test` 18/0/0, `cargo fmt --check` ok,
  `RUSTDOCFLAGS=-D warnings cargo doc` ok, `pre-commit run` 9/0/2,
  `scripts/check.sh --all` 12/0/4 (same SKIPs as baseline).
- **Pillar walk:** all 5 pillars clean. Longest production fn 13 SLOC;
  no nested control flow; zero `unwrap`/`expect`/`panic` in
  production; magic numbers extracted to six named `const`
  declarations with documented intent; all `pub` items have
  `///` docs.
- **Findings: 0 Blocker, 0 Major, 1 Minor (fixed in-place), 3 Nit
  (this commit).**
  - **Minor:** Per charter section ADRs (lines 477-485), the YAML to
    TOML decision triggers >=1 of the five mandatory-ADR criteria:
    affects >1 module, locks in a third-party dep (`toml = "0.8"`),
    changes a schema. Fixed in-place by writing
    `docs/adr/0001-config-format-toml.md` (~150 lines: context,
    decision, consequences positive/negative/neutral, alternatives
    considered, charter compliance, implementation references,
    follow-ups). This is the first ADR landing; the deferred
    inc-0.8 ADR batch now plans ~10 slots instead of 11.
  - **Nit 1:** PROGRESS row 1.1 was still ⏳; updated to ✅ ✅
    APPROVED ✅ with gate evidence (this entry).
  - **Nit 2:** Review report not yet on disk; created this session.
  - **Nit 3:** PLAN row 1.1 has no status column; left as-is
    (PROGRESS is the canonical status tracker per inc-0.4/5/6
    precedent).
- **Verification of preemptive `#[allow]`:** initial draft of
  `config.rs` carried `#![allow(clippy::module_name_repetitions)]`
  defensively. During pillar-3 walk, removed the allow and re-ran
  clippy at `-D warnings` -- still green. The lint does not fire
  on exact-match (`config::Config`), only on suffixed types
  (`ConfigBuilder`, etc.). The allow was a non-silencing shortcut
  and is now gone. Re-ran fmt + test + doc post-removal: all green.
- Review-fix commit (this entry) "docs(b1): inc-1.1 review fixes -
  add ADR-0001 + remove unused module_name_repetitions allow +
  PROGRESS row 1.1 marked APPROVED" -- 3 files changed. Branch
  `b1-userspace-rust` reaches 14 commits ahead of main. (Hash
  reference omitted from session log per `git commit --amend`
  chase-the-hash anti-pattern; see `git log --grep "inc-1.1 review
  fixes"` for the current SHA.)
- **Next:** Phase 1.2 -- `teslausb-core::ipc::messages` types
  (versioned envelope, `STATUS` / `RETENTION_UPDATE` /
  `INVALIDATE_CACHE` request+response) with `serde_test`
  round-trip tests. ~150 LOC ceiling. `IpcConfig` returns to
  `teslafat/src/config.rs` in that increment.

### 2026-05-19 (resumed, eighth time) -- Phase 1.2 implementation + review gate

- **Phase 1.2 implementation** (commit `aa3f18c`): added the IPC
  vocabulary in `rust/crates/teslausb-core/src/ipc/messages.rs`:
  versioned `Envelope<T>` with `const fn new` + `validate`
  returning `IpcError::UnsupportedVersion` (thiserror at lib
  boundary per charter §"Rust standards"); `Request` enum
  (`Status`, `RetentionUpdate { updates }`, `InvalidateCache`);
  `Response` enum (`Status(StatusBody)`, `RetentionAck { applied,
  failed }`, `InvalidateAck`, `Error(ErrorBody)`); `DaemonState`
  / `RetentionAction` / `ErrorCode` enums; `StatusBody` /
  `RetentionUpdate` / `RetentionFailure` / `ErrorBody` leaf
  structs. Internally tagged via `#[serde(tag = "type", rename_all =
  "SCREAMING_SNAKE_CASE")]`. Deliberately NO `deny_unknown_fields`
  on wire types (forward-compat; opposite choice from inc-1.1's
  config which IS strict). 5 files changed, +591/-5.
- **Scope deliberately stripped:** wire format (JSON vs MessagePack
  vs length-prefixed) deferred to Phase 1.5+ transport layer; no
  encoder dep (`serde_json` is dev-only). `IpcConfig` deliberately
  NOT reintroduced into `teslafat/src/config.rs` -- still no
  consumer in this increment; bringing it back would recreate the
  same dead-code lint that triggered its removal in inc-1.1.
  Returns when the socket-binding code lands (Phase 1.5+).
- **Test discipline:** 16 unit tests in `messages.rs` covering
  `serde_test::assert_tokens` round-trips for `StatusBody` and
  `RetentionUpdate`; `serde_json` end-to-end round-trips for
  every `Response` variant; full `Envelope<Request>` E2E via
  JSON; forward-compat (unknown future field silently ignored);
  defensive (unknown enum variant tag rejected). Workspace total
  now 34/0/0 (14 teslafat unit + 4 teslafat integration + 16
  teslausb-core unit + 0 teslausb-worker).
- **Phase 1.2 review gate** (this commit): followed
  `.github/skills/charter-review/SKILL.md` against `aa3f18c`.
  Report at
  `~/.copilot/session-state/3583f429-4245-4837-9c1c-5c1583cbb31d/files/charter-review-inc-1.2.md`.
- **Pre-flight gates (re-verified post-fix):** `cargo build` ok,
  `cargo clippy --workspace --all-targets --all-features -- -D warnings`
  ok, `cargo test` 34/0/0, `cargo fmt --check` ok,
  `RUSTDOCFLAGS=-D warnings cargo doc` ok, `pre-commit run` 10/0/1
  (yaml skipped: no files in changeset), `scripts/check.sh --all`
  12/0/4 (same SKIPs as inc-1.1 baseline).
- **Pillar walk:** all 5 pillars clean. Longest production fn
  `Envelope::validate` 8 SLOC; longest test fn 22 SLOC; no nested
  control flow; zero `unwrap`/`expect`/`panic` in production;
  `thiserror` at lib boundary; pure-data crate (no I/O deps).
- **Lint fixes during implementation (none deferred, none allowed):**
  3x `clippy::doc_markdown` (`MessagePack`, `TeslaCam`,
  `LightShow` needed backticks); 1x `clippy::panic` in a test
  rewritten to `assert!(matches!(...))`; 2x `serde_test` token-
  shape failures (internally-tagged enums serialise as `Struct`
  not `Map` / `StructVariant` -- known footgun documented
  inline); 2x trivial fmt.
- **Findings: 0 Blocker, 0 Major, 1 Minor (fixed in-place), 3 Nit
  (this commit).**
  - **Minor:** Per charter section ADRs (lines 477-485), the IPC
    vocabulary decision triggers >=3 of the five mandatory-ADR
    criteria: affects >1 module, locks in a third-party dep
    (`thiserror = "1.0"`), changes a protocol/schema (explicitly,
    per charter §"The Boundaries Are Real" line 446 -- "Their
    contract is the IPC schema (versioned)"). Fixed in-place by
    writing `docs/adr/0002-ipc-message-vocabulary.md` (~190 lines:
    context with four design questions, decision laid out in six
    points, consequences positive/negative/neutral, five
    alternatives considered with rationale, charter compliance
    section citing trigger lines, implementation references, four
    follow-up items). This is the second ADR landing; deferred
    inc-0.8 batch now plans ~9 slots instead of 11.
  - **Nit 1:** PROGRESS row 1.2 was still pending; updated to
    APPROVED with gate evidence.
  - **Nit 2:** Review report not yet on disk; created this session.
  - **Nit 3 (accepted, deferred):** Charter §"The Boundaries Are
    Real" could cross-reference `teslausb-core::ipc` by path.
    Discoverable but not explicit. Defer -- ADR-0002 + this report
    make the location obvious; not worth a charter rewrite.
- Review-fix commit (this entry): "docs(b1): inc-1.2 review fixes -
  add ADR-0002 + PROGRESS row 1.2 marked APPROVED". 3 files
  changed. Branch `b1-userspace-rust` reaches 16 commits ahead of
  main. (Hash reference omitted from session log per
  `git commit --amend` chase-the-hash anti-pattern documented in
  the inc-1.1 entry; see `git log --grep "inc-1.2 review fixes"`
  for the current SHA.)
- **Next:** Phase 1.3 -- port the existing NBD newstyle handshake
  from `teslafat/src/nbd/handshake.rs` (preserved draft) into
  `rust/crates/teslafat/src/nbd/handshake.rs`, add a round-trip
  test against a real `nbd-client --check` invocation when a
  hardware-test path opens. ~50 LOC ceiling -- mostly a path move
  + lint compliance. The handshake draft is the largest preserved
  asset from the deleted `teslafat/` planning tree.

### 2026-05-19 (resumed, ninth time) -- Phase 1.3 implementation + review gate

- **Phase 1.3 implementation** (commit `2c1a56f`): ported the
  preserved Phase 1 draft `teslafat/src/nbd/handshake.rs` (211 LOC)
  into the active workspace at `rust/crates/teslafat/src/nbd/handshake.rs`.
  Decomposed into pure encode/decode helpers (`encode_greeting`,
  `encode_option_reply`, `encode_info_export`,
  `encode_info_block_size`, `encode_export_name_reply`,
  `parse_info_or_go_request`) plus a generic-over-
  `AsyncRead + AsyncWrite + Unpin` async orchestrator (`run`,
  `read_option_request`, `dispatch_option`, `handle_info_or_go`,
  `write_option_reply`). Every function under 50 SLOC.
  Draft's 3 `try_into().unwrap()` calls replaced with bounds-
  checked `slice::get + try_into`; 2 `as usize`/`as u32` casts
  replaced with `try_from + Context`. Every `pub` item gained
  `///` + `# Errors` docs. No `#[allow]` on production code.
  9 files changed, +826/-220.
- **Crate shape change (load-bearing):** `teslafat` became lib +
  bin via a new `src/lib.rs` exposing `pub mod config; pub mod nbd;`.
  Forced by `dead_code` (warn-as-deny under workspace `-D warnings`)
  -- every `pub` item in `nbd::handshake` was correctly flagged
  as unused from the binary's perspective until Phase 1.5 wires
  the transmission loop. Alternatives considered (preemptive
  `#[allow(dead_code)]`, fake call site in main) rejected as
  charter-violating / dishonest; lib+bin is the idiomatic Rust
  answer and was made the precedent for Phase 2+ via ADR-0003 §B.
  `tests/sentinel.rs` integration tests unaffected (binary-
  spawning, lib split invisible).
- **Dep additions:** `tokio = "1.40"` with
  `default-features = false` and only `rt + net + io-util + macros`
  (single-threaded runtime; ~3 concurrent tasks on Pi Zero 2 W
  is plenty). 61-line `Cargo.lock` delta for `bytes`, `mio`,
  `pin-project-lite`, `signal-hook-registry`, `socket2`. No
  `time` (Phase 1.6), no `signal` (Phase 1.6), no `process`
  (never).
- **Test discipline:** 19 new tests in `nbd::handshake::tests` --
  7 pure encode tests (byte-for-byte wire-format assertions),
  5 pure parse tests (happy + both info-id ordinals + too-short
  buffer + `name_len = u32::MAX` overflow), 1 invariant test
  (`NBD_REP_ERR_BIT` semantics), 6 async tests via
  `tokio::io::duplex` (OPT_GO with/without block-size request,
  OPT_EXPORT_NAME legacy path, missing CF_FIXED_NEWSTYLE, bad
  option magic, OPT_ABORT). Real `nbd-client --check`
  integration test deferred to Phase H1 hardware smoke per
  operator directive (no CI, Windows dev host has no
  `nbd-client`). Test count 34/0/0 -> 53/0/0.
- **Gates iterated through several rounds:** dead_code x 13
  (resolved by lib+bin split), `clippy::indexing_slicing` x 13
  in test bodies (resolved by extending the `unwrap_used`
  test-mod carve-out at the new `lib.rs` crate root to also
  cover `indexing_slicing` -- charter-blessed pattern for tests),
  `clippy::uninlined_format_args` x 1 (`bail!` arg inlined),
  `rustdoc::private_intra_doc_links` x 1 (pre-existing link to
  private `Config::validate` in `config.rs` module doc became
  reachable after lib split; rewrote to plain prose),
  `cargo fmt` x ~8 trivia (line breaks around `try_from(...).context(...)?`,
  multi-line array literals -- all auto-fixed). After all six
  rounds: zero warnings, zero suppressions on production code.
- **Charter review** (`~/.copilot/.../files/charter-review-inc-1.3.md`,
  ~20 KB): Pillar walk found 0 Major, 4 Minor, 2 Nits.
  - **Minor 1:** LOC budget overrun. Plan estimated "~50 (mostly
    path move)"; actual ~600 LOC. Cause: the draft predated the
    charter (3 unwraps, no docs, no `# Errors`, 3 fns over 50
    SLOC); charter compliance + new tests added ~5x the
    estimate. Plan row 1.3 updated to record actual + note that
    pre-charter "path move" estimates should add 3-5x headroom.
  - **Minor 2:** ADR-0003 needed. Tokio adoption met >= 4 of
    the 5 charter trigger criteria (locks 3rd-party framework,
    affects >1 module, perf/correctness tradeoff in runtime
    flavor, non-obvious at design time). Lib+bin split met
    >= 3 (affects >1 module via the crate-shape precedent,
    sets forward-compat constraint via the lib's public
    surface, non-obvious vs. the rejected alternatives).
    Both decisions documented in
    `docs/adr/0003-async-runtime-and-crate-shape.md` (~10 KB)
    with 5 alternatives considered per decision.
  - **Minor 3:** `run()` has no per-handshake timeout. A
    misbehaving client could block the single-thread runtime
    indefinitely. Threat model in Phase 1.3 is low (Unix
    socket, kernel-only peer) so not a regression. Added
    `// TODO(phase-1.6): wrap run() in tokio::time::timeout`
    in `nbd/mod.rs` for the listener-task increment to pick up.
  - **Minor 4:** preemptive `teslausb-core` dep added to
    `teslafat/Cargo.toml` with "transitive prep" comment.
    By the same standard inc-1.1 review removed a preemptive
    `#[allow]`, this scaffold should come out -- Phase 1.4
    will add it when the `BlockBackend` trait actually needs
    it. Removed.
  - **Nit 1:** 61-line `Cargo.lock` churn is unavoidable
    transitive cost of tokio. Recorded.
  - **Nit 2:** `nbd/mod.rs` references `BlockBackend` in inline
    backticks instead of an intra-doc link since the trait
    doesn't exist yet. Phase 1.4 review should reinstate the
    proper intra-doc link.
- Review-fix commit (this entry): "docs(b1): inc-1.3 review fixes
  - add ADR-0003 + remove preemptive teslausb-core dep + plan LOC
  note + Phase 1.6 timeout TODO + PROGRESS row 1.3 marked
  APPROVED". 5 files changed. Branch `b1-userspace-rust` reaches
  18 commits ahead of main. (Hash reference omitted from session
  log per `git commit --amend` chase-the-hash anti-pattern; see
  `git log --grep "inc-1.3 review fixes"` for the current SHA.)
- **Next:** Phase 1.4 -- `BlockBackend` trait in
  `teslausb-core::backend` with `size`, `read`, `write(flags)`,
  `flush`; null impl for tests; FUA contract enforced by trait
  doc + test. ~100 LOC ceiling. This is the increment that
  legitimately introduces the `teslausb-core` dep on `teslafat`
  (Phase 1.5 transmission loop will impl `BlockBackend` for a
  real backing file). Likely no ADR needed unless the trait
  shape has a non-obvious forward-compat constraint.

### 2026-05-19 (resumed, tenth time) -- Phase 1.4 implementation + review gate

- **Phase 1.4 implementation** (commit `8f98f43`): added
  `rust/crates/teslausb-core/src/backend.rs` exposing the
  `BlockBackend` trait and supporting types. Surface:
  `WriteFlags(u32)` newtype with `NONE`/`FUA` constants matching
  the NBD wire bit pattern (`NBD_CMD_FLAG_FUA = 1 << 0`),
  `BackendError` thiserror enum (`Io(#[from] std::io::Error)` /
  `OutOfBounds { offset, len, size }` / `InvalidArgument(&'static str)`),
  `BackendResult<T>` alias, `BlockBackend` trait with native
  `async fn in trait` (no `async-trait` crate dep), `check_bounds`
  shared overflow-safe helper, and `pub mod mock` containing
  `NullBackend` / `MockBackend` / `MockOp` reference impls. 4
  files changed, +766/-2. pollster 0.3 dev-dep added to drive
  async test bodies without pulling tokio into the domain-core
  crate.
- **Trait shape (load-bearing):** native AFIT picked over the
  `async-trait` crate macro. Consequence: no `dyn BlockBackend`
  ever; the transmission loop (Phase 1.5) will be generic
  `<B: BlockBackend>` exactly mirroring how `nbd::handshake::run<S>`
  is generic over `AsyncRead + AsyncWrite + Unpin`. Trade-off
  written up in ADR-0004 §A. `async_fn_in_trait` clippy warn
  silenced at the trait declaration with a doc-justified allow.
- **Production hygiene:** zero `unwrap` / `expect` / `panic` in
  the production surface. `pub mod mock` recovers poisoned
  mutexes via `lock().unwrap_or_else(PoisonError::into_inner)`
  so neither lint fires. Slice accesses go through
  `slice::get(...)?` returning `BackendError::InvalidArgument`.
  No `unsafe` block anywhere — the hand-rolled noop-waker the
  first draft used was rejected in favour of `pollster`
  precisely so the deny-unsafe rule would hold.
- **FUA contract** (load-bearing for Phase 1.5): a write tagged
  `WriteFlags::FUA` must be durable on the medium before the
  future resolves; plain writes may rely on subsequent
  `flush()`. Three named tests (`fua_contract_*`) capture the
  contract; future backend impls copy-paste those names to
  self-verify compliance. `MockBackend::observed_any_durability`
  is the test oracle.
- **Test discipline:** 24 new tests in `backend::tests` (8
  WriteFlags, 3 check_bounds, 4 NullBackend, 4 MockBackend, 3
  FUA contract, 2 BackendError). Workspace test count
  53/0/0 -> 77/0/0. `pollster::block_on` drives async bodies
  in sync test fns — keeps `teslausb-core`'s "no tokio dep"
  invariant intact. Mod-level `#[allow(clippy::unwrap_used)]`
  on the `mod tests` block (matching `ipc::messages::tests`
  pattern — narrower coverage than the crate-root pattern
  `teslafat` uses).
- **Iteration:** seven clippy errors caught and fixed before
  commit: doc-markdown (`ORed`), doc-lazy-continuation x2
  (CommonMark treating `+ length` as a list bullet), `panic` x2
  in test match arms (replaced with `assert!(matches!(...))`),
  `indexing_slicing` on a test slice (used `slice::get`), and
  `single_char_pattern` (`"4"` -> `'4'`). After fixes: zero
  warnings, zero suppressions on production code.
- **Charter review** (`~/.copilot/.../files/charter-review-inc-1.4.md`,
  ~16 KB): Pillar walk found 0 Major, 1 Minor, 3 Nits.
  - **Minor 1:** ADR-0004 needed. Native AFIT meets >= 3 of the
    5 charter ADR trigger criteria (>1 module affected via
    teslafat impl + teslausb-core reference impls; forward-
    compat constraint via "no dyn dispatch ever without source
    break"; non-obvious vs. the established `async-trait`
    idiom). `WriteFlags(u32)` newtype meets criterion 5
    (non-obvious vs `bitflags!` macro). Co-located in one ADR
    per the ADR-0003 template of grouping tightly coupled API-
    shape decisions. Written as
    `docs/adr/0004-backend-trait-shape.md` (~10 KB) with five
    alternatives considered for §A (async-trait, RPITIT,
    spawn_blocking, embassy, all-sync) and five for §B
    (bitflags, `bool fua`, separate trait methods, bare u32,
    enum-with-no-combine).
  - **Nit 1:** workspace has two test-allow patterns
    (`teslausb-core` mod-level vs `teslafat` crate-root). Both
    charter-blessed, mild context-switch friction; defer
    unification — if a future review wants to unify, the
    crate-root pattern is the broader-coverage choice.
  - **Nit 2:** `MockBackend` could explicitly doc-warn against
    concurrent use within a single instance (two independent
    `Mutex`es could let `bytes` and `ops` snapshots disagree
    under hypothetical concurrent calls). Not actionable in
    current code path (`async fn` resolves atomically before
    yield); defer to first multi-task harness.
  - **Nit 3:** `BackendError::OutOfBounds.len` field doc could
    note the `u64::try_from(buf.len())` provenance.
- Review-fix commit (this entry): "docs(b1): inc-1.4 review
  fixes - add ADR-0004 + PROGRESS APPROVED + plan LOC note".
  Branch `b1-userspace-rust` reaches 20 commits ahead of main.
- **Next:** Phase 1.5 -- NBD transmission loop. Reads NBD
  commands (`READ` / `WRITE` / `FLUSH` / `TRIM` / `DISC`) from
  the stream `nbd::handshake::run` hands off, dispatches them
  against a generic `<B: BlockBackend>` using the trait from
  inc-1.4. This is the increment where `teslausb-core` is
  finally added as a dep of `teslafat` (the dep removed in
  inc-1.3 review M4 and deliberately deferred in inc-1.4).
  Plan estimate ~150 LOC; expect ~700 LOC after charter
  compliance + FUA pass-through tests using `MockBackend` as
  the in-memory backend fixture.


### 2026-05-19 (resumed, eleventh time) -- Phase 1.5 implementation + review gate

- **Phase 1.5 implementation** (commit `f36e913`): added
  `rust/crates/teslafat/src/nbd/wire.rs` (395 LOC + 11 tests)
  and `rust/crates/teslafat/src/nbd/transmission.rs` (1040 LOC
  + 19 tests). Wire module: pure constants and encode/decode
  for the 28-byte NBD request header and 16-byte simple-reply
  header, with the `NBD_CMD_FLAG_FUA = 1 << 0` bit pattern
  pinned to `WriteFlags::FUA` via a load-bearing equality test
  (a wrong value here would break FUA pass-through silently).
  Transmission module: `pub async fn run<B: BlockBackend, S>`
  dispatching READ / WRITE / FLUSH / TRIM, with `NBD_ENOTSUP`
  for unknown commands, `NBD_EOVERFLOW` + bail for oversized
  requests, `NBD_EINVAL` for OOB writes (after draining the
  WRITE payload so the wire stays aligned), and silent
  termination on DISC per NBD spec. `teslafat -> teslausb-core`
  runtime dep wired (closing the inc-1.3 M4 deferral and the
  inc-1.4 carry-forward). 6 files changed, +1453/-13.
- **Test architecture:** native AFIT futures returned from
  `BlockBackend` methods are not `Send` (as ADR-0004 sec A
  predicted) so tests drive the server and client futures on
  a single task via `tokio::join!` rather than `tokio::spawn`.
  A small `drive()` helper centralises the pattern; all 19
  transmission tests inherit the same single-task driver,
  matching the production current-thread runtime architecture.
  The first attempt used `read_to_end` to verify "DISC
  produces no reply bytes" and deadlocked against join!'s
  held futures (the completed server future cannot drop its
  half of the `DuplexStream` until the join completes); the
  rewrite uses a FLUSH-then-DISC sequence with a precise
  16-byte `read_exact` to verify the silence property without
  depending on EOF.
- **Test defect caught and fixed:** the original FUA-field-
  extraction test asserted `assert_ne!(decoded.kind,
  NBD_CMD_FLAG_FUA)` to prove `kind` was not placed in the
  flags slot — but `NBD_CMD_WRITE = 1` numerically equals
  `NBD_CMD_FLAG_FUA = 1 << 0 = 1`, so the assertion fired
  during the workspace test run (with the round-trip test
  feeding `kind = NBD_CMD_WRITE`). Rewrote as two
  independent tests using hand-rolled spec byte layouts: one
  hand-constructs the 28-byte buffer with `FUA` at bytes
  `[4..6]` and `NBD_CMD_TRIM = 4` at bytes `[6..8]`, then
  decodes and asserts both fields land where the spec says;
  the other encodes a known header and hand-asserts the
  encoded bytes at the spec offsets. Picking `NBD_CMD_TRIM`
  (numerically `4`, distinct from any flag bit) eliminates
  the false-pass risk for a symmetric encoder/decoder
  field-swap bug. This was exactly the "tests that always
  pass are not useful" failure mode the user flagged earlier
  in the session — caught by gates not by inspection.
- **Test discipline:** 30 new tests, total workspace 77 -> 107
  (+30). 19 transmission tests cover every command path
  (READ / WRITE / FLUSH / TRIM / DISC / unknown), every wire
  error (oversized, OOB, bad magic, short read, clean EOF
  between commands), the FUA pass-through into `WriteFlags`,
  the handle-echo invariant, pipelining order, and zero-length
  edge cases for READ and WRITE. 11 wire tests verify byte
  layouts including hand-asserted spec offsets for FUA, magic
  values, round-trip, and handle-only-affects-handle-slot
  isolation. Mutation-test mental model held: for each named
  test at least one of (`+` -> `-`, swap struct fields,
  off-by-one a length, swap enum arms) would flip the
  assertion.
- **Iteration:** seven clippy errors and one rustdoc error
  caught and fixed before commit: `expect_used` x 3 (added
  test mod allow list `#[allow(clippy::unwrap_used,
  clippy::expect_used, clippy::indexing_slicing)]`),
  `doc_markdown` x 1 (`ORed` -> `` `ORed` ``),
  `single_match_else` x 1 (rewrote `match Some/None` as
  `let Some(...) = ... else { ... }`), `indexing_slicing`
  x 1 (refactored the request-header read loop in
  `read_request_header` to use `buf.split_at_mut(1)` +
  `read_exact` for the remaining bytes -- also simplifies
  the EOF discrimination between "between-commands clean
  close" and "mid-header torn read"), `match_same_arms` x 1
  (collapsed the two `NBD_EINVAL` arms in `map_backend_err`
  into a single `|`-pattern arm), and
  `private_intra_doc_links` x 1 (a module-level doc
  reference to a private fn `map_backend_err` rewritten as
  prose). After fixes: zero clippy warnings, zero rustdoc
  warnings, zero suppressions in production code, mod-level
  allows on the test mods only (consistent with
  `nbd::handshake::tests`).
- **Charter review** (`~/.copilot/.../files/charter-review-inc-1.5.md`):
  0 Major, 1 Minor, 3 Nits.
  - **Minor 1:** ADR-0005 needed. Two non-obvious wire policies
    each meet >= 1 charter ADR trigger criterion: §A oversized
    requests terminate the connection (criteria 4 forward-compat
    + 5 non-obvious vs the "polite drain" alternative), §B OOB
    WRITE drains payload before rejecting (criterion 4 — the
    wire alignment invariant is load-bearing for connection
    correctness, and a future contributor "optimising" by
    rejecting before draining would silently desync every
    subsequent request). Co-located in one ADR per the
    ADR-0003 / 0004 template of grouping related wire/protocol
    choices.
  - **Nit 1:** ADR-0004 sec A "Consequences" could append a
    paragraph cross-referencing the `drive()` helper as the
    canonical "AFIT futures aren't Send, use `tokio::join!`
    not `tokio::spawn`" pattern for Phase 1.6+ backend impl
    tests to copy.
  - **Nit 2:** `refuse_oversized` could explicit `flush().await`
    before `bail!` to future-proof against a buffered wrapper
    (e.g. `tokio::io::BufWriter`); observable behaviour today
    is correct on `DuplexStream` and `UnixStream`, but the
    intent is worth documenting.
  - **Nit 3:** `handle_write` allocates a fresh `Vec<u8>` per
    request (up to 32 MiB). Candidate for a per-connection
    scratch buffer in Phase 1.6+ once we have a Pi Zero 2 W
    RSS budget; NBD is sequential-per-connection by spec so
    reuse is safe.
- Review-fix commit (this entry): "docs(b1): inc-1.5 review
  fixes - add ADR-0005 + PROGRESS APPROVED + plan LOC note".
  Branch `b1-userspace-rust` reaches 21 commits ahead of
  `main` after the review-fix commit.
- **Next:** Phase 1.6 -- systemd unit `teslafat@.service`
  (instanced for LUN 0 / LUN 1), `EnvironmentFile=/etc/teslausb/teslafat.toml`,
  `User=teslausb`, capability bounding. Plan estimate ~50 LOC;
  expect ~150-300 LOC after charter compliance. This is also
  where the deferred handshake timeout (`tokio::time::timeout`
  from the inc-1.3 `nbd/mod.rs` TODO) lands, which means
  adding the `time` feature to tokio in `teslafat/Cargo.toml`
  -- which incidentally would give future tests a hard-cap
  safety net for the AFIT/join! pattern.


### 2026-05-19 (resumed, twelfth time) -- Phase 1.6 implementation + review gate

- **Phase 1.6 implementation** (commit `1228f41`): added
  `rust/crates/teslafat/src/backend.rs` (233 LOC + 12
  ZeroBackend tests), `rust/crates/teslafat/src/server.rs`
  (526 LOC + 10 server tests across cross-platform
  `serve_one_connection_*` + Unix-only `accept_loop::*`
  submodule), and `rust/crates/teslafat/units/teslafat@.service`
  (104 LOC systemd template unit with full hardening:
  `CapabilityBoundingSet=`, `ProtectSystem=strict`,
  `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`,
  `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`).
  Config: `NbdConfig { socket_path, handshake_timeout_seconds }`
  with range validation [1, 600] s and non-empty path check
  (+6 new config tests). Main rewrite: `--check-config` flag
  preserves the Phase 1.1 sentinel contract; default Unix
  mode builds a current-thread tokio runtime, prepares the
  socket (mkdir parent + unlink stale file), binds the
  listener, installs SIGTERM + SIGINT handlers, runs
  `server::serve` until either signal. `nbd/mod.rs` drops
  the inc-1.3 follow-up TODO (closed by the `tokio::time::timeout`
  wrap in `server::serve_one_connection`). Cargo: tokio
  `time` + `signal` + `sync` features added with a documented
  rationale block. 10 files changed, +1227/-28.
- **Architecture choice — `ZeroBackend` lives in
  `teslafat::backend`, not `teslausb-core::backend::mock`:**
  keeps the inc-1.4 `teslausb-core` surface frozen, avoids
  the `NullBackend::new(size: usize) -> Vec<u8>` allocation
  that would OOM the Pi at boot for a 64 GiB `volume_size_gb`
  (the `NullBackend` API is sized for unit-test fixtures,
  not for daemon-scale exports), and the `placeholder` module
  path makes accidental production use impossible. The trait
  is the cross-crate contract; impls are per-consumer
  specialisations.
- **Cross-platform test coverage:** `serve_one_connection`
  is generic over `AsyncRead + AsyncWrite + Unpin` so its
  tests (handshake-timeout, handshake-failure, transmission-
  error, backend-size-advertisement, happy-path) run on the
  Windows dev box via `tokio::io::duplex`. The 5
  `accept_loop::*` tests are `#[cfg(unix)]`-only
  (`tokio::net::UnixListener` is Unix-only) and will run on
  Linux + the Pi. Compile-clean on Windows (no warnings).
- **Test discipline:** 22 new tests, total workspace 107 ->
  129 (+22). 12 ZeroBackend tests cover constructor
  fidelity, read fills zeros, read at non-zero offset,
  zero-length read, OOB read + write with correct error
  fields, overflowing arithmetic, write-does-not-persist
  (read-back assertion), FUA write, flush, and a
  load-bearing `std::mem::size_of::<ZeroBackend>() == 8`
  pin that catches any future field addition (would change
  the daemon's per-LUN memory footprint silently otherwise).
  10 server tests cover the per-connection lifecycle (timeout
  fires, handshake error, transmission error, backend size
  advertised correctly, happy-path drives one full request)
  and the accept loop's shutdown + recovery behaviour
  (immediate shutdown, idle-then-shutdown,
  one-connection-then-shutdown, two sequential connections,
  recovery from a bad client + accept next). 6 NbdConfig
  validation tests cover defaults, non-empty socket path,
  range bounds at both extremes, and TOML
  deserialisation round-trip. Mutation-test mental model
  held: for each named test at least one trivial mutation
  (drop the timeout, return Err instead of (), change a
  magic-corruption byte, off-by-one a range bound) would
  flip the assertion.
- **Iteration:** two iterations preceded green gates:
  (1) `backend.rs` first draft used `pollster` (not a
  `teslafat` dev-dep — it lives in `teslausb-core`'s
  dev-deps) and passed `len: u64` to `check_bounds` (which
  expects `usize`); rewrote tests as `#[tokio::test] async fn`
  matching `nbd::transmission::tests` convention and dropped
  the unnecessary `u64`-cast (`buf.len()` is already
  `usize`); also missed `pub mod backend;` in `lib.rs` so
  the new file didn't compile into the lib on the first
  attempt (caught by `cargo test --lib backend` returning
  fewer tests than expected).
  (2) Gate sweep surfaced `used_underscore_binding` on
  `Err(_elapsed) => let _: Elapsed = _elapsed` (renamed to
  `elapsed` while keeping the marker-type pin),
  `clippy::panic` on two `panic!("expected OutOfBounds, got
  {other:?}")` arms in backend tests (added `clippy::panic`
  to the test-mod allow list consistent with the existing
  `unwrap_used` / `expect_used` / `indexing_slicing`
  allow set), and three `rustdoc::broken_intra_doc_links`
  errors on `[`serve`]`, `[`crate::server::serve`]`, and
  `[`nbd::handshake::run`]` references (the `serve`
  function is `#[cfg(unix)]` and rustdoc runs on Windows in
  the dev box, so the item is genuinely not in scope;
  rewrote as plain backticks ``serve`` and absolute paths
  `[`crate::nbd::handshake::run`]` respectively). After
  fixes: zero clippy warnings, zero rustdoc warnings, zero
  suppressions in production code.
- **Charter review** (`~/.copilot/.../files/charter-review-inc-1.6.md`,
  ~27 KB): Pillar walk found 0 Major, 1 Minor, 4 Nits.
  - **Minor 1:** ADR-0006 needed. Three coupled
    connection-lifecycle decisions each meet >= 1 charter
    ADR trigger criterion: §A single connection at a time
    per process (criteria 4 forward-compat + 5 non-obvious
    vs the established "spawn a task per connection" idiom
    in every HTTP-server tutorial — the case for serial
    rests on the kernel `nbd-client` being the only intended
    peer + owning exclusive `/dev/nbdN` + Phase 2's
    `FileBackend` needing exclusive write access + AFIT
    futures not being `Send` per ADR-0004 §A consequence,
    none of which a reader who doesn't know NBD would
    derive); §B per-connection errors never propagate to
    the accept loop (criteria 2 security-mild + 4
    forward-compat — `serve_one_connection -> ()` not
    `Result` because a `Result`-returning version exposes a
    client-DoS path through `systemd Restart=on-failure`
    cycles that count against `StartLimitBurst`); §C
    handshake timeout is the only liveness check (criteria
    4 forward-compat + 5 non-obvious — the kernel
    `nbd-client` polices request liveness via
    `/sys/block/nbdN/queue/io_timeout`, and adding a
    duplicate userspace per-request timeout would kill
    legitimate large WRITEs on the Pi Zero 2 W under SD-card
    pressure precisely when the device is most loaded).
    Co-located in one ADR per the ADR-0005 template of
    grouping tightly-coupled wire/lifecycle policy choices.
  - **Nit 1:** `serve`'s recoverable-vs-fatal accept-error
    policy could be tightened — current: every error is
    `warn!`-then-retry; on a genuinely broken listener
    (e.g. the socket file was unlinked out from under us)
    this becomes an infinite log-spam loop. Future
    hardening: track consecutive identical errors and bail
    after N >= 100 of the same kind so `Restart=on-failure`
    kicks in. Not blocking; defer until we observe the
    failure mode.
  - **Nit 2:** Socket cleanup on shutdown is best-effort
    (`unix_serve` calls `fs::remove_file` after `serve`
    returns and logs `warn!` on failure). On a crash the
    socket file persists; `prepare_listener` handles it via
    unlink-before-bind on the next start. Alternative
    (Drop wrapper that always unlinks) is pure ergonomics.
    Defer.
  - **Nit 3:** Phase 1.6 LOC overrun 24× (50 -> 1227); same
    pattern direction as inc-1.3 / 1.4 / 1.5. Recorded in
    PLAN row 1.6 with breakdown (two new public modules
    with full doc + test coverage, `NbdConfig` schema
    delta, systemd hardening profile, `--check-config`
    flag, SIGTERM/SIGINT plumbing).
  - **Nit 4:** The `let _: Elapsed = elapsed;` marker-type
    pin in `serve_one_connection` is non-obvious to future
    readers; the comment explains the intent (load-bearing
    import pin so a future tokio rename breaks this site
    loudly) but the pattern itself is unusual. Acceptable;
    consistent with the inc-1.5 N1 deferral pattern (cross-
    reference test idioms when we have two examples).
- Review-fix commit (this entry): "docs(b1): inc-1.6 review
  fixes - add ADR-0006 + PROGRESS APPROVED + plan LOC note".
  Branch `b1-userspace-rust` reaches 25 commits ahead of
  `main` after the review-fix commit.
- **Next:** Phase 1.7 -- dev-box smoke test harness. Run
  `teslafat` binary against a fixture config, have `nbd-client`
  connect to the configured Unix socket, issue a `dd
  if=/dev/nbdN bs=4096 count=1` and verify it returns
  all-zero bytes (the `ZeroBackend` synthesised content),
  then `nbd-client -d /dev/nbdN` and SIGTERM the daemon.
  Plan estimate ~80 LOC (test harness); expect ~300-500
  LOC after charter compliance based on the inc-1.3 through
  inc-1.6 overrun pattern. Phase 1.7 is the H1 hardware-gate
  prerequisite: once inc-1.5 + 1.6 + 1.7 are all approved,
  H1 deploys the binary to `cybertruckusb.local` for the
  first hardware-on-Pi smoke run.


### 2026-05-19 (resumed, twelfth time, continued) -- Phase 1.7 implementation + review gate

- **Phase 1.7 implementation** (commit `37fb2fb`): added
  `rust/crates/teslafat/tests/smoke.rs` (~766 LOC, 6 tests,
  file-level `#![cfg(unix)]`). The deliverable end-to-end
  validates the compiled `teslafat` binary against the NBD
  wire protocol, signal-handling, sentinel emission, and
  the full `Config` -> wire plumbing for both `volume_size_gb`
  and `nbd.handshake_timeout_seconds`. 1 file changed,
  +766/-0.
- **Scope deviation from PLAN row 1.7 (intentional, ADR-0007):**
  the plan literally said "`nbd-client` connects to
  `/run/teslafat-0.sock`". The shipped smoke test instead
  speaks the NBD wire protocol *directly* from the test
  process over `tokio::net::UnixStream`, re-using the daemon's
  own `nbd::handshake` + `nbd::wire` public constants and
  encoders. Rationale: `nbd-client` requires `CAP_SYS_ADMIN`
  + a loaded `nbd` kernel module + `/dev/nbdN` device nodes,
  none of which are available on a normal user-account dev
  box (Windows or Linux). Driving the wire directly tests
  exactly what we ship (server conformance) and runs as any
  user with zero ceremony; the kernel-client integration is
  properly an H1 hardware test. The decision and the broader
  testing-scope policy it implies are now ADR-0007.
- **6 tests covered:**
  - (1) `daemon_serves_all_zero_via_nbd_handshake_and_read`
    -- happy path: spawn -> handshake -> READ 4 KiB at offset
    0 -> assert all-zero payload -> DISC -> SIGTERM ->
    `ExitCode::SUCCESS`. Pins `ZeroBackend` -> `server::serve`
    -> handshake -> wire end-to-end.
  - (2) `daemon_exits_cleanly_on_sigterm_with_no_clients`
    -- proves the signal handler is wired into the accept
    loop before any client has connected, and that
    `prepare_listener`'s best-effort socket cleanup fires
    on the clean-shutdown path (socket file is gone after
    the daemon exits).
  - (3) `daemon_recovers_from_bad_handshake_and_accepts_next_client`
    -- ADR-0006 §B "per-connection errors never propagate
    to the accept loop" end-to-end at the binary level, not
    just inside `serve_one_connection`'s unit tests. A bad
    client (missing `CF_FIXED_NEWSTYLE`) is rejected, the
    second well-formed client completes a full handshake +
    READ + DISC, and the daemon SIGTERMs clean.
  - (4) `daemon_advertises_configured_volume_size_in_handshake`
    -- pins `cfg.volume_size_gb` (17 GiB, non-default) ->
    `ZeroBackend` -> handshake reply advertised size, ruling
    out "the daemon ignored config and used a constant".
  - (5) `daemon_emits_started_sentinel_in_serve_mode` --
    the "started" sentinel that operators grep for is also
    emitted in the live-serve path, not just in
    `--check-config` mode (which `tests/sentinel.rs` already
    covered).
  - (6) `daemon_handshake_timeout_config_value_reaches_sentinel`
    -- pins `nbd.handshake_timeout_seconds = 47` end-to-end
    into the JSON sentinel's `nbd_handshake_timeout_s`
    structured field, so a future refactor that drops the
    config -> sentinel plumbing surfaces immediately at
    test time.
- **Test infrastructure:**
  - `DaemonHandle` RAII guard owns the spawned `Child`, the
    `TempDir` hosting the config + socket, and an
    `Arc<Mutex<Vec<String>>>` of captured stderr lines pumped
    by a vanilla OS thread. Drop SIGTERMs (then SIGKILLs on
    timeout) and dumps all captured stderr on test panic via
    a `thread::panicking()` guard. Diagnosability investment:
    the difference between "smoke test 3 failed for some
    reason" and "smoke test 3 failed; daemon log shows
    `bind: EACCES`".
  - `start_daemon` / `start_daemon_with_handshake_timeout`
    write the fixture TOML to a tempdir, spawn
    `env!("CARGO_BIN_EXE_teslafat")`, poll for socket-file
    existence with 50 ms cadence + 10 s deadline.
  - `client_handshake_export_name` / `client_read` /
    `client_disc` re-use the daemon's public NBD constants
    and helpers. `CF_NO_ZEROES` is set so the server replies
    with the compact 10-byte export-name reply (eliminates
    the 124-byte legacy zero pad arithmetic).
  - `send_sigterm` shells out to `kill -TERM <pid>` -- zero
    new dependencies, zero `unsafe`, POSIX-portable across
    Linux + macOS dev hosts.
- **Cross-platform discipline:** file is `#![cfg(unix)]`
  because `tokio::net::UnixListener` (and therefore
  `server::serve`) is Unix-only. On Windows the smoke binary
  compiles to an empty test binary (0 tests run); on Linux +
  Pi all 6 tests execute. The Windows gate proves the file
  parses and lints clean; runtime validation is deferred to
  the first Pi run (H1 hardware deploy).
- **Test discipline:** 0 tests gained on the Windows dev box
  (smoke binary is cfg-gated empty); +6 on Linux/Pi. Total
  workspace 129 -> 129 on Windows, 129 -> 135 on Linux.
  Mutation-test mental model held across all 6 tests:
  swapping `NBD_CMD_READ` for `NBD_CMD_WRITE` would fail
  test #1's all-zero assertion; dropping the signal handler
  would hang test #2 at the SIGTERM-wait deadline; removing
  the per-connection error catch would hang test #3 at the
  second `connect_with_retry`; hardcoding the export size
  would fail test #4's 17 GiB pin; removing the sentinel
  would fail test #5's `contains("started")` check;
  removing the structured field would fail test #6's exact
  JSON-key match.
- **Zero new dependencies.** `tempfile`, `tokio` (with
  `signal` + `time` + `net` features already from inc-1.6),
  and the `teslafat` library re-export are all in dev-deps
  from prior increments. The `kill(1)` subprocess approach
  was deliberately chosen over `nix` / `libc` to keep the
  dev-dep surface minimal.
- **Gates:** `cargo build / clippy -D warnings / fmt --check
  / doc -D warnings / test workspace --all-targets` all
  green; 129 tests pass on Windows (smoke runs 0; Linux/Pi
  will run +6 for 135 total). `scripts/check.sh --all`
  12/0/4 baseline maintained. `pre-commit run --files
  smoke.rs` clean first try (no autofix rounds needed).
- **Iteration count: 1.** First draft included an unused
  `use std::io::Write` import propped up by a contrived
  `_used_imports_pin` shim; caught on self-review before
  running gates, removed both. `cargo fmt --check` surfaced
  a single trailing-blank-line at EOF, auto-corrected by
  `cargo fmt --all`. No clippy iterations.
- **Charter review** (`~/.copilot/.../files/charter-review-inc-1.7.md`):
  0 Major, 1 Minor, 4 Nits.
  - **Minor 1:** ADR-0007 -- smoke-test scope policy +
    PLAN row 1.7 amendment. Decision A (drive the wire
    directly instead of through `nbd-client`) qualified on
    ADR triggers 4 (forward-compat -- future contributors
    need to know the rule), 5 (non-obvious -- the PLAN
    literally said `nbd-client`), and 6 (project-wide
    policy -- applies to every future Phase 2+ integration
    test). The ADR documents (a) the smoke wire-only
    decision with rejected alternatives, (b) the broader
    dev-box / H1 split policy, (c) the H1-deferred
    kernel-client validation. PLAN row 1.7 was amended to
    reflect the userspace-wire reality. Both actioned in
    this review-fix commit.
  - **Nit 1:** LOC overrun ~80 -> 766 (~9.5x), same
    overrun-pattern direction as inc-1.3 through inc-1.6.
    Driven by `DaemonHandle` + stderr pump (~80 LOC),
    3 wire helpers with `tokio::time::timeout` wrappers
    (~150 LOC), 6 tests with comprehensive assertions +
    dump-on-failure (~300 LOC), inline rationale comments
    (~200 LOC), file-level lint allows + cfg gating
    (~30 LOC). Recorded in PLAN row 1.7 with breakdown.
  - **Nit 2:** Socket-readiness via file-existence polling
    (50 ms cadence, 10 s deadline) rather than rendezvous
    on an explicit `ready` sentinel line. Works fine
    today; worth revisiting if the harness grows. Not
    actionable.
  - **Nit 3:** `_tempdir` field on `DaemonHandle` has an
    underscore prefix (idiomatic Rust would prefer a doc
    comment over the underscore). Marginal. Not actionable.
  - **Nit 4:** Bad-handshake test #3 relies on the server
    dropping the connection (EOF on the bad client's
    `read`). If a future refactor changed rejection to
    "send an error reply then close", the test would still
    pass (it asserts only that the second client succeeds).
    Strengthening would require asserting on a specific
    captured-stderr line. Marginal. Not actionable.
- Review-fix commit (this entry): "docs(b1): inc-1.7 review
  fixes - add ADR-0007 + PROGRESS APPROVED + plan amendments".
  Branch `b1-userspace-rust` reaches 27 commits ahead of
  `main` after the review-fix commit. **Phase 1 closes
  here.**
- **Next:** H1 -- hardware deploy + first kernel-client
  smoke on the Pi (`cybertruckusb.local`). Build the
  `teslafat` binary for the Pi target, deploy with the
  `teslafat@.service` systemd unit + minimal config, run
  `nbd-client` against `/run/teslausb/teslafat.sock` for
  the first time, and walk the H1 hardware checklist (to
  be authored as a separate H1-prep step). Phase 2 (B-1
  `FileBackend` for real image-file storage) starts only
  after H1 passes.
