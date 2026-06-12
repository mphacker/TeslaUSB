# PLAN — TeslaUSB most-reliable architecture (4-model synthesis)

## 🧭 NORTH STAR & LOCKED MEDIA READ/WRITE CONTRACT (2026-06-12)

> **Read this before proposing ANY media/storage change.** This section was
> added after repeated architectural drift. It is the operator-confirmed,
> non-negotiable framing. If a design conflicts with anything here, the design
> is wrong — not this section.

**The goal in one sentence:** be **v1** — same features, capabilities, and
look-and-feel — **re-implemented in Rust**, and *more efficient* (lower CPU,
lower I/O, lower memory) and overall *better* than v1. We are not inventing a
new product or a novel media architecture. When in doubt, do what v1 did, in
Rust, more efficiently. Do **not** go down clever side-paths that diverge from
v1 behavior.

**The four media requirements (operator-stated, verbatim intent — LOCKED):**

1. **TeslaCam USB is NEVER disconnected.** The vehicle can always write to it.
   No media operation, read, or app action may eject, stall, or gate `lun.0`.
2. **TeslaCam USB must be READABLE for *recorded* (not just archived) clips.**
   The map/trip viewer must be able to read clips straight off the live
   TeslaCam image, even before they are copied to the Pi-side ext4 archive.
3. **Media writes (upload/delete) may momentarily eject the MEDIA USB ONLY.**
   That brief `lun.1` eject/re-present is expected and fine. It must **never**
   impact, block, or wait on the TeslaCam USB (`lun.0`).
4. **ALL media files live IN the IMG (the USB drive), never as files on the
   SD card.** Music, boombox, lightshows, wraps, plates, the active
   `LockChime.wav`, **and the lock-chime *library*** all live inside the
   media image. Nothing media is shadow-copied to `/data` on the SD card.

**The resulting architecture (LOCKED — this is "v1 in Rust, done better"):**

