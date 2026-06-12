# SPEC — TeslaUSB (B-1 reset: kernel-backed LUN + full-Rust rebuild)

> Status: Draft for build. Source of truth for the reset architecture.
> Derived from `docs/plan.md` (4-model synthesis) and operator decisions D1/D2.
> Component specs live alongside this file in `docs/specs/`.

This is the overarching system specification. It defines the objective, the
single non-negotiable invariant, the chosen architecture, the component map, and
the cross-cutting standards (commands, project structure, code style, testing,
boundaries). Each service has its own spec file; this document is the index and
the contract they all share.

---

## 1. Objective & target users

**Objective.** Turn a Raspberry Pi Zero 2 W into the most *reliable possible*
USB dashcam drive for a Tesla, plus a local web app to manage the recorded media
and the car's USB media features — **without ever causing the car to stop
recording**. We are resetting the B-1 architecture: removing the userspace
"pretend drive" middleware (teslafat over NBD) and the entire Python/Flask web
app, and rebuilding on a kernel-backed mass-storage gadget with a full-Rust
service layer. We preserve the *look, feel, and feature set* of today's web app.

**Why reset.** B-1's userspace daemon sits in the car's write path. When it (or
the Pi) dies — wifi-watchdog reboot, OOM on 512 MB, crash, hang — the car gets a
hard I/O error, latches the USB port off, and only a vehicle power-cycle
recovers it. The kernel-backed approach makes a Pi crash/reboot look like a
*clean unplug*, which the car tolerates. This is how the reliable V1 worked.

**Target users.**
- **Primary:** the device owner — a Tesla driver who wants a set-and-forget
  dashcam drive plus a friendly local web UI to review trips/events, play clips
  with a telemetry HUD, and manage chimes/lightshows/music/boombox/plates/wraps.
- **Secondary:** the operator/maintainer (often the same person) doing in-place
  upgrades and diagnostics over SSH on the live device.
- **Tertiary:** contributors building/testing the Rust services and SPA.

**Fixed constraints.**
- Hardware is **Pi Zero 2 W (512 MB RAM, single USB OTG, BCM43436 WiFi)** — fixed.
- **No OS reinstall, no SD reflash.** Convert the existing device in place
  (decision D2).
- **Full Rust app layer. Python/Flask removed entirely** (decision D1).
- The backing "drive" is a **kernel-served disk-image file** (S1), not a
  repartition of the live boot card.

---

## 2. THE #1 INVARIANT (everything serves this)

> **The car must ALWAYS be able to write TeslaCam when powered on.**

If the drive disappears mid-write or returns I/O errors, the car latches the USB
port off and ONLY a vehicle VBUS power-cycle recovers it. No Pi-side software
recovers a latched port. Therefore:

1. The car-facing LUNs are **kernel-owned block devices** (`usb_f_mass_storage`,
   configfs/libcomposite) backed by **image files** (`lun.0`←`teslacam.img`,
   `lun.1`←`media.img`). **Zero
   userspace in the write path.** A Pi crash/OOM/reboot looks like a clean
   unplug, never EIO.
2. The Pi **never** mounts the Tesla filesystem read-write while the car owns it.
3. Pi-side writes go through an **eject-handoff** (soft-eject → mount RW locally →
   mutate → fsync → re-present), never during an active Sentry/honk save.
4. `gadgetd` is the **only** critical service. Everything else is disposable and
   memory-capped, and **nothing else may ever trigger a reboot or gadget restart
   on the car's behalf** — with **one narrowly sanctioned exception**: `wifid` may,
   as a **last-resort SDIO-deadlock recovery** after a chip-reset
   (`rmmod`/`modprobe brcmfmac`) has failed, request a **Pi reboot only while the
   USB write path is idle**, gated on `gadgetd`'s write-heartbeat (never during an
   active write/save). This is the *only* permitted non-`gadgetd` reboot path; see
   [`wifid.md` §4](./wifid.md). It is safe precisely because a Pi reboot with the
   LUN idle presents to the car as a clean unplug, never EIO.

