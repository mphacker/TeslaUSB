# TeslaUSB B-1 — Implementation Plan

**Branch:** `b1-userspace-rust`
**Base:** `main` @ `75bfca0`
**Started:** 2026-05-19
**Architecture:** Rust-first userspace exFAT/FAT32 synthesizer over NBD,
with all hot-path and background-CPU work in Rust. Python is retained
only for the user-facing web UI (where UI parity is a binding
constraint and the framework underneath does not affect any user-visible
performance metric). Eliminates the byte-copy I/O problem permanently
AND eliminates the Python-GC/RAM/SDIO contention class of bugs.

---

## Guiding principles (post-anti-anchoring)

> *Operator directive (2026-05-19):* **"Don't feel locked into the
> original way of doing things. We want to find the best way to
> achieve the goals and requirements of this project. Don't feel we
> need to use the same config files, or approaches."*

> *Operator directive (2026-05-19):* **"If something could work
> faster in Rust vs Python, do it in Rust. Don't worry about
> potential regression. This is a new build. I want the best
> outcome."*

These directives REPLACE any "port v1 verbatim" inclination in this
plan. Default for every component:

1. If it is on a latency-sensitive path (NBD I/O, SCSI ACK, USB
   gadget responsiveness) → **Rust**.
2. If it is CPU-bound and runs frequently (SEI parsing, file-tree
   walking, FAT encoding/decoding) → **Rust**.
3. If it is memory-pressured (would hold > 20 MB in Python due to
   interpreter overhead alone) → **Rust**.
4. If it is glue-code that just shells out to another binary
   (rclone wrapper) → **Rust** (so we have ONE language for the
   daemon side).
5. If it is the **user-facing web UI** and UI parity with v1 is
   binding (it is) → **Python Flask**, because the templates,
   static assets, and blueprint URL contracts port one-to-one
   and the web request rate is so low that runtime language is
   noise.

There is no "we already have v1 Python that works" carry-forward
argument. v1 code is reference material, not a starting point.

---

## North-star architecture

```
┌──────────┐
│  Tesla   │  (sees TWO USB drives, one for dashcam, one for media)
└────┬─────┘
     │ USB SCSI BBB
     ▼
┌──────────────────────────────────────────────────────┐
│  f_mass_storage  (configfs, no `g_mass_storage`      │
│                   module — pure configfs)            │
│  lun.0/file = /dev/nbd0    ← TeslaCam drive          │
│  lun.1/file = /dev/nbd1    ← Media drive             │
└──────┬────────────────────────────┬──────────────────┘
       │ NBD                        │ NBD
       ▼                            ▼
┌──────────────────────┐  ┌──────────────────────┐
│ teslafat-0 (Rust)    │  │ teslafat-1 (Rust)    │
│ /run/teslausb/       │  │ /run/teslausb/       │
│   teslafat-0.sock    │  │   teslafat-1.sock    │
│ - NBD server         │  │ - NBD server         │
│ - exFAT/FAT32 synth  │  │ - exFAT/FAT32 synth  │
│ - 60-min retention   │  │ - no retention       │
│ - control IPC server │  │ - control IPC server │
└──────┬───────────────┘  └──────┬───────────────┘
       │ POSIX                   │ POSIX
       ▼                         ▼
┌─────────────────────────┐  ┌──────────────────────────┐
│ /srv/teslausb/teslacam/ │  │ /srv/teslausb/media/     │
│   TeslaCam/             │  │   LightShow.fseq         │
│     RecentClips/        │  │   LockChime.wav          │
│     SentryClips/        │  │   Wraps/                 │
│     SavedClips/         │  │   Music/                 │
└─────────────────────────┘  └──────────────────────────┘
        └────────── ext4 data roots ──────────────┘
                ▲                   ▲
                │ inotify           │ inotify
                │                   │
        ┌───────┴───────────────────┴───────┐
        │ teslausb-worker (Rust)            │
        │ /run/teslausb/worker.sock          │
        │ - inotify watcher (both subvols)  │
        │ - SEI parser (H.264/H.265)        │
        │ - Indexer → SQLite                │
        │ - Cleanup worker (GPS-aware)      │
        │ - Cloud uploader (rclone driver)  │
        │ - Cache-invalidation orchestrator │
        │ - Control IPC server              │
        └───────────────┬───────────────────┘
                        │ rusqlite (WAL mode)
                        ▼
        ┌──────────────────────────────────┐
        │ /var/lib/teslausb/teslausb.db    │
        │  (single consolidated SQLite DB) │
        └──────────────────────────────────┘
                        ▲
                        │ sqlite3 read-only + IPC
                        │
        ┌───────────────┴───────────────────┐
        │ teslausb-web (Python Flask)       │
        │  gunicorn worker, runs as         │
        │  `teslausb` user (NOT root)       │
        │  binds 127.0.0.1:5000             │
        │ - User-facing UI (v1 port)        │
        │ - Read-only DB queries            │
        │ - IPC client → teslafat-0/-1      │
        │ - IPC client → teslausb-worker    │
        └───────────────┬───────────────────┘
                        │ HTTP loopback
                        ▼
        ┌──────────────────────────────────┐
        │ nginx (port 80, root binds,      │
        │   drops to www-data)             │
        │ - reverse proxy → Flask          │
        │ - captive portal rewrite rules   │
        │ - static asset serving (fast)    │
        │ - optional TLS termination       │
        └──────────────────────────────────┘
```

**Three Rust processes, one Python process, one nginx process.**
Memory budget on Pi Zero 2 W (512 MB total RAM):

| Process | Budget | Why |
|---|---:|---|
| teslafat-0 | 60 MB | NBD hot path; per-LUN isolation |
| teslafat-1 | 60 MB | NBD hot path; per-LUN isolation |
| teslausb-worker | 80 MB | inotify+SEI+indexer+cleanup+cloud all in one Rust process |
| teslausb-web (gunicorn + Flask) | 80 MB | Single gunicorn worker, no background threads |
| nginx | 15 MB | Reverse proxy, static assets, captive portal |
| smbd (when on) | 50 MB | Optional |
| System overhead (kernel, systemd, NetworkManager, dnsmasq, wpa_supplicant, sshd) | ~150 MB | |
| **Total when idle** | **~395 MB** | leaves ~115 MB for page cache |
| **Total with Samba on** | **~445 MB** | leaves ~65 MB for page cache |

Compare to "Python everywhere" alternative which would have run
4-5 separate Python services for ~250-300 MB just for background
work — leaving negative headroom under typical Tesla recording load.

**Per-LUN process isolation:** teslafat-0 crash does not affect
teslafat-1, and vice versa. The kernel's NBD client (with `-persist`)
buffers Tesla writes briefly during the ≤ 1 s teslafat restart;
systemd `Restart=always RestartSec=1` brings it back fast.

**Why teslausb-worker is one process, not four:**
- All four jobs (inotify, indexer, cleanup, cloud) share the same
  SQLite handle, the same SEI parser, the same flock-coordinated
  SD-card access pattern.
- Splitting them into 4 Rust binaries would add ~40 MB of duplicated
  runtime (tokio, rusqlite, etc.) and force IPC for what should be
  in-process channel sends.
- A single Rust process with separate tokio tasks gives us
  fault-isolated logical workers (each task is `catch_unwind`-wrapped
  in supervisor mode) with shared resources.

**Why no Python services for background work:**
- Python's GC + interpreter overhead is real on a Pi Zero 2 W —
  each Python service starts at ~30-50 MB RSS before doing anything.
- SEI parsing in Python is 5-10× slower than Rust on this hardware.
  At sustained 4 files/min the difference shows up as missed
  watchdog windows under load.
- The cooperative `task_coordinator` complexity in v1 was symptomatic
  of trying to share one Python GIL across many SDIO-bound workers.
  Rust threads share the kernel's I/O scheduler directly; no
  user-space coordinator needed. A simple `fcntl(LOCK_EX)` on
  `/run/teslausb/sd-write.lock` arbitrates between teslausb-worker
  and Samba/web-triggered writes — fast, crash-safe, kernel-managed.

---

## Repository layout

```
TeslaUSB/
├── README.md                  # top-level entry point
├── setup.sh                   # idempotent installer
├── uninstall.sh               # revert-to-vanilla-Pi-OS
├── config/
│   ├── teslausb.toml.example  # user config template (TOML, not YAML)
│   └── defaults.toml          # baked-in defaults, never edited
├── rust/                      # single Cargo workspace
│   ├── Cargo.toml             # workspace root
│   ├── rust-toolchain.toml    # pinned stable version
│   ├── deny.toml              # cargo-deny config (license + RUSTSEC)
│   ├── crates/
│   │   ├── teslausb-core/     # shared lib used by all binaries
│   │   │   ├── Cargo.toml
│   │   │   └── src/
│   │   │       ├── lib.rs
│   │   │       ├── config.rs      # TOML loader, single source of truth
│   │   │       ├── paths.rs       # FHS path constants
│   │   │       ├── ipc/           # JSON-line IPC protocol
│   │   │       │   ├── mod.rs
│   │   │       │   ├── messages.rs
│   │   │       │   └── server.rs
│   │   │       ├── db/            # rusqlite layer (WAL, migrations)
│   │   │       │   ├── mod.rs
│   │   │       │   ├── schema.rs
│   │   │       │   └── migrations.rs
│   │   │       ├── sei/           # SEI parser (NEW, Rust-native)
│   │   │       │   ├── mod.rs
│   │   │       │   ├── mp4.rs     # mvhd, mdhd box parsing
│   │   │       │   ├── h264.rs    # NAL type 6 SEI walker
│   │   │       │   ├── h265.rs    # HEVC SEI walker
│   │   │       │   └── gps.rs     # Tesla GPS protobuf decoder
│   │   │       ├── flock.rs       # fcntl LOCK_EX wrapper for SDIO arbitration
│   │   │       └── observability.rs # tracing-journald setup
│   │   ├── teslafat/          # NBD daemon — binary
│   │   │   ├── Cargo.toml
│   │   │   ├── src/
│   │   │   │   ├── main.rs        # arg parsing, signal handling
│   │   │   │   ├── nbd/           # NBD protocol
│   │   │   │   │   ├── mod.rs
│   │   │   │   │   ├── handshake.rs
│   │   │   │   │   └── transmission.rs
│   │   │   │   ├── fs/            # FS synthesizers
│   │   │   │   │   ├── mod.rs     # `Filesystem` trait
│   │   │   │   │   ├── fat32/
│   │   │   │   │   │   ├── mod.rs
│   │   │   │   │   │   ├── geometry.rs
│   │   │   │   │   │   ├── boot_sector.rs
│   │   │   │   │   │   ├── fsinfo.rs
│   │   │   │   │   │   ├── fat_table.rs
│   │   │   │   │   │   ├── directory.rs
│   │   │   │   │   │   ├── synth.rs
│   │   │   │   │   │   └── parse.rs
│   │   │   │   │   └── exfat/
│   │   │   │   │       ├── mod.rs
│   │   │   │   │       ├── geometry.rs
│   │   │   │   │       ├── boot_region.rs
│   │   │   │   │       ├── allocation_bitmap.rs
│   │   │   │   │       ├── upcase_table.rs
│   │   │   │   │       ├── directory.rs
│   │   │   │   │       ├── synth.rs
│   │   │   │   │       └── parse.rs
│   │   │   │   ├── backend/
│   │   │   │   │   ├── mod.rs     # `BlockBackend` trait
│   │   │   │   │   └── dir_tree.rs
│   │   │   │   ├── retention.rs   # 60-min RecentClips hide
│   │   │   │   ├── cluster_map.rs # extent-based virtual cluster map
│   │   │   │   └── gadget.rs      # configfs writer (LUN clear/set)
│   │   │   └── tests/
│   │   │       ├── fat32_geometry.rs
│   │   │       ├── fat32_synth.rs
│   │   │       ├── exfat_synth.rs
│   │   │       ├── nbd_protocol.rs
│   │   │       └── proptest_fs.rs  # property tests on FS layer
│   │   └── teslausb-worker/   # background worker — binary
│   │       ├── Cargo.toml
│   │       ├── src/
│   │       │   ├── main.rs            # tokio runtime, task supervisor
│   │       │   ├── watcher.rs         # `notify` crate inotify
│   │       │   ├── indexer.rs         # SEI parse + trip/event merge
│   │       │   ├── cleanup.rs         # GPS-aware retention deletes
│   │       │   ├── cloud/             # rclone driver
│   │       │   │   ├── mod.rs
│   │       │   │   ├── queue.rs       # SQLite-backed upload queue
│   │       │   │   ├── rclone.rs      # subprocess driver
│   │       │   │   └── priority.rs
│   │       │   ├── cache_invalidate.rs # debounced gadget LUN clear/set
│   │       │   └── ipc.rs              # control IPC server
│   │       └── tests/...
├── web/                       # Python Flask app (UI ONLY)
│   ├── pyproject.toml         # ruff + mypy config per charter
│   ├── teslausb_web/
│   │   ├── __init__.py
│   │   ├── app.py             # Flask factory, no background threads
│   │   ├── wsgi.py            # gunicorn entry point
│   │   ├── config.py          # reads same TOML as Rust side
│   │   ├── ipc.py             # Unix-socket JSON-line client (Rust IPC)
│   │   ├── db.py              # sqlite3 read-only connection (via :memory: cache)
│   │   ├── blueprints/        # ported from v1, mode-removal edits applied
│   │   │   ├── mapping.py
│   │   │   ├── settings.py
│   │   │   ├── lock_chimes.py
│   │   │   ├── light_shows.py
│   │   │   ├── music.py
│   │   │   ├── wraps.py
│   │   │   ├── cloud_archive.py
│   │   │   ├── network_sharing.py  # Samba toggle
│   │   │   ├── system_health.py
│   │   │   └── captive_portal.py
│   │   ├── templates/         # ported VERBATIM from v1
│   │   └── static/            # ported VERBATIM from v1
│   └── tests/
├── systemd/
│   ├── teslafat@.service              # template: %i = 0 or 1
│   ├── nbd-client@.service.d/         # drop-in for NBD client per LUN
│   ├── teslausb-gadget.service        # bind f_mass_storage via configfs
│   ├── teslausb-worker.service        # Rust background worker
│   ├── teslausb-web.service           # gunicorn + Flask
│   ├── teslausb-nginx.conf            # nginx config (port 80, reverse proxy)
│   ├── wifi-monitor.service
│   ├── ap-monitor.service
│   └── watchdog.service.d/
│       └── teslausb-priority.conf
├── scripts/                           # only deploy + ops glue
│   ├── present_usb.sh                 # bind gadget configfs
│   ├── hide_usb.sh                    # unbind gadget
│   ├── ap_control.sh
│   ├── wifi-monitor.sh
│   ├── safe_mode.sh
│   ├── refresh_cloud_token.py
│   └── helpers/
├── config-fragments/
│   ├── networkmanager-wifi-roaming.conf
│   ├── hostapd.conf.template
│   ├── dnsmasq.conf.template
│   ├── nginx-teslausb.conf
│   └── samba-teslausb.conf.template
├── .github/
│   └── workflows/
│       └── ci.yml                     # rust fmt+clippy+test+coverage, python ruff+mypy+pytest+coverage
├── .pre-commit-config.yaml
├── setup-dev.sh                       # dev tools installer (charter requires)
└── docs/                              # living documentation
    ├── 00-PLAN.md                     # THIS FILE
    ├── 01-PROGRESS.md
    ├── 02-LEARNINGS.md
    ├── 03-CODE-QUALITY-CHARTER.md
    ├── 05-UI-UX-DESIGN-SYSTEM.md      # copied from v1, authority for UI
    ├── adr/                           # architecture decision records
    │   ├── 0001-rust-for-daemon.md
    │   ├── 0002-nbd-newstyle-over-unix-socket.md
    │   ├── 0003-two-luns-mirror-v1.md
    │   ├── 0004-extent-based-cluster-map.md
    │   ├── 0005-crash-and-restart-on-backend-error.md
    │   ├── 0006-rust-for-sei-and-indexer.md
    │   ├── 0007-toml-config-format.md
    │   ├── 0008-nginx-reverse-proxy.md
    │   ├── 0009-single-consolidated-sqlite-db.md
    │   ├── 0010-fhs-standard-paths.md
    │   └── 0011-flock-for-sdio-arbitration.md
    ├── architecture.md
    ├── fs-synthesis.md
    ├── nbd-protocol.md
    ├── ipc-protocol.md
    ├── tesla-cache-invalidation.md
    ├── setup.md
    ├── uninstall.md
    └── development.md
```

