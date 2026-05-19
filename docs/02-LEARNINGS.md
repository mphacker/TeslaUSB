# TeslaUSB B-1 — Learnings (carry-forward + new)

A living catalog of technical findings. Every learning here was
paid for in real bugs, real outages, or real research. Future
contributors (including future-me) should consult this before
making architectural changes.

---

## Carry-forward from v1 architecture (still apply in B-1)

These were discovered in the IMG-based architecture but the
underlying truths apply to any TeslaUSB design:

### Power & vehicle environment

- **Tesla power can drop at ANY moment.** Vehicle sleep, garage
  door triggered, driver opening a door — these all cut USB
  power without warning. Every persistent write must be `fsync`'d
  before acknowledging completion.
- **Tesla writes ~4-6 clip groups per minute when recording.**
  Each group is 6 cameras × ~10-75 MB. Real-time throughput
  requirement: ~30 MB/s sustained. The Pi Zero 2 W's SDIO bus
  caps at ~12-20 MB/s shared with WiFi, so I/O efficiency
  matters enormously.
- **Vehicle sleep is indistinguishable from a crash in journal
  forensics.** Distinguishing requires looking at workload (was
  archiving active? was Tesla writing?) and `max hold` times in
  the task coordinator (>60 s = real near-miss; <2 s = clean
  sleep).
- **RecentClips rotates on TIME (60-minute ring), not free
  space.** Bigger storage doesn't extend retention. Any
  unprocessed clip older than 60 min is overwritten by Tesla.

### Hardware

- **Pi Zero 2 W has ONE SDIO bus shared between SD card and the
  Broadcom WiFi chip.** Heavy SD I/O during catch-up can
  saturate the bus and starve WiFi, which leads to
  `brcmf_cfg80211_stop_ap` errors, then watchdog reset.
- **Hardware watchdog timeout is 90 s.** Any task holding the
  task_coordinator lock for more than ~60 s is a near-miss. The
  watchdog daemon itself must be `Nice=-5
  IOSchedulingClass=realtime`.
- **`max-load-1=24` in watchdog.conf** allows transient
  catch-up to run hot without triggering. Don't lower this
  without re-testing.

### Filesystem

- **FAT32 + USB MSC = single writer.** The Pi cannot safely
  modify the FAT structure while the gadget is bound — two
  writers corrupt the FAT table. v1 used `quick_edit_part2` to
  unbind, mount RW, edit, unmount, rebind. **B-1 eliminates
  this entirely** because there is no FAT to corrupt — it's
  synthesized fresh on every Tesla read.
- **VFS cache invalidation between loopback mount and gadget
  block device** required `echo 2 > /proc/sys/vm/drop_caches`
  in v1. Never use `umount -l + remount` for cache refresh —
  that breaks the gadget's view. **B-1 eliminates this** —
  there's no loopback mount.
- **Tesla writes via the gadget block layer; v1 Pi read via
  VFS.** The two paths had no lock contention but required the
  drop_caches dance. **B-1 unifies these** — both paths go
  through the same Rust daemon.

### USB gadget

- **USB gadget presentation is the #1 priority at boot.** Tesla
  must see the drive within ~3 s. All other work is deferred.
  `present_usb.sh` does the minimum possible to bind the
  gadget; everything else runs after.
- **Loop devices for v1 IMG mounting could not be detached
  while the gadget held them.** `quick_edit_part2` had a
  specific sequence to clear LUN1 backing → unmount → detach →
  recreate → mount → restore LUN1. Any deviation produced
  read-only-stuck filesystems or kernel locks. **B-1
  eliminates loop devices.**
- **The gadget LUN file path goes into the configfs `lun.0/file`
  attribute, NOT a loop device path.** In v1 this was an IMG
  file; in B-1 it's `/dev/nbd0` (which is a real block device,
  so this is supported).

### Filesystem repair

- **`fsck.vfat` runs ~1 s at boot in v1 when
  `disk_images.boot_fsck_enabled: true`.** **B-1 doesn't need
  this** — there is no on-disk FAT to repair. The synthesized
  FAT is regenerated fresh on every Tesla read.

### Web app

- **Web service runs on port 80** (not 5000) for captive portal
  to work without iptables redirects. Must run as root to bind
  privileged port — keep that.