Any change that adds risk to the write path is rejected by default. When a spec
and this invariant conflict, the invariant wins.

---

## 3. Architecture (decided): S1 + A + O1

Chosen optimum from the options table in `docs/plan.md`, updated per operator
decisions: **S1 (image-file LUN) + A (full-Rust app) + O1 (existing Pi OS,
cleaned in place + hardened).**

- **Storage / gadget (S1, 2-image):** kernel `usb_f_mass_storage` on the
  existing ext4 data area, presenting **two LUNs backed by two image files**:
  `lun.0` ← `teslacam.img` (`TESLACAM` exFAT, sacred — the car writes it
  continuously) and `lun.1` ← `media.img` (`MEDIA` exFAT, read-only to the car
  via `ro=1`). Each image is MBR + a single exFAT partition, fully
  `fallocate`d. A media write cycles **`lun.1` only**; `lun.0` is never touched.
  (The car reads chimes/lightshows/boombox/music/wraps/plates from `lun.1`; that
  Tesla scans a *second* LUN is the gating hardware spike — see §9 #1 and
  [`usb-io-and-archiving-architecture.md`](./usb-io-and-archiving-architecture.md).
  The live device still runs an older single `disk.img`; the 2-image migration
  is operator + spike gated.)
- **Reads (two simplest-fit paths — [`ADR-0003`](../adr/0003-media-read-path.md)):**
  - **Live TeslaCam (`lun.0`):** a conservative Rust **raw exFAT/MP4/SEI parser**
    (`scannerd`) that `pread()`s the image and **never mounts**; trusts only
    files whose dir entry + cluster chain + MP4 tail are stable across scans.
    Map playback is **archive-first** (serve the ext4 archive copy); the
    not-yet-archived recent window is a bounded, best-effort raw read
    (stable-only, clamped to `valid_data_length`, identity-fenced, `410` on
    change). **Never** a kernel mount of the live car-written volume; **never**
    dm-thin under the live LUN (pool-full → EIO → latch).
  - **Static media (`lun.1`):** a `gadgetd`-owned **persistent read-only kernel
    loop-mount** of `media.img`, read via `std::fs`. `media.img` is static
    outside the (rare, Pi-only) handoff window, so the RO mount is
    cache-coherent. A media handoff drains in-flight reads (read-lease),
    unmounts RO, mutates RW, re-mounts RO. This serves media audio, wrap/plate
    thumbnails, and the Active Lock Chime player — no custom byte-server, no SD
    shadow copy.
- **Writes:** Rust **eject-handoff mutator**, car-state-aware, never during saves.
- **App layer (A — full Rust):** small cooperating binaries (§4). UI rebuilt as a
  small **static SPA** that achieves **visual parity** with today.
- **OS (O1, in place):** keep the existing Raspberry Pi OS on the existing SD;
  clean out all old B-1 software + leftover/temp/junk; then harden: read-only
  root + overlay, hardware watchdog armed, cgroup `MemoryMax` caps, gadgetd
  `OOMScoreAdjust=-1000`.

```mermaid
flowchart LR
  Car[(Tesla car)] -- USB writes lun.0 / reads lun.1 --> LUN[gadgetd: kernel usb_f_mass_storage\nlun.0=teslacam.img, lun.1=media.img ro=1]
  LUN -- backs --> IMG[teslacam.img: MBR + TESLACAM exFAT;\nmedia.img: MBR + MEDIA exFAT]
  IMG -. raw pread lun.0 .-> scannerd[scannerd: raw exFAT/MP4/SEI parser R1]
  scannerd --> indexd[indexd: SEI -> SQLite WAL]
  indexd --> DB[(SQLite: trips/events/clips)]
  webd[webd: axum REST/SSE + static SPA] --> DB
  webd -- eject-handoff mutate --> gadgetctl[gadgetd handoff: delete/install]
  retentiond --> DB
  retentiond --> ARCH[(Pi archive dir on ext4)]
  uploadd --> ARCH
  uploadd -. throttled .-> Cloud[(rclone remote)]
  wifid --> WiFi[STA/AP + SDIO watchdog]
  SPA[[Static SPA: parity UI\nLeaflet + Chart.js + Canvas HUD]] --- webd
```

---

## 4. Component map (one spec file each)

| Service | Criticality | Responsibility | Spec |
|---------|-------------|----------------|------|
| `gadgetd` | **CRITICAL** (the invariant guardian) | Configure/maintain the kernel mass-storage LUN; own the disk image + partition layout; perform the eject-handoff for Pi-side writes | [`gadgetd.md`](./gadgetd.md) |
| `scannerd` | disposable | R1 raw exFAT/MP4/SEI parser; emit stable-clip + SEI records; never mount | [`scannerd.md`](./scannerd.md) |
| `indexd` | disposable | Consume parser output; derive trips/events/clips into SQLite (WAL) | [`indexd.md`](./indexd.md) |
| `webd` | disposable | axum REST + SSE API; serve the static SPA; drive eject-handoff mutations | [`webd.md`](./webd.md) |
| SPA | static assets | Parity UI: media hub, trip map, event player + telemetry HUD, media managers, cloud, storage, settings | [`spa.md`](./spa.md) |
| `uploadd` | disposable | Durable, resumable, throttled, prioritized cloud upload from the Pi-side archive directory | [`uploadd.md`](./uploadd.md) |
| `retentiond` | disposable | Retention + archive rules so the car buffer never loses wanted clips and the disk never fills | [`retentiond.md`](./retentiond.md) |
| `wifid` | disposable | STA/AP state machine; TX rate cap; SDIO chip-reset watchdog | [`wifid.md`](./wifid.md) |
| Migration | ops | In-place M1–M5 + hardening; reversible, rails-safe | [`migration.md`](./migration.md) |
| Setup / install | ops | Idempotent `setup.sh` to install/configure a device from a clone; public + maintainer-release | [`setup.md`](./setup.md) |

The external Tesla USB interface every component must respect is captured
separately (not a service): [`tesla-usb-contract.md`](./tesla-usb-contract.md) —
partitions, required folders, case sensitivity, camera-file naming, media
features, and the `RecentClips` rotation reality.

The **SD-card space-management** design (the continuous space governor, reserve
tiers, value-scoring eviction, single-deleter authority, crash-safe deletion) is
specified in [`storage.md`](./storage.md) and implemented inside `retentiond`.

The **development methodology** — spike/PoC on real hardware before any long
buildout, with an ordered, risk-gated de-risking backlog and a fail-fast agile
loop — is [`hardware-first-development.md`](./hardware-first-development.md). It
governs the order in which §9's unknowns are proven and turned into buildout.

**Reuse vs. new.** KEEP the existing Rust SEI/indexing parser knowledge and the
hard-won feature behavior (event thresholds, retention rules, cloud layout,
every screen's look/feel). REMOVE teslafat/NBD and the Flask app entirely. NEW:
`gadgetd`, the raw reader, the eject-handoff, and the Rust web/API server + SPA.

---

## 5. Commands (cross-cutting)

The Rust workspace lives under `rust/`. Component specs may add service-specific
commands; these are the shared ones.

```bash
# Build (host)
cargo build --workspace
cargo build --workspace --release

# Cross-compile for the target (Pi Zero 2 W)
cargo build --workspace --release --target aarch64-unknown-linux-gnu

# Test / lint / format / supply-chain
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all --check
cargo deny check            # licenses + advisories (rust/deny.toml)

# SPA (static frontend)
npm ci
npm run build               # emits hashed static bundle served by webd
npm run test                # component/unit tests
npx playwright test         # E2E + perf + console assertions (see §7 and .github/copilot-instructions.md)
```

Deployment to the live device is **only** via the hardware-test skill (dead-man
reboot timer, SSH/WiFi/boot protected, backups before mutate). Never deploy by
hand-editing the device. See [`migration.md`](./migration.md).

---

## 6. Project structure (target)

```
rust/
  Cargo.toml                 # workspace
  crates/
    teslausb-core/           # shared types, config, SQLite access, SEI model (KEEP/extend)
    teslafat/                # legacy synthesizer — REMOVED from runtime; kept only until cutover
    gadgetd/                 # CRITICAL: kernel LUN + eject-handoff
    scannerd/                # R1 raw exFAT/MP4/SEI reader
    indexd/                  # trips/events/clips derivation -> SQLite
    webd/                    # axum API + static SPA host
    uploadd/                 # cloud upload queue
    retentiond/              # retention + archive
    wifid/                   # STA/AP + SDIO watchdog
spa/                         # static SPA source (Preact/Svelte/Solid + Leaflet + Chart.js)
  src/, public/, dist/
deploy/                      # systemd units, configfs templates, hardening configs
setup.sh                     # idempotent installer (thin orchestrator) — see setup.md
setup-lib/                   # numbered idempotent step files + shared helpers
uninstall.sh                 # safe-by-default reversal (see setup.md §9)
docs/
  plan.md                    # architecture synthesis (background)
  specs/                     # THIS spec set
```

Each service is a **small, single-purpose binary**. Shared logic goes in
`teslausb-core`. No service except `gadgetd` may touch the car-facing write path.
The device is installed/configured **only** via `setup.sh`
([`setup.md`](./setup.md)) — prebuilt aarch64 artifacts + the hashed SPA bundle;
never built on the Pi.

### 6.1 On-device storage layout (no-reflash reality)

Because the design is **no-reflash / no-repartition** (O1), there is **no new
physical partition** for our data. Everything Pi-side lives on the **existing
Linux ext4 data filesystem** (the SD card's data area, rooted at `/srv/teslausb`
today). The car-facing drive is a single **image file** on that same filesystem;
the archive and index are **directories on the Linux side, outside the image**.

```
/data/teslausb/
  teslacam.img             # lun.0 — the SACRED car-write LUN (fixed/fallocated).
                           #   Internally: MBR + single TESLACAM exFAT partition.
                           #   The car writes this continuously; never ejected.
  media.img                # lun.1 — the MEDIA LUN (fixed/fallocated), ro=1 to car.
                           #   Internally: MBR + single MEDIA exFAT partition.
                           #   Holds chimes library + active LockChime.wav,
                           #   boombox/music/lightshows/wraps/plates.
                           #   gadgetd owns a persistent RO loop-mount for reads;
                           #   a media write cycles ONLY this LUN (eject-handoff).
  archive/                 # Pi-side ARCHIVE — NOT inside any image; car cannot see it
    SavedClips/<ts>/...    #   mirrors Tesla naming (tesla-usb-contract.md §4)
    SentryClips/<ts>/...
    RecentClips/<ts>-<cam>.mp4
/var/lib/teslausb/
  index.sqlite3            # SQLite (WAL) catalog of trips/events/clips + archive
/run/teslausb/*.sock       # local IPC sockets
```

Key consequences:
- **Archived videos are stored in `…/archive/` on the Linux ext4 filesystem**,
  separate from both images. `webd` serves clip playback from there; `indexd`
  catalogs it; `uploadd` uploads from there. The car never sees the archive.
- Both images are **fixed-size/fallocated**, so a growing archive cannot corrupt
  a LUN — but they share free space with the host ext4, so `retentiond`'s quota
  MUST keep the ext4 healthily free (so SQLite/WAL and operations never run out
  of space).
- **Image sizing is a provisioning decision (flag, not yet fixed).** On a finite
  card the budget must close: `card_total ≥ teslacam.img + media.img (both fully
  fallocated) + OS/root + archive budget + all reserves`
  ([`storage.md` §2](./storage.md)). `teslacam.img` is the large dashcam buffer;
  `media.img` is small (chimes/boombox/lightshow/music/wraps/plates are MB-scale).
  Bigger `teslacam.img` = more car buffer but less Pi archive room; sizes are
  chosen at provisioning per card capacity and **measured on hardware** (§9), not
  hard-coded here. M3 ([`migration.md`](./migration.md)) must confirm enough free
  space exists post-cleanup before creating them, and the single→2-image
  migration runbook lives in
  [`usb-io-and-archiving-architecture.md`](./usb-io-and-archiving-architecture.md).
- A future reflash (S2) could promote `archive/` and the index to their own
  physical partition; until then "Pi-side archive **directory**" is the precise
  term — not "archive partition".

**Keeping the card from filling is a safety function.** Because the images are
fixed/preallocated, the car's incoming video never grows ext4 — **our** archive +
index + WAL + staging + logs are the unbounded consumers. A continuous **space
governor** ([`storage.md`](./storage.md)) watches free space/inodes, holds an
OS/`gadgetd`/SQLite reserve **sacrosanct**, and evicts the **least-valuable** safe
archived item first (never undurable Saved/Sentry, never pinned/leased). A starved `gadgetd` is the
**principal** path by which low space could endanger car writes; a secondary,
slower path is that if Pi ext4 is exhausted, archiving (and the car-side cleanup
handoffs it drives) stalls, so the **car's** own exFAT volume can fill over time
([`retentiond` §3](./retentiond.md)). The governor's job is to bound **our**
footprint so neither path is reached.


---

## 7. Code style & engineering standards

These specs are self-contained: the standards below bind every component. If a
future code-quality document is added, it supplements (does not replace) these.

- **Rust:** 2021+ edition; `cargo fmt` clean; `clippy -D warnings`; no `unwrap()`/
  `expect()` in service paths (return `Result`, handle errors); `unsafe` only
  where the kernel/FFI boundary requires it, with a safety comment and a test.
- **Memory discipline (512 MB Pi):** bounded buffers, streaming I/O, no loading
  whole videos into RAM. Every non-critical service runs under a cgroup
  `MemoryMax`. `gadgetd` gets `OOMScoreAdjust=-1000`. **OOM kill order**
  (most-disposable first → never):
  `uploadd → wifid → webd → scannerd → retentiond → indexd → NEVER gadgetd`.
  Rationale: `uploadd` (pure convenience) sheds first; `wifid`/`webd`/`scannerd`
  are stateless/restartable and the car is unaffected when they pause;
  `retentiond` and `indexd` are the **protected pair** just below `gadgetd`
  because together they run safe eviction — `retentiond` hosts the space governor
  and `indexd` is the sole SQLite writer the governor (and UI) depend on, so
  killing either stops the card from being freed. There is **no** `thumbnailer`
  service: keyframe thumbnails are a capped, best-effort task inside `scannerd`
  ([`scannerd` §3](./scannerd.md)), killed with it.
- **No transcoding on the Pi, ever.** SEI is parsed once at index time; the HUD
  is rendered **client-side** (Canvas/WebGL over native `<video>`). Tesla
  TeslaCam footage is **H.264/AVC** (observed on HW3/HW4, Main/High profile —
  this is what v1's `sei_parser.py` and `teslausb-core/src/sei` already parse in
  production); telemetry lives in an **H.264 SEI `user_data_unregistered`**
  (`payload_type=5`) NAL as a protobuf. Keep the parser and the player
  **codec-aware** (detect from `avcC`/`hvcC`) so a future HEVC variant can be
  added, but do **not** assume H.265 — H.264 plays natively in all target
  browsers, so a "download to view" path is an edge-case guard, not the norm.
- **Security / trust model.** Today's web UI has **no app-level login** (verified:
  the Flask app uses cloud **OAuth** only, no `login_required`); the device runs
  on a **trusted home LAN**, and AP-onboarding mode must use **WPA2** (never an
  open AP). Preserve that model — do **not** silently add or drop auth — but treat
  it explicitly: `webd` mutations (clip delete, media install) are powerful, and
  cloud **OAuth refresh tokens** + WiFi/Samba credentials are secrets that must be
  stored with restrictive permissions (root-only, `0600`), never world-readable,
  never logged, never in the SPA bundle or the Tesla volume. See [`webd` §security](./webd.md).