**FHS-standard installed paths:**

| Path | Purpose |
|---|---|
| `/etc/teslausb/teslausb.toml` | User config (TOML) |
| `/etc/teslausb/teslausb.toml.bak.*` | Auto-backups on every edit |
| `/usr/local/bin/teslafat` | Rust NBD daemon binary |
| `/usr/local/bin/teslausb-worker` | Rust background worker binary |
| `/usr/local/lib/teslausb/web/` | Python web app install root |
| `/usr/local/bin/teslausb-{present,hide}-usb` | Gadget control scripts |
| `/srv/teslausb/teslacam/` | ext4 data root — Tesla writes here |
| `/srv/teslausb/media/` | ext4 data root — user-uploaded chimes/lightshows/music/wraps |
| `/var/lib/teslausb/teslausb.db` | Single consolidated SQLite DB |
| `/var/lib/teslausb/cache/` | rclone cache, temp downloads |
| `/run/teslausb/teslafat-0.sock` | LUN 0 control IPC |
| `/run/teslausb/teslafat-1.sock` | LUN 1 control IPC |
| `/run/teslausb/worker.sock` | Worker control IPC |
| `/run/teslausb/sd-write.lock` | flock arbitration for SD writes |
| `/run/teslausb/gunicorn.sock` | Flask app socket (nginx upstream) |

No `/home/pi/TeslaUSB`. No `~/ArchivedClips`. No `~/.config/...`.
Everything in proper system paths. The `pi` user is for SSH only;
all teslausb processes run as a dedicated `teslausb` system user
(except teslafat which needs root for configfs and `/dev/nbdN`).

---

## Hardware test environment

**Target device:** `cybertruckusb.local` (login user `pi`).

This is a real Raspberry Pi Zero 2 W currently running v1. Until
explicit decommissioning (Increment H0 below), v1's `gadget_web`
and friends are LIVE in production on it. We MUST NOT cause:

1. **WiFi disconnection.** If WiFi drops while we hold the SSH
   session, the device is unreachable. Pi physical access requires
   removing it from the truck. NetworkManager + wpa_supplicant +
   `/etc/NetworkManager/conf.d/wifi-roaming.conf` are untouchable
   until B-1 has its own replacement validated.
2. **SSH lockout.** `sshd` and the `sshd-protect.conf` drop-in
   from v1 are sacred. Do not stop `ssh.service`, do not edit
   `/etc/ssh/sshd_config` without a `systemd-run --on-active=2m
   reboot` safety net in place.
3. **Boot failure.** `/boot/firmware/cmdline.txt` and
   `/boot/firmware/config.txt` must be edited only with a backup
   (`*.b1-backup`) staged for one-command rollback. Same for
   `/etc/fstab`.

### Hardware test cadence

| Trigger | What runs | Where |
|---|---|---|
| Per-increment local | `cargo test` (Rust) + `pytest -x` (Python) + `pre-commit run --all-files` | Dev machine |
| Per-increment hardware smoke | `scripts/hw_smoke_<increment>.sh` (one for each increment that touches Rust I/O, NBD, the gadget, configfs, or any service) | `cybertruckusb.local`, SSH, runs in dedicated `/home/pi/teslausb-b1/` directory, NEVER overwrites v1 paths until Increment H0 has run |
| Per-phase hardware soak | 24 h `journalctl -f` + `vmstat 60` + `top -bn1` capture | Live device |
| Pre-cutover full soak | 72 h on the truck with Tesla actively recording | Live device |

### Hardware smoke-test framework

A single helper, `scripts/hw/run-on-target.sh`, wraps every
hardware interaction. It:

- Reads `B1_TARGET_HOST` (default `cybertruckusb.local`) and
  `B1_TARGET_USER` (default `pi`) from env.
- Refuses to run if `~/.ssh/known_hosts` does not already contain
  the host's key (forces operator to manually verify the first
  connection).
- Wraps every `ssh` invocation with `-o ServerAliveInterval=15
  -o ConnectTimeout=10 -o BatchMode=yes`.
- Before any potentially-destructive command, runs a "dead-man
  switch": `ssh ... 'systemd-run --on-active=180 --unit=b1-deadman
  /sbin/reboot'`. If our test wedges the device, it reboots
  itself in 3 minutes. After successful test completion, we
  call `systemctl stop b1-deadman.timer` to cancel.
- Verifies SSH responsiveness with `ssh ... 'echo alive'`
  before AND after every step.
- Logs every remote command + exit code to
  `~/.copilot/session-state/<sid>/files/hw-<timestamp>.log`.

Increments H0, H1, ..., Hn (the "H-series") are the only ones
that touch the live device. Code-only increments (Rust + Python
unit tests on the dev machine) are interleaved between H-series.

---

## Phased implementation

The plan is broken into small **increments**. Each increment is a
single cohesive change ≤ ~500 LOC of new code. Each ends with two
mandatory gates:

- 🔍 **REVIEW GATE** — invoke `.github/skills/charter-review`
  against the increment. Fix ALL Blocker, Major, Minor, and Nit
  findings before continuing. No deferring to follow-ups.
- ✅ **TEST GATE** — every increment must show green automated
  tests at minimum (`cargo test` / `pytest -x` / `pre-commit run
  --all-files`). H-series increments also require a green
  hardware smoke test.

The cadence is binding (operator directive 2026-05-19):
*"Don't do a ton of work and wait to do code reviews. Have
specific code review breaks and then fix ALL issues you find."*

> **PR strategy.** Each increment is a single Git commit on the
> `b1-userspace-rust` branch. Phases group commits for review
> bundling. The first PR off the branch ships Phase 0 + Phase 1
> together so reviewers see the foundation in one go; subsequent
> PRs ship a phase at a time.

### Phase 0 — Scaffolding

Per `01-PROGRESS.md` Phase 0, all increments below run once. Each
ends with a charter-review gate and a `pre-commit run --all-files`
test gate. No hardware work yet.

| # | Deliverable | LOC ceiling |
|---|---|---|
| 0.1 | Branch rename `b1-userspace-fat32` → `b1-userspace-rust`; first commit (wipe + docs) | n/a |
| 0.2 | Cargo workspace skeleton at `rust/` with `[lints]` blocks per charter; empty crates `teslausb-core`, `teslafat`, `teslausb-worker`; `rust-toolchain.toml`; `deny.toml` | ~150 |
| 0.3 | Python skeleton at `web/teslausb_web/` with `pyproject.toml` (ruff + mypy + pytest config per charter); empty `__init__.py` per module | ~200 |
| 0.4 | `scripts/check.sh` local gate runner with every gate from charter §"CI Gates" (Rust + Python + hygiene + markdown links). NOT a GitHub Actions workflow — operator-driven, runs on demand pre-commit. Cloud CI deferred indefinitely; hardware testing is H-phase territory anyway. | ~250 |
| 0.5 | `.pre-commit-config.yaml` mirroring CI gates locally | ~80 |
| 0.6 | `setup-dev.sh` (installs Rust + Python + tools on a dev box; idempotent) | ~150 |
| 0.7 | `CODEOWNERS` + PR template referencing the charter checklist | ~50 |
| 0.8 | ADRs 0001 – 0011 written | ~50 LOC each |

**🔍 REVIEW GATE after each increment.** **✅ TEST GATE:**
`scripts/check.sh` green (every charter gate passes locally);
`pre-commit run --all-files` green; `cargo build` green (empty
crates compile); `pytest` green (0 tests OK). Cloud CI is
intentionally NOT a Phase 0 deliverable — the operator runs
`scripts/check.sh` before each commit. Cloud / GitHub-Actions
enforcement is deferred indefinitely; full integration testing
requires real hardware and is owned by the H-phases.

### Phase H0 — Decommission v1 from `cybertruckusb.local`

This is the FIRST hardware work. Until it succeeds, the device
runs v1 in production. Until it succeeds, no B-1 binary or service
may be deployed to it. This phase MUST be run by the operator on
the dev machine via the `hardware-test` skill which uses the
safety wrapper described above.

**Safety contract for every step below:**
- Dead-man switch armed (3-min reboot timer) before each
  systemctl/file operation.
- After every step, run `ssh pi@cybertruckusb.local 'echo alive
  && uptime'`. If it fails, WAIT for the dead-man reboot, then
  STOP and notify the operator. Do not retry.
- Touch nothing under `/etc/ssh/`, `/etc/NetworkManager/`,
  `/etc/wpa_supplicant/`, `/etc/systemd/system/sshd*`,
  `/etc/sudoers*`. WiFi roaming config is preserved verbatim.
- Take a `tar` snapshot of `/etc/` and `/home/pi/TeslaUSB` to
  `/home/pi/v1-backup-<date>.tar.gz` BEFORE deleting anything.
  Copy it off the device to `~/.copilot/session-state/<sid>/files/`
  via `scp` for off-device archival.

