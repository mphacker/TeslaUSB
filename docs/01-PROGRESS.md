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
| 0.4 | `.github/workflows/ci.yml` mirroring charter §"CI Gates" | ⏳ | ⏳ | ⏳ |
| 0.5 | `.pre-commit-config.yaml` mirroring CI gates locally | ⏳ | ⏳ | ⏳ |
| 0.6 | `setup-dev.sh` (idempotent Rust + Python + tools install on a dev box) | ⏳ | ⏳ | ⏳ |
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
| 1.1 | `teslafat::main` CLI + tracing + TOML loader | ⏳ | ⏳ | ⏳ |
| 1.2 | `teslausb-core::ipc::messages` types + serde tests | ⏳ | ⏳ | ⏳ |
| 1.3 | NBD newstyle handshake (port from existing draft) | ⏳ | ⏳ | ⏳ |
| 1.4 | `BlockBackend` trait + null impl + FUA contract test | ⏳ | ⏳ | ⏳ |
| 1.5 | NBD transmission loop + FUA fdatasync test | ⏳ | ⏳ | ⏳ |
| 1.6 | `teslafat@.service` systemd unit | ⏳ | ⏳ | ⏳ |
| 1.7 | Dev-box smoke test harness | ⏳ | ⏳ | ⏳ |

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
| H1.1 | Cross-build teslafat for armv7 | ⏳ |
| H1.2 | scp to /home/pi/teslausb-b1/bin/ | ⏳ |
| H1.3 | Install teslafat-test@.service (NOT production name) | ⏳ |
| H1.4 | systemctl start teslafat-test@0 | ⏳ |
| H1.5 | nbd-client connects + handshake completes | ⏳ |
| H1.6 | blockdev --getsize64 returns non-zero | ⏳ |
| H1.7 | Teardown | ⏳ |
| H1.8 | SSH + WiFi liveness final check | ⏳ |

**🔍 REVIEW GATE on the H1 script + journal.**
**✅ TEST GATE:** all H1.x green via `hardware-test`.

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
