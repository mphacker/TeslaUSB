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
| 0.2 | Cargo workspace at `rust/` (`Cargo.toml`, `rust-toolchain.toml`, `deny.toml`, empty crates `teslausb-core`, `teslafat`, `teslausb-worker`, each with `[lints]` per charter) | ⏳ | ⏳ | ⏳ |
| 0.3 | Python skeleton `web/teslausb_web/` with `pyproject.toml` (ruff + mypy + pytest per charter) | ⏳ | ⏳ | ⏳ |
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
| H0.1 | tar snapshot of /etc + ~/TeslaUSB, scp off-device | ⏳ |
| H0.2 | systemctl stop v1 services (6 of them, in order) | ⏳ |
| H0.3 | systemctl disable v1 services | ⏳ |
| H0.4 | Reboot — verify boot + WiFi + SSH | ⏳ |
| H0.5 | systemctl mask v1 services | ⏳ |
| H0.6 | rm v1 systemd unit files at /etc/systemd/system/ | ⏳ |
| H0.7 | rm v1 sudoers drop-ins (preserving non-teslausb entries) | ⏳ |
| H0.8 | rm v1 NetworkManager dispatcher scripts | ⏳ |
| H0.9 | Comment out v1 cmdline.txt + config.txt entries (with .b1-backup) | ⏳ |
| H0.10 | rm -rf ~/TeslaUSB + ~/ArchivedClips (after operator confirm) | ⏳ |
| H0.11 | Disable smbd + nmbd | ⏳ |
| H0.12 | Disable v1's watchdog.service | ⏳ |
| H0.13 | Reboot — verify clean baseline | ⏳ |
| H0.14 | Capture clean-boot journal as reference baseline | ⏳ |

**🔍 REVIEW GATE:** charter-review on `scripts/hw/` helper +
the H0 step-script. **✅ TEST GATE:** all H0.x green via the
`hardware-test` skill.

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