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
| 2.1 | `fs::geometry` + `fs::fat32::geometry` | ✅ | ✅ | ✅ |
| 2.2 | `fs::fat32::boot_sector::synthesize` | ✅ | ✅ | ✅ |
| 2.3 | `fs::fat32::fsinfo::synthesize` | ✅ | ✅ | ✅ |
| 2.4 | `fs::fat32::fat_table::synthesize` | ✅ | ✅ | ✅ |
| 2.5 | `fs::fat32::directory::synthesize` (8.3 + LFN) | ✅ | ✅ | ✅ |
| 2.6 | `fs::fat32::synth::read` dispatcher | ✅ | ✅ | ✅ |
| 2.7 | `fs::fat32` integration test (synth+mount+cmp) | ✅ | ✅ | ✅ |
| 2.8 | `fs::exfat::geometry` + boot region | ✅ | ✅ | ✅ |
| 2.9 | `fs::exfat::allocation_bitmap` + `upcase_table` | ✅ | ✅ | ✅ |
| 2.10 | `fs::exfat::directory` | ✅ | ✅ | ✅ |
| 2.11 | `fs::exfat::synth::read` dispatcher | ✅ | ✅ | ✅ |
| 2.12 | `fs::exfat` integration test | ✅ | ✅ | ✅ |
| 2.13 | `lazy_load.rs` (deferred deep-dir materialization) | ✅ | ✅ | ✅ |
| 2.14 | Cold-start benchmark (≤ 1 s for 10K files) | ✅ | ✅ | ✅ |
| 2.15 | `fs::backing_tree` types + walker (read-only Phase-3 prep) | ✅ | ✅ | ✅ |
| 2.16 | `fs::cluster_layout` planner | ✅ | ✅ | ✅ |
| 2.17 | FAT32 cluster-content synth from layout | ✅ | ✅ | ✅ |
| 2.18 | exFAT cluster-content synth from layout | ✅ | ✅ | ✅ |
| 2.19 | `teslafat::SynthBackend` + `fs_type` config + daemon wiring | ✅ | ✅ | ✅ |

## Phase H2 — Read-only synth on hardware

Target `cybertruckusb.local` (pi). Full results: `~/.copilot/session-state/<sid>/files/hw-results.md`.

| Inc | Step | Status |
|---|---|---|
| Preflight | `ExfatSynth::bitmap_cluster_count` + `upcase_cluster_count` accessors (closes 2.18/2.19 charter-review nit) | ✅ (`a13a5c8`) |
| Preflight-D3 | Shrink exFAT upcase table 131_072 → 256 bytes (ASCII-only) to work around exfatprogs 1.2.9 u16 truncation bug; new ADR-0009; 2 new regression tests; hardware-verified `fsck.exfat -v` clean on H2.6 re-run | ✅ (`f50a454`) |
| Preflight-D1D2 | FAT32 root-dir volume label entry (D1) + FSInfo free-cluster accounting (D2); 18 new regression tests; hardware-verified `fsck.vfat -v -n` clean on H2.5 re-run (zero warnings, `blkid` reports both LABEL_FATBOOT and LABEL) | ✅ (`57d90da`) |
| H2.1 | Cross-build `teslafat` aarch64 binary + deploy to `cybertruckusb.local` | ✅ (sha256 device-side matches dev box) |
| H2.2 | Create tiny synthetic backing tree (3 mp4s, 2 subdirs, 917 KiB total) | ✅ at `/var/teslacam-test/` |
| H2.3 | Start `teslafat-test@0` pointing at the tree | ✅ (`SynthBackend ready fs_type=fat32 size=4 GiB file_count=3`) |
| H2.4 | `nbd-client -unix` + `losetup` + `mount -o ro -t vfat` + `cmp` byte-identical readback | ✅ 3/3 files cmp clean, sha256 matches source |
| H2.5 | `fsck.vfat -v -n /dev/nbd0` clean | ✅ (post-`57d90da` re-run 2026-05-20): exit 0, zero warnings, `blkid` reports both `LABEL_FATBOOT` and `LABEL` (D1 fixed), "Checking free cluster summary." emits no correction warning (D2 fixed), 3/3 files byte-identical |
| H2.6 | Same in exFAT mode (`fs_type = "exfat"`, 32 GiB) | ✅ (post-`f50a454` re-run): mounts cleanly + 3/3 cmp clean + **`fsck.exfat -v` → `clean. directories 3, files 3`** (was D3 ⚠️ on first H2 run — see Defect D3 below for root cause and fix) |
| H2.7 | Cold-start wall-clock: synth start → mount succeeds. Target ≤ 1 s. | ⚠️ 1577 ms total (1324 ms systemd unit start + 159 ms NBD attach + 93 ms kernel mount). **Synth itself ~1 ms**; overhead is systemd `ExecStartPre` + Tokio init. Closes naturally via Phase 6 socket activation. |
| H2.8 | Teardown, SSH alive, WiFi alive | ✅ socket gone, service inactive, /dev/nbd0 detached, SSH+WiFi up, boot `degraded` (baseline) |

**Defects discovered (all CLOSED):**
- **D1 (low)** — ✅ **CLOSED** (commit `57d90da`, hardware-verified 2026-05-20). FAT32 root-dir volume label entry now synthesized at byte 0 of root cluster per fatgen103 §6.1 (attribute 0x08 alone, FstClusHI=FstClusLO=FileSize=0). `blkid /dev/nbd0` now reports both `LABEL_FATBOOT="TESLACAM"` (boot-sector field) AND `LABEL="TESLACAM"` (root-dir entry); `fsck.vfat -v -n` emits no "Label in boot sector ... but there is no volume label in root directory" warning.
- **D2 (low)** — ✅ **CLOSED** (commit `57d90da`, hardware-verified 2026-05-20). FAT32 FSInfo `FSI_Free_Count` now carries the planned free-cluster count (= data clusters − used clusters) and `FSI_Nxt_Free` carries the first unallocated cluster hint per fatgen103 §4.1. `fsck.vfat -v -n` "Checking free cluster summary." step emits no "Free cluster summary uninitialized" warning.
- **D3 (HIGH)** — ✅ **CLOSED** (commit `f50a454`, hardware-verified 2026-05-20). Root cause was **NOT** ours — `exfatprogs` 1.2.9's `boot_calc_checksum()` has a u16 truncation bug (`size` declared `unsigned short`), so our 131,072-byte upcase table truncated to 0 and fsck's computed checksum stayed 0; the misleading "expected:" label in the error printed our (correct) stored value. Fix: shrink upcase table to 256 bytes (128 ASCII entries U+0000..U+007F) per spec §7.2.4's partial-table allowance. ADR-0009 codifies the decision + the 0xFFFF interop ceiling enforced by const-assert + regression test. `fsck.exfat -v /dev/nbd0` now reports `clean. directories 3, files 3` on hardware (H2.6 re-run). See `hw-d3-fix-journal-20260520.log`.

## Phase 3 — FS write-side (FAT32 + exFAT)

| Inc | Deliverable | Status |
|---|---|---|
| 3.1 | `fs::fat32::parse::decode_write` | ✅ `4288e70` |
| 3.2 | `fs::exfat::parse::decode_write` | ✅ `0f5dc78` |
| 3.3 | `backend::dir_tree` POSIX adapter | ✅ `a23c4e2` |
| 3.4 | `cluster_map` extent-based | ✅ `6cbfb5c` |
| 3.5 | Wire `synth::write` integration test | ✅ `895c75c` (3.5a `9077357` + 3.5b `f6c3a8a` + 3.5c `a6e58d6` + 3.5d `d37d57c` + 3.5e `895c75c` + 3.5f `089342f` + 3.5g `<pending>`) |
| 3.6 | Power-cut harness | ✅ `b3437d2` |