- **Samba is an optional feature in B-1, off by default.** v1
  used Samba only in "edit mode" because the IMG was only
  RW-mounted then. In B-1 the backing tree at `/var/teslacam/`
  is always RW, so Samba can run continuously when enabled.
  The user toggles it from Settings → Network Sharing in the
  web UI. When on, Samba shares
  `/var/teslacam/TeslaCam/` and
  `/var/teslalightshow/` read-write. A file watcher
  (inotify on the Samba paths) triggers
  `cache_invalidation.schedule_invalidation()` when a file
  is modified via SMB so Tesla picks up the change. Off by
  default keeps the resident footprint small for users who
  don't need it.

### Cloud sync

- **rclone with `nice -n 19 ionice -c3` and bandwidth cap is
  mandatory.** Without it, sync starves the web server and
  Tesla writes. Carry these flags into B-1.
- **Dedup oracle is `cloud_synced_files` SQLite table** — never
  delete rows. The "reset stats" feature writes a baseline
  timestamp, doesn't delete rows.
- **`RecentClips` is NEVER a valid sync target** because Tesla
  rotates the underlying files. In B-1 we have a choice: either
  expose `RecentClips` for cloud sync (now safe because B-1
  keeps the underlying file even after Tesla "rotates" it), or
  keep the v1 policy. **Decision pending in Phase 5.**

### WiFi

- **Power-save disabled (`wifi.powersave=2`) is the single most
  important setting for responsive roaming.** Carry this into
  B-1.
- **WiFi STA retry must use exponential backoff** when the AP
  is up. The May 2026 crash was caused by fixed-interval STA
  retries thrashing the brcmfmac driver. `wifi-monitor.sh`
  retry: 2.5 → 5 → 15 → 30 min cap.
- **NetworkManager manages wpa_supplicant via D-Bus** — does
  NOT read `/etc/wpa_supplicant/wpa_supplicant.conf`. Config
  goes in NM connection profiles.

### Safety

- **SSH is sacred** — systemd drop-in `/etc/systemd/system/
  ssh.service.d/teslausb-protect.conf` prevents stop/mask.
- **Safe-mode boot detection** — 3+ reboots in 10 min skips
  TeslaUSB services. State at `/var/lib/teslausb/reboot_log`.

### Tesla USB filesystem cache

- **Tesla caches the USB directory listing and won't see file
  changes without a re-enumeration.** v1's lock-chime upload
  rebinds the gadget (unplug/replug simulation). **B-1 should
  do the same after `LockChime.wav` overwrites** even though
  the file change is instant on our side.

---

## New learnings (B-1 specific)

(populated as we discover them)

### B-1 fundamental: anti-anchoring (Rust where it helps, redesign where v1 was wrong)

**Operator directive (2026-05-19), verbatim:**
> *"Don't feel locked into the original way of doing things. We
> want to find the best way to achieve the goals and requirements
> of this project. Don't feel we need to use the same config
> files, or approaches."*

> *"If something could work faster in Rust vs Python, do it in
> Rust. Don't worry about potential regression. This is a new
> build. I want the best outcome."*

**Practical implications baked into B-1:**

1. **Three Rust binaries + one Python web app**, not "Rust for
   FAT synth + Python for everything else". `teslafat-0`,
   `teslafat-1`, `teslausb-worker` are all Rust. Only the
   user-facing Flask UI (where UI parity is binding and the
   templates port one-to-one) stays in Python.

2. **SEI parser, indexer, cleanup, cloud uploader, file watcher
   all in Rust.** No more "we already have working v1 Python so
   port it" reasoning. v1 code is reference material; B-1 writes
   the better version from scratch in the faster language.

3. **Parity test suites gate the rewrites.** B-1's Rust SEI
   parser must produce byte-identical waypoint/event output on
   the v1 fixture set before it is allowed to replace the Python
   parser. We get the speed without trusting "I rewrote it
   correctly" — we prove it.

4. **No v1 carry-forwards by default.** Every v1 pattern is
   evaluated on its merits for B-1 and either explicitly KEPT
   (with reasoning) or explicitly REPLACED (with what replaces
   it). The PLAN.md has two explicit tables: "v1 carry-forwards
   we are NOT taking" and "v1 carry-forwards we ARE keeping".

5. **TOML over YAML for config**, FHS paths over `/home/pi/...`,
   nginx + gunicorn over "Flask as root on port 80", configfs
   over `g_mass_storage` module, single consolidated SQLite DB
   over v1's three DBs, `fcntl(LOCK_EX)` over Python
   `task_coordinator`. Each of these is a deliberate
   improvement, not a habit.

6. **"It worked in v1" is not a justification for B-1.** A v1
   pattern only carries forward if it also passes the test
   "would a greenfield project starting today choose this?".
   Many v1 patterns were forced by the v1 architecture (loop
   devices, edit mode, quick_edit lock, task coordinator).
   B-1 doesn't have those forcing constraints — and so doesn't
   inherit the patterns.

7. **Risk of regression is acceptable, risk of carrying forward
   the wrong design is not.** The operator explicitly said
   "Don't worry about potential regression." That removes the
   conservative bias that would otherwise drag v1 patterns
   forward. We have parity test suites (point 3) to catch real
   regressions; we don't need the false safety of "we kept the
   Python".

### B-1 fundamental: Code Quality Charter is binding from day one

`docs/03-CODE-QUALITY-CHARTER.md` is the single source of truth
for how B-1 code is written and reviewed. Five pillars:

1. **No code smells** — long functions, deep nesting, magic
   values, primitive obsession, data clumps all rejected
2. **Best architecture practices** — hexagonal layering,
   dependency inversion, immutability by default, pure
   functions where possible, no global mutable state
3. **No shortcuts** — pick the right approach even when the
   easy one would compile; if Approach B takes 2× longer but
   is correct, we take B
4. **Fix bugs immediately, never defer** — Boy Scout rule,
   regression tests mandatory, no `TODO` without a linked issue
5. **No dead code** — unused imports/functions/parameters
   deleted; commented-out code deleted; vestigial config
   deleted; "backup" files never committed

**Why this matters for B-1 specifically:** the v1 codebase
accumulated significant tech debt over its lifetime — a
copier pipeline that wasn't needed, dead config keys, the
`task_coordinator` complexity layered on to handle issues
that better architecture would have prevented. B-1 starts
clean. The charter is what keeps it clean.

**Enforcement:** CI gates (clippy `-D warnings`, ruff,
`mypy --strict`, ≥80% coverage on critical paths, dead-code
detectors). Pre-commit hooks for local enforcement.
Reviewer checklist for everything tooling can't catch.

**Operator directive (2026-05-19), verbatim:**
> *"We also want to make sure we don't have code smells, we
> follow best architecture practices, we don't take shortcuts
> or go with the 'easy' approach when there is a better
> approach that might just take a bit more work. Don't be
> lazy. Never leave bad code or bugs, fix things as they are
> found. No dead code."*

### B-1 fundamental: UI parity with v1 is a binding constraint

The user explicitly directed (2026-05-19):
> *"I want the website to look the same as it does now. Same
> look, feel and features. Of course some modifications will
> need to happen since we won't need to switch between present
> mode and edit mode anymore."*

**B-1 is a backend rewrite, NOT a UI rewrite.** The v1 web UI
is the reference design and ships into B-1 essentially
unchanged. Templates, CSS tokens, JS modules, Lucide icons,
Inter fonts, the map-integrated video panel, the camera
switcher, the dual fullscreen affordances, the disambiguation
popup, the mobile bottom-tab / desktop sidebar layout, dark +
light mode — all preserved verbatim.

**The only allowed UI changes** are the minimal surgical edits
required because the underlying infrastructure no longer
exists:

1. No "Network File Sharing" status dot (no quick_edit
   means no "sharing active" state to indicate)
2. No fsck widget (btrfs scrub status replaces it)
3. No disk image size sliders (no IMG files)
4. No mode-related toggles (no modes)
5. Lock chime / light show / music / wraps upload paths
   are simpler internally but the user-facing flow is the
   same

**`docs/UI_UX_DESIGN_SYSTEM.md` from `main` is the authority.**
Copied verbatim into B-1 as `docs/05-UI-UX-DESIGN-SYSTEM.md`.
Every UI decision references that doc — no second source of
truth, no quiet drift from v1's design.

**Verification gate before cutover:** side-by-side screenshot
comparison of v1 vs B-1 on every page, at 375 px (mobile)
and 1280 px (desktop), in both dark and light mode. Zero
visible differences except the documented mode-removal list.