| Concern | Mechanism | Touches TeslaCam? |
| --- | --- | --- |
| **Read** TeslaCam clips for the map (recorded + archived) | **Archive-first:** serve the Pi-side ext4 archive copy; for the not-yet-archived recent window, `scannerd` raw `pread` reader (**no mount, no eject**) over `teslacam.img`, best-effort, stable clips only | No |
| **Read** media (list / play audio / thumbnail source) | **RO kernel loop-mount of `media.img`** (gadgetd-owned, persistent; torn down with read-drain only around a `lun.1` handoff), read via `std::fs` | No |
| **Write** media (upload/delete) | `gadgetd` eject-handoff on **`lun.1` only** (clears that one LUN's `file`), mutate, re-present | No |
| **Storage** | Everything the car/app exposes lives **in the IMG**; nothing media on SD | — |

**READ-PATH RECONCILIATION (2026-06-12, Opus + GPT-5.5 + mai).** The read path
was simplified after an adversarial second-opinion round (operator-mandated
GPT-5.5 + mai review; see [`ADR-0003`](./adr/0003-media-read-path.md)). Two
read mechanisms, each the *simplest proven* tool for its volume:

- **Static `media.img` (lun.1) → RO kernel loop-mount.** The Pi is the sole
  writer and only mutates it inside a `lun.1` handoff window; outside a handoff
  the image is static, so a read-only kernel mount is cache-coherent and
  battle-tested. `gadgetd` owns one persistent RO mount; a media handoff
  drains in-flight reads (a short read-lease), unmounts, mutates RW, re-mounts
  RO. `webd` serves media audio, wrap/plate thumbnails, and the **Active Lock
  Chime** player by reading files through that mount with `std::fs` — **no
  custom exFAT byte-server for media**, no SD shadow copy. This is "use the OS
  loop, it's proven."
- **Live `teslacam.img` (lun.0) → raw `pread`, never mounted.** The car writes
  this exFAT continuously, so a kernel mount would be cache-incoherent and
  could error under the car's writes. Map playback is **archive-first**: serve
  the durable ext4 archive copy whenever it exists; only the recent,
  not-yet-archived window falls back to a **bounded, best-effort** raw read
  (catalog-`stable` clips only, clamped to `valid_data_length`, identity-fenced,
  `410 Gone` on change — never wrong bytes). This live fallback may be built
  *after* the archive loop is running, since archive-first covers the common case.

**EXPLICITLY REJECTED (do not revisit — these caused the drift):**

- ❌ **SD-card shadow copy / gadgetd apply-time cache to `/data`** for the
  active chime (or any media). Violates requirement #4. The honest source of
  truth is the bytes in the IMG.
- ❌ **Loop-mounting the LIVE TeslaCam image (`lun.0`/`teslacam.img`)** — RO or
  RW — to read it. The car mutates it continuously; a kernel mount is
  cache-incoherent and risks the write path. `lun.0` reads are always raw
  `pread`, no mount. (RO loop-mounting the **static `media.img`** is explicitly
  *allowed* and preferred — see above; the earlier blanket ban applied only to
  the live car-written volume.)
- ❌ **A heavyweight content-read seam** (two-RPC handle model, per-slot
  generation counters bumped every scan, per-chunk exFAT `SetChecksum`
  re-validation, handoff-edge fencing). Over-engineered for the actual risk.
  Media uses the loop-mount; the lun.0 live fallback uses a *cheap* identity
  handle + path jail + bounded length + `410` on change (ADR-0003).
- ❌ **Any media operation that ejects, stalls, or is gated on `lun.0`/TeslaCam.**
  Media writes are `lun.1`-only and independent.

**Chime library location (LOCKED 2026-06-12):** the lock-chime *library* (the
pool of candidate WAVs uploaded/deleted/previewed before one is set active)
lives **inside `media.img`** — list/preview via the content-read seam, upload/
delete via the `lun.1` eject-handoff. It is **moving off** the current SD-card
location (`/data/teslausb/chimes`). Only the active selection is copied to the
fixed root `LockChime.wav` that Tesla actually consumes.

---

## PLAIN-ENGLISH SUMMARY (read this first)

**Are we trashing B-1 and starting over? No.** We keep almost all of B-1 and
replace ONE part — the exact part that has been causing the car to lose the
drive.

Think of B-1 as having two halves:
1. **The "pretend USB drive" half** — the software that makes the Pi look like a
   USB stick to the car. Today this is a custom program (teslafat) that the car
   talks to for every video write. **This is the troublemaker.** When that
   program hiccups, runs out of memory, or the Pi reboots, the car gets an error,
   decides the drive is broken, and stops recording until you power-cycle the car.
2. **Everything else** — the website, the trip map, the event bubbles, the video
   player with the data overlay, deleting clips, retention rules, cloud backup,
   WiFi/hotspot handling, the video-indexing engine. **All of this stays.**

**The plan:** rip out half #1 and replace it with a standard, built-in Linux
feature that turns the Pi into a USB drive *at the kernel level* — the same rock-
solid way a normal USB stick works. There's no custom program in the car's path
anymore, so if the Pi reboots or a program crashes, the car just sees a brief
"unplug," not an error. **It keeps recording.** This is exactly how the older V1
worked, which is why V1 never had this problem.

**What changes for you day-to-day:** essentially nothing visible. Same website,
same features, same look. The Pi just becomes far more reliable at the one job
that matters most — always letting the car record.

**The one real trade-off:** because the car and the Pi can no longer both touch
the drive at the exact same instant, when you delete a clip or add a chime/
lightshow, the Pi briefly "ejects" and re-inserts the drive (a few seconds) to
make the change safely. The car's built-in ~1-hour video buffer easily covers
that gap, so no footage is lost.

**DECIDED with operator:**
- **Go FULL RUST. The Python/Flask web app is removed entirely and rebuilt in
  Rust.** No backwards compatibility, no keeping old code for its own sake. We
  design the new system as if starting fresh — one language, one toolchain, the
  lightest possible footprint on the 512 MB Pi.
- **No OS reinstall, no clean SD card.** We clean up and convert the EXISTING Pi
  in place. That means: remove all the old B-1 software (teslafat, NBD, the
  Python app, old services) and any leftover v1/temp/junk files, then stand up
  the new Rust system on the same card the device boots from today.

**How much is reused vs new?**
- KEEP (the proven engine room): the Rust video-indexing / SEI-telemetry parser
  (already Rust, works) and the hard-won knowledge baked into the current
  features (event detection thresholds, retention rules, cloud layout, the
  look/feel of every screen). We rebuild the UI in Rust but it looks and behaves
  like today.
- REMOVE: the teslafat/NBD "pretend drive" middleware AND the entire Python/Flask
  web app — both replaced by Rust.
- NEW (Rust): the kernel "drive manager" service, the raw safe-reader, the
  eject/re-insert handoff, and a Rust web/API server serving a static UI.

**The backing drive — use the V1-proven approach (because no reflash):**
Since we are NOT repartitioning your live boot card, the car's "USB drive" is a
single **disk image file** that the Linux kernel serves directly — which is
exactly how the reliable old V1 worked. Inside that one image we lay out a normal
2-partition disk (TeslaCam + media) so chimes/lightshows still work. No custom
software in the car's path; a Pi reboot is just a clean "unplug." (A dedicated
raw partition would shave one thin layer, but it requires re-imaging the SD — so
that's a future option only if you ever choose to reflash.)

**Rollout on your existing device (in place, reversible at each step):**
1. Back up your current clips and configs off the Pi first.
2. Clean the device: stop and remove all old B-1 services, the Python app,
   teslafat/NBD, and sweep out leftover temp/junk files (this also directly
   answers your earlier "review the device for leftover/temp content" ask).
3. Create the kernel-served disk-image drive in the free space on the card and
   bring it up via the new Rust drive manager — proving the car records reliably
   BEFORE we remove the safety nets.
4. Stand up the Rust web app + indexer + cloud/WiFi services; migrate your
   existing clips into the new layout.
5. Harden: read-only root + overlay, hardware watchdog, memory caps.
Every step keeps SSH/WiFi/boot alive (hardware-test safety rails) and is
reversible, so if anything misbehaves we roll back without losing recording.

---

> Operator directive: design the whole TeslaUSB solution the most reliable,
> stable way possible. Consult GPT-5.5 + 2 other premium models, have each
> propose BOTH a ground-up rebuild AND an evolve-current-stack option, then
> deliver a pros/cons options table driving toward ~0 cons, and identify the
> single most-optimum architecture. Pi Zero 2 W is fixed; be creative; ignore
> implementation difficulty.

Full feature set required: USB dashcam drive for the car; web app to manage
media (chimes, lightshows, boombox, music, license plate, wraps); map of trips
by day; event bubbles (honk, Sentry, hard brake/accel); click event/path ->
front-cam video at that moment; video player with SEI-data HUD; scale to
full-page/full-screen with HUD; group of angle videos = a "clip"; delete clips;
configurable retention so the car's ~1h rotating buffer doesn't lose wanted
video; archive retention so disk doesn't fill; configurable cloud upload
(prioritized, since WiFi time is limited); AP-mode fallback when home WiFi is
unreachable. Look/feel like today.

## THE #1 INVARIANT (everything below serves this)
The car must ALWAYS be able to write TeslaCam when powered on. If the drive
disappears mid-write or returns I/O errors, the car latches the USB port off and
ONLY a vehicle VBUS power-cycle recovers it. No Pi-side software recovers a
latched port.

Root cause of today's recurring failure: B-1's **userspace daemon (teslafat
exFAT synthesizer over NBD) sits in the car's write path.** When it/the Pi dies
(wifi-watchdog reboot, OOM on 512 MB, crash, hang) the car gets hard EIO ->
latches off. V1's kernel-backed static image looked like a CLEAN disconnect on
reboot -> never latched. THIS is the fragility we are designing out.

---

## CONSENSUS across all 4 designs (GPT-5.5, Gemini 3.1 Pro, Opus 4.7, me)
Remarkably strong agreement. These are now treated as settled, non-negotiable:

1. **Kernel `usb_f_mass_storage` (configfs/libcomposite), zero userspace in the
   write path.** The LUN is backed by a KERNEL-OWNED block device. A Pi
   crash/OOM/reboot then looks like a clean USB unplug, not EIO -> never latches.
   (Replaces NBD/FUSE/synthesized-FS entirely. This single change is THE fix.)
2. **One physical disk, MBR + 2 partitions** (TeslaCam exFAT + media exFAT) —
   hardware-proven (ADR-0023): the car reads chimes/lightshows/boombox/music
   ONLY from a partition of the same physical device it writes dashcam to.
3. **Pi NEVER mounts the Tesla filesystem read-write while the car owns it.**
   No shared-writer cache coherency between the car OS and Linux -> corruption.
4. **Pi-side WRITES go through an EJECT-HANDOFF**: soft-eject the LUN, mount RW
   locally, mutate (delete clips, install chime/lightshow), fsync, re-present.
   Window ~5 s; absorbed by the car's ~1h internal buffer; never during an
   active Sentry/honk save.
5. **Video HUD is client-side** (Canvas/WebGL over native `<video>`). SEI
   telemetry parsed ONCE at index time into sidecar/DB. **No transcoding on the
   Pi, ever** (Pi Zero 2 W cannot).
6. **SQLite (WAL) on a Pi-only partition** — rebuildable side state, NEVER on the
   Tesla volume. Map/trips/events derived from it.
7. **WiFi is non-critical**: never concurrent AP+STA; TX rate-limited (token
   bucket / `tc`) to stay under the BCM43436 SDIO-deadlock threshold; a liveness
   watchdog resets the chip (`rmmod/modprobe brcmfmac`), NOT the whole Pi unless
   USB is already idle. AP-mode is convenience, not a reliability dependency.
8. **Hardware watchdog armed** (`/dev/watchdog`). Read-only root + overlay/tmpfs
   so power-loss/reboot can't corrupt the OS. `gadgetd` is the ONLY critical
   service (`OOMScoreAdjust=-1000`, tiny `MemoryMax`); everything else is
   disposable/restartable with cgroup `MemoryMax` caps. OOM kill order:
   uploader -> thumbnailer -> web -> indexer -> NEVER gadgetd.
9. **Cloud upload** = durable queue (SQLite) + rclone or a small Rust uploader,
   resumable, throttled, prioritized by user policy, sourced from the Pi-side
   ARCHIVE partition (never directly from the live LUN). Never triggers a reboot
   or gadget restart.
10. **All recommend a HYBRID biased to a Rust core**: the kernel-backed LUN +
    gadgetd + raw reader + handoff mutator is mandatory and must be Rust; the
    app/web layer can stay Flask temporarily (quarantined) or be rewritten.

## The ONE genuinely contested fork: the Pi-side READ path
(How the Pi reads the car's volume to index video, extract SEI, stream clips.)

- **R1 — Raw userspace exFAT parser** (Gemini + GPT-5.5): a Rust process
  `pread()`s the raw block device, parses MBR->exFAT->FAT->MP4->SEI, never
  mounts. Tolerates the car writing concurrently by only trusting files whose
  dir entry + cluster chain + MP4 tail are stable across scans (skip-and-retry).
  Pro: simplest, fully disposable, nothing extra under the car's LUN.
  Con: best-effort consistency; must be written conservatively.
- **R2 — dm-thin block snapshot** (Opus 4.7): back the LUN with an LVM thin LV;
  take an instantaneous `lvcreate --snapshot`, RO-mount the frozen copy, index
  from it. Pro: CONSISTENT point-in-time view, no torn-metadata races.
  Con (decisive): puts the car's sacred LUN permanently on a CoW/dm-thin stack
  -> CoW write amplification, thin-pool metadata exhaustion, and pool-full ->
  origin write failures that the car could see as EIO -> LATCH. **This adds risk
  to the #1 write path** — exactly what we are eliminating.

**RESOLUTION (rubber-duck-validated):** Do NOT put the sacred LUN on dm-thin.
Use a **plain raw kernel-owned block device** for the car LUN + **R1
conservative raw userspace parser** as the default reader. If a fully-consistent
view is ever needed for explicit user playback/export, take a SHORT-LIVED,
hard-time-limited block snapshot and read it with the RAW PARSER (not a kernel
exFAT mount) — never an unbounded snapshot, never dm-thin under the live LUN.
This dominates: raw-parser-over-snapshot gives consistency with no kernel exFAT
mount and a disposable reader, while the always-on write path stays a plain
block device.

---

## OPTIONS TABLE (deliverable)

### Storage / gadget (the part that protects the invariant)
| Opt | Backing | Reads | Writes | Latch risk | Cons |
|-----|---------|-------|--------|-----------|------|
| S0 (today) | userspace teslafat+NBD | live POSIX | live POSIX | **HIGH** (daemon EIO) | the bug |
| **S1 image-file LUN (REC — no reflash)** | kernel `file=disk.img` (loop) on ext4 | **raw parser (R1)** | eject-handoff | LOW | image-on-FS = one extra layer; **V1-proven**, needs no repartition |
| S2 raw-partition LUN | kernel `file=/dev/mmcblk0pX` | raw parser (R1) | eject-handoff | LOWEST | needs SD reflash/repartition -> future option only |
| S3 dm-thin LV LUN | kernel `file=/dev/mapper/...` | dm-snapshot RO | eject-handoff | LOW-MED | **CoW/pool-full risk to write path** — rejected |

### App / backend stack
| Opt | Stack | Pros | Cons |
|-----|-------|------|------|
| **A (ground-up, REC — operator chose)** | Rust (axum+tokio+sqlx) monolith of small binaries; Preact/Svelte/Solid static SPA; MapLibre | smallest RAM (~30-50 MB), no GC, one toolchain, RO appliance image | full rewrite; less familiar stack |
| B (evolve) | Flask quarantined behind nginx/gunicorn + Rust worker; existing UI | reuse working UI; ship faster | ~100 MB Python overhead; 2 toolchains forever; **rejected by operator** |
| H (hybrid) | Rust core now; keep Flask UI; migrate later | gets invariant immediately; preserves UI | interim 2-toolchain footprint; **superseded — operator chose full Rust** |

### OS base
| Opt | Base | Pros | Cons |
|-----|------|------|------|
| **O1 (REC — operator chose, no reflash)** | current Raspberry Pi OS, cleaned in place + RO-root + overlay | least migration risk; known-good drivers/firmware; **no reinstall needed** | heavier than appliance images |
| O2 | Buildroot/Yocto RO squashfs + A/B OTA | reproducible, smallest RAM, atomic updates | **needs reflash — off the table** |
| O3 | Alpine diskless (rootfs in RAM) | power-loss can't corrupt OS; tiny | **needs reflash — off the table** |

---

## RECOMMENDED OPTIMUM (closest to ~0 cons) — UPDATED per operator decisions
**S1 + A + O1, converted IN PLACE on the existing device.**

Operator decided: full Rust (remove Python entirely), and NO reflash / NO clean
SD — clean up and convert the existing Pi. That makes the V1-proven **image-file
LUN (S1)** the right backing (no risky in-place repartition of the live boot
card) and the **full Rust app (A)** the app layer.

- **Gadget/storage (S1):** kernel `usb_f_mass_storage` with `file=<disk.img>` on
  the existing ext4 data area. Inside the image: MBR + 2 partitions (TeslaCam
  exFAT + media exFAT). Kernel owns the LUN -> Pi crash/reboot = clean unplug,
  not EIO. This is literally how reliable V1 worked. (S2 raw-partition is a
  future option ONLY if the operator ever chooses to reflash.)
- **Reads (R1):** conservative Rust raw exFAT/MP4/SEI parser, `pread()` on the
  image/loop, stability-gated; optional short-lived raw-parser-over-snapshot for
  explicit playback/export.
- **Writes:** eject-handoff mutator (Rust), car-state-aware quiescence, never
  during Sentry/honk saves.
- **App layer (A — full Rust):** small Rust binaries — `gadgetd` (critical),
  `scannerd`, `indexd`, `webd` (axum, serves a static SPA + REST/SSE), `uploadd`,
  `retentiond`, `wifid`. **Python/Flask removed entirely.** Keep/reuse only the
  existing Rust SEI/indexing parser. UI rebuilt in a small static SPA
  (Preact/Svelte/Solid) that looks/behaves like today.
- **OS (O1, in place):** keep the EXISTING Raspberry Pi OS on the EXISTING SD;
  clean it up (remove all old B-1 software + leftover/temp/junk), then harden:
  read-only root + overlay, hardware watchdog armed, cgroup `MemoryMax` caps,
  gadgetd `OOMScoreAdjust=-1000`. No reinstall.
- **HUD/map/cloud/wifi:** client-side Canvas HUD; MapLibre; SEI parsed at index
  time; rclone/Rust durable upload queue from the archive partition; STA/AP
  state machine with TX rate cap + SDIO chip-reset watchdog.

Net: every "latch" failure mode (backend OOM, whole-Pi hang, WiFi wedge, indexer
interfering, user delete) is closed because the car's write path is a
kernel-owned image-file LUN and a reboot is a clean unplug. Residual risks (raw
SD EIO, brownout, kernel panic, Tesla firmware bug) are the irreducible floor.

## IN-PLACE MIGRATION PLAN (no reflash, reversible, rails-safe)
Done via the hardware-test skill (dead-man timer, SSH/WiFi/boot protected,
backups before mutate, GPT-5.5 review before risky live steps).
- M1 — **Back up** existing clips + configs off the Pi (verify checksums).
- M2 — **Inventory + clean the device:** list and stop all old B-1 services
  (teslafat, NBD, Python web app, watchdogs), remove their units/files, and sweep
  leftover temp/junk/orphaned files. (Directly satisfies the earlier
  "review device for leftover/temp content" request.)
- M3 — **Stand up the kernel image-file LUN** in existing free space, bring it up
  via the new Rust `gadgetd`; **prove the car records reliably** (UDC configured,
  diskstats write counters climbing) BEFORE removing any safety nets.
- M4 — **Deploy the Rust app** (web/index/upload/wifi); migrate existing clips
  into the new image layout; verify UI end-to-end (Playwright, per repo rule).
- M5 — **Harden:** RO-root + overlay, hardware watchdog, memory caps; soak.
Each step is reversible; the old card state is backed up so we can roll back
without losing recording.

## Highest-risk unknowns to PROTOTYPE FIRST (gate everything else)
1. **Tesla acceptance of one image-file LUN with MBR + 2 partitions** (chimes/
   lightshow read from p2). PROVE before anything else.
2. **Clean eject (`forced_eject`/UDC unbind) + rebind behavior** across the
   car — confirm soft-eject is treated as benign (no latch) and re-insert
   resumes recording in ~2 s; measure how long mid-write disappearance is
   tolerated.
3. **Raw exFAT parsing + clip-stability detection while the car writes** —
   accuracy of the skip-and-retry gating; no false "stable".
4. **BCM43436 TX throttle threshold** — exact Mbps/chunk size that avoids the
   SDIO deadlock; confirm `rmmod/modprobe` recovery reliability.
5. **microSD latency under simultaneous car-write + Pi archive-copy/index** —
   ensure Pi I/O can't starve the car's writes (ionice/IOWeight); pick
   high-endurance A2/V30 media; consider overprovisioning.
6. **Cold boot-to-gadget-ready time** (target < 8-10 s; bring gadget up before
   mounting /data if needed).
7. **H.265 SEI HUD sync + browser playback compatibility** across real
   Chrome/Safari/Firefox/Edge desktop + iOS/Android; "download to view"
   fallback where H.265 unsupported.

## Decisions — RESOLVED by operator
- D1 — App layer: **FULL RUST. Remove Python/Flask entirely**, rebuild UI in
  Rust/static SPA. No backwards compatibility. (Was: hybrid — now superseded.)
- D2 — Migration: **NO reflash, NO clean SD.** Clean up and convert the EXISTING
  Pi in place (see IN-PLACE MIGRATION PLAN). (Reverses the earlier fresh-SD idea.)

## Carry-over incident (do not lose)
Recording may still be DOWN from the prior session; recovery needs an operator
VBUS power-cycle. After this planning task, verify recovery on the live device
ONLY via the hardware-test skill (UDC `state=configured`; nbd0/diskstats write
counters incrementing). Mandatory GPT-5.5 second-opinion before any live action.