**Phase 3.5f (`089342f`):** synth read overlay + Bug H3-1
extent-replacement fix. Two hardware-surfaced bugs from the
H3 NBD-loopback exFAT write smoke (Phase 3.5e binary on
`cybertruckusb.local`):

- **Bug H3-1 (data corruption):** when the kernel rewrote a
  directory entry with progressively larger `data_length`
  values during a 50 MB copy, the second `try_resolve_file`
  call silently dropped the new larger extents via the
  idempotent-insert path. Tail data writes fell into
  `pending_data` forever. Backing file got correct size but
  the last ~21 MB was zero-filled. Fix: unconditional
  `cluster_map.remove_file(path)` before re-inserting all
  extents for the path. Symmetric fix in both
  `fat32_write::try_resolve_file` and
  `exfat_write::try_resolve_file`.
- **Bug H3-2 (read invisibility after write):** kernel-written
  FAT entries and dir cluster bytes lived only in
  `WriteState.fat` / `DirectoryState.buffer` and were
  invisible to subsequent `SynthBackend::read` calls. New
  `DirtyByteMap` (sparse BTreeMap of disjoint byte intervals)
  tracks per-byte dirty regions in both write states; new
  `overlay_read` methods walk the FAT + per-directory cluster
  buffers and overlay only kernel-written bytes onto the
  synth's startup snapshot. Wired into `SynthBackend::read_sync`
  after the file-extents overlay.

Tests: +14 `dirty_map` unit tests, +2 exfat_write H3
regression tests (`growing_extent_replaces_stale_chain_h3_1`,
`overlay_read_returns_kernel_written_fat_and_dir_bytes_h3_2`),
+2 fat32_write H3 regression tests (same names). Workspace
total now 1010 pass / 0 fail. All gates green.

**Phase 3.5g (pending commit):** Bug H3-2 part 2 — allocation
bitmap + data-cluster overlay. The Phase 3.5f overlay surfaced
FAT entries and directory cluster bytes the kernel wrote, but
left two gaps that re-broke the H3 smoke for files ≥ 2 MB
(verified breakpoint between 1 MB and 2 MB on hardware
2026-05-20):

- **Data-cluster overlay (uncommitted addendum to 3.5f):** new
  files' DATA bytes were not in `SynthBackend.file_extents`
  (that snapshot is captured at startup), so a post-remount
  read of the file's data clusters returned zeros even though
  the directory entry was visible. Added
  `overlay_data_clusters_from_cluster_map` which, for every
  extent in `cluster_map` overlapping the read range, opens
  the backing file and reads the overlapping bytes into
  `read_buf`. Same helper used by both write states.