### B-1 fundamental: power can drop at ANY moment, multiple times per day

The Pi runs in a vehicle. It has no UPS, no battery, no graceful
shutdown signal. Normal events that cut power without warning:

- Vehicle goes to sleep (12V rail drops)
- Owner unplugs the USB cable to use a different drive
- Vehicle service / maintenance disconnects power
- Owner power-cycles the device to "fix something"
- 12V battery dips below the threshold during a long park
- Cybertruck's Sentry-mode deep sleep cuts the USB port power

**Several power cuts per day is normal, not exceptional.**

**Implications, enforced throughout B-1:**

1. **FUA + fdatasync on every Tesla write.** Tesla's SCSI write
   doesn't complete (Tesla doesn't think the file is durable)
   until `fdatasync(2)` returns on the underlying btrfs file.
   The kernel NBD layer propagates the FUA flag from g_mass_storage
   → NBD → teslafat → POSIX. We honor it.

2. **No daemon state lives only in RAM.** The cluster_map is
   pure derivation: scan `/var/teslacam/`, build the map. On
   crash, the map is gone but the data is on disk; on next
   start, we rebuild from disk. No persistent metadata DB
   that could itself be corrupted.

3. **Atomic file creation via `.partial` suffix.** Tesla's
   write sequence for a new clip is:
   ```
   1. write FAT entries reserving cluster chain
   2. write directory entry pointing at the chain (still marked
      "in progress" — Windows convention is the size starts at 0
      and grows)
   3. write data clusters
   4. write directory entry update with final size
   ```
   We translate step 1-2 into `open(path + ".partial", O_CREAT)`,
   step 3 into `pwrite()`, step 4 into `rename(path + ".partial",
   path)`. A power cut between steps 2 and 4 leaves
   `clip.mp4.partial` on disk with the data written so far. The
   cleanup worker reaps `.partial` files older than 5 minutes.
   The indexer never sees a half-formed MP4 in its inotify watch.

4. **Cold-start ≤ 1 s.** The gadget must be up within ~3 s of
   boot or Tesla's USB enumeration retry loop adds latency or
   gives up. teslafat's full directory scan + FAT/exFAT
   synthesis must complete in under 1 s for a typical 10K-file
   backing tree. Achieved by lazy materialization of deep
   subdirectories (root + top-level dirs eager, deeper dirs
   on-demand).

5. **Multi-reboot tolerance.** "Multiple reboots per day" means
   we can't have any failure mode that requires a manual
   recovery step. The safe-mode boot detector (3 reboots in
   10 min → skip teslafat) is the absolute last line of defence;
   normal operation must NEVER trigger it.

6. **No file lock files.** v1 used `.quick_edit_part2.lock` with
   a 120 s stale timeout — a power cut during an RW edit could
   strand a lock that blocked future operations until the
   timeout. B-1 has no quick-edit at all (Tesla and the cleanup
   worker write to the same native files), so there's nothing
   to lock and nothing to leak.

7. **btrfs as the underlying FS.** Selected over ext4 for: CoW
   semantics (write amplification reduction), per-file
   `chattr +C` to disable CoW on hot-rewrite files if needed,
   built-in checksum (detect SD-card silent corruption — real
   on cheap cards), atomic snapshots for backup. btrfs's
   journal recovery is fast and automatic on mount.

8. **Web UI state.** All durable state (geodata.db, settings)
   uses SQLite with `PRAGMA journal_mode=WAL` + `PRAGMA
   synchronous=NORMAL` (not FULL — WAL on btrfs already gives
   us crash safety without the fsync-per-COMMIT cost). On a
   power cut, SQLite's WAL recovery is automatic on next open.

### B-1 fundamental: B-1 eliminates the "RecentClips rotation race" entirely

In v1, Tesla owned the FAT table and rotated files on a 60-min
ring. The Pi's archive worker raced to copy files out of the IMG
before Tesla overwrote them. We frequently lost the race — 199
RecentClips files marked DEAD in one overnight outage.

**In B-1, the race does not exist:**
- Tesla writes a clip → it lands as a native file at
  `/var/teslacam/TeslaCam/RecentClips/clip.mp4` immediately
- 60 min later, Tesla decides to "rotate" the clip — it issues
  SCSI writes to clear the directory entry (mark 0xE5 in FAT32
  or clear InUse bit in exFAT) and free the clusters