- **SQLite (WAL)** lives on the **Pi-side ext4 data filesystem** (outside the
  car's `disk.img` LUN). It is rebuildable side state; it is **never** placed on
  the Tesla volume.
- **SPA:** small framework (Preact/Svelte/Solid), vendored map/chart libs to keep
  parity (**Leaflet + MarkerCluster**, **Chart.js**, the existing `dashcam-mp4`
  SEI HUD approach). No heavy SPA frameworks; ship a small hashed static bundle.
  MapLibre is a **rejected** alternative (would change look/feel; parity wins).
- **Comments** only where they add clarity. No dead code, no speculative
  abstractions.

---

## 8. Testing strategy (cross-cutting)

- **Unit/integration (Rust):** `cargo test` per crate. The raw parser, the
  stability gating, the eject-handoff state machine, and the SEI decoder MUST
  have property/fixture tests with recorded byte-level fixtures.
- **Invariant tests:** `gadgetd` must have tests proving that a service
  crash/restart/handoff presents as a clean unplug, never an error to the LUN
  consumer. Handoff must refuse to mutate during a simulated active save.
- **UI (mandatory Playwright, per `.github/copilot-instructions.md`):** every
  UI-affecting change is verified end-to-end in a real browser — assert on
  navigation TTFB, DOMContentLoaded, FCP, the slowest 5–10 network requests,
  **zero** console/pageerror, a screenshot at 375px and ≥1280px, and proof the
  changed JS module is actually loaded by the served page. "Tests pass" /
  "endpoint 200" is **not** sufficient. Drive the served app through the
  **Playwright MCP** for interactive UI/UX user-acceptance testing (accuracy,
  render speed, professional/parity appearance), backed by a **durable, checked-in
  Playwright suite** as the repeatable gate ([`spa.md` §5–§6](./spa.md)).
- **Hardware acceptance:** the highest-risk unknowns (§9) are prototyped on the
  live device first, via the hardware-test skill, before anything depends on them.
  The spike methodology (time-boxed PoC loop, ordered/gated risk backlog,
  fail-fast outcomes, fold-back-into-specs) is
  [`hardware-first-development.md`](./hardware-first-development.md).
- **Second-opinion gate:** for root-causing issues and before any risky
  live-hardware step, run a parallel GPT-5.5 second opinion and reconcile (per
  `.github/copilot-instructions.md`).

---

## 9. Prototype-first unknowns (gate everything else)

These must be proven on hardware before downstream work depends on them. **The
*process* for spiking these — the time-boxed spike loop, the ordered/gated spike
backlog of risk-named spikes, PASS/FAIL/INCONCLUSIVE outcomes, and the agile feedback cycle
— is specified in [`hardware-first-development.md`](./hardware-first-development.md).**
That doc is binding: do not start a long buildout on any unknown below until its
gating spike PASSes with captured parameters.

1. Tesla acceptance of **two image-file LUNs** (`lun.0`=TESLACAM the car writes,
   `lun.1`=MEDIA read-only the car reads chimes/lightshow/etc from). Prove first.
   **[2026-06-08: single-LUN MECHANISM PASS, 2-LUN gate still OPEN —
   `usb_f_mass_storage`+`file=` enumerates and round-trips R/W on a
   real USB host (bench); car acceptance of a SECOND read-only media LUN is
   car-only and unconfirmed. See [`hardware-first-development.md` §5.1](./hardware-first-development.md).]**
2. **Clean eject + rebind** behavior — soft-eject treated as benign (no latch);
   re-insert resumes recording in ~2 s; measure mid-write disappearance tolerance.
3. **Raw exFAT parsing + clip-stability detection while the car writes** — no
   false "stable".
4. **BCM43436 TX throttle threshold** — Mbps/chunk size that avoids the SDIO
   deadlock; `rmmod/modprobe brcmfmac` recovery reliability.
5. **microSD latency under car-write + Pi index/copy** — Pi I/O must never starve
   car writes (ionice/IOWeight; A2/V30 media).
6. **Cold boot-to-gadget-ready** time (target < 8–10 s).
7. **H.264 SEI HUD sync + browser playback** across desktop + mobile. Confirm the
   telemetry-bearing **H.264 SEI** (`user_data_unregistered`, `payload_type=5`)
   is present and matches `teslausb-core/src/sei` across the **target build/HW
   range** (incl. any HW4/Cybertruck clips, which could ship a different codec or
   SEI layout); a "download to view" fallback only where a browser can't decode a
   clip's codec.
8. **Image sizing + space-budget closure on the real card** — pick the absolute
   sizes of `teslacam.img` + `media.img`, fully `fallocate` them, and verify
   `card_total ≥ teslacam.img + media.img + OS + archive budget + reserves`
   holds with healthy headroom (§6.1, [`storage.md` §2](./storage.md)).

---

## 10. Boundaries

**ALWAYS**
- Treat the #1 invariant as supreme; protect the car's write path above all else.
- Keep `gadgetd` the only critical service; cap memory on everything else.
- Do Pi-side writes via the eject-handoff; never mount the Tesla FS RW while the
  car owns it; never mutate during an active save.
- Read media via the `gadgetd`-owned **read-only** loop-mount of the static
  `media.img`, and live TeslaCam clips via raw `pread` (never mounting the
  car-written `teslacam.img`) — [`ADR-0003`](../adr/0003-media-read-path.md).
- Parse SEI once at index time; render the HUD client-side; never transcode.
- Keep SQLite/derived state on the Pi-side ext4 filesystem (outside the car's
  image-file LUNs); treat it as rebuildable.
- Deploy/migrate only via the hardware-test skill, reversibly, with backups
  first and SSH/WiFi/boot protected.
- Verify UI changes with Playwright (perf + console + screenshot + wiring).
- Preserve the existing look, feel, and feature set.

**ASK FIRST**
- Any change that touches or could add latency/failure to the car's write path.
- Reflashing / repartitioning the live boot card (S2) — off the table unless the
  operator explicitly chooses to reflash.
- Dropping or materially redesigning an existing user-facing feature/screen.
- Introducing a new heavyweight dependency, language, or toolchain.
- Any irreversible live-device operation.

**NEVER**
- Put the sacred LUN on dm-thin / CoW, or take an unbounded block snapshot under
  the live LUN.
- Let any non-`gadgetd` service reboot the Pi or restart the gadget on the car's
  behalf — **except** `wifid`'s last-resort SDIO-recovery reboot, permitted **only
  while the USB write path is idle** (gated on `gadgetd`'s write-heartbeat, chip-reset
  tried first). See [`§2 invariant 4`](#) and [`wifid.md` §4](./wifid.md).
- Mount the Tesla filesystem read-write concurrently with the car.
- Transcode video on the Pi, or load whole clips into RAM.
- Reintroduce Python/Flask into the runtime, or NBD/teslafat into the write path.
- Store derived/SQLite state on the Tesla volume.
- Commit secrets; bypass these specs' standards; declare UI work done without
  Playwright
  verification.