- **Allocation bitmap overlay (Phase 3.5g proper):** the
  kernel writes to the allocation bitmap clusters (in the
  data region) to mark new clusters as allocated. Pre-3.5g
  these writes landed in `pending_data` and were silently
  dropped because the bitmap clusters aren't in `cluster_map`
  (the bitmap is part of synth's metadata, not a user file).
  On remount, the synth re-rendered the bitmap with only
  the startup-time files marked allocated. The Linux exFAT
  driver rejected the resulting dir-entry-vs-bitmap
  inconsistency (file claims clusters allocated, bitmap says
  free) with EIO on `stat` — observed as `ls -? ? ?` +
  "Input/output error" on every file ≥ 16 clusters. Fix:
  new `ExfatWriteState::with_allocation_bitmap` builder
  attaches a `bitmap_buf: Vec<u8>` + `dirty_bitmap:
  DirtyByteMap` sized to the bitmap stream; new fast path in
  `apply_data_cluster_write` routes bitmap-region writes
  into the buffer + marks dirty; `overlay_read` surfaces
  the dirty bytes onto subsequent reads of the bitmap
  region.

Hardware re-test on `cybertruckusb.local` 2026-05-20:
- 4 KB, 1 MB, 2 MB, 10 MB, 50 MB files all round-trip
  byte-identical after umount + remount (pre-3.5g: ≥ 2 MB
  reproduced `?????????` EIO every time).
- `fsck.exfat -n /dev/nbd0` → "clean. directories 3,
  files 5" after writing 50 MB + 2 MB + 4 KB files.
- Daemon journal: zero WARN/ERROR entries across all five
  size sweeps.

Tests: +1 regression test
(`overlay_read_returns_kernel_written_bitmap_bytes_h3_2_part_2`).
Workspace total now 1011 pass / 0 fail. All gates green.

**Phase 3.5g charter follow-up (commit pending):** addresses
the 4 Majors found in the Phase 3.5g charter review. All are
internal refactors; no behavior change visible to callers.

- **MAJOR-3 (primitive obsession / magic-value sentinel) →**
  the three `bitmap_first_cluster: u32 (== 0 means none) +
  bitmap_buf: Vec<u8> + dirty_bitmap: DirtyByteMap` fields
  collapsed into `bitmap: Option<BitmapTracker>` with an
  encapsulated `BitmapTracker { first_cluster, cluster_count,
  buf, dirty }` struct. Eliminates the `== 0` sentinel and
  removes the data clump.
- **MAJOR-1 (function > 50 SLOC) →** `apply_data_cluster_write`
  now delegates bitmap routing to `BitmapTracker::owns_cluster`
  + `BitmapTracker::apply_write`; the fn body is back well
  under the charter ceiling.
- **MAJOR-2 (function > 50 SLOC) →** `overlay_read` extracted
  per-surface helpers: the directory-cluster loop moved to
  `overlay_directory_clusters`; the bitmap overlay moved into
  `BitmapTracker::overlay`; the data-cluster overlay stays in
  the existing free fn. `overlay_read` is now a ~25-line
  driver that names the four surfaces.
- **MAJOR-4 (untested overlay surface) →** new direct unit
  test `overlay_data_clusters_serves_file_bytes_from_backing_tree_h3_2_part_3`
  covers cluster-boundary-straddling read, fully-outside-extent
  read (caller buffer preserved), and missing-backing-file
  (log-and-skip, caller buffer preserved). The function was
  previously only covered by hardware smoke.

Refactor surfaced one real bug the original code carried: the
old bitmap fast-path only checked `cluster >= first_cluster`,
so a file-data write to a cluster above the bitmap range with
a small `byte_in_cluster` could fall inside `bitmap_buf.len()`
and be silently swallowed. The new `BitmapTracker::owns_cluster`
bounds against `first_cluster + cluster_count`, and a new
regression test (`bitmap_tracker_does_not_capture_writes_to_higher_clusters`)
pins the fix. The
`exfat_power_cut_mid_write_recovery_discards_partial` harness
test caught the regression mid-refactor.

Tests: +2 regression tests
(`bitmap_tracker_does_not_capture_writes_to_higher_clusters`,
`overlay_data_clusters_serves_file_bytes_from_backing_tree_h3_2_part_3`).
Workspace total now 1013 pass / 0 fail. All gates green.

## Phase H3 — Write-side on hardware

H3.1 – H3.5 per `00-PLAN.md`. All ⏳.

## Phase 4 — RecentClips retention shim

| Inc | Deliverable | Status |
|---|---|---|
| 4.1 | `retention::filter` (mtime hide) | ✅ ccc7066 |
| 4.2 | Tesla-delete interception | ✅ db0a44b |
| 4.3 | Virtual free-cluster reporting | ✅ 778bde1 |
| 4.4 | TOML config + IPC reload | ✅ (pending commit) |

**Phase 4.1 (`ccc7066`):** new `teslafat::retention`
module — pure-logic hide-from-view filter. `decide(relative_path,
mtime, now, &policy) -> Decision { Show | Hide }` scopes
filtering to the `RecentClips/` subtree only (Sentry, Saved, and
top-level files always shown). Threshold is strict `age >`
(not `>=`) so the boundary is pinned by a regression test.
`apply(&mut tree, backing_root, now, &policy) -> ApplyStats`
walks a `BackingTree` recursively, drops hidden files, and
returns counters for observability.

Defensive behavior — pinned by tests:
- mtime in the future (clock skew) → age clamped to zero, file
  shown
- `Duration::MAX` policy → no file ever hidden
- `Duration::ZERO` policy → any file with mtime < now hidden
  (boundary `==` shown)
- substring match (`RecentClipsBackup/`) → not under
  `RecentClips/`, file shown
- nested path (`RecentClips/<event>/<file>.mp4`) → filter applies
  by first path component regardless of depth
- `backing_path` not under `backing_root` → file kept visible
  (better to show a confusing entry than to silently delete on
  a walker bug)
- empty directories preserved (Tesla UI handles them fine; the
  alternative could confuse Tesla on remount)

All time inputs are explicit so tests use a frozen `SystemTime`
constant — no `Clock` trait, no `mockall`, no `tokio::time::pause`.
Wiring into the synth deferred to Phase 4.3.

Tests: 15 new (4 surface invariants for `decide`; 8 semantic
cases; 3 `apply` end-to-end + 1 pathological). Workspace total
1013 → 1027 (+14, one new lib test was a config test that
already existed). All gates green.

**Phase 4.2 (`db0a44b`):** Tesla-delete interception. New
`retention::DeletedSet` — `HashSet<PathBuf>` of relative paths
Tesla has marked deleted via directory-entry mutation
(FAT32: SFN leading byte rewritten to `0xE5`; exFAT: File-entry
`InUse` bit cleared, modeled in tests by a full dir-cluster
zeroing which the redecode treats identically).

Behavior change: both `ExfatWriteState::handle_child_deleted`
and `Fat32WriteState::handle_child_deleted` no longer call
`DirTreeWriter::unlink`. Instead they:

- discard any in-flight `.partial` companion (still safe — that
  was uncommitted bytes for *this* generation of the dir entry)
- free the cluster_map extent (the kernel reused those clusters;
  keeping the extent would mis-route a future write that
  allocates into the same clusters)
- drop the in-flight tracker
- record the path in the `DeletedSet`

`handle_child_seen` calls `DeletedSet::forget(path)` when Tesla
re-creates a file at a previously-deleted path, so the cleanup
worker doesn't reap a now-live file.

Both write states expose `pub fn deleted(&self) -> &DeletedSet`
for the Phase 4b cleanup worker. The set is also wired into the
Phase 4.4 IPC snapshot (deferred).

Existing integration tests `fat32_deletion_removes_backing_file_after_finalize`
and `exfat_deletion_removes_backing_file_after_finalize` were
renamed to `*_keeps_backing_file_and_records_retention` and
inverted to assert the new contract.

Tests: 6 new (4 lib unit tests for `DeletedSet`; 2 new state-machine
tests asserting backing-file preservation + retention mark + mark
clearing on re-create) + 2 integration test inversions. Workspace
total 1027 → 1037 (+10). All gates green. Pre-existing rustdoc
breakages in `teslausb-core/src/fs/exfat/parse.rs` (FAT\[0\]/\[1\]
bracket escapes) and `teslafat/src/backend/fat32_write.rs`
(`FileExtent` intra-doc link) fixed in the same commit per the
charter "fix bugs as you find them" rule.

**Phase 4.3 (pending commit):** Virtual free-cluster reporting.
`SynthBackend::open` now applies `retention::apply(&mut tree, ...)`
between `backing_walker::walk` and the FS-specific layout
planner. Hidden files (top-level `RecentClips/` aged past
`recentclips_hide_after_seconds`) are dropped from the
`BackingTree` before any cluster is allocated to them — they
don't appear in directory listings, don't claim cluster numbers,
and their clusters reflect as free in the FAT32 FSInfo
`FSI_Free_Count` and the exFAT allocation bitmap because the
layout planner never sees them. The backing files persist on
disk until Phase 4b's cleanup worker decides whether to reap
them.

Operator-facing knob `recentclips_hide_after_seconds = 0` is
explicitly translated to `Duration::MAX` (retention disabled)
to avoid the footgun of `Duration::ZERO`'s "hide everything in
the past" semantic. Documented in `RetentionConfig` and pinned
by a regression test.

Runtime deletes (`handle_child_deleted` from Phase 4.2) free
their clusters via the existing Phase 3.5f read-after-write
overlay — the kernel writes FAT entries / bitmap bits when it
deletes the dir entry, and the overlay reflects those writes
back to Tesla on the next read. No new free-cluster reporting
path is needed for runtime deletes; the synth + overlay already
agree.

Tests: 5 new at `backend::synth::tests::retention_*` — aged
RecentClips hidden in both FAT32 and exFAT layouts, Sentry/Saved
never hidden regardless of age, `0 = disabled` knob honored,
hidden file's first cluster proven absent from the layout's
extent table. Workspace total 1037 → 1042 (+5). All gates green.

**Phase 4.4 (pending commit):** TOML + IPC runtime reload of
the retention threshold.

Wire format (`teslausb-core::ipc::messages`): two new variants on
the existing tagged-JSON envelope —
`Request::ReloadRetention { hide_after_seconds: u64 }` and
`Response::RetentionReloadAck { hide_after_seconds, hidden, shown }`.
The ack echoes the threshold so async clients can correlate without
keeping state.

Backend hook (`teslafat::backend::synth::SynthBackend`):
`reload_retention(&self, hide_after_seconds) -> ReloadStats`
re-walks the backing tree (path captured at construction time),
applies the new retention policy via the shared
`build_retention_policy` helper (so `0 = disabled` semantic is
honored identically with `open`), and atomically shrinks the live
extent table by retaining only the files that survived the new
filter. The extent table is now wrapped in `RwLock<Vec<FileExtent>>`
so the runtime swap is safe against concurrent NBD reads.

Scope note: this is a real (not stub) runtime reload, but it is
intentionally **partial** — it stops hidden files from overlaying
their content on reads (clusters return synth zeros instead) but
does not re-render the FAT/bitmap/directory entries the synth
pre-built at `open` time. The full layout swap requires a
host-level `Arc<dyn BlockBackend>` `ArcSwap` that lands with the
Phase 1.5 daemon dispatcher; the returned `ReloadStats` reflect
what a full re-open WOULD show so operators can confirm the new
threshold's scope before deciding whether to restart. This
limitation is documented inline on `reload_retention`.

Tests: 6 new — 3 IPC wire-format round-trip tests
(`reload_retention_round_trips_via_json`,
`retention_reload_ack_round_trips_via_json`,
`reload_retention_zero_seconds_round_trips`) and 3 backend
integration tests (`reload_retention_with_stricter_threshold_drops_aged_extents`,
`reload_retention_with_zero_disables_filter`,
`reload_retention_returns_walk_error_if_root_removed`).
New `SynthBackendError::LockPoisoned` variant covers the
unreachable-in-practice case where another thread panics holding
the `RwLock` write guard. Workspace total 1042 → 1048 (+6). All
gates green (fmt, clippy `-D warnings`, doc `-D warnings`, full
test suite).

## Phase 4b — Cleanup + indexer-driven preservation (Rust)

| Inc | Deliverable | Status |
|---|---|---|
| 4b.1a | `teslausb-core::sei::{nal,mp4}` — AVCC NAL iterator, emulation-prevention strip, BMFF box scanner, mvhd extractor | ✅ |
| 4b.1b | `teslausb-core::sei::payload` — H.264 SEI envelope + Tesla 0x42-padding/0x69-marker framing | ✅ |
| 4b.1c | `teslausb-core::sei::tesla` — protobuf demarshal + `SeiMessage` + v1 golden parity | ✅ |
| 4b.1z | `teslausb-worker::sei` — clip walker that drives 4b.1a/b/c against real files | ✅ |
| 4b.2a | `teslausb-worker::config` + `teslausb-worker::store` — TOML loader + SQLite-backed clip/waypoint store (ADR-0010) | ✅ |
| 4b.2b | `teslausb-worker::watcher` + `teslausb-worker::indexer` — inotify clip watcher + bootstrap/event indexer (ADR-0011) | ✅ |
| 4b.3 | `teslausb-worker::cleanup` (GPS-aware deletion, rustix-statvfs pressure floor, path-traversal defense, store split, ADR-0012) | ✅ |
| 4b.4 | `teslausb-worker::main` task supervisor (tokio current-thread, ADR-0013) | ✅ |
| 4b.5 | `teslausb-worker.service` systemd unit + sample TOML | ✅ |

### Phase 4b.1a — SEI byte-level foundations ✅

Pure-logic primitives the rest of Phase 4b builds on, ported
line-for-line from v1's `scripts/web/services/sei_parser.py`
(saved verbatim under the session-state files dir for traceability).

* `teslausb-core::sei::nal` (~400 LOC, 15 tests):
  - `AvccIter` — iterator over NAL units in an AVCC byte buffer
    (4-byte big-endian length prefix; Tesla's `lengthSizeMinusOne
    = 3` is universal across HW3/HW4). Stops silently on
    zero-length padding (matches v1's `nal_size < 1: break`);
    one-shot errors on truncated prefix or length overrun.
  - `NalUnit { nal_type, payload }` — `nal_type` pre-extracted
    (`payload[0] & 0x1F`) for branch-friendly filtering against
    the SEI / IDR / non-IDR-slice constants.
  - `strip_emulation_prevention(&[u8]) -> Cow<'_, [u8]>` — H.264
    `0x000003` removal. Borrowed fast path when no preventions
    are present (the common case for short non-zero-prone payloads);
    Owned otherwise. State machine matches v1 line-for-line
    (`zeros >= 2 and byte == 0x03 → drop`).
* `teslausb-core::sei::mp4` (~450 LOC, 16 tests):
  - `find_box(buf, start, end, name) -> Result<BoxRef, Mp4Error>`
    — sibling-level BMFF box scan. Handles 32-bit sizes,
    extended 64-bit sizes (`size == 1`), and "to end of
    container" (`size == 0`). Rejects truncated headers,
    truncated extended sizes, oversized boxes, and
    size-smaller-than-header malformations explicitly — refusing
    to silently skip past corruption matches v1's `_find_box`
    behaviour.
  - `find_box_path(buf, &[*b"moov", *b"trak", *b"mdia", *b"mdhd"])`
    — convenience descent.
  - `parse_mvhd(body) -> Result<Mvhd, Mp4Error>` — decodes the
    Movie Header. Handles version 0 (32-bit times at body
    offset 4) and version 1 (64-bit times at body offset 4).
    Converts MP4-epoch (1904-01-01 UTC) seconds to Unix
    `SystemTime`; rejects creation_time ≤ epoch offset (the
    "uninitialised GPS clock" guard from v1).

**Charter compliance:** no `tokio`, no `std::fs`, no syscalls —
fits the `teslausb-core` layering rule. Indexing operations are
guarded by explicit length checks earlier in the same arm and
file-scoped `#[allow(clippy::indexing_slicing)]` follows the
project pattern already established in `fs::exfat::boot_sector`
and friends. Every error variant and public type carries doc
comments; every byte-level invariant pinned by tests.

**Test count:** workspace 1048 → 1079 (+31 sei tests).

### Phase 4b.1b — SEI envelope + Tesla framing ✅

`teslausb-core::sei::payload` (~500 LOC, 17 tests). Sits on
top of 4b.1a's `nal` + `mp4` and turns a raw SEI NAL unit into
the protobuf-ready byte slice.

* **`extract_tesla_payload(&[u8]) -> Result<Cow<'_, [u8]>, SeiError>`**
  — the fast path that matches v1's `_decode_sei_nal`
  byte-for-byte. Skips the first 3 NAL bytes (header + SEI
  type + size), scans the variable 0x42 padding run, asserts
  the 0x69 marker, slices `[i+1 .. len-1]` to drop the 0x80
  RBSP trailing byte, and routes through
  [`super::nal::strip_emulation_prevention`]. Returns
  `Cow::Borrowed` when no emulation-prevention triples were
  present (common); `Cow::Owned` when 0x03 stripping was
  needed.
* **`parse_h264_sei_envelope(&[u8]) -> Result<Vec<Sei<'_>>, SeiError>`**
  — the spec-correct ITU-T H.264 §7.3.2.3.1 envelope decoder
  for multi-payload SEI NALs. Handles the `0xFF`-chain encoding
  for both `payload_type` and `payload_size`; rejects overflow
  and truncated fields with typed errors. Currently unused by
  the Tesla fast path; provided so future non-Tesla SEI parsing
  (e.g. cleanup-time CEA-608/708 closed captions) does not
  have to re-discover the envelope rules.
* Typed [`SeiError`] variants: `TooShort` (NAL < 7 bytes —
  tighter than v1's `< 4` lax guard), `UnexpectedEnd`,
  `NoTeslaPadding`, `MissingProtobufMarker { found }`,
  `EnvelopeFieldOverflow`. v1 collapses all framing failures
  to `return None`; the indexer (4b.2) will use the typed
  errors to distinguish Tesla-vs-non-Tesla SEI in logs.
* Public constants: `SEI_PAYLOAD_TYPE_USER_DATA_UNREGISTERED`
  (= 5), `TESLA_PADDING_BYTE` (= 0x42),
  `TESLA_PROTOBUF_MARKER` (= 0x69), `RBSP_TRAILING_BYTE`
  (= 0x80). Documented so the indexer can filter without
  re-importing the envelope decoder.

**Charter compliance:** no `tokio`, no `std::fs`, no syscalls.
Lints addressed in-place (no deferral): file-scoped
`#[allow(clippy::indexing_slicing)]` (every index is preceded
by an explicit `len < N` or `i + N >= len` guard within the
same arm) + per-test allows for `unwrap_used` / `panic` /
`expect_used` matching the project pattern from
`fs::exfat::lazy_load::tests`.

**Test count:** workspace 1079 → 1096 (+17 tests). 31 + 17 + 3
deny-warning-only doc tests = 51 reported by
`cargo test -p teslausb-core --lib sei`.

### Phase 4b.1c — Tesla SeiMetadata protobuf decoder ✅

`teslausb-core::sei::tesla` (~650 LOC, 27 tests). Hand-rolled
minimal protobuf wire decoder targeted at the frozen
`SeiMetadata` schema in v1's `scripts/web/static/dashcam.proto`.

**Why hand-rolled instead of `prost`:** the Pi cross-build
podman image does not carry `protoc`, and adding the
`prost-build` build dep would force every contributor to install
the protobuf compiler. `dashcam.proto` is a 16-field, no-nesting,
no-repeated, frozen schema; a hand-roll is ~250 LOC of pure
Rust with no build script, no codegen, and no new workspace
deps. (Per charter: avoiding a third-party dep removes the need
for an ADR entirely, and "the hard right" — owning the wire
decode — is more transparent than a generated 100k-line
codegen pipeline for a single 16-field message.)

* **`SeiMessage`** struct — 16 fields matching the proto schema
  one-for-one (u32 / u64 / f32 / f64 / bool / Gear /
  AutopilotState). `Default` follows proto3 semantics
  (zero-valued numerics, `Park`, `None`). Public `has_gps_fix()`
  helper matches v1's "lat ≠ 0 OR lon ≠ 0" preserve-clip
  heuristic.
* **`Gear`** / **`AutopilotState`** enums with `Unknown(u32)`
  catch-alls so a future Tesla firmware revision adding new
  enum integers does not crash the decoder. Each has a `From<u32>`
  impl.
* **`decode_sei_message(&[u8]) -> Result<SeiMessage, ProtoError>`**
  — the wire decoder. Supports the four proto3 wire types
  (varint / fixed32 / fixed64 / length-delimited). Skips
  unknown field numbers AND unknown-wire-type-on-known-field
  silently per proto3 forward-compat. Rejects
  `VarintTooLong` (> 10 bytes), `UnexpectedEnd`,
  `LengthOverflow`, `UnknownWireType` (3 = deprecated group
  start, 4 = group end, etc.), and `InvalidFieldNumber`
  (field 0 is reserved).
* Internal `Cursor<'a>` keeps the read-varint /
  read-fixed-N / skip-field state machine in one place with
  panic-free `.get()`-based access (no `[..]` indexing in the
  hot path).

**Charter compliance:** pure logic, no `tokio`, no `std::fs`,
no syscalls, no new deps (so no ADR needed per charter
"locks in a new third-party dependency" trigger). Each enum
unknown-variant carry-through documented; every wire-type
guard pinned by tests.

**Test count:** workspace 1096 → 1123 (+27 tests). Total SEI
suite: 78 tests across 4b.1a/b/c.

### Phase 4b.1z — `teslausb-worker::sei` clip walker ✅

I/O adapter that drives the pure-logic `teslausb-core::sei`
primitives against on-disk Tesla MP4 clips. New library face
on `teslausb-worker` (lib.rs) so unit tests run against a
stable public surface (same pattern as `teslafat`).

**New in `teslausb-core::sei::mp4`** (11 added tests):

* `Mdhd` struct + `parse_mdhd` — Media Header timescale +
  duration, v0 and v1 layouts.
* `parse_stts_durations` — expands the (count, delta) sample-
  to-time table into a flat `Vec<f64>` of per-sample ms
  durations. Caps at `STTS_MAX_ENTRY_COUNT = 50_000` declared
  entries and `STTS_MAX_TOTAL_SAMPLES = 10_000` emitted samples
  (v1 parity — bounds RSS on the Pi Zero 2 W).
* Two new `Mp4Error` variants: `MdhdTruncated`, `SttsTruncated`,
  `SttsEntryCountSuspicious`.

**New in `teslausb-worker::sei`** (17 tests):

* `walk_clip(path, sample_rate) -> Result<ClipWalk, WalkError>`
  — public API. Reads the file (`std::fs::read`, capped at
  `MAX_CLIP_BYTES = 150 MB` matching v1) and drives the walker.
* `walk_clip_bytes(buf, sample_rate)` — same logic against a
  pre-loaded buffer. Used by the tests and any future caller
  that already has the bytes in RAM.
* `Waypoint { frame_index, timestamp_ms, message }` — one
  decoded SEI sample with its frame-accurate ms offset.
* `ClipWalk { clip_started_utc, timescale, frame_count, waypoints }`
  — walk result. `clip_started_utc` is best-effort from `mvhd`
  (None on missing / pre-epoch value); the walker tolerates
  missing timing tables by falling back to a 30 fps default.
* `WalkError` (thiserror) — `Io`, `FileTooSmall`, `FileTooLarge`,
  `Mp4`. Per-frame decode failures (corrupt NAL, non-Tesla SEI,
  garbled protobuf) are silently dropped — matches v1's
  `if sei is not None` filter.

**Why `std::fs::read` and not `mmap`:** v1 used mmap to escape
OOM when *multiple concurrent* indexer/archive ops stacked
~60 MB clip buffers. B-1's supervisor runs the walker strictly
one-clip-at-a-time, so worst-case RSS is one 60-150 MB clip —
fine on the Pi Zero 2 W's 512 MB. Avoiding `memmap2` keeps the
dep list short (no ADR needed) and dodges a small `unsafe`
surface where `Mmap` deref-to-`&[u8]` relies on the file not
being unmapped or truncated mid-walk (exactly what could
happen if `teslafat` reflows the backing store). If profiling
shows this matters, the `walk_clip` signature stays — only the
body changes, behind an ADR.

**Walker algorithm (v1 parity):** moov→mvhd → start time;
moov→trak→mdia→{mdhd, minf/stbl/stts} → timescale + durations;
mdat → `AvccIter` → on SEI NAL with `frame_index % sample_rate
== 0`: `extract_tesla_payload` then `decode_sei_message` and
yield a `Waypoint`; on VCL NAL (type 1 / 5): advance frame
counter and cumulative ms.

**Charter:** typed errors via thiserror (no `anyhow` outside
main.rs); pure logic stays in core; only the file-read I/O
boundary lives in worker. No new workspace deps (teslausb-core
+ thiserror were already approved). All gates green.

**Test count:** workspace 1123 → 1151 (+28 tests: 11 mp4 +
17 worker). Phase 4b.1 series (a+b+c+z) now totals 106 tests.

### Phase 4b.2a — worker config + SQLite indexer store ✅

First half of the indexer pillar. Stands up the worker's
config file and the SQLite-backed `clips` + `waypoints`
store the cleanup worker (4b.3) and the future web map
(Phase 5+) will query against.

**New deps** (worker `Cargo.toml`):

* `rusqlite = "0.31"` with `features = ["bundled"]` — see
  ADR-0010 for the rationale vs. JSON sidecars, redb, sled.
  Bundling SQLite C source keeps cross-build self-contained
  and the on-Pi binary apt-free at runtime.
* `anyhow`, `clap`, `serde`, `toml`, `tracing`,
  `tracing-subscriber` — needed now for the config loader
  (`anyhow::Result` at the binary boundary, `serde` + `toml`
  for the file format). The remaining ones are part of the
  worker's binary surface that 4b.4 will fill in; pulling
  them now keeps the dep set stable across 4b.

**New in `teslausb-worker::config`** (18 tests):

* `Config` (TOML, `deny_unknown_fields`) with three sections:
  `backing_root` + `db_path` at the top level, plus
  `[indexer]` (`sei_sample_rate`, `debounce_ms`) and
  `[cleanup]` (`interval_seconds`, `retention_days`,
  `min_free_pct`, `preserve_with_gps`).
* All fields have defaults that match v1 cadence (5-min
  cleanup, 1-day retention, 10% free floor, GPS-preserve on,
  sample-every-30th-frame, 1.5 s inotify debounce).
* `Config::load(&Path)` returns `anyhow::Result` with the
  offending file path attached via `.with_context(...)` —
  mirrors `teslafat::config`. `validate()` enforces
  semantic invariants (`retention_days ≤ 730`,
  `min_free_pct ≤ 100`, no-empty paths, positive intervals).

**New in `teslausb-worker::store`** (22 tests):

* `Store` wrapping `rusqlite::Connection`. WAL + foreign-keys
  pragmas set at open; in-memory variant skips WAL so tests
  do not need a tempdir.
* `Bucket` enum (`Recent`/`Saved`/`Sentry`) → stable DB
  strings (`"recent"`/`"saved"`/`"sentry"`). The enum makes
  it impossible for a cleanup query to typo a bucket name.
* `ClipRecord { id, relative_path, bucket, clip_started_utc,
  indexed_at_utc, waypoint_count, gps_waypoint_count }`.
  `has_gps()` derives the cleanup-worker's preserve check
  from the cached GPS waypoint count (no per-clip waypoint
  scan needed at delete time).
* `record_clip(bucket, relative_path, &ClipWalk)` — single
  transaction. UPSERTs the `clips` row (idempotent on path),
  wipes its existing waypoints, re-inserts the new set.
  Re-indexing a clip after a sample-rate change leaves no
  stale rows.
* `clip_by_path` / `knows_clip` / `clip_has_gps` /
  `list_clips_in_bucket_older_than` (with
  `COALESCE(clip_started_utc, indexed_at_utc)` fallback so a
  clip with a missing `mvhd` is not immortal) /
  `delete_clip_by_path` / `clip_count` / `waypoint_count`.
* Migration discipline: `MIGRATIONS: &[&str]` ordered list,
  `CURRENT_SCHEMA_VERSION` constant, `schema_version` row in
  a `meta` table. Reopen of a current-version DB is a no-op.
  Reopen of a future-version DB is rejected with
  `StoreError::SchemaTooNew` rather than silently corrupting
  data.

**Charter:** layering preserved — `rusqlite` is a Layer-3
adapter dep, lives only in `teslausb-worker`, never imported
by `teslausb-core`. Typed errors via thiserror at module
boundaries; `anyhow` only at the binary outer layer (the
config loader). All gates green.

**Test count:** workspace 1151 → 1191 (+40 tests: 18 config +
22 store). Phase 4b.1+4b.2a now totals 146 tests in the worker
crate.

### Phase 4b.2b — clip watcher + indexer service ✅

Closes out the indexer pillar. The watcher subscribes to
inotify CLOSE_WRITE/MOVED_TO on the three Tesla bucket
directories; the indexer glues watcher events + bootstrap
walks to the SEI walker (4b.1z) and the store (4b.2a).

**New dep** (worker `Cargo.toml`, Linux target only):

* `inotify = "0.10"` under `[target.'cfg(target_os = "linux")'.dependencies]`
  — direct `IN_CLOSE_WRITE | IN_MOVED_TO` mask avoids the
  "was the write complete?" race that polling or `notify`'s
  coarser `Modify` event would force. See ADR-0011.

**New in `teslausb-worker::watcher`** (7 host + 2 Linux-only
integration tests):

* `WatchEvent { bucket, path, kind }` and `WatchKind`
  (`CloseWrite` | `Moved`) — typed event the indexer
  consumes.
* `is_indexable(&Path) -> bool` — pure helper: accepts
  `*.mp4` (case-insensitive), rejects dotfiles and empty
  names. Unit-tested without an inotify FD.
* `event_to_bucket(&Path, &Config) -> Option<Bucket>` —
  maps an absolute event path back to its bucket by
  comparing against `config.bucket_root(b)`.
* `ClipWatcher` (Linux-only) — wraps `inotify::Inotify`,
  reads events in 4 KiB batches, filters via
  `is_indexable`, returns `Vec<WatchEvent>`. Non-Linux
  builds get a stub that returns `WatcherError::Unsupported`
  so the test suite compiles on developer workstations.
* `Bucket` gained `tesla_dir_name()` (e.g. `"RecentClips"`),
  `from_tesla_dir_name`, and `all()` so the watcher and
  indexer can iterate buckets without hard-coding the list.
* `Config::bucket_root(Bucket) -> PathBuf` returns
  `<backing_root>/TeslaCam/<dir>` — derived from the
  canonical Tesla layout, no config knob needed.

**New in `teslausb-worker::indexer`** (14 tests):

* `Indexer { config, store, last_handled }` — owns the
  store, tracks an in-memory debounce dictionary
  (`HashMap<PathBuf, Instant>`) capped at 8 192 entries
  (oldest half is evicted on overflow so long uptime can
  never leak memory).
* `Indexer::bootstrap()` — startup pass. Walks each bucket
  directory, indexes any `*.mp4` the store does not yet
  `knows_clip`. Returns a `BootstrapSummary { seen,
  indexed, skipped, failed }`. Per-clip parse failures log
  at WARN and continue (one bad clip cannot stall the
  daemon).
* `Indexer::handle_event(&WatchEvent)` — drains one event:
  filter via `is_indexable`, debounce via
  `config.indexer.debounce_ms`, walk via `walk_clip`,
  persist via `store.record_clip`. Returns `Ok(true)` on
  successful index, `Ok(false)` on filter / debounce /
  parse-failure, `Err` only on a store error.
* Store rows key on the *relative* path
  (`relative_to_backing_root` strips `backing_root` so a
  restored backup at a different mount point still
  matches).

**Charter:** `inotify` is Layer-3, lives in worker only and
behind a `cfg(target_os = "linux")`. Pure-logic helpers
(`is_indexable`, `event_to_bucket`, debounce dict) are
always compiled and unit-tested. Per-clip parse failures
are logged via `tracing::warn!`, never panicked. All gates
green.

**Test count:** workspace 1191 → 1212 (+21 host-test
deltas: 7 watcher + 14 indexer). Linux-only integration
tests add another +2 on the Pi.

### Phase 4b.3 — cleanup worker (GPS-aware deletion) ✅

Final increment of the indexer pillar. The cleanup module
sweeps `RecentClips` only, preserves any clip whose
indexed waypoints carry a real GPS fix (when
`preserve_with_gps = true`), and broadens the cutoff to
"now" when free-space drops below `min_free_pct`. Order of
operations is **store-first, file-second**: a failed unlink
leaves the file in place for re-indexing on the next
bootstrap pass, so the system converges rather than losing
ground.

**Free-space probe (rustix, not libc).** The Linux
free-space check goes through `rustix::fs::statvfs` — a
safe wrapper — so we don't need an `unsafe` block (would
fail the workspace `unsafe_code = "deny"` lint). Documented
in **ADR-0012**. The non-Linux build returns `100.0` as a
stub so dev-workstation tests never see synthetic pressure.

**Pressure-driven cutoff.** Extracted `effective_cutoff`
as a pure-logic helper so tests verify the broaden-cutoff
behaviour without freezing the clock or stubbing
`statvfs`. Under pressure the helper returns `now_unix_s`,
which causes `Store::list_clips_in_bucket_older_than` to
return every no-GPS `RecentClips` clip; the ASC ordering
on `clip_started_utc` then ensures oldest-first deletion.

**Path-traversal defense (security review).** Cleanup is
the only code path that deletes files, so it re-validates
every `relative_path` from the store before calling
`std::fs::remove_file`. The `safe_absolute_path` helper
rejects absolute paths, Windows prefix components, root
components, and any `..` segment; `.` segments are
harmless (normalized by `Path::components()`). A bad row
counts as `failed` and stays in the store; the file is
untouched. Five tests pin the behaviour, including one
end-to-end test that injects a `../victim.txt` row and
asserts the victim file outside `backing_root` is *not*
deleted.

**Store split (charter §1 god-module fix).** The original
`store.rs` was 1148 lines (674 prod SLOC), over the
charter's 500-line hard cap. Split into a `store/`
directory with one responsibility per file: `mod.rs`
(re-exports + module docs), `bucket.rs`, `types.rs`,
`schema.rs`, `helpers.rs`, `store_impl.rs`, `tests.rs`.
Every file is now under 500 lines (largest is
`store_impl.rs` at 377, with the `tests.rs` sibling at
481).

**Charter findings addressed in-band.** The pre-commit
charter-review on 4b.2+4b.3 flagged 3 Blockers + 2 Majors
— all fixed in this same commit rather than deferred:
Blocker #1 (`unsafe`-libc statvfs would fail Linux build)
became the rustix swap; Blocker #2 (`min_free_pct`
documented but unused) became the pressure logic; Blocker
#3 (1148-line `store.rs`) became the directory split;
Major #1 (easy libc over harder safe wrapper) was the
same as Blocker #1; Major #2 (new dep without ADR) became
ADR-0012.

**Test count:** workspace 1212 → 1253 (+41 host-test
deltas: cleanup module now 27 tests including 5 new
traversal-defense tests, 4 new pressure tests, 1
Linux-gated `statvfs` smoke test; store split kept all 22
existing store tests intact). Linux-only integration test
adds another +1 on the Pi.

### Phase 4b.4 — task supervisor ✅

Real `teslausb-worker::main` replaces the Phase 0.2
placeholder. The supervisor is a tokio current-thread
runtime that:

1. Loads the config + opens the SQLite store.
2. Runs the indexer bootstrap pass on the blocking pool.
3. Spawns the inotify clip watcher on a dedicated blocking
   thread (`Watcher::next_batch` parks on `read(2)`) that
   feeds a tokio `mpsc` channel.
4. Enters a `tokio::select!` loop over four arms:
   SIGTERM, SIGINT, watcher event, cleanup tick.
5. Translates the [`ShutdownReason`] into a process exit
   code so systemd `Restart=on-failure` does the right
   thing (zero on graceful shutdown, non-zero on subsystem
   failure).

**Why tokio?** ADR-0013 documents the trade-off vs. raw
`std::thread + mpsc` and `signal-hook`. `select!` + the
`tokio::signal::unix` handlers replace ~100 lines of
hand-rolled glue; the current-thread flavor keeps the
threadcount predictable (one reactor + one blocking-pool
thread for the watcher + occasional blocking-pool threads
for indexer bootstrap).

**Pure-logic carve-outs.** Two helpers are unit-testable
without standing up the reactor: `cleanup_interval_with_floor`
(defends against `interval_seconds = 1` typo with a 5 s
floor) and `ShutdownReason::is_fatal` (decides graceful
vs. error-exit). Tracing install is also a pure helper so
the binary's `main.rs` stays trivial.

**CLI surface.** `clap`-derived:
`teslausb-worker --config <path> [--bootstrap-only]`.
`--bootstrap-only` is an operator flag for verifying a
fresh deployment without entering the steady-state loop.

**Test count:** workspace 1253 → 1263 (+10 supervisor
tests: 3 interval-floor + 4 ShutdownReason + 1 channel
capacity invariant + 1 tracing idempotency + 1 missing-config
error + 1 Linux-gated bootstrap-only smoke test).

### Phase 4b.5 — systemd unit + sample config ✅

Wraps up Phase 4b. Two artifacts:

* `rust/crates/teslausb-worker/units/teslausb-worker.service`
  — `User=teslausb`, `After=teslafat@0.service`,
  `Restart=on-failure`. Same hardening template as
  `teslafat@.service`: empty `CapabilityBoundingSet`,
  `NoNewPrivileges`, `ProtectSystem=strict`,
  `PrivateNetwork=yes` (the worker uses no sockets in 4b),
  `RestrictAddressFamilies=` (empty), `@system-service`
  syscall filter. `ReadWritePaths=` is scoped to
  `/var/lib/teslausb` (SQLite store) and `/srv/teslausb`
  (backing tree); operators with non-default paths edit
  this one line. `Nice=10 IOSchedulingClass=idle` so the
  worker never disrupts a recording in progress.

* `rust/crates/teslausb-worker/examples/worker.toml` —
  documented sample of every config knob the loader
  understands, with defaults that match the unit's
  `ReadWritePaths`. Operators copy this to
  `/etc/teslausb/worker.toml` during install.

`main.rs` grew a `--check-config` flag wired to
`ExecStartPre=` so a malformed config surfaces in
journalctl as a fast-fail (exit 2) before the supervisor
opens the watcher / store. New regression test
`example_worker_toml_parses_and_validates` does
`include_str!` on the example so a typo in the file fails
the test suite, not the operator's deploy.

**Phase 4b complete.** All five increments (4b.1 SEI,
4b.2 indexer, 4b.3 cleanup, 4b.4 supervisor, 4b.5
unit+config) shipped on branch `b1-userspace-rust` with
charter-review findings addressed in-band. Next: hardware
phase H4 (deploy + smoke on `cybertruckusb.local`).

**Test count:** workspace 1263 → 1264 (+1 example TOML
round-trip; no new modules).

## Phase H4 — Retention + worker on hardware

H4.1 – H4.5 per `00-PLAN.md`. All ⏳.

## Phase 4c — Tesla cache invalidation ✅ COMPLETE

Single combined charter-review at end of phase per plan.md.
Charter-review fixes landed in commit `432f828`. Report:
`~/.copilot/session-state/.../files/charter-review-phase-4c.md`.

| Inc | Deliverable | Status | Review | Test |
|---|---|---|---|---|
| 4c.1 | `scripts/tesla_cache_invalidate.sh` (149 LOC, idempotent LUN clear/restore via configfs writes; `set -uo pipefail`; trap-based restore; exit codes 0/2/3/4/5 documented in --help; shellcheck `-S warning` clean) — commit `ba07097` | ✅ | ✅ APPROVED (charter-review Major #2 found arg-missing returned bash's exit 1 unbound-var instead of documented exit 2; fixed in `432f828` via `require_value` helper + 4 regression integration tests; Major #1 found script committed `100644` instead of `100755`, fixed in `432f828` via `git update-index --chmod=+x`; report at `~/.copilot/session-state/.../files/charter-review-phase-4c.md`) | ✅ shellcheck `-S warning` clean; 15 integration tests pass on Linux (podman `python:3.11-slim`); cycle/idempotent/dry-run/empty-LUN/missing-gadget/missing-value paths all covered |
| 4c.2 | `etc/sudoers.d/teslausb-cache-invalidate` (36 LOC, NOPASSWD pinned to zero-args via trailing `""`, `Defaults!cmd env_reset, !requiretty`, visudo-validated against debian:bookworm-slim sudo 1.9.13p3) + `scripts/check.sh` shellcheck gate wired into `--hygiene` (skips with WARN if shellcheck missing) + 5 pre-existing SC2164 unchecked-`cd` warnings fixed across `check.sh` — commit `1bb93ee` | ✅ | ✅ APPROVED (no new findings; sudoers `""` syntax + `sudo -n` in DEFAULT_COMMAND prevent argv injection and password-prompt hangs) | ✅ `visudo -c` parsed OK; shellcheck `-S warning` clean across all 3 tracked `.sh` files; gate skips cleanly if shellcheck not installed |
| 4c.3 | `web/teslausb_web/services/cache_invalidation.py` (242 LOC, `CacheInvalidator` dataclass with `schedule()` debounce + `invalidate_now()` sync bypass + `shutdown()` + context manager; single-flight via `_in_flight` + `_pending` flags; `DEFAULT_COMMAND = ("sudo", "-n", "/usr/local/bin/tesla_cache_invalidate.sh")` locked to sudoers fragment; ruff/mypy --strict/vulture/bandit clean) — commit `88ebd7b` | ✅ | ✅ APPROVED (charter-review Minor #1 found `invalidate_now()` could overlap an in-flight `_fire()` cycle; tightened in `432f828` to acquire `_in_flight` slot with bounded busy-wait, subprocess still runs outside the lock; Minor #2 reworded misleading `# noqa: S603` rationale) | ✅ see 4c.4 |
| 4c.4 | `web/tests/test_cache_invalidation.py` — 13 unit tests, centerpiece `test_five_rapid_calls_coalesce_to_one`; covers in-flight drain via `threading.Event`-pinned `subprocess.run`, shutdown, timeout, missing-binary, nonzero-exit, context manager; 100% coverage on `cache_invalidation.py` — commit `88ebd7b` (+ `test_invalidate_now_waits_for_in_flight_fire` added in `432f828` for Minor #1 regression) | ✅ | ✅ APPROVED (single-flight invariant now has dedicated test exercising both `schedule()` and `invalidate_now()` thread contention) | ✅ pytest 16 passed 0 failed; coverage 100% on cache_invalidation.py (98 stmts, 16 branches, 0 missed); strict-markers + strict-config |
| 4c.5 | `web/tests/test_cache_invalidation_integration.py` (215 LOC, 11 tests originally) — spawns real `bash <SCRIPT>` with `CONFIGFS_ROOT` env redirected to tmpdir; module-level `pytest.mark.skipif(sys.platform == "win32")` because WSL bash.exe corrupts paths; covers --help, unknown flag, non-integer args, real cycle, dry-run, idempotent, empty-LUN, custom gadget/function names, executable bit — commit `1643588` (+ 4 missing-value regression tests added in `432f828` for Major #2; total 15 tests) | ✅ | ✅ APPROVED (executable-bit test strengthened in `432f828` to assert `S_IXUSR` on POSIX; falls back to readable-only on Windows where exec bit is synthetic) | ✅ pytest 15 passed on Linux (podman `python:3.11-slim`); 15 skipped on Windows dev box (module-level win32 skip); all 28 unit+integration tests pass on Linux |

**Phase 4c summary:** 5 increments shipped in 4 commits (`ba07097`, `1bb93ee`, `88ebd7b`, `1643588`) plus 1 charter-review-fix commit (`432f828`). Tracked-files delta: 4 new files, 2 modified (`scripts/check.sh` + `web/pyproject.toml` not touched — check.sh hygiene gate is now real). Gates green on Windows dev box (16/15 pytest) + Linux podman (28/0 pytest). Hardware verification deferred to Phase H4c (post-Phase-6) per operator directive: "finish phases 4 and 5 and 6 and then test and debug on the hardware."


## Phase H4c — Cache invalidation on hardware

H4c.1 – H4c.6 per `00-PLAN.md`. All ⏳.

## Phase 5 — Python web app (Flask, UI only)

Each increment ends with charter-review + (for blueprints/templates)
a screenshot diff vs. v1 baseline.

| Inc | Deliverable | Status |
|---|---|---|
| 5.1 | Copy `UI_UX_DESIGN_SYSTEM.md` from v1 (doc-only) | ✅ `af71ed0` — ported from `main@75bfca0` as `docs/05-UI-UX-DESIGN-SYSTEM.md`; B-1 preamble documents 7 mode-removal edits (mode tokens renamed → samba-on/off, mode_token references stripped from Status Indicator + Information Architecture + Desktop Sidebar sections). Inline review only — doc-only increment. |
| 5.2 | Flask app skeleton + factory + gunicorn entry | ✅ `bd3fef9` — `web/teslausb_web/{config.py,app.py,wsgi.py}` (242 + 171 + 24 LOC). Frozen-dataclass TOML config tree (`WebConfig`/`WebSection`/`PathsSection`/`FeaturesSection`); load-order: explicit > `TESLAUSB_WEB_CONFIG` env > `/etc/teslausb/teslausb-web.toml` > defaults (`allow_defaults=True` only). Factory wires ENOSPC→413 handler, `/tile-cache-sw.js` SW, `/healthz`. 40 new tests (22 config + 18 factory). All gates green: ruff/ruff-format/mypy --strict/vulture/bandit clean; pytest 60p 15s; coverage 99.18%. Charter-review: `~/.copilot/session-state/.../files/charter-review-phase-5.2.md`. |
| 5.3 | Static assets port (fonts, SVGs, CSS, JS) | ✅ `c868a34` — bulk byte-identical copy of v1 `main:scripts/web/static/` (23 files: Inter woff2, Lucide sprite, tile-cache SW, 5 CSS, 5 JS, Chart.js, Leaflet bundle, 3 PNG markers, dashcam.proto). SHA-256 verified against `git show main:...` on a representative sample (woff2/svg/js/css/png). New `tests/test_static_assets.py` (29 cases): existence + magic-byte checks + Flask-routing smoke test. Adjusted 5.2's missing-tile-cache test (file now exists). Gates: ruff/format/mypy/vulture/bandit clean; pytest 89p 15s; coverage 99.18%. Charter-review: `~/.copilot/session-state/.../files/charter-review-phase-5.3.md`. |
| 5.4 | Templates skeleton (base.html, partials, theme) | ✅ `3869717` — base.html ported from `main:scripts/web/templates/base.html` (368 LOC v1) with 5 mode-removal edits (B-1 preamble + Samba-only status dot + `mode_control.index`→`settings.index` ×3 + health-poll comment rewrite); media_hub_nav.html partial copied verbatim. New `blueprints/_scaffold.py` (65 LOC) registers placeholder blueprints for `mapping`/`analytics`/`media`/`cloud_archive`/`settings` so `url_for()` resolves before per-feature increments land. `app.py` extended with `_register_template_globals` context processor supplying 16 conservative defaults for every flag base.html reads. 13 new tests (167 LOC) verify scaffold registration, endpoint resolution, base.html rendering with samba on/off, mode-removal contract, extras-override-scaffold pattern. Gates: ruff/format/mypy --strict/vulture/bandit clean; pytest 102p Win + 117p Linux podman; coverage 99.03%. Charter-review: `~/.copilot/session-state/.../files/charter-review-phase-5.4.md`. |
| 5.5 | `services/teslafat_client.py` IPC | ✅ `b2a17db` — ADR-0014 pins NDJSON-over-AF_UNIX framing with 64 KiB envelope cap; `services/teslafat_messages.py` (~200 LOC) provides frozen-dataclass wire types mirroring Rust `ipc::messages` (PROTOCOL_VERSION=1, Envelope, StatusBody, RetentionAck/Failure/ReloadAck, ErrorBody, RetentionUpdate/Extend) + `serialise_envelope`/`parse_envelope` with version+id+payload validation; `services/teslafat_client.py` (~350 LOC) implements `TeslaFatClient` with `status()`/`invalidate_cache()`/`reload_retention()`/`update_retention()`, `RetryPolicy` bounded exponential backoff, typed `IpcDaemonError`, fully-injectable seams (`socket_factory`, `sleep`, `id_generator`) for testability. 27 new tests (~430 LOC) cover wire round-trip + version/id/payload rejection + happy paths + retry-on-ConnectionRefused + no-retry-on-protocol-error + 64 KiB framing cap on both in/out + partial-read reassembly + EOF handling + daemon ERROR → typed exception. Gates: ruff/format/mypy --strict/vulture/bandit clean; pytest 129p Win (15 AF_UNIX-skipped) + 144p Linux podman; coverage 92.81%. Charter-review: `~/.copilot/session-state/.../files/charter-review-phase-5.5.md` (APPROVED WITH NITS, 0/0/2/1). |
| 5.6 | Register `services/cache_invalidation.py` (built in 4c.3) | ✅ `e2b7e00` — `create_app` instantiates one `CacheInvalidator` per worker (command = `("sudo", str(cfg.paths.cache_invalidate_script))` matching the Phase 4c.2 sudoers fragment), stashes it on `app.extensions["cache_invalidator"]`, and registers `invalidator.shutdown` via `atexit` so pending timers drain on graceful shutdown. 3 new tests (~50 LOC) verify registration, configured-path threading, and atexit wiring (via monkeypatching `teslausb_web.app.atexit.register`). Gates: ruff/format/mypy --strict/vulture/bandit clean; pytest 132p Win + 147p Linux podman; coverage 92.88%. Charter-review: `~/.copilot/session-state/.../files/charter-review-phase-5.6.md` (APPROVED, 0/0/0/0). |
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