- **The teslafat daemon intercepts these writes and does
  nothing destructive.** Tesla now believes the file is gone.
- The retention shim hides the file from Tesla's next
  directory enumeration. To Tesla, the slot is free for new
  writes.
- The backing Linux file at `/var/teslacam/...` is **untouched**.
- A separate cleanup worker (in the Python web app, async,
  not in the SCSI hot path) decides when to actually delete
  the backing file. Policy: preserve indefinitely if the clip
  has GPS data (indexed by the SEI parser); delete after N
  days otherwise.

**Implication for design:** the teslafat daemon's view of "what
files exist" diverges from Tesla's view. Tesla sees a 60-min
ring; teslafat sees an ever-growing archive bounded only by the
cleanup policy. We need data structures that handle this
divergence:
- The `cluster_map` (which backing file owns which virtual
  cluster) must support stable identity even when Tesla
  "deletes" and re-allocates the same cluster numbers
- New file writes from Tesla must NEVER overwrite existing
  backing files; each Tesla "create" maps to a NEW backing path
- Stable backing paths: `RecentClips/2026-01-15_14-32-15-front.mp4`
  is how Tesla names it (timestamp prefix); collisions are
  effectively impossible at the second granularity so we can
  use Tesla's name as-is

**Indexer-driven preservation:**
- An inotify watcher in the Python app sees every new MP4 land
- Parses SEI / mvhd for GPS waypoints and recording time
- Writes to `geodata.db` (waypoints, trips, detected_events)
- The cleanup worker queries `geodata.db` before deleting any
  RecentClips file: if waypoints exist for that file, preserve

**Result:** zero data loss for any clip Tesla recorded GPS for,
regardless of how long catch-up takes. This is THE primary win
of B-1 over v1 — even bigger than the I/O efficiency win.

### exFAT is the primary filesystem; FAT32 is the fallback

Modern Tesla firmware (2022+) uses exFAT for drives > 32 GiB,
and that's what real owners run. FAT32 has a hard 4-GiB-1 single
file cap that we'd hit if Tesla ever ships continuous-trip
recording. Volume size > ~256 GiB starts to feel awkward with
FAT32 (32-KiB or 64-KiB clusters get wasteful). exFAT scales
cleanly to multi-TB volumes and ETB single files.

**Implementation strategy:**
- `teslafat::fs` trait abstracts "synthesize a filesystem view
  from a directory tree"
- Two impls: `fat32` (the 1996 spec) and `exfat` (the 2006 spec
  open-sourced by Microsoft in 2019)
- Config selects per-install. Auto-default: exFAT if
  `volume_size_gb > 32`, FAT32 otherwise.

Why also keep FAT32: smaller-volume installs, dev/test
machines where mkfs.vfat is faster than mkfs.exfat for
debugging, and older Tesla firmware (rare but exists).

### Phase 0 (scaffolding)
- TBD

### Phase 1 (Rust daemon)
- TBD

### Phase 2-4 (FAT/exFAT synthesis)
- TBD

### Phase 5 (Python web)
- TBD

### Phase 6 (setup/uninstall)
- TBD

---

## Anti-patterns (DO NOT REPEAT)

From v1's history, things we will deliberately avoid in B-1:

1. **Don't introduce a copy pipeline.** B-1's whole purpose is
   to make archive copies unnecessary. If a future contributor
   adds a worker that copies files from `/var/teslacam` to
   another path, that's a regression.
2. **Don't add quick-edit-style mount-cycling.** Files are
   native; just edit them.
3. **Don't add mode switching.** There is no "present mode" vs
   "edit mode." The gadget is always bound; writes happen
   through it.
4. **Don't add multiple workers that contend for a lock.** v1
   had indexer + copier + triage + archive + cloud + watcher
   all fighting for `task_coordinator`. B-1 should have at
   most: indexer (reads), cloud sync (reads), retention sweep
   (reads + occasional delete). All read-mostly. No writers
   contend with each other.
5. **Don't expose a "Present mode" / "Edit mode" toggle in the
   UI.** Those are dead concepts in B-1.
6. **Don't compose FAT32 entries by hand without library
   support for LFN edge cases.** Use a well-tested encoder or
   write one with exhaustive unit tests against `mtools`
   reference output.