| # | Step | Verify after |
|---|---|---|
| H0.1 | `scp` snapshot of `/etc/` + `~/TeslaUSB` to dev machine | tarball exists locally, sha256 matches |
| H0.2 | `systemctl stop` (NOT mask yet) the v1 services in order: `chime_scheduler.timer`, `chime_scheduler.service`, `teslausb-deferred-tasks.service`, `gadget_web.service`, `wifi-monitor.service`, `present_usb_on_boot.service` | `ssh ... 'echo alive'` after each |
| H0.3 | `systemctl disable` same list (NO mask — disable lets us recover by re-enable) | services no longer auto-start on next boot |
| H0.4 | **Reboot once** — verifies the device boots without v1 services, WiFi still works, SSH still works | `ssh pi@cybertruckusb.local 'uptime'` succeeds within 90 s of reboot |
| H0.5 | `systemctl mask` same list (now permanent — can't be accidentally started) | `systemctl is-enabled` returns `masked` |
| H0.6 | Remove the systemd unit files at `/etc/systemd/system/{gadget_web,present_usb_on_boot,wifi-monitor,chime_scheduler}.service` (and `.timer`) — files were installed by v1's `setup_usb.sh`, not by Debian package | `systemctl daemon-reload` clean |
| H0.7 | Remove v1's sudoers drop-in at `/etc/sudoers.d/teslausb-*` (if present); leave any non-teslausb entries untouched | `sudo -l -U pi` shows no teslausb rules |
| H0.8 | Remove v1's NetworkManager dispatcher script at `/etc/NetworkManager/dispatcher.d/*-refresh_cloud_token` — but keep the WiFi-roaming config file | `nmcli connection show` unchanged |
| H0.9 | Disable the v1 `g_mass_storage` cmdline entries — comment out (do NOT delete) the v1 `modules-load=` / `dtoverlay=dwc2` lines in `/boot/firmware/cmdline.txt` and `config.txt`, with `.b1-backup` siblings created. **Skip if those lines aren't present (dwc2 stays loaded because B-1 needs it too).** | `cat /boot/firmware/cmdline.txt.b1-backup` exists; SSH still works |
| H0.10 | Delete `~/TeslaUSB` and `~/.quick_edit_part2.lock` and `~/ArchivedClips` (huge dir — confirm with operator before destructive `rm -rf`); leave `~/.ssh/` alone | `du -sh ~/` shows reclaimed space |
| H0.11 | Stop and disable `smbd` + `nmbd` (v1 may have enabled them) | `systemctl is-active smbd` = inactive |
| H0.12 | Stop and disable `watchdog.service` (v1's hardware watchdog) — B-1 will reinstall its own version with a different config in Phase 6 | `systemctl is-active watchdog` = inactive |
| H0.13 | **Reboot once more** to confirm the device boots clean with zero v1 footprint, WiFi works, SSH works | login succeeds within 90 s |
| H0.14 | Capture `journalctl -b --no-pager > /tmp/clean-boot.log` and `scp` it back for archival — this is our "clean v1-free baseline" reference | `clean-boot.log` exists locally |

**🔍 REVIEW GATE:** charter-review the `scripts/hw/` framework
and the H0 step-script(s) for shell-safety issues (quoting,
race conditions, exit-on-error, idempotency). **✅ TEST GATE:**
manual — operator confirms SSH responsive, WiFi connected,
disk space reclaimed, no v1 processes in `ps -ef`.

### Phase 1 — Rust daemon skeleton

| # | Deliverable | LOC ceiling |
|---|---|---|
| 1.1 | `teslafat` `main.rs`: `clap` CLI, `tracing-subscriber` init (JSON to stderr, level via `RUST_LOG`), TOML config loader, sentinel "started" log line | ~120 |
| 1.2 | `teslausb-core::ipc::messages` types (versioned envelope, `STATUS`, `RETENTION_UPDATE`, `INVALIDATE_CACHE` request/response) with `serde_test` round-trip tests | ~150 |
| 1.3 | NBD newstyle handshake — port the existing `teslafat/src/nbd/handshake.rs` into `rust/crates/teslafat/src/nbd/handshake.rs`, add round-trip test against a `nbd-client --check` invocation in CI | ~50 (mostly path move) — **actual: ~600 LOC** (port + decomposition into pure encode/decode helpers + 19 unit tests via `tokio::io::duplex` + lib+bin split for `dead_code` discipline; the draft predated the charter, so charter compliance added ~5× the estimate; "path move" estimates for pre-charter drafts should add 3–5× headroom going forward) |
| 1.4 | `BlockBackend` trait in `teslausb-core::backend` with `size`, `read`, `write(flags)`, `flush`; null impl for tests; FUA contract enforced by trait doc + test | ~100 — **actual: ~620 LOC** (same 5-6× headroom as inc-1.3; pre-charter "trait + null" estimates didn't budget for `# Errors` docs on every fallible pub fn, the `WriteFlags` newtype + bitwise ops, `BackendError` thiserror enum, `check_bounds` shared overflow-safe helper, `NullBackend` + `MockBackend` reference impls, the 24 unit tests, and `pollster` dev-dep wiring) |
| 1.5 | NBD transmission loop (`teslafat::nbd::transmission`) that dispatches READ / WRITE / FLUSH / TRIM to a `BlockBackend`, with FUA fdatasync test | ~200 — **actual: ~1453 LOC** (same 5-7× headroom as inc-1.3 / 1.4; pre-charter "dispatch loop" estimates didn't budget for the wire-vs-orchestrator module split, spec-citation docstrings on every const + pub fn, hand-asserted spec byte-layout tests on both encoder and decoder, full command-coverage tests (READ / WRITE / FLUSH / TRIM / DISC / unknown), every wire-error path (oversized, OOB, bad magic, short read, clean EOF), the `tokio::join!`-based single-task test harness driven by ADR-0004 §A's no-Send consequence, and the FUA pass-through verification using `MockBackend::observed_any_durability()` as the oracle) |
| 1.6 | systemd unit `teslafat@.service` (instanced for LUN 0 / LUN 1), `EnvironmentFile=/etc/teslausb/teslafat.toml`, `User=teslausb`, capability bounding | ~50 — **actual: ~1227 LOC** (24× — same overrun-pattern direction as inc-1.3 / 1.4 / 1.5; pre-charter "systemd unit" estimate didn't budget for two new public modules (`crate::backend::ZeroBackend` placeholder + `crate::server` accept loop) with full `///` + `# Errors` docs and 22 new tests across them, the `crate::config::NbdConfig` schema delta with range/non-empty validation, the `--check-config` flag wiring (preserves the Phase 1.1 sentinel contract for both `tests/sentinel.rs` and the unit's `ExecStartPre=` fast-fail gate), the SIGTERM + SIGINT signal handlers with graceful shutdown via `tokio::sync::oneshot`, the cross-platform split (`serve_one_connection` generic over `AsyncRead + AsyncWrite` so 5 connection-policy tests run on the Windows dev box via `tokio::io::duplex` + 5 Unix-only `accept_loop::*` tests gated `#[cfg(unix)]`), and the systemd unit's full hardening profile (`CapabilityBoundingSet=`, `ProtectSystem=strict`, `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, `MemoryDenyWriteExecute=yes`, `SystemCallFilter=@system-service`) with per-directive inline comments) |
| 1.7 | Smoke test: `teslafat` binary runs on the dev box; integration tests in `rust/crates/teslafat/tests/smoke.rs` speak the NBD wire protocol *directly* from the test process over `tokio::net::UnixStream` (no kernel `nbd-client` tool), reads return all-zero from the `ZeroBackend`, no panics. Kernel `nbd-client` integration is deferred to the H1 hardware gate (see ADR-0007). | ~80 (test harness) — **actual: ~766 LOC** (~9.5x; same overrun-pattern direction as inc-1.3 / 1.4 / 1.5 / 1.6; pre-charter "test harness" estimate didn't budget for the `DaemonHandle` RAII guard with stderr-pump OS thread + drop-on-panic dump, the 3 async wire helpers (`client_handshake_export_name` / `client_read` / `client_disc`) each wrapped in `tokio::time::timeout`, the 6 tests (happy-path READ + no-client SIGTERM + ADR-0006 §B end-to-end + non-default `volume_size_gb` plumbing + sentinel-in-serve-mode + `handshake_timeout_seconds` -> sentinel JSON field), the inline rationale comments documenting Decision A (userspace wire instead of `nbd-client`), Decision C (`CF_NO_ZEROES` so the server replies compactly), and Decision F (current-thread runtime to match the daemon), and the `#[cfg(unix)]` gating + file-level lint allows for test ergonomics) — see ADR-0007 |

**🔍 REVIEW GATE** after every numbered increment. **✅ TEST
GATE:** `cargo test -p teslafat` green; new clippy lints pass.

### Phase H1 — Daemon smoke on hardware

| # | Step |
|---|---|
| H1.1 | `cargo build --release --target=aarch64-unknown-linux-gnu` cross-build on dev box via the podman cross-build image at `tools/xbuild/` (see `tools/xbuild/README.md`). Confirmed Pi arch via `dpkg --print-architecture` = `arm64`, `uname -m` = `aarch64` (Pi Zero 2 W, kernel 6.12.47+rpt-rpi-v8). The earlier `armv7-unknown-linux-gnueabihf` value pre-dated the hardware probe and was wrong. **Building on-device is FORBIDDEN** (see ADR-0008 — two H1 attempts to `cargo build` on the Pi both wedged the device, one requiring a hard power cycle and one requiring a sysrq+b kernel reboot; cross-compile only). |
| H1.2 | `scp $env:TEMP/teslausb-h1/teslafat pi@cybertruckusb.local:/tmp/teslafat` then `sudo install -m 0755 /tmp/teslafat /usr/local/bin/teslafat` |
| H1.3 | Install systemd unit as `teslafat-test@.service` (NOT the production name yet); test-variant rewrite of `units/teslafat@.service` (test only — `RuntimeDirectory=teslausb-test`, `/etc/teslausb-test/`, `/var/teslacam-test`) |
| H1.4 | Validate: `sudo -u teslausb /usr/local/bin/teslafat --config /etc/teslausb-test/teslafat-0.toml --check-config` (must emit started-sentinel JSON + exit 0); then `sudo systemctl daemon-reload && sudo systemctl start teslafat-test@0` |
| H1.5 | `sudo nbd-client -unix /run/teslausb-test/teslafat-0.sock /dev/nbd1` — confirm newstyle negotiation completes (note: `nbd-client` 3.x deprecated `-u`; use `-unix`) |
| H1.6 | `sudo blockdev --getsize64 /dev/nbd1` — exactly 4294967296 (4 GiB); `dd if=/dev/nbd1 bs=64 count=1 \| od -An -tx1` returns all zeros (ZeroBackend by design — FAT BPB lands in Phase 2) |
| H1.7 | `sudo nbd-client -d /dev/nbd1`, `sudo systemctl stop teslafat-test@0`. Leave the unit file + binary + config installed for downstream H-increments; mark inactive. Cleanup of `/run/teslausb-test/` happens automatically via `RuntimeDirectory=`. |
| H1.8 | SSH still alive (`ssh pi@cybertruckusb.local 'uptime'`), load < 1.0, no OOM in `dmesg`, no NEAR-MISS, swap usage 0 |

**🔍 REVIEW GATE** on the smoke script. **✅ TEST GATE:** all
H1.x steps green, journal captured to session files for charter-review.

### Phase 2 — Static FS synthesis (read-only)

Same cold-start budget as before: ≤ 1 s for 10K-file tree.

| # | Deliverable | LOC ceiling |
|---|---|---|
| 2.1 | `fs::geometry` shared trait + `fs::fat32::geometry` impl (BPB layout for a given total size). Unit tests against known-good sizes (32 MiB, 4 GiB, 32 GiB) | ~250 |
| 2.2 | `fs::fat32::boot_sector::synthesize` + golden-file test comparing bytes to `mkfs.vfat` output | ~150 |
| 2.3 | `fs::fat32::fsinfo::synthesize` + test | ~80 |
| 2.4 | `fs::fat32::fat_table::synthesize` with an in-memory `DirTreeBackend` mock; tests cover chain construction for fragmented and contiguous files | ~300 |
| 2.5 | `fs::fat32::directory::synthesize` (8.3 + LFN encoding) with golden-file tests for ASCII, Unicode, max-length names | ~350 |
| 2.6 | `fs::fat32::synth::read(offset, len)` dispatcher; tests cover region boundaries, partial-region reads, beyond-EOF | ~200 |
| 2.7 | `fs::fat32` integration test: synthesize a known tree, mount via `nbd-client` + `loop`, `cmp` against source | ~200 (test) |
| 2.8 | `fs::exfat::geometry` + boot region (parallel to 2.1+2.2) | ~250 |
| 2.9 | `fs::exfat::allocation_bitmap` + `fs::exfat::upcase_table` | ~250 |
| 2.10 | `fs::exfat::directory` (FileDirectoryEntry + StreamExtension + FileName entries, name hash) | ~350 |
| 2.11 | `fs::exfat::synth::read` dispatcher + parity tests with the fat32 dispatcher | ~200 |
| 2.12 | `fs::exfat` integration test: synth, mount, cmp | ~200 (test) |
| 2.13 | `lazy_load.rs` — deferred deep-directory materialization, with concurrency tests | ~250 |
| 2.14 | Cold-start benchmark `cargo bench` target: assert ≤ 1 s for 10K-file synthetic tree (CI gate) | ~150 (bench) |

**🔍 REVIEW GATE** after EACH numbered increment (14 reviews
in this phase — yes, that's the point). **✅ TEST GATE:**
per-increment `cargo test -p teslafat fs::` green;
post-phase: cold-start bench ≤ 1 s on dev box.

### Phase H2 — Read-only synth on hardware

| # | Step |
|---|---|
| H2.1 | Deploy new `teslafat` binary to `cybertruckusb.local` |
| H2.2 | Create a tiny synthetic backing tree at `/home/pi/teslausb-b1/test-backing/` (3 mp4 files, 2 subdirs) |
| H2.3 | Start `teslafat-test@0` pointing at it |
| H2.4 | `nbd-client + losetup + mount -o ro` and verify all files visible + byte-identical to source |
| H2.5 | `fsck.vfat -v /dev/nbd1` clean |
| H2.6 | Same for exFAT mode |
| H2.7 | Cold-start time captured: synth start → mount succeeds. Target ≤ 1 s. |
| H2.8 | Teardown, SSH alive, WiFi alive |

**🔍 REVIEW GATE** on H2 script + journal. **✅ TEST GATE:**
all H2.x green; bench number recorded in `docs/V2_HARDWARE_VALIDATION.md`.

### Phase 3 — FS write-side

| # | Deliverable | LOC ceiling |
|---|---|---|
| 3.1 | `fs::fat32::parse::decode_write` — translates pwrite-into-BPB / FAT / dir regions into a typed enum. Unit tests for every region. | ~300 |
| 3.2 | `fs::exfat::parse::decode_write` parallel | ~300 |
| 3.3 | `backend::dir_tree` adapter — implements `BlockBackend` over a POSIX directory tree, `.partial` write atomicity, `O_CREAT|O_EXCL` collision handling | ~400 |
| 3.4 | `cluster_map` extent-based `BTreeMap<u32, Extent>` with `Arc` for lock-free reads; rebuild-on-startup test | ~300 |
| 3.5a | `fs::fat32::dir_decode` — read-side FAT32 directory-entry decoder (LFN aggregation, SFN, deleted, malformed) | ~600 |
| 3.5b | `fs::fat32::chain` — FAT32 cluster-chain walker with cycle/oob/free-entry diagnostics | ~300 |
| 3.5c | Wire 3.1 + 3.3 + 3.4 + 3.5a + 3.5b into FAT32 `synth::write` via `backend::fat32_write` state machine; 11-test end-to-end integration through public `BlockBackend::write/flush` API | ~1700 (incl. 23 tests) |
| 3.5d | `fs::exfat::dir_decode` — read-side exFAT directory-entry decoder (file/stream-ext/file-name set decode, attribute extraction, deleted state) | ~500 |
| 3.5e | `backend::exfat_write` state machine + wire 3.2 + 3.3 + 3.4 + 3.5d into exFAT `synth::write` | ~1500 |
| 3.6 | Power-cut harness: `kill -9 teslafat` mid-write; on restart, partial files have `.partial` suffix and are not visible to Tesla | ~250 (test) |

**🔍 REVIEW GATE per increment. ✅ TEST GATE:** per-increment
`cargo test` green; post-phase: power-cut harness passes 100 of
100 random kill points.

### Phase H3 — Write-side on hardware

| # | Step |
|---|---|
| H3.1 | Deploy. Plug-in a Windows laptop as USB host to the Pi (Pi acts as gadget). Format the LUN as FAT32 from Windows. |
| H3.2 | Copy a 50 MB file from Windows. Verify it appears at `/home/pi/teslausb-b1/test-backing/` byte-identical. |
| H3.3 | Same for exFAT. |
| H3.4 | Power-cut test: while Windows is copying, yank power. On boot, partial file has `.partial` suffix. |
| H3.5 | SSH alive after recovery. |

**🔍 REVIEW GATE. ✅ TEST GATE:** all H3.x green.

### Phase 4 — RecentClips retention shim + preservation policy

| # | Deliverable | LOC ceiling |
|---|---|---|
| 4.1 | `teslafat::retention::filter` — mtime-based hide-from-view test, configurable threshold; tests use a frozen clock | ~200 |
| 4.2 | Tesla-delete interception: dir-entry-0xE5 / exFAT-InUse-clear is recorded in `retention` state, backing file untouched; test | ~200 |
| 4.3 | Virtual free-cluster reporting: cluster allocation honours hidden files, tests cover deleted-then-reused cluster numbers | ~250 |
| 4.4 | TOML config schema for `retention.recentclips_minutes` (default 60), reloadable via `RETENTION_UPDATE` IPC message | ~100 |

**🔍 REVIEW GATE per increment. ✅ TEST GATE:** per-increment
`cargo test -p teslafat retention::` green.

### Phase 4b — Cleanup policy + indexer-driven preservation (Rust)

Per Decision #21, the cleanup worker lives in `teslausb-worker`,
not in Python.

| # | Deliverable | LOC ceiling |
|---|---|---|
| 4b.1 | `teslausb-worker::sei` — port v1's SEI extraction logic to Rust; mmap-based, golden-file parity tests vs. v1 fixtures (H.264 + H.265) | ~600 |
| 4b.2 | `teslausb-worker::indexer` — inotify on `/srv/teslausb/teslacam/`, claim-and-parse loop, writes waypoints/trips/events to `rusqlite` WAL DB | ~500 |
| 4b.3 | `teslausb-worker::cleanup` — periodic worker, GPS-aware deletion, never deletes Sentry/Saved, never deletes Recent with GPS | ~300 |
| 4b.4 | `teslausb-worker` `main.rs` — task supervisor for indexer + cleanup, single binary, `tokio` runtime | ~200 |
| 4b.5 | systemd `teslausb-worker.service`, `User=teslausb`, after `teslafat@0.service` | ~50 |

**🔍 REVIEW GATE per increment. ✅ TEST GATE:** per-increment
green tests; SEI parity test passes byte-for-byte vs. 100 v1
fixtures.

### Phase H4 — Retention + worker on hardware

| # | Step |
|---|---|
| H4.1 | Deploy `teslafat` + `teslausb-worker`. |
| H4.2 | Populate backing tree with 10 known mp4 files (5 with GPS, 5 without, varied mtimes). |
| H4.3 | Confirm Tesla-view shows only files within the retention window. |
| H4.4 | Trigger cleanup worker. Confirm: no-GPS expired clips deleted; GPS clips preserved; Sentry untouched. |
| H4.5 | SSH alive. |

**🔍 REVIEW GATE. ✅ TEST GATE:** all H4.x green.

### Phase 4c — Tesla cache invalidation ("media changed" signal)

**The problem.** Tesla aggressively caches USB filesystem
contents — directory listings, and the contents of files it
reads frequently like `LockChime.wav` and `LightShow.fseq`. A
fresh upload via the web UI lands at
`/srv/teslausb/media/LockChime.wav` instantly, but Tesla
will continue playing the OLD cached chime until something
tells the SCSI layer that the medium has changed. In v1 this
was solved by `partition_mount_service.rebind_usb_gadget()`
after every lock chime / light show change.

**The mechanism.** g_mass_storage's configfs interface
supports a "media change" notification via the LUN file
attribute:

```
echo "" > /sys/kernel/config/usb_gadget/teslausb/functions/mass_storage.0/lun.0/file
# Kernel sends MEDIUM NOT PRESENT to Tesla
sleep 0.2
echo /dev/nbd0 > /sys/kernel/config/usb_gadget/teslausb/functions/mass_storage.0/lun.0/file
# Kernel sends UNIT ATTENTION: NOT READY TO READY CHANGE,
# MEDIUM MAY HAVE CHANGED to Tesla
```

Tesla sees the medium eject and reinsert within ~200 ms,
clears its cache, and re-reads any file it cared about. This
is gentler than a full UDC unbind+rebind because the gadget
itself stays bound and the USB enumeration is preserved —
only the SCSI layer sees the medium change.

**Implementation.**
- `scripts/tesla_cache_invalidate.sh` — does the LUN-clear /
  LUN-set sequence. Idempotent; safe to call from anywhere.
  Returns 0 on success.
- `web/services/cache_invalidation.py` — Python wrapper:
  - `invalidate_now()` — call the script
  - `schedule_invalidation(delay_seconds=2.0)` — coalesce
    multiple calls within a window (user uploading 5 chimes
    in a row triggers ONE invalidation at the end, not 5)
  - Uses a debounce timer with `threading.Timer`; the latest
    call's window wins
- **Every blueprint that mutates user-visible files** (lock
  chimes, light shows, music tracks, future "wraps", etc.)
  calls `schedule_invalidation()` after writing the file.
  The wrapper coalesces so the user sees: upload → "saved"
  → 2 s later → Tesla cache invalidated → ready to use.
- **Files Tesla writes itself** (RecentClips, SentryClips,
  SavedClips, event.json) do NOT trigger invalidation —
  Tesla already knows about its own writes.
- **Brief Tesla recording disruption window.** During the
  ~200 ms eject/reinsert, Tesla cannot write to the USB.
  If Tesla is mid-write of a Sentry clip, that block is
  retried by the SCSI layer; typically invisible. For
  active dashcam recording, Tesla pauses recording for the
  duration of the medium change (≤ 500 ms) and resumes
  with a tiny gap in the video timeline. The user is
  always the one who initiated the change (they uploaded
  a chime), so a sub-second disruption is acceptable UX.
- **Race avoidance.** The web UI shows a brief "Updating
  USB — Tesla will reconnect in a moment..." toast during
  the invalidation window so the user understands the
  expected brief disruption.

**Acceptance criteria:**
- Upload lock chime via web UI → within 3 s, Tesla plays
  the new chime on next lock event
- Upload 5 chimes in rapid succession → exactly ONE
  invalidation fires (verify in journalctl)
- Active Sentry recording continues without filesystem
  corruption (verify with `fsck.exfat` after invalidation
  during recording)
- Invalidation called while teslafat is rebuilding the
  cluster map → invalidation waits, does not corrupt
  the cluster map

**Increment breakdown:**

| # | Deliverable | LOC ceiling |
|---|---|---|
| 4c.1 | `scripts/tesla_cache_invalidate.sh` — idempotent LUN clear/set wrapper, exit codes documented; shell-lint clean | ~80 |
| 4c.2 | sudoers fragment + helper script install path; charter-review for shell-injection vectors | ~30 |
| 4c.3 | Python `services/cache_invalidation.py` — debounce via `threading.Timer`, single-flight coalescing, `tracing` logging | ~150 |
| 4c.4 | Unit tests: 5 rapid calls → 1 invocation; `subprocess.run` mocked, timeout enforced | ~150 (test) |
| 4c.5 | Integration test on dev box: real `tesla_cache_invalidate.sh` against a `g_mass_storage` test gadget under `configfs` | ~200 (test) |

**🔍 REVIEW GATE per increment. ✅ TEST GATE:** all green;
shell lint passes (`shellcheck -S warning`).

### Phase H4c — Cache invalidation on hardware

| # | Step |
|---|---|
| H4c.1 | Deploy script + Python service to `cybertruckusb.local`. |
| H4c.2 | Start `teslafat-test@1` with a real backing tree containing `LockChime.wav` and `Chimes/old.wav`. |
| H4c.3 | Write a NEW `LockChime.wav` to the backing dir. |
| H4c.4 | Trigger `cache_invalidation.invalidate_now()` via Python REPL on the device. |
| H4c.5 | Observe `dmesg`: see medium-eject + medium-ready cycle. SSH alive. |
| H4c.6 | Tear down. (No Tesla involved yet — that's Phase 7 / soak.) |

**🔍 REVIEW GATE. ✅ TEST GATE:** all H4c.x green.

### Phase 5 — Web app (Python Flask)

**Binding constraint from the operator (user, 2026-05-19):**
*"I want the website to look the same as it does now. Same
look, feel and features. Of course some modifications will
need to happen since we won't need to switch between present
mode and edit mode anymore."*

The v1 web UI is the reference design. B-1 is a backend rewrite,
not a UI rewrite. The user-facing experience must be visually
and functionally indistinguishable from v1 except for the small,
explicit list of mode-removal changes below.

**Port-as-is from `main` branch (do NOT redesign):**
- Entire `scripts/web/templates/` tree → `web/templates/`
  (all `base.html`, `mapping.html`, `lock_chimes.html`,
  `light_shows.html`, `settings.html`, `cloud_archive.html`,
  `captive_portal.html`, `system_health.html`, `music.html`,
  partials, etc.)
- Entire `scripts/web/static/` tree → `web/static/` (Inter
  WOFF2 fonts, Lucide SVG icons, CSS with custom-property
  colour tokens, inline-critical CSS strategy, JS modules)
- All blueprints in `scripts/web/blueprints/` with their
  endpoint contracts preserved (URLs, JSON shapes, form
  fields) → `web/blueprints/`
- The UI design system documented in `docs/UI_UX_DESIGN_SYSTEM.md`
  (from `main`) carries over verbatim and is the authority
  for all visual decisions in B-1. Copy it to
  `docs/05-UI-UX-DESIGN-SYSTEM.md` so it lives alongside the
  other B-1 docs. **All UI rules from that doc apply:** no
  emoji (Lucide SVG), CSS custom-property tokens only, dark
  + light mode, 44×44 touch targets, mobile-first bottom tab
  bar (<1024 px), desktop sidebar rail (≥1024 px), WCAG AA
  contrast, inline critical CSS, no JS frameworks, bundle
  fonts locally.
- Map-integrated video panel (no standalone Videos page) —
  the three-tab side panel on the map (Events / Trips / All
  Clips) is preserved exactly.
- Camera switcher in the overlay player with Lucide
  directional icons, two distinct fullscreen affordances
  (Fullscreen = `requestFullscreen()` on `.video-overlay-stage`,
  Maximize = `.maximized` class) — all preserved.
- Disambiguation popup for overlapping clips on the same
  map point — preserved.

**What changes (the minimal surgical edit list):**

1. **Remove all "Edit Mode" / "Present Mode" UI surfaces.**
   The current v1 design already hides this from end users
   (per `UI_UX_DESIGN_SYSTEM.md`: *"No 'Edit Mode' / 'Present
   Mode' in UI — these are internal implementation details"*),
   but internal toggles and the "Network File Sharing"
   status dot remain in v1 to indicate when quick_edit is
   active. In B-1:
   - REPURPOSE the "Network File Sharing" status dot to
     reflect Samba on/off state (green = Samba off, amber
     = Samba currently sharing files over SMB). The dot's
     meaning changes from "quick_edit running" to "Samba
     enabled" — useful at-a-glance status. Hidden when
     Samba is disabled in config.
   - DELETE the auto-trigger that v1 used to flip into
     edit mode before file uploads (no edit mode in B-1;
     uploads are always direct file writes)
   - DELETE the `mode_control.py` blueprint entirely
   - DELETE the `mode_service` import from every blueprint
   - DELETE the JavaScript "polling for mode state" loop
   - DELETE the "Reboot to apply" prompts that v1 sometimes
     showed after mode-related changes
2. **Remove the "fsck status" widget** from Settings →
   System Health. B-1 ships on a single ext4 partition and the
   per-partition FAT/exFAT images of v1 do not exist. Replace
   with a Storage Health card (`blueprints/storage_health.py`)
   that surfaces ext4 mount state, `dumpe2fs` error counters,
   recent kernel I/O errors, and `fstrim` / `e2scrub_all` timer
   status.
3. **Lock chime / light show / music / wraps upload flows:**
   no longer go through quick_edit. They become simple
   `POST /api/lock_chimes/upload` → write file to
   `/srv/teslausb/media/LockChime.wav.partial` → fsync →
   `os.replace` to final name → call
   `cache_invalidation.schedule_invalidation()`. Same JSON
   shape, same UX, same toast message; the mechanism behind
   it is just much simpler.
4. **Settings page:** remove these v1-specific options that
   have no B-1 equivalent:
   - "Boot fsck enabled" toggle (B-1 has no fsck)
   - "Disk image sizes" sliders (B-1 has no disk images;
     volume size is a single setting now)
   - "Quick edit timeout" slider
   - "Mode switch on boot" toggle
   - Replace "Disk image sizes" with a single "Tesla
     volume size (GB)" slider that controls teslafat's
     `volume_size_gb` config — restart-required noted.
5. **Samba (optional, off by default, user-controllable):**
   in v1, Samba was tied to edit mode and toggled by the
   mode service. In B-1, Samba is a first-class user feature:
   - Settings → Network Sharing page with a single on/off
     toggle (default off)
   - When ON: `smbd` + `nmbd` services start, sharing
     `/srv/teslausb/teslacam/TeslaCam/` and `/srv/teslausb/media/`
     RW continuously. The web UI status dot turns amber
     and shows the share path the user can connect to
     (e.g., `\\teslausb.local\TeslaCam`).
   - When OFF: services stop, no SMB exposure.
   - `samba_watcher.py` (inotify on the Samba paths) runs
     only when Samba is enabled; on any file change via
     SMB it calls `cache_invalidation.schedule_invalidation()`
     so Tesla sees the change.
   - Persisted in `/etc/teslausb/teslausb.toml` under
     `[samba] enabled = true` so state survives reboots.
6. **Feature availability:** v1 gated nav items by IMG file
   existence (e.g., Lock Chimes hidden if `usb_lightshow.img`
   missing). In B-1, all features are always available
   because there are no IMG files. Each page shows an
   appropriate empty state ("Upload your first light show!")
   when the backing folder is empty. Music can still be
   disabled via config if the user doesn't want it.
7. **Cloud sync UI:** identical to v1. Reads from the
   simplified backend (no clip_groups table, no
   pipeline_v2) but the user-facing checklist, "Reset
   Counters" button, sync_folders selection, priority
   ordering — all preserved. `RecentClips` is once again
   a valid sync target in B-1 (race-free preservation
   means clips don't disappear under the cloud worker's
   feet) — the operator can opt in via config.

**Port-with-simplification (internal services, UI unchanged):**
- `services/indexer.py` — no loop devices, no quick_edit,
  just `inotify(/srv/teslausb/teslacam/)` → SEI parse → DB write
- `services/cloud_archive.py` — same rclone invocations,
  same priority semantics, reads native files
- `services/file_watcher.py` — inotify on /srv/teslausb/teslacam
  (replaces the v1 inotify on /mnt/gadget/part1-ro)
- `services/lock_chime_service.py` — drop `quick_edit_part2`
  calls; write file + fsync + cache invalidate
- `services/cleanup_service.py` (new) — GPS-aware deletion

**DELETE entirely from v1 (services that B-1 doesn't need):**
- `services/partition_service.py` (no partitions)
- `services/partition_mount_service.py` (no mounts)
- `services/mode_service.py` (no modes)
- `blueprints/mode_control.py`
- `services/fsck.py` and the fsck blueprint
- `services/pipeline_v2/*` (no copier/triage/watcher)
- `services/archive_watchdog.py`
- `services/task_coordinator.py` (no contended SDIO writers)
- `services/file_watcher_service.py` v1 inotify-on-RO-mount

**Add `services/teslafat_client.py`** for IPC to the Rust
daemon (status query, retention policy updates, force
cache invalidate). Pure Python, talks Unix socket.

**Add `services/cache_invalidation.py`** — debounced LUN
clear/set wrapper (see Phase 4c).

**Increment breakdown for Phase 5:**

Each increment is one cohesive UI surface or service. Each
ends with a charter-review gate AND a UI-parity screenshot
diff (where applicable) per `docs/03-CODE-QUALITY-CHARTER.md`
§"Pick the Hard Right" and `docs/05-UI-UX-DESIGN-SYSTEM.md`
(once copied from v1).

| # | Deliverable | LOC ceiling |
|---|---|---|
| 5.1 | Copy `docs/UI_UX_DESIGN_SYSTEM.md` from v1 to `docs/05-UI-UX-DESIGN-SYSTEM.md`. Adjust the small list of mode-removal edits. | doc-only |
| 5.2 | Flask app skeleton: `web/teslausb_web/app.py`, factory pattern, blueprint registration stubs, `tracing` logging via stdlib `logging`, gunicorn entry point. Pytest fixture for app client. | ~300 |
| 5.3 | Static asset port: `web/teslausb_web/static/` from v1's `scripts/web/static/` (Inter fonts, Lucide SVGs, CSS tokens, JS). Screenshot diff at 375 px + 1280 px in dark + light shows zero visible delta on `base.html` shell. | ~50 (copy) |
| 5.4 | Templates skeleton: `base.html`, layout partials, theme switcher. Same UI rules as v1 (no emoji, CSS tokens only). | ~200 |
| 5.5 | `services/teslafat_client.py` — Unix socket IPC client with retry, async-friendly. Unit tests against a mock socket. | ~250 |
| 5.6 | `services/cache_invalidation.py` (covered by 4c.3, just register with the app). | tiny |
| 5.7 | `blueprints/system_health.py` + template — port from v1, replace fsck widget with the Storage Health card delivered by `blueprints/storage_health.py`. Screenshot diff vs. v1. | ~300 |
| 5.8 | `blueprints/lock_chimes.py` + service + template + JS — full upload/list/set-active flow, drop quick_edit, add cache invalidate. Screenshot diff. | ~500 |
| 5.9 | `blueprints/light_shows.py` + service + template + JS — same pattern. Screenshot diff. | ~400 |
| 5.10 | `blueprints/wraps.py` + service + template + JS — PNG dimension validation per v1 rules. Screenshot diff. | ~400 |
| 5.11 | `blueprints/music.py` + service + template + JS. Screenshot diff. | ~400 |
| 5.12 | `blueprints/boombox.py` + service + template + JS — 5-file alphabetical cap. Screenshot diff. | ~300 |
| 5.13 | `blueprints/mapping.py` + service + template + JS + overlay player. Heaviest single increment; may need sub-split if it busts the 500 LOC ceiling (then 5.13a/b/c). | ~500 |
| 5.14 | `blueprints/cloud_archive.py` + service. Reads the simplified backend; same priority semantics; same UI. Screenshot diff. | ~400 |
| 5.15 | `blueprints/captive_portal.py` + template — verbatim from v1. | ~150 |
| 5.16 | `blueprints/settings.py` + template — port + remove mode-related options + add Samba toggle + add Tesla-volume-size slider. Screenshot diff. | ~400 |
| 5.17 | `services/samba_service.py` — start/stop `smbd`/`nmbd`, manage `/etc/samba/smb.conf.teslausb.d/` snippet, write inotify watcher (`samba_watcher.py`) that triggers cache_invalidation on file change. | ~300 |
| 5.18 | `services/cleanup_service.py` — Python orchestrator for the Rust cleanup worker (if needed for UI surfacing), plus on-demand "purge orphans" UI button. | ~200 |
| 5.19 | gunicorn config + nginx config snippet under `config/` for review (deployment happens in Phase 6). | ~80 |

**🔍 REVIEW GATE per increment.** Reviews for 5.7–5.16 also
include the UI-parity screenshot diff per Phase H5 below.
**✅ TEST GATE:** per-increment `pytest -k <blueprint>` green;
mypy clean; ruff clean; no `# type: ignore` or `Any` added.

### Phase H5 — Web app on hardware

Runs after every batch of 3 web increments (i.e., H5.a after
5.4, H5.b after 5.7, H5.c after 5.10, etc.). The Phase H5 helper
script:

| # | Step |
|---|---|
| H5.x.1 | Rsync `web/teslausb_web/` to `/home/pi/teslausb-b1/web/`. |
| H5.x.2 | `pip install -e .` inside a venv at `/home/pi/teslausb-b1/venv/`. |
| H5.x.3 | Start gunicorn under a test-only systemd unit bound to the Unix socket at `/run/teslausb/gunicorn.sock`; bring up nginx on port 80 in front. v1's `gadget_web.service` is masked (per H0), so port 80 is free. |
| H5.x.4 | Curl every new endpoint added in the batch. Verify 200 + expected JSON / HTML. |
| H5.x.5 | Take screenshots via `chromium --headless` at 375×667 and 1280×800, both `prefers-color-scheme: dark` and light. |
| H5.x.6 | Diff against the v1 baseline screenshots taken in Phase 0. Any non-trivial visual delta requires charter-review approval (Pillar 1: UI parity is non-negotiable). |
| H5.x.7 | Tear down test gunicorn. SSH alive. |

**🔍 REVIEW GATE on screenshots + endpoint outputs.**
**✅ TEST GATE:** zero visual delta on shared surfaces; all
mode-removal deltas in the documented allow-list.

### Phase 6 — setup.sh + uninstall.sh

> *Operator directive (2026-05-19):* **"We need to make sure our
> solution has a script or an installer that can be run to fully
> configure the Raspberry PI device to run the solution. This is
> for installing any services, making sure we have the right
> virtual memory config, shutting down unnecessary components on
> the device, etc. Similar to how the past version of this
> solution used setup_usb.sh."**

The installer is a **load-bearing deliverable** — B-1 is not
production-ready until a fresh Raspberry Pi OS Lite SD card can be
turned into a fully-configured TeslaUSB device by running ONE script.
"Manually `apt install`, manually create users, manually load kernel
modules" (as we are doing for H1) is acceptable ONLY for
hardware-smoke phases (H1–H4) and MUST be replaced by `setup.sh`
before Phase 7 soak.

| # | Deliverable | LOC ceiling |
|---|---|---|
| 6.1 | `setup.sh` package install (`nbd-client`, `nginx`, `python3-venv`, `network-manager`, `watchdog`, `dnsmasq-base`, `hostapd`, kernel headers if needed) + idempotency check + `--dry-run` flag. **DO NOT install `rustup` / `cargo` / `gcc` / `build-essential`** — building Rust on a Pi Zero 2 W is forbidden by ADR-0008; the device runs cross-compiled binaries only. | ~200 |
| 6.2 | `setup.sh` user/group creation (`teslausb` system user, sudoers fragment install) | ~100 |
| 6.3 | `setup.sh` per-LUN data root creation at `/srv/teslausb/teslacam/` + `/srv/teslausb/media/`, idempotent. Creates plain directories on whatever filesystem hosts `/srv` (the live device is ext4; teslafat + worker only need POSIX I/O, so the FS underneath is opaque — revised 2026-05-21 after the btrfs subvolume path was abandoned). | ~200 |
| 6.4 | `setup.sh` systemd unit install (teslafat@0, teslafat@1, teslausb-worker, teslausb-web, nginx, watchdog) | ~150 |
| 6.5 | `setup.sh` NetworkManager + AP config — IDEMPOTENT, never overwrites without `.b1-backup` siblings | ~250 |
| 6.6 | `setup.sh` boot cmdline + config.txt edits — IDEMPOTENT, always with backup siblings, dry-run shows diff before apply | ~200 |
| 6.7 | `setup.sh` watchdog priority drop-in + sshd-protect drop-in (mirror v1's safeguards) | ~80 |
| 6.8 | `setup.sh` **memory & VM tuning** — 1 GB persistent swap file at `/var/swap/b1.swap`, `/etc/fstab` entry, `vm.swappiness=10`, `vm.min_free_kbytes=8192`, kernel.panic=10 sysctls dropped in at `/etc/sysctl.d/90-teslausb-b1.conf`. (Mirrors v1's `optimize_memory_for_setup` minus the lightdm-disable, which is covered below.) | ~120 |
| 6.9 | `setup.sh` **disable unnecessary components** — `systemctl mask` for `lightdm.service`, `pipewire.service`, `pipewire.socket`, `wireplumber.service`, `colord.service`, `cups.service`, `cups.socket`, `cups-browsed.service`, `triggerhappy.service`, `triggerhappy.socket`, `avahi-daemon.service` if not needed for `.local` resolution (decide at impl time), `ModemManager.service`. Each mask is conditional on the unit existing; idempotent. Reclaims ~30–50 MB RAM on a Pi Zero 2 W. (Mirrors v1's desktop-services-disable in `setup_usb.sh`.) | ~80 |
| 6.10 | `setup.sh` final enable + start + post-start health check | ~150 |
| 6.11 | `uninstall.sh` — exact inverse, restore from `.b1-backup` siblings; `--purge` flag for data wipe. Re-enables / un-masks every unit row 6.9 masked. Removes the swap file row 6.8 created. | ~450 |
| 6.12 | Both scripts pass `shellcheck -S warning` and have a `--help` output that documents every flag | n/a |

**Idempotency contract:** every row 6.1–6.10 MUST be safe to re-run
on an already-configured device — no duplicated fstab lines, no
double-masked units, no overwritten config without backup. The
charter-review for each increment will verify this with a `setup.sh
&& setup.sh` smoke test (second run is a no-op).

**🔍 REVIEW GATE per increment.** Each setup increment is
also separately exercised by Phase H6.

### Phase H6 — setup.sh on a clean Pi

This phase requires either (a) a SECOND Pi the operator
reserves for clean-install testing, or (b) booting
`cybertruckusb.local` from a freshly-flashed SD card with
the v1 SD card preserved. Operator confirms which.

| # | Step |
|---|---|
| H6.1 | Flash Raspberry Pi OS Lite Bookworm to a SD card. SSH-enable. WiFi pre-configured. |
| H6.2 | Boot, confirm SSH alive. Capture pristine baseline (`journalctl -b`, `dpkg -l`, `systemctl list-units`, `free -m`, `cat /proc/swaps`). |
| H6.3 | Run `setup.sh --dry-run`. Review every proposed change. |
| H6.4 | Run `setup.sh`. Observe boot to all-green services within target (~60 s). Verify `free -m` shows the 1 GB swap and the masked units no longer show in `systemctl list-units --state=running`. |
| H6.5 | Reboot. Confirm services start automatically, SSH still works, WiFi still works. Re-verify swap + masked units survived reboot. |
| H6.6 | **Re-run `setup.sh`** — confirm it is a true no-op (zero changes reported; exit 0). This is the idempotency gate. |
| H6.7 | Run `uninstall.sh --purge`. Confirm device returns to pristine baseline (diff `dpkg -l` and `systemctl list-units` — only B-1-installed packages remain in "not removed" list, all B-1 units gone, swap file removed, sysctls reverted). |
| H6.8 | Reboot once more. Confirm pristine boot. |

**🔍 REVIEW GATE on the script + transcripts.**
**✅ TEST GATE:** all H6.x green; pristine restore verified.

### Phase 7 — Integration + hardware soak

| # | Deliverable |
|---|---|
| 7.1 | `tests/integration/` Rust + Python suite — end-to-end via the daemon + worker + Flask, no Tesla involvement. |
| 7.2 | Synthetic load harness simulating Tesla writes (6 cameras × ~60 s × continuous). Asserts: no queue backlog growth, no NEAR-MISS, all GPS clips preserved. |
| 7.3 | Cache-invalidation acceptance harness on the live truck (operator drives) — upload chime A, set active, observe lock event; repeat with chime B; record the elapsed time between "set active" and "Tesla plays chime B". Target < 3 s. |
| 7.4 | 24-hour soak on a parked truck with Sentry active. Capture `journalctl`, `vmstat 60`, `top -bn1` every 5 min. Assert: zero unclean shutdowns, RAM working set < 400 MB, no SDIO bus errors. |
| 7.5 | 72-hour soak with the truck driven daily (operator's normal usage). Same captures, plus end-of-soak: every preserved GPS clip is in `geodata.db` and on disk. Zero data loss. |

**🔍 REVIEW GATE on test reports.** **✅ TEST GATE:** all
acceptance harnesses + 24h + 72h soak green.

### Phase 8 — Documentation

| # | Deliverable |
|---|---|
| 8.1 | `README.md` — short, points to docs/ |
| 8.2 | `docs/architecture.md` |
| 8.3 | `docs/fs-synthesis.md` |
| 8.4 | `docs/tesla-cache-invalidation.md` |
| 8.5 | `docs/setup.md` |
| 8.6 | `docs/uninstall.md` |
| 8.7 | `docs/development.md` (includes the hardware-test framework) |
| 8.8 | `docs/charter-review-playbook.md` — how to invoke the skill, how to interpret findings, how the gates work |

**🔍 REVIEW GATE per doc.** **✅ TEST GATE:** `mkdocs build`
(if used) clean; markdown lints clean; all code samples in
docs are extracted into testable snippets that CI runs.

---

## Tesla on-USB folder/filename conventions (canonical)

**Source of truth:** v1's working implementation on `main @ 75bfca0`.
v1 has shipped these features successfully against real Tesla
firmware; folder names and filenames below are verified, NOT
guessed. Specific v1 source files are cited per row.

**Case sensitivity matters.** Tesla's firmware looks for these
names with EXACT case. exFAT preserves case in long file names
(both v1's existing FAT32 setup and B-1's exFAT default) so the
correct casing makes it through to Tesla. Do not lowercase, do
not titlecase differently, do not let `setup.sh` or any installer
ever create these directories with the wrong case "to fix a typo".

### LUN 0 (`/srv/teslausb/teslacam/`) — TeslaCam drive

Tesla writes here; we never touch it except for the retention
shim and indexer.

| What | Path / name | Notes |
|---|---|---|
| TeslaCam root | `TeslaCam/` | Required folder name — Tesla looks for this exact directory at the USB root. |
| Recent dashcam ring | `TeslaCam/RecentClips/` | Tesla rotates this ~60 min ring. B-1's retention shim hides expired-from-Tesla's-view files but preserves backing files. |
| Sentry events | `TeslaCam/SentryClips/EVENT_TS/` | One sub-folder per event. Format: `YYYY-MM-DD_HH-MM-SS` (Tesla's onboard local time). |
| User-saved events | `TeslaCam/SavedClips/EVENT_TS/` | Same per-event layout as Sentry. |
| Per-camera clip files | `EVENT_TS/<ts>-<camera>.mp4` | Camera suffix is one of: `front`, `back`, `left_repeater`, `right_repeater`, `left_pillar`, `right_pillar`. v1 ports the same suffix list — see `scripts/web/services/mapping_queries.py` from v1. |
| Per-event metadata | `EVENT_TS/event.json` | Tesla-written JSON with event type, timestamp, GPS lat/lon, reason code. Used by the indexer to classify events without re-parsing video. |
| Per-event thumbnail | `EVENT_TS/thumb.png` | Tesla-written small JPEG/PNG of the front camera. B-1 does not generate or rely on thumbnails (intentional from v1's redesign). |

The `RecentClips` folder is NOT chunked into per-event subdirs —
it's a flat list of `<ts>-<camera>.mp4` files. Sentry/Saved use
the per-event subdir layout.

### LUN 1 (`/srv/teslausb/media/`) — user-managed media drive

Per operator directive (2026-05-19): the active lock chime
`LockChime.wav` lives at the **root** of this drive; everything
else lives in feature folders alongside it.

| What | Path / name | Format | Size cap | v1 source |
|---|---|---|---|---|
| **Active lock chime** | `LockChime.wav` (root, exact case) | WAV — PCM 16-bit, 44.1 or 48 kHz, mono or stereo | 1 MiB | `scripts/web/services/lock_chime_service.py`; `config.yaml: web.lock_chime_filename` |
| Lock-chime library | `Chimes/` | WAV (same rules as active) | per-file 1 MiB | `scripts/web/services/lock_chime_service.py`; `config.yaml: web.chimes_folder` — **TeslaUSB-only construct**; Tesla never reads this folder, it's storage for the UI's "select active chime" picker |
| **Light shows** | `LightShow/` | `.fseq` (xLights), companion `.mp3` or `.wav` | per `web.max_upload_size_mb` (default 2 GiB) | `scripts/web/services/light_show_service.py`; `config.yaml: web.lightshow_folder` |
| **Wraps** (PNG backgrounds) | `Wraps/` | PNG only | 1 MiB per file, 10 files max, 512×512 to 1024×1024 px | `scripts/web/services/wrap_service.py` — `WRAPS_FOLDER = "Wraps"` |
| **Music** | `Music/` | `.mp3`, `.flac`, `.wav`, `.aac`, `.m4a` | per `web.max_upload_size_mb` | `scripts/web/services/music_service.py` — hardcodes folder name `"Music"` |
| **Boombox** (pedestrian speaker custom sounds) | `Boombox/` | `.mp3` or `.wav` | 1 MiB per file, 5 files max (Tesla loads first 5 alphabetically) | `scripts/web/services/boombox_service.py` — `BOOMBOX_FOLDER = "Boombox"` |

**Important Tesla-firmware constraints captured from v1:**

1. **Boombox 5-file alphabetical cap is Tesla's behaviour**,
   not a TeslaUSB choice. Tesla loads the first 5 files in
   alphabetical order and silently ignores the rest. v1 enforces
   the cap on upload so users see an error instead of a silent
   miss. B-1 must do the same.
2. **Wraps dimensions are 512–1024 px square.** Outside this
   range, Tesla rejects the file silently (won't appear in the
   in-car Background selector). v1 validates on upload via
   `wrap_service.validate_wrap_dimensions`.
3. **NHTSA 22V-068 (Feb 2022)**: custom Boombox sounds only
   play to occupants when parked, not over the pedestrian
   warning speaker in motion. This is a Tesla-firmware change
   we cannot work around (and shouldn't try — it's a safety
   recall). UI just informs the user.
4. **Tesla aggressively caches USB filesystem contents**, so
   any change to `LockChime.wav`, files inside `LightShow/`,
   `Chimes/`, `Wraps/`, `Boombox/`, or `Music/` REQUIRES a
   cache-invalidation event (Phase 4c) to be picked up. v1
   does this via `rebind_usb_gadget`; B-1 does it via the
   gentler configfs LUN clear/set on LUN 1 only (LUN 0 is
   never disturbed — Tesla may be actively recording).
5. **`Chimes/` is a TeslaUSB construct.** Tesla itself does
   NOT scan a `Chimes/` folder for lock chimes — it only ever
   plays the file named `LockChime.wav` at the root. Our
   `Chimes/` folder is purely storage so the user can keep a
   library and the web UI's "Set Active" button copies the
   chosen file to `LockChime.wav` + invalidates cache. Keep
   this distinction in mind when writing new code: writing
   to `Chimes/` requires no cache invalidation; replacing
   `LockChime.wav` does.

**Filename collision rule (also from v1):** uploaded files in
`Chimes/`, `LightShow/`, `Wraps/`, `Music/`, `Boombox/` must
never be named `LockChime.wav` (case-insensitive). v1 explicitly
rejects this in `lock_chime_service.py` — a user-uploaded
"LockChime.wav" placed in `Chimes/` would get copied to root
when "Set Active" is clicked, producing an infinite-loop UX bug.

**Why no third LUN for Music/Boombox** (v1 had one):
v1 split Music/Boombox onto a third optional `usb_music.img` so
that quick_edit on the LightShow/Chimes/Wraps drive (part2)
wouldn't disturb music playback. In B-1, the operator explicitly
asked for a single `media/` folder containing all user content
(2026-05-19), so we run on two LUNs. Cache invalidation on LUN 1
during a chime upload will briefly pause music playback (~200 ms
medium-not-present, same as v1's quick_edit). This is acceptable
UX because the user is the one who initiated the change.

**B-1 `/srv/teslausb/media/` layout (final):**

```
/srv/teslausb/media/                # LUN 1 root, exposed as exFAT (or FAT32)
├── LockChime.wav                   # ACTIVE chime, exact case, root
├── Chimes/                         # TeslaUSB chime library (not read by Tesla)
│   ├── alert.wav
│   ├── boop.wav
│   └── ...
├── LightShow/                      # Tesla reads this folder
│   ├── lightshow.fseq
│   ├── lightshow.mp3               # OR lightshow.wav
│   └── ...
├── Wraps/                          # Tesla reads this folder
│   ├── background1.png             # 512–1024 px square, PNG, ≤ 1 MiB
│   └── ...
├── Music/                          # Tesla reads this folder
│   ├── Artist/Album/Track.mp3
│   └── ...
└── Boombox/                        # Tesla reads this folder
    ├── 01_horn.wav                 # first 5 alphabetically loaded
    ├── 02_alert.mp3
    └── ...
```

The exact strings above (`LockChime.wav`, `Chimes`, `LightShow`,
`Wraps`, `Music`, `Boombox`) are baked into B-1's `teslausb-core`
crate as `pub const` items so no caller can typo them. Tests will
assert that `setup.sh`, the worker, the web app, and the
documentation all reference the constants and nothing else.

```rust
// rust/crates/teslausb-core/src/paths.rs (planned)
pub const TESLA_LOCK_CHIME_FILENAME: &str = "LockChime.wav";
pub const TESLA_CHIMES_DIR: &str = "Chimes";
pub const TESLA_LIGHTSHOW_DIR: &str = "LightShow";
pub const TESLA_WRAPS_DIR: &str = "Wraps";
pub const TESLA_MUSIC_DIR: &str = "Music";
pub const TESLA_BOOMBOX_DIR: &str = "Boombox";
pub const TESLA_TESLACAM_DIR: &str = "TeslaCam";
pub const TESLA_TESLACAM_RECENT: &str = "RecentClips";
pub const TESLA_TESLACAM_SENTRY: &str = "SentryClips";
pub const TESLA_TESLACAM_SAVED: &str = "SavedClips";
```

The Python side (`web/teslausb_web/tesla_paths.py`) re-exports
the same strings from the same source-of-truth — TOML config
loads them as defaults and the strings can be overridden ONLY
via the IPC `STATUS` query (i.e. they're read-only from Python's
perspective; only Rust owns the constants).

---

## Non-negotiable invariants

These come from the v1 codebase's lessons (see
`docs/02-LEARNINGS.md` for the full list):

1. **USB gadget visible to Tesla within ~3 s of boot.** This
   includes teslafat startup + NBD client connect + g_mass_storage
   bind. teslafat's cold-start FS synthesis must complete in
   ≤ 1 s (budget for everything else: ~2 s).
2. **The Pi can be hard-power-cut at any moment.** A vehicle
   sleep cycle, a 12V drop, an owner unplugging the USB cable —
   all are normal events that happen multiple times per day. The
   system must:
   - Never leave a corrupt or partially-written backing file
     that the indexer would mistake for a real clip (atomic
     `.partial` → rename pattern)
   - Recover all state on cold start without manual intervention
   - Never require a human to run `fsck` or "press a button"
   - Tolerate dozens of cold cuts per day with zero data loss
3. **SSH is sacred.** sshd cannot be stopped/masked.
4. **Safe-mode boot.** 3+ reboots in 10 min → skip teslafat
   service so the user can SSH in and recover. **Critical
   given the "powered off at any moment" reality:** without
   this, a bad teslafat update could brick the device the
   moment the vehicle's next sleep cycle reboots it the third
   time.
5. **Hardware watchdog 90 s.** Workers must yield.
6. **Crash-safe writes.** Every Tesla SCSI write is `fdatasync(2)`'d
   to the underlying ext4 file before the NBD/SCSI completion
   is returned. The kernel's NBD layer + g_mass_storage will
   propagate this back to Tesla's SCSI request, so Tesla only
   sees "write complete" when the data is durable on the SD card.
   **NBD's FUA flag and `NBD_CMD_FLUSH` map directly to this.**
7. **All daemon state is reconstructible from disk alone.** No
   in-memory caches that, if lost in a power cut, corrupt the
   user's view of their data. The cluster_map is rebuilt from
   scratch on every startup by walking `/srv/teslausb/teslacam/`.
8. **No partial writes survive as real files.** New files land
   at `<name>.partial`; only the final directory-entry-finalize
   from Tesla triggers `rename(2)` to the visible name. A power
   cut mid-write leaves a `.partial` orphan that the cleanup
   worker reaps; the indexer never sees a half-formed MP4.
9. **No emoji in UI.** Lucide SVG icons only.
10. **Mobile-first.** 44×44 touch targets.
11. **Pi Zero 2 W resource budget.** ≤ 60 MB RSS for `teslafat`,
    ≤ 150 MB for the Flask app.
12. **UI parity with v1.** The web UI is a 1:1 port from `main`,
    except for the explicit mode-removal edit list documented
    in Phase 5. `docs/05-UI-UX-DESIGN-SYSTEM.md` (copied from
    v1's `docs/UI_UX_DESIGN_SYSTEM.md`) is the single
    authority for all visual decisions. Side-by-side
    screenshot comparison gates cutover.
13. **Code Quality Charter is binding.** Every line of B-1 code
    is reviewed against `docs/03-CODE-QUALITY-CHARTER.md`.
    Five pillars: no code smells, best architecture practices,
    no shortcuts, fix bugs immediately, no dead code. CI
    enforces what tooling can (clippy, ruff, mypy, coverage
    gates, ADR discipline); reviewers enforce the rest. A red
    CI is a blocked PR — no "I'll fix it after merge."

---

## Language choice rationale

| Component | Language | Why |
|---|---|---|
| `teslafat` (NBD daemon, per-LUN) | **Rust** | Zero-overhead I/O, ~30-60 MB RSS per process, no GC pauses, byte-level control over FAT/exFAT structures, `tokio` for async NBD, mature crates (`bytes`, `tokio`, `tracing`, `rusqlite`) |
| `teslausb-worker` (inotify, SEI, indexer, cleanup, cloud) | **Rust** | CPU-bound work (SEI parsing) + memory-pressured work (10K-file inotify queue) + frequent SQLite writes all benefit hugely from no-GC, no-GIL, native byte handling. ~5-10× faster than Python equivalent, ~3-5× less RAM. |
| `teslausb-core` (shared lib used by both Rust binaries) | **Rust** | DRY for config, IPC, DB schema, SEI parser, observability — no duplication across crates |
| `teslausb-web` (Flask web UI) | **Python (Flask + Jinja)** | UI parity with v1 is a binding constraint (templates and blueprints port one-to-one). The web app is not on any latency-sensitive path — peak request rate is ~1/min during user interaction. Rewriting the substantial Jinja+blueprint code in Rust (e.g., axum + minijinja) would be massive effort for noise-level perf gain. Behind nginx + gunicorn, runs as `teslausb` user. |
| Glue scripts (`setup.sh`, `present_usb.sh`, etc.) | **Bash** | Simple, no runtime deps, idiomatic for systemd integration |
| `refresh_cloud_token.py` (NM dispatcher) | **Python** | NetworkManager dispatchers are conventionally Python; trivial code |

Rust was chosen over C (memory safety), Go (GC pauses + larger
RSS than Rust), and Python (GIL + 5-10× higher CPU per request
+ ~50 MB RSS minimum per process). The decision to put SEI
parsing, indexing, cleanup, and cloud upload into Rust as well
(not just teslafat) is the new "if faster in Rust, do it in Rust"
directive from the operator (2026-05-19).

**Why NOT rewrite the web UI in Rust:** the web app is roughly
3000 LOC of blueprints + 50KB of Jinja templates + a few JS modules.
Rewriting all that in Rust (e.g., `axum` + `minijinja` + `tower`)
would take weeks and gain ~70 MB of RAM that the user can't see and
zero user-facing latency improvement (HTTP responses are bottlenecked
on DB queries and rclone, not on framework overhead). The 70 MB of
RAM savings is already covered by moving the background work out of
Flask into the Rust worker. Net: same RAM, much less effort, UI
parity guaranteed.

**Why NOT put cloud upload into Python (the rclone wrapper is dumb
glue):** consistency. With cloud in Python, the web app needs a
background thread, which needs `task_coordinator`, which is what we
just removed. Keeping the daemon side single-language means one
flock owner, one DB connection pool, one supervisor pattern.

---

## Decisions (locked in)

| # | Question | Decision | Source |
|---|---|---|---|
| 1 | FAT32 or exFAT? | **Both.** exFAT default for `volume_size_gb > 32`, FAT32 for ≤32. `Filesystem` trait abstracts. | Phase 2, invariant 11 (user 2026-05-19) |
| 2 | LFN case preservation? | **Yes** — Tesla's filenames are case-significant. | (decided 2026-05-19) |
| 3 | Cluster size? | **Auto** from volume size, matching `mkfs.vfat` / `mkfs.exfat` defaults. | (decided 2026-05-19) |
| 4 | NBD or BUSE? | **NBD newstyle** over Unix socket. | (decided 2026-05-19) |
| 5 | Unix socket or TCP? | **Unix socket** for security + zero overhead. | (decided 2026-05-19) |
| 6 | `RecentClips` cloud sync target? | **Safe in B-1, opt-in.** Race elimination means cloud worker can't lose a clip mid-upload. Config flag `cloud.sync_folders` may include `RecentClips`. | Phase 4b (decided 2026-05-19) |
| 7 | Samba? | **Optional, off by default.** User-controllable from Settings → Network Sharing. Shares `/srv/teslausb/teslacam/TeslaCam/` and `/srv/teslausb/media/` RW continuously when on. inotify in `teslausb-worker` covers Samba writes and triggers cache invalidation. | Phase 5 edit list, LEARNINGS (user 2026-05-19) |
| 8 | Mode toggle in UI? | **Removed.** No modes in B-1. | Phase 5 edit list |
| 9 | One LUN or many? | **Two LUNs**: TeslaCam (LUN 0) + media (LUN 1, lock chimes / lightshows / wraps / music). Isolates cache invalidation on media from active dashcam recording. | (user 2026-05-19) |
| 10 | Volume capacity reporting? | **Actual SD free space − headroom.** A 128 GB SD with ~110 GB free → advertise ~100 GB. Re-checked at boot. Tesla never sees more than is real. | (user 2026-05-19) |
| 11 | btrfs auto-snapshots? | **No.** B-1 ships on ext4; snapshots ruled out architecturally. Cloud sync is the backup. Web UI deletes are final. | (user 2026-05-19) |
| 12 | Music & Wraps subsystems? | **Both in scope.** Reimplemented in Rust (worker side) + Python (UI side); user-facing behaviour matches v1 but no v1 code is reused. | (user 2026-05-19, revised 2026-05-19) |
| 13 | Web UI auth? | **Optional basic auth** via TOML config (`web.auth.username` / `web.auth.password_hash`). Off by default. nginx layer can also add TLS later. | (user 2026-05-19) |
| 14 | teslafat upgrade strategy? | **Brief outage acceptable** (~1-3 s). `systemctl restart teslafat@N` is the upgrade. Tesla sees medium gone, then back. | (user 2026-05-19) |
| 15 | cluster_map memory model? | **Extent-based per file**, NOT per-cluster. Fits comfortably under 1 MB for 10K files even at 256 GiB volumes with 32 KiB clusters. | (decided 2026-05-19) |
| 16 | `.partial` reap policy? | **Inactivity-based**, not absolute age. A `.partial` with no `pwrite()` activity for ≥ 5 min is considered abandoned. Active long writes (multi-GB continuous recording) are safe. | (decided 2026-05-19) |
| 17 | Unrecoverable teslafat backend error? | **Crash + systemd restart.** `Restart=always RestartSec=1`. | (decided 2026-05-19) |
| 18 | Database schema in B-1? | **Designed fresh.** Single consolidated `/var/lib/teslausb/teslausb.db` with WAL mode. Tables: `files`, `trips`, `waypoints`, `events`, `cloud_uploads`. NO queue tables (no `archive_queue`, `indexing_queue`, `pipeline_queue` — events drive everything inotify→inline). v1 schema is reference material only; column names and types are revisited for clarity. | (revised 2026-05-19) |
| 19 | SEI parser implementation? | **Rust, from scratch.** New `crates/teslausb-core/src/sei/` module. H.264 NAL type 6 + H.265 SEI prefix + mvhd creation_time + Tesla GPS protobuf. ~5-10× faster than Python `sei_parser.py`, ~10× less RAM. Verified against v1 fixtures during Phase 3 (parity test suite). | (revised 2026-05-19 per "if faster in Rust, do it in Rust") |
| 20 | Indexer implementation? | **Rust, inside `teslausb-worker`.** inotify → SEI parse → SQLite write, all in one tokio task. No Python in the hot indexing path. | (new, 2026-05-19) |
| 21 | Cleanup worker implementation? | **Rust, inside `teslausb-worker`.** GPS-aware retention, `.partial` reaping, capacity pressure. | (new, 2026-05-19) |
| 22 | Cloud uploader implementation? | **Rust, inside `teslausb-worker`.** Queue management + rclone subprocess driver. Python web UI only reads upload state, never schedules work. | (new, 2026-05-19) |
| 23 | Config format? | **TOML, not YAML.** Rust-native (`serde`+`toml` crate), Python stdlib (`tomllib`, no PyYAML dep), much less foot-gunny than YAML (no significant whitespace, no octal-vs-int, no `Norway problem`). Single file: `/etc/teslausb/teslausb.toml`. | (new, 2026-05-19) |
| 24 | Web framework? | **Flask + Jinja, behind gunicorn behind nginx.** UI parity is binding (templates port verbatim). Flask is not a hot path — gunicorn runs as the `teslausb` user and binds the Unix socket at `/run/teslausb/gunicorn.sock` (no TCP port, no privileged bind). nginx on port 80 (binds privileged, drops to www-data) handles captive portal regex + reverse proxy + static asset serving. No Python service ever runs as root. | (new, 2026-05-19; updated 2026-05-21 to drop the legacy `127.0.0.1:8080` reference — production uses the Unix socket, port 80 is the only public port) |
| 25 | USB gadget setup? | **Pure configfs / `usb_f_mass_storage` module.** NO `g_mass_storage` module load. Configfs is more flexible and is required anyway for cache invalidation. v1's hybrid approach is dropped. | (new, 2026-05-19) |
| 26 | Filesystem paths? | **FHS standard.** `/etc/teslausb/`, `/srv/teslausb/`, `/var/lib/teslausb/`, `/run/teslausb/`, `/usr/local/bin/teslausb-*`. No `/home/pi/TeslaUSB`. No `~/ArchivedClips`. | (new, 2026-05-19) |
| 27 | SDIO write coordination? | **`fcntl(LOCK_EX)` on `/run/teslausb/sd-write.lock`.** Crash-safe (kernel releases on process death), language-agnostic (Rust + Python both have first-class flock), no in-process state to corrupt. Replaces v1's Python `task_coordinator`. | (new, 2026-05-19) |
| 28 | Background worker decomposition? | **Single `teslausb-worker` Rust binary** for inotify + SEI + indexer + cleanup + cloud + cache-invalidation. Internal tokio tasks are fault-isolated via supervisor pattern. NOT separate processes (saves ~40 MB duplicated runtime). | (new, 2026-05-19) |
| 29 | Production WSGI server? | **gunicorn (single sync worker).** Flask dev server NEVER runs in production. gunicorn binds Unix socket; nginx proxies. | (new, 2026-05-19) |
| 30 | Database driver in Rust? | **`rusqlite` with bundled SQLite + WAL mode.** Bundled SQLite avoids version skew with Python's `sqlite3` and Pi OS's `libsqlite3-0`. WAL mode required (single-writer multi-reader, what we have). | (new, 2026-05-19) |
| 31 | Logging backend? | **journald only.** `tracing-journald` for Rust, stdlib `logging.handlers.SysLogHandler` for Python. No log files on the SD card (wear + complexity). | (new, 2026-05-19) |

---

## v1 carry-forwards we are NOT taking

This is the explicit anti-anchoring list. Every item below was the
v1 way; B-1 deliberately chooses a different way and the new way is
named.

| v1 pattern | What B-1 does instead | Why |
|---|---|---|
| `g_mass_storage` kernel module | `usb_f_mass_storage` via configfs | Required anyway for cache invalidation; cleaner |
| YAML config (`config.yaml`) | TOML config (`/etc/teslausb/teslausb.toml`) | Rust-native, no foot-guns, Python stdlib supports it |
| Flask running as root on port 80 | nginx (port 80, root binds → www-data) → gunicorn (`teslausb` user) → Flask | Massive attack-surface reduction; standard ops pattern |
| Flask dev server in production | gunicorn behind nginx | Real WSGI server, real reverse proxy |
| Python `sei_parser.py` | Rust `crates/teslausb-core/src/sei/` | 5-10× CPU, ~10× less RAM, no GIL contention |
| Python `mapping_service.py` indexer | Rust task inside `teslausb-worker` | CPU-bound; runs in supervisor-managed tokio task |
| Python `file_watcher_service.py` inotify | Rust `notify` crate inside `teslausb-worker` | One language for daemon side; less RAM |
| Python `cloud_archive_service.py` worker | Rust task inside `teslausb-worker` | Eliminates Python background-thread complexity in Flask process |
| Python `task_coordinator.py` (in-process mutex) | `fcntl(LOCK_EX)` on a sentinel file | Crash-safe, language-agnostic, kernel-managed |
| Multiple SQLite DBs (`geodata.db`, `cloud_sync.db`, …) | Single `/var/lib/teslausb/teslausb.db` (WAL) | One backup, one schema, one consistency story |
| v1 SQLite schema (verbatim port) | Fresh schema, no legacy tables, columns renamed for clarity | No `indexing_queue`, no `pipeline_queue`, no `archive_queue` |
| `/home/pi/TeslaUSB/`, `~/ArchivedClips/`, `~/.config/...` | FHS paths (`/etc/`, `/srv/`, `/var/lib/`, `/run/`) | Standard Unix layout; `pi` user is for SSH only |
| Single `gadget_web.service` running everything | Three services: `teslafat@.service`, `teslausb-worker.service`, `teslausb-web.service` (+nginx, +optional smbd) | Fault isolation, clean restart semantics |
| Bash setup script with interactive prompts | Bash setup script that reads `/etc/teslausb/teslausb.toml` (no prompts) | Stdin-closable, idempotent, automatable |
| v1 thumbnail subsystem (removed) | Stays removed | Was correctly removed in v1; no regression |
| v1 archive subsystem (copy from RO mount to SD) | Does not exist in B-1 | Tesla writes natively to SD via teslafat; no copy needed |
| v1 quick_edit lock + mode service | Does not exist in B-1 | No modes; web writes are direct |
| v1 video pipeline v2 (copier/triage/indexer queues) | Does not exist in B-1 | The "Tesla writes to image, we copy to SD" problem doesn't exist |
| Comment in `# config.yaml` saying "you must restart X service" | TOML field `reload_strategy = "sighup" \| "restart"` per section, enforced by code | Self-documenting; reload logic lives next to the field |

If you find yourself reaching for a v1 pattern not listed above,
either (a) it's a deliberate carry-forward (UI templates, AP/WiFi
scripts, Samba config template, captive portal mechanism — the
infrastructure stuff Linux-the-OS already does well) or (b) it's
an oversight in this list and the anti-anchoring rule still
applies — propose the better way in an ADR, don't copy v1.

---

## v1 carry-forwards we ARE keeping (with reasoning)

Not every v1 decision needs to change. These are kept on purpose:

| v1 pattern | Why we keep it |
|---|---|
| Raspberry Pi OS Lite (Bookworm or later) | Best Pi gadget-mode kernel support; first-class `dwc2` overlay |
| NetworkManager + wpa_supplicant for WiFi | Default on Bookworm; the WiFi roaming config we tuned in v1 still applies |
| `hostapd` + `dnsmasq` for AP + captive portal DNS | Standard, well-tested, the alternatives are no better |
| rclone for cloud sync | Supports every provider; well-tested; subprocess interface is trivial |
| Hardware watchdog with `Nice=-5 IOSchedulingClass=realtime` drop-in | v1 learning; SDIO contention is real on Pi Zero 2 W regardless of language |
| Safe-mode boot detector (3 reboots in 10 min → skip teslausb services) | Without this, a bad teslafat update bricks the device |
| sshd systemd drop-in preventing stop/mask | SSH is sacred (operator recovery vector) |
| Inter Variable fonts bundled locally | UI parity binding; CDN calls forbidden by `UI_UX_DESIGN_SYSTEM.md` |
| Lucide SVG icon set | UI parity binding; no emoji |
| `prefers-reduced-motion` + WCAG AA contrast + 44×44 touch targets | UI parity binding |
| Mode-removal UI edit list (no "edit mode" / "present mode" surfaces) | UI parity binding + B-1 has no modes anyway |
| `tracing-journald` / `journald` log capture | systemd-native; no log files on SD card |
| `cargo` / `pip` / `npm`-free runtime layout (no node_modules) | Pi Zero 2 W resource budget |

---

## Open questions (none blocking — all have a default + decision deadline)

These are deliberate "decide during implementation" items. Each has
a tentative default so we can start coding; the listed phase is
where the question MUST be answered (with an ADR) before that phase
can be marked complete.

### Filesystem / on-disk layout

1. **FAT32/exFAT boot-sector byte layout** — exact match for
   `mkfs.vfat` / `mkfs.exfat` reference output.
   *Default:* generate, then `dd`-compare against reference; iterate
   until bit-identical.
   *Resolved in:* Phase 2 validation.

2. **`tokio::fs` (async) vs `std::fs` (blocking)** for backing I/O.
   *Default:* `tokio::fs` for hot read/write path (one task per NBD
   request, lets the runtime overlap I/O with NBD framing);
   `std::fs` for one-shot config/setup and cleanup worker.
   *Resolved in:* Phase 2 (microbenchmark on Pi).

3. **Cluster size selection** — already decided "auto from volume
   size" (Decision #3), but the exact lookup table for exFAT (32 KiB
   for 32-256 GiB? 128 KiB above?) needs to match what Windows /
   Tesla expect.
   *Default:* match `mkfs.exfat`'s default table verbatim.
   *Resolved in:* Phase 2.

### Kernel / system integration

4. **Kernel modules + `cmdline.txt`/`config.txt`**: which modules
   to autoload (`nbd`, `dwc2`, `libcomposite`, `g_mass_storage`?
   or load `g_mass_storage` only via configfs to avoid auto-bind?),
   what `dwc2` overlay parameters, what `nbd-client` `-timeout`
   value.
   *Default:* `dwc2,g_ether` removed; `dtoverlay=dwc2`; `modules-load`
   = `dwc2 libcomposite nbd`; `nbd-client -timeout 30 -persist`.
   *Resolved in:* Phase 1 smoke test.

5. **systemd boot sequence + ordering** between `teslafat@0.service`,
   `teslafat@1.service`, `nbd-client@0.service`, `nbd-client@1.service`,
   `teslausb-gadget.service` (configfs binder), `gadget_web.service`.
   *Default:* template units. `nbd-client@N` `After=teslafat@N.service`
   + `Requires`. `teslausb-gadget` `After=nbd-client@0.service
   nbd-client@1.service` + `Requires`. `gadget_web` `After=teslausb-gadget.service`
   but NOT `Requires` (web should come up even if gadget fails so
   operator can debug).
   *Resolved in:* Phase 6 (`setup.sh` + units).

6. **`g_mass_storage` behaviour during transient NBD disconnect**
   (during teslafat upgrade). Expected: transient I/O error to
   Tesla, Tesla's SCSI layer retries. Worst case: Tesla treats it
   as media-removed and re-mounts (which is fine — that's what
   cache invalidation does anyway).
   *Default:* assume retry works; verify on hardware.
   *Resolved in:* Phase 1 smoke test.

7. **USB gadget identifiers** — vendor/product ID, serial number
   generation (per-device vs static), `iManufacturer` / `iProduct`
   strings. v1 used a static serial; some Tesla firmware versions
   are reportedly fussy about serial changes.
   *Default:* mirror v1's exact identifiers; serial derived from
   `/etc/machine-id` (stable across reboots, unique per Pi).
   *Resolved in:* Phase 6.

8. **NBD client timeout + reconnect tuning**: how long the kernel
   NBD client waits before declaring the server dead.
   *Default:* `-timeout 30 -persist` (matches typical SAN client
   tuning; longer than teslafat restart, shorter than Tesla's
   SCSI patience).
   *Resolved in:* Phase 1.

### Daemon ↔ web UI control plane

9. **IPC protocol** between `gadget_web` (Python/Flask, runs as
   `pi`) and `teslafat-N` (Rust, runs as `root`). Two questions:
   socket path/permissions, and wire format.
   *Default for transport:* Unix socket per LUN at
   `/run/teslafat-N.ctl.sock`, owned `root:teslausb` mode 0660; web
   service member of `teslausb` group.
   *Default for wire format:* line-delimited JSON, one
   request/response per line, no streaming. Simple to debug with
   `socat`, no schema framework dependency.
   *Command set (minimum viable):* `STATUS`, `INVALIDATE_CACHE`,
   `RELOAD_CONFIG`, `STATS`, `SHUTDOWN_GRACEFUL`.
   *Resolved in:* Phase 4c + Phase 5 (whichever lands first).

10. **Cache-invalidation debounce window**: how long to coalesce
    rapid sequential uploads on LUN 1 before re-presenting media to
    Tesla. Too short = thrashes Tesla. Too long = user uploads a
    chime, walks to car, doesn't see it.
    *Default:* 2 s debounce after last write event, hard-capped at
    10 s of suppression so a continuous stream of uploads still
    fires invalidation periodically.
    *Resolved in:* Phase 4c.

11. **Config reload mechanism**: `SIGHUP` (in-process reload) vs
    `systemctl restart` (process restart) vs custom IPC command.
    Tradeoff: SIGHUP is faster but every reloadable field needs
    explicit handling. Restart is dead simple but causes the 1-3 s
    media outage per Decision #14.
    *Default:* SIGHUP for log-level, debounce-window, retention-age,
    capacity-headroom (cheap in-memory fields only). Restart
    required for socket paths, LUN size, filesystem choice.
    Document the split in the config schema.
    *Resolved in:* Phase 5.

### Observability

12. **Logging backend**: journald-only, file-only, or both? Rate
    limits to prevent SD-card wear from a stuck error loop.
    *Default:* journald-only (systemd captures stdout/stderr).
    `tracing-journald` for Rust, stdlib `logging` with
    `JournalHandler` for Python. Per-message rate limit:
    `journald` defaults are fine (10 k msgs / 30 s burst).
    *Resolved in:* Phase 1 (Rust) + Phase 5 (Python).

13. **Health/stats endpoint**: how `gadget_web` reports teslafat
    state to the System Health page. Pull via the IPC `STATS`
    command, polled every 5 s by web. No push, no Prometheus, no
    extra deps.
    *Resolved in:* Phase 5 (System Health rewire).

### User content (LUN 1)

14. **Music subsystem scope**: same formats as v1 (mp3, flac, m4a,
    wav, ogg, opus)? Metadata reads (id3, vorbis comments) for
    track listings, or filename-only?
    *Default:* mirror v1 verbatim per Decision #12 — same formats,
    same metadata libs (`mutagen`).
    *Resolved in:* Phase 5 (port verbatim).

15. **Wraps preview thumbnails**: v1 generates a small preview.
    Keep, or drop? (v1 video thumbnails were dropped; wraps are
    different — they're static images.)
    *Default:* keep wrap thumbnails; they're cheap and the UI
    expects them.
    *Resolved in:* Phase 5 (port verbatim).

16. **User-content backup strategy**: with btrfs snapshots ruled
    out (ext4-only architecture; see Decision #11), is there an
    explicit backup path for
    `/srv/teslausb/media/`? The user content there (custom chimes,
    wraps, music) is not on Tesla and not in the cloud sync target.
    *Default:* explicit "Export ZIP" button per directory in the
    web UI; operator pulls a backup before risky changes. No
    automatic backup.
    *Resolved in:* Phase 5.

### Cleanup / lifecycle

17. **Cleanup worker niceness / cadence**: how aggressively the
    background reaper sweeps `.partial` files, old no-GPS
    RecentClips, and capacity-pressure deletions.
    *Default:* `nice -n 19 ionice -c3`, runs every 60 s, max 20
    deletions per cycle, yields to `task_coordinator`. (Mirrors v1
    Pi-Zero-2-W-safe pattern.)
    *Resolved in:* Phase 4 / Phase 4b.

18. **Cleanup worker pause on first boot**: brand-new Pi with
    empty `/srv/teslausb/teslacam/` — cleanup must not run before retention
    has anything to retain. Risk: aggressive cleanup on first boot
    deletes operator's test data.
    *Default:* skip cleanup until both LUNs have been mounted by
    Tesla at least once (heuristic: `g_mass_storage` `lun_X` `file`
    has been non-empty for ≥ 10 min).
    *Resolved in:* Phase 4.

19. **First-boot setup flow**: brand-new Pi from `setup.sh` — is
    there a web wizard, a CLI prompter, or config-file-only?
    *Default:* `setup.sh` writes a minimal `/etc/teslausb/teslausb.toml`
    from template; web UI exposes the rest. No interactive wizard
    needed (setup script + Settings page covers everything).
    *Resolved in:* Phase 6.

### Charter / process

20. **ADRs to write before implementation**: per the Code Quality
    Charter, decisions affecting >1 module need an ADR.
    *Required ADRs before Phase 1 first commit:*
    - `0001-rust-for-daemon.md` (Decision #4-adjacent: why Rust)
    - `0002-nbd-newstyle-over-unix-socket.md` (Decisions #4 + #5)
    - `0003-two-luns-mirror-v1.md` (Decision #9)
    - `0004-extent-based-cluster-map.md` (Decision #15)
    - `0005-crash-and-restart-on-backend-error.md` (Decision #17)
    *Resolved in:* Phase 0 finish.

21. **Coverage exemption policy**: charter mandates ≥80% on
    `fs/` and `nbd/` (Rust) and `services/` (Python). What's the
    process for exempting genuinely-untestable code (e.g.,
    `unsafe` ioctl wrappers, signal handlers)? Per-line
    `// LCOV_EXCL_LINE` comments require ADR justification?
    *Default:* yes — any coverage exemption requires a one-line
    inline justification and an ADR if more than 10 lines exempted
    in one module.
    *Resolved in:* Phase 0 (`.github/workflows/ci.yml` config).
