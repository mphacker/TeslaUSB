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
  RW-mounted then. In B-1 the backing tree at `/srv/teslausb/teslacam/`
  is always RW, so Samba can run continuously when enabled.
  The user toggles it from Settings → Network Sharing in the
  web UI. When on, Samba shares
  `/srv/teslausb/teslacam/TeslaCam/` and
  `/srv/teslausb/media/` read-write. A file watcher
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
  rebinds the gadget (unplug/replug simulation). **B-1 now does
  the same after `LockChime.wav` activation** — see the B-1
  learning "Lock chime needs a full UDC re-enumeration" below.
  A soft SCSI medium-change is NOT enough for the chime.

---

## New learnings (B-1 specific)

(populated as we discover them)

### Lock chime needs a full UDC re-enumeration, not a soft invalidate

**Symptom (2026-05-29):** Activating a new lock chime from the web
UI updated the active-chime banner and copied the audio to the media
LUN root as `LockChime.wav`, but the car kept playing the OLD chime
on lock/unlock.

**Root cause:** The file *location* was already correct (root of the
MEDIA LUN, which is what the car reads). The bug was the *cache
invalidation method*. B-1's web activation originally fired only a
soft SCSI medium-change via `tesla_cache_invalidate.sh` (eject +
re-insert a single LUN, ~200 ms). That is enough for the car to
re-scan a LUN's *directory listing* (e.g. Light Show `.fseq` files),
but the car only re-reads `LockChime.wav` itself on a **full USB
re-enumeration** — a UDC unbind/rebind that simulates an
unplug/replug. This is exactly what v1 did on chime upload.

**Fix:** Keep the chime on the media LUN (no drive move), and on
chime activation trigger a full gadget re-enumeration via
`scripts/tesla_gadget_rebind.sh` (sync → UDC unbind → settle →
restore LUN backing files → UDC rebind → bounded health wait). The
web `GadgetRebinder` service runs it synchronously and single-flight;
if the rebind fails it falls back to the soft invalidate. Only the
three activation paths (upload+set-active, set-as-chime, delete of the
active chime) re-enumerate; other mutations keep the cheaper soft
invalidate.

**Takeaway:** Soft medium-change ≠ re-enumeration. For anything the
car reads *by name from a fixed path* (the lock chime), you must
re-enumerate the gadget; a per-LUN eject/insert is insufficient.

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
   pure derivation: scan `/srv/teslausb/teslacam/`, build the map. On
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
  `/srv/teslausb/teslacam/TeslaCam/RecentClips/clip.mp4` immediately
- 60 min later, Tesla decides to "rotate" the clip — it issues
  SCSI writes to clear the directory entry (mark 0xE5 in FAT32
  or clear InUse bit in exFAT) and free the clusters
- **The teslafat daemon intercepts these writes and does
  nothing destructive.** Tesla now believes the file is gone.
- The retention shim hides the file from Tesla's next
  directory enumeration. To Tesla, the slot is free for new
  writes.
- The backing Linux file at `/srv/teslausb/teslacam/...` is **untouched**.
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

## Two-LUN media layout (TESLACAM vs MEDIA)

Tesla expects USB drives to follow specific root-level folder names
for non-recording features:

| Tesla folder | Purpose |
|---|---|
| `Boombox/` | Custom Boombox horn sounds (max 5 files, alphabetical) |
| `LightShow/` | Sequence files for Light Show (paired `.fseq` + `.wav`) |
| `LockChime.wav` | Custom lock chime file |

On B-1 the TESLACAM LUN (LUN 0, exFAT, 256 GB) is reserved for
recordings. **All media-feature files must land on the MEDIA LUN
(LUN 1, FAT32, 32 GB)** — backing root `/srv/teslausb/media`. If
they land on the TeslaCam LUN, the analytics dashboard double-counts
them against the TeslaCam quota AND Tesla can't find them on the
expected drive.

The historical layout (carried over from v1) was wrong:

| Feature | Old path (TESLACAM, wrong) | New path (MEDIA, correct) |
|---|---|---|
| Boombox | `/srv/teslausb/teslacam/Music/Boombox/` | `/srv/teslausb/media/Boombox/` |
| Light Show | `/srv/teslausb/teslacam/lightshow/LightShow/` | `/srv/teslausb/media/LightShow/` |
| Lock Chime (active) | `/srv/teslausb/teslacam/lightshow/LockChime.wav` | `/srv/teslausb/media/LockChime.wav` |
| Chime library | `/srv/teslausb/teslacam/lightshow/Chimes/` | `/srv/teslausb/media/Chimes/` |
| Wrap library | `/srv/teslausb/teslacam/lightshow/Wraps/` | `/srv/teslausb/media/Wraps/` |
| License plates | `/srv/teslausb/teslacam/lightshow/LicensePlate/` | `/srv/teslausb/media/LicensePlate/` |

Notice that the new layout also drops the `lightshow/` wrapper —
Tesla's spec is unambiguous that `LightShow/` lives at root.

### Config rule for future media features

When adding any new "media" feature (anything Tesla itself reads from
the USB drive, or anything in the website's Media section):

1. Add a `media_root` reference in `PathsSection`, default
   `/srv/teslausb/media`. **Do not** anchor at `paths.backing_root`.
2. Use Tesla's exact folder name at the root of `media_root`. Do
   not nest under a wrapper folder unless Tesla itself expects one
   (e.g., wraps go under `LightShow/wraps/` when *active*, but the
   *library* lives at `media_root/Wraps/`).
3. Update the analytics LUN probe expectations — both LUN backing
   roots resolve to the same btrfs filesystem, so the LUN cards
   show the LUN's `volume_size_gb` cap, not the underlying fs
   size. Adding/removing files only moves the per-LUN `du`-based
   "used" number, not the displayed total.

### Migration note

When changing media-file storage locations between releases, the
upgrade script must `mv` (not `cp`) the existing files from the
old location to the new one and **delete the old parent folders**.
Leaving stale empty directories on the TESLACAM LUN causes
operators to think the migration is partial and to manually re-copy
files, double-storing them on both LUNs.

### Symptom → cause cheat sheet

| Symptom | Likely cause |
|---|---|
| Analytics MEDIA card shows `0 used` despite uploaded files | Code is still writing to `backing_root/lightshow/...` instead of `media_root/...` |
| Tesla doesn't see uploaded Light Show / Lock Chime | Files landed under `lightshow/` wrapper instead of root of MEDIA LUN |
| Tesla still plays OLD Lock Chime after activation (file is correct on MEDIA root) | Only a soft SCSI medium-change fired; the chime needs a full UDC re-enumeration (`tesla_gadget_rebind.sh`) |
| Boombox upload "works" but car doesn't play it | Wrote to `Music/Boombox/` on TESLACAM instead of `/Boombox` on MEDIA |
| Analytics TESLACAM card shows usage growing for non-recording files | A media feature is still anchored on `backing_root` instead of `media_root` |

---

## Anti-patterns (DO NOT REPEAT)

From v1's history, things we will deliberately avoid in B-1:

1. **Don't introduce a copy pipeline.** B-1's whole purpose is
   to make archive copies unnecessary. If a future contributor
   adds a worker that copies files from `/srv/teslausb/teslacam`
   to another path, that's a regression.
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


---

## Phase 6 — live bring-up on cybertruckusb.local (2026-05-21)

These were paid for in real outages during Phase 6 hardware bring-up.
Every entry corresponds to a specific failure mode observed on the
live Pi.

### Two systemd units MUST NOT share `RuntimeDirectory=`

**Symptom:** After a few `teslausb-web` restarts the gadget chain
silently broke — `nbd-attach@{0,1}` failed with `CONNECT failed`,
`usb-gadget` failed its dependency, the UDC was unbound, and the
Tesla saw no USB drive. SSH and the web UI still worked, so it
was easy to miss.

**Root cause:** Both `teslafat@.service` (User=teslausb) and
`teslausb-web.service` (User=pi) declared `RuntimeDirectory=teslausb`.
systemd treats RuntimeDirectory per-unit: every restart of one unit
recreates `/run/teslausb` with that unit's User/Group AND wipes any
file inside it that the unit doesn't own. A `teslausb-web` restart
therefore deleted `/run/teslausb/teslafat-{0,1}.sock` — the NBD
sockets — and there is no service that recreates them except a full
`teslafat` restart.

**Rule:** Each systemd unit gets its own `RuntimeDirectory=` name.
Production layout:

| Unit | RuntimeDirectory | Contents |
|---|---|---|
| `teslafat@.service` | `teslausb` | `teslafat-N.sock` (NBD server sockets), `worker.sock` (future IPC) |
| `teslausb-web.service` | `teslausb-web` | `gunicorn.sock` (only) |

Update `config/gunicorn.conf.py` and `config/nginx-teslausb.conf`
together when the socket path changes — they must agree.

### Don't share `/var/lib/teslausb` between the Rust worker and the Flask app without g+w

**Symptom:** gunicorn worker failed to boot with
`sqlite3.OperationalError: attempt to write a readonly database`,
returning 502 on every page.

**Root cause:** `teslausb-worker` (User=teslausb) needs to own
`/var/lib/teslausb/index.sqlite3`. `teslausb-web` (User=pi, until
phase 6.2) also opens `mapping.db` and `cloud_sync.db` in the same
dir. The dir was created `0750 teslausb:teslausb` so pi could not
open the .db files for write.

**Rule:** `/var/lib/teslausb` is mode `0770 teslausb:teslausb`.
`setup-lib/02-users.sh` already adds `pi` to the `teslausb` group,
so both users have group-write. Don't tighten to 0750 until the
web app's `User=` migrates to `teslausb` (phase 6.2 TODO).

### Phase-7 IPC daemon doesn't exist yet — gate the health probe

**Symptom:** Web UI showed a permanent red "Daemon socket missing"
dot in the header and in the System Health card.

**Root cause:** `system_health._daemon_block` unconditionally tries
to connect to `cfg.paths.ipc_socket` (`/run/teslausb/worker.sock`).
The wire types exist in `teslausb-core::ipc::messages` but no
in-tree binary binds that socket — `teslausb-worker` is purely
indexer/cleanup with no `UnixListener`, and `teslafat`'s
`UnixListener` speaks binary NBD, not JSON envelope.

**Rule:** All health probes that depend on optional/future
subsystems must be gated by an explicit `features.*_enabled` flag
(matching how `samba_enabled` and `cloud_archive_enabled` already
work). Added `features.ipc_daemon_enabled` (default false). Flip
true the day the daemon ships.

### Dashboard "Connected to Tesla" must reflect live kernel state

**Symptom:** During the runtime-dir outage above, the dashboard
still showed the green "Connected to Tesla" card while the gadget
was actually unbound. The status was a lie because the template
branched on a pinned `mode_token='present'` constant.

**Rule:** The status card MUST probe configfs at request time.
`teslausb_web.services.gadget_state.gadget_mode_token()` returns
`'present'` only when `/sys/kernel/config/usb_gadget/g1/UDC` is
non-empty AND both `lun.{0,1}/file` point at real backing devices.
Anything else returns `'unknown'` (orange card). The probe is
filesystem-only (no IPC, no sudo) so it's cheap enough to call on
every page render.

### Worker `backing_root` is the LUN root, not the data parent

**Symptom:** `teslausb-worker` failed at startup looking for
`/srv/teslausb/RecentClips` — wrong path for B-1's 2-LUN layout.

**Rule:** B-1 layout is
`/srv/teslausb/teslacam/TeslaCam/{Recent,Sentry,Saved}Clips` for
LUN 0 (Tesla writes) and `/srv/teslausb/media/{LightShow,Boombox}`
for LUN 1 (user populates). The worker only walks the TeslaCam
LUN, so `worker.toml` sets `backing_root = "/srv/teslausb/teslacam"`.
The repo example matches; setup-lib step 04 should install this
file from `rust/crates/teslausb-worker/examples/worker.toml` — not
yet wired (gap noted in 01-PROGRESS).

### Boot-time order: nbd-attach races teslafat socket creation

**Symptom:** On a cold boot, `nbd-attach@0/1` would race and fail
because the teslafat sockets weren't bound yet. `usb-gadget` then
failed its dependency. Manual `systemctl restart` of nbd-attach +
usb-gadget after teslafat was up resolved it.

**Rule:** `nbd-attach@.service` needs `After=teslafat@%i.service`
AND `Requires=teslafat@%i.service`, plus a short `ExecStartPre`
that waits for the socket file to exist (e.g.,
`/bin/bash -c 'for i in $(seq 1 30); do test -S /run/teslausb/teslafat-%i.sock && exit 0; sleep 0.5; done; exit 1'`).
This is a Phase 6.11 follow-up; cold-boot still depends on
systemd-managed restart-on-failure to converge.

### `ping cybertruckusb.local` may resolve to public IPv6 first

**Symptom:** Operator reported "can't ping the device but SSH and
web work."

**Root cause:** Windows mDNS resolves `.local` to three addresses
including two public IPv6 (Comcast prefix) and the LAN IPv4. `ping`
picks the first answer (IPv6) and ICMPv6 to that public address is
filtered upstream. TCP (SSH, HTTP) falls back through the address
list.

**Rule:** When debugging Pi reachability, use `ping -4` or pin to
the LAN address. The Pi itself has zero firewall rules — never
chase a phantom ICMP block on the device when the real problem is
the operator's resolver picking the wrong address family.

### Always cancel the dead-man inside the SAME ssh command that arms it

**Symptom:** Pi spontaneously rebooted ~4 min after a manual H-test
completed successfully.

**Root cause:** I armed `b1-deadman.timer` (180s reboot) in one ssh
session and ran the test in another. The test succeeded but I
forgot to cancel the timer.

**Rule:** Arm + run + cancel in ONE command:

```bash
ssh pi@host 'sudo systemd-run --on-active=180 --unit=b1-deadman /sbin/reboot; <do thing>; sudo systemctl stop b1-deadman.timer'
```

Never two separate ssh calls. The cancel must be tied to the same
shell exit as the dangerous operation.


## Phase 6 — Tesla requires exFAT on the TeslaCam LUN

**Symptom.** With LUN 0 (256 GiB) configured as `fs_type = "fat32"`:

- gadget enumerates fine (`configured`, `high-speed`);
- Linux mounts the volume read-only fine (sees
  `TeslaCam/{Recent,Sentry,Saved}Clips`);
- Tesla performs the initial scan (hundreds of reads, a handful
  of metadata writes to BootSector / FsInfo) and then **stops
  writing entirely** — RecentClips stays empty even in active
  sentry mode that should write a new 6-camera + thumbnail
  bundle every minute;
- `ep1 is stalled` floods `dmesg` whenever Tesla retries the
  scan.

**Root cause.** Tesla's USB-storage stack rejects FAT32 volumes
larger than the Microsoft format limit (~32 GiB). Windows
refuses to `mkfs.vfat` past that boundary; Tesla adopted the
same rule. teslafat `synth` happily produces a 256 GiB FAT32
image, the Linux kernel mounts it (Linux's FAT driver tolerates
oversized FAT32), but Tesla treats it as malformed and silently
aborts the write path. Tesla's own docs recommend exFAT for any
USB drive >= 32 GiB.

**Fix.** Flip the TeslaCam LUN to exFAT in
`/etc/teslausb/teslafat-0.toml`:

```toml
fs_type = "exfat"
```

then restart `teslafat@0` -> `nbd-attach@0` -> `usb-gadget`.
teslafat already supports exFAT (Phase 3.5e). After the flip,
Tesla started writing within 60 s; in the next 100 s it shipped
13 files / ~270 MiB into
`/srv/teslausb/teslacam/TeslaCam/RecentClips/` with zero
endpoint stalls.

The MEDIA LUN (32 GiB) stays FAT32 — well within the Microsoft /
Tesla limit, and FAT32 is what the Tesla music player prefers.

Source change: `setup-lib/11-gadget.sh` template for LUN 0 now
ships `fs_type = "exfat"` by default, with a header comment
explaining why a future `setup.sh` run cannot silently regress
this to FAT32.

## Phase 6 — NBD logical block size must match the synthesized FAT BPB

**Symptom.** With `nbd-attach@.service` using `-block-size 4096`
and teslafat synthesizing a FAT32 BPB with `BPB_BytsPerSec =
0x0200` (512 bytes/sector), the kernel rejects mount with:

```
FAT-fs (nbd0): logical sector size too small for device
```

Linux refuses, Tesla refuses, the drive is unusable even though
enumeration succeeds.

**Fix.** Drop `nbd-attach@.service` to `-block-size 512` so the
NBD client advertises a 512-byte logical block, matching the BPB
exactly. `blockdev --report` after the change shows `SSZ=512
BSZ=512`; mount succeeds.

This applies to **both** FAT32 and exFAT, because teslafat also
synthesizes exFAT with 512-byte `BytesPerSectorShift` by default
and a 4096-byte NBD block would create the same mismatch.

The earlier comment in `B1_NBD_ATTACH_UNIT_BODY` claimed
`-block-size 4096` matches teslafat's "4 KiB sector emulation" —
that was incorrect. The unit body now ships `-block-size 512`
and the comment is replaced with a pointer to this learning plus
a warning that changing it will break BOTH the kernel mount AND
Tesla writes.

## Phase 6 — nbd-attach@ must wait for teslafat's socket

teslafat is `Type=simple`: systemd considers it "started" the
moment exec() returns, well before the daemon has bound
`/run/teslausb/teslafat-N.sock`. Without an explicit poll,
`nbd-attach@N` races, `nbd-client` fails CONNECT, the service
`Requires=` propagates the failure up to `usb-gadget`, and at
boot we end up with no gadget.

Fix: add an `ExecStartPre` to `nbd-attach@.service` that polls
for `/run/teslausb/teslafat-%i.sock` (30 attempts at 0.5 s each,
total ~15 s) before calling `nbd-client`. This eliminates the
previously manual unbind/rebind UDC workaround on cold boot.


---

## Phase 6 (live bring-up, cont'd): every subsystem must self-report to System Health

### The bug

After Tesla started writing RecentClips successfully, `teslausb-worker`
silently emitted hundreds of:

```
SEI walk failed; clip not indexed ...
error: "sqlite error: attempt to write a readonly database"
```

The DB file (`/var/lib/teslausb/index.sqlite3`) and its directory
were both group-rw and the worker had write capability — yet SQLite
returned `SQLITE_READONLY`. Root cause: the DB file was created by
`pi` (an ad-hoc `sqlite3 …` invocation during earlier debugging) and
was never re-chowned to the worker's `User=teslausb`. SQLite's WAL
implementation requires the journal/shm files to be **owned** by
the connecting user — group-write is not sufficient.

### The deeper lesson

This bug ran undetected for hours because **nothing in the web UI
surfaced it**. The Settings → System Health card showed everything
green: gadget bound, daemon happy, disk fine, samba disabled. The
indexer was silently producing zero clip records the whole time.

**Rule (binding):** every B-1 background subsystem MUST have a
probe wired into `/api/system/health` that returns ERROR (not OK,
not UNKNOWN) when that subsystem cannot do its job. The card is
the operator's *only* feedback loop short of `ssh + journalctl`.

Probes added in this pass:

| Subsystem | What the probe catches |
|---|---|
| `gadget` | UDC unbound, LUN backing file missing — "Tesla can't see drives" |
| `indexer` | DB readonly / corrupt / missing / stale (>30 min since last clip) |
| `worker` | `teslausb-worker.service` not active |
| `network` | `nmcli STATE general` ≠ `connected` |
| `storage_writable` | touch-test of every write root (catches RO remount) |
| `journal` | tails `journalctl -p err` for our units (10 min lookback, cached 15 s) |

### The persistent fix

`setup-lib/03-data-roots.sh` now sweeps `${B1_STATE_DIR}/index.sqlite3*`
on every run and chowns to `teslausb:teslausb` mode 0664 if it
doesn't already match. Idempotent. The next operator who pokes the
DB with a one-off `sqlite3` won't reintroduce this bug because
the next `setup.sh` run heals it.

### When adding a new background service in the future

Mandatory checklist before merging:

1. New probe block in `web/teslausb_web/blueprints/system_health.py`.
2. Probe returns `SEV_ERROR` on the actual failure mode (not just
   "process gone" — also "process running but doing nothing useful",
   like our stale-DB check).
3. Test in `web/tests/test_system_health.py` covering both healthy
   and degraded paths.
4. If the new service writes files, add it to `setup-lib/03-data-roots.sh`
   ownership sweep.


## Phase 6 — web UI gotchas (live-debug session, 2026-05-22)

These were paid for with real "it just doesn't work" bug reports
from the operator. None were caught by the test suite because
they're either browser-runtime behaviors (drag-and-drop, label
semantics) or false-positive UI signals masquerading as real
problems.

### Drop zones must NOT be `<label for=hidden-input>`

The boombox upload zone was a `<label for="boomboxFileInput">`
wrapping a `display: none` `<input type="file">`. Click-to-pick
worked (that's the label's native behavior). Drag-and-drop
silently failed: dragover/drop events fired on the label, but
the browser ALSO tries to forward the dropped FileList to the
associated input element, and an input with no layout
(`display: none`) silently swallows it. The result: drop
handler ran but `e.dataTransfer.files` behavior was
inconsistent and the upload never started.

**Pattern (proven working in license_plates.html, light_shows.html,
wraps.html):**

- Use a `<div role="button" tabindex="0">` for the drop zone,
  NOT a `<label>`.
- Wire an explicit `dropZone.addEventListener('click', () => fileInput.click())`.
- Add a keyboard handler for Enter/Space → `fileInput.click()`
  to preserve keyboard accessibility (since we lost the label).
- Keep the `<input type="file" style="display: none">` separate
  (sibling, not child) — or as a child of the div is fine; the
  point is it must not be the label's target.

**Detection:** if click-to-pick works but drag-drop "does
nothing", and the zone is a `<label>` containing the file
input — that's the bug. There's no JS error in the console.

### Update (2026-05-22): the label→div fix was necessary but NOT SUFFICIENT

After the label→div migration, drag-drop *still* failed in real
browsers — drag-over showed the "copy" cursor but dropping
opened the audio file in a new browser tab. Two compounding bugs
were uncovered, both worth their own learning:

#### (a) Missing Jinja template variables silently abort the entire IIFE

The script started with:

```js
const maxFileSize = {{ max_file_size }};
```

The blueprint context only set `max_file_size_str`, not
`max_file_size`. Jinja rendered the variable as the empty
string, producing the literal JavaScript:

```js
const maxFileSize = ;
```

That is a **parse-time syntax error**. The browser discarded
the ENTIRE IIFE — so no click handler, no drag handler, NOTHING
got attached. The default browser behavior (open the dropped
file as a URL) was all that ran. No console warning, no
network error, no visible symptom on initial page load.

**Rule:** any Jinja value interpolated directly into a
JavaScript literal MUST have a known default in the context
dict, AND a test should assert the rendered page contains the
fully-formed literal (e.g. `const maxFileSize = 1048576`), not
just the variable name. A unit test that does
`assert "maxFileSize" in html` would have passed the bug
through; what we need is
`assert re.search(r"const maxFileSize = \\d+", html)`.

#### (b) `fileInput.files = e.dataTransfer.files` is unreliable

The boombox tried to inject the dropped FileList into the form
input and then call `form.submit()`, to share the upload path
with click-to-pick. That assignment is only allowed in modern
Chrome/Firefox/Safari under specific gesture conditions, and
even there it can silently no-op without throwing. All the
other working drop zones in this codebase (`light_shows.html`,
`license_plates.html`, `wraps.html`) use the same pattern
instead:

```javascript
const files = Array.from(e.dataTransfer.files);
const fd = new FormData();
files.forEach(f => fd.append('field_name', f, f.name));
const xhr = new XMLHttpRequest();
xhr.open('POST', form.action, true);
xhr.onload = () => {
    if (xhr.status < 400) window.location.reload();
    else alert('Upload failed (' + xhr.status + ')');
};
xhr.send(fd);
```

This is also what handles upload progress, error display, and
partial-failure reporting in the bigger pages. **Use it for
every new drop zone — DO NOT try to bridge through
`fileInput.files`.**

#### Triage checklist for "drag-and-drop doesn't work"

In this order:

1. View-source the page and grep for empty `const X = ;`
   patterns left over from Jinja. Fix the context dict.
2. Open DevTools Console; reload; look for any syntax error.
   If the IIFE failed to parse, all handlers are missing —
   that's a "stops at the first broken line" problem, not a
   drag/drop problem.
3. Confirm the drop zone is a `<div role="button">`, not a
   `<label>` (see prior section).
4. Confirm the drop handler uses FormData + XHR, not
   `fileInput.files = ...`.

### Diagnose "drag shows copy cursor but drop opens the file in a new tab"

This specific symptom = the page's drop handler is NOT running.
The browser is taking over because no listener called
`preventDefault()` on the drop event in time. Almost always
caused by (a) above — IIFE never attached its handlers because
a JS syntax error aborted parsing. NOT typically caused by
"need to preventDefault on document/window" (we don't have to
do that on the other working pages, so missing handlers is
the more likely cause).

### System Health probes must report OUR subsystem's health, not
### external activity

The indexer probe used to warn `{N} clips indexed; newest is
{M} min old` when the newest clip was >30 min old. This fired
constantly because **clip recency reflects Tesla activity, not
indexer health**. When the car is parked on Sentry with no
motion events, Tesla writes nothing and the newest clip
naturally ages — the indexer is doing exactly what it should
(nothing). The amber dot on System Health trained the operator
to ignore it ("crying wolf"), which would mask real problems.

**Rule:** A System Health probe must signal a state that the
B-1 stack itself can act on. If the only fix for a probe's
warning is "the operator drives the car", the probe is
mis-scoped.

**Specifics that violate this rule (avoid in future probes):**

- Time-since-last-clip (depends on Tesla activity).
- Time-since-last-archive-upload (depends on cloud sync activity
  AND on having clips to archive).
- Free-space-as-percent-of-rated (depends on user retention
  settings).

**Specifics that are good (keep this pattern):**

- DB read/write probe (PRAGMA quick_check + create-temp-table).
- `systemctl is-active` for each owned unit.
- Mount-exists / socket-exists / configfs-node-exists.
- Orphan rows in index vs files on disk (a delta the indexer
  controls).

### configfs gadget name is `g1`, NOT `teslausb`

When triaging "is Tesla seeing the drives", the canonical path
is:

` 
/sys/kernel/config/usb_gadget/g1/UDC
`

`g1` is the default name libcomposite assigns when you create
a gadget via configfs and is what `teslausb-gadget-up` actually
uses. The systemd unit and the documentation say "teslausb" in
many places because that's the unit name, but the configfs
directory is `g1`. Looking under `/sys/kernel/config/usb_gadget/teslausb/`
returns ENOENT and looks like a catastrophic failure when
nothing is wrong.

**Concrete check:**

` bash
test -s /sys/kernel/config/usb_gadget/g1/UDC || echo "GADGET UNBOUND"
`

(An empty file means the gadget exists but isn't bound to a UDC.
A missing file means the configfs node is gone entirely —
usb-gadget.service has been stopped or torn down.)

### Tesla write behavior — Sentry vs SentryClips vs RecentClips

**Correction to an earlier draft of this section (operator
feedback, 2026-05-22):** Sentry mode records **continuously**
while it is on. It writes rolling 1-minute `.mp4` segments
into `RecentClips/` the whole time the car is armed. What
Sentry adds on top of that is **event detection**: when the
car detects something (motion, impact, etc.), it promotes the
relevant minute into `SentryClips/` (or `SavedClips/` for
honk-saved / manually-saved events).

Correct mental model:

- `RecentClips/` — rolling buffer, written **continuously**
  whenever Sentry (or Dashcam while driving) is active. New
  files every minute per camera. If this directory's filename
  dates go stale, **something is wrong** — gadget unbound,
  filesystem read-only, Sentry actually off, etc.
- `SentryClips/` — created only when an event triggers.
  Bursty by nature. Zero new files in this directory for hours
  is normal and healthy in a quiet location.
- `SavedClips/` — driver-initiated (honk-save). Even burstier;
  may go days between entries.

**Implications for diagnostics:**

- Stale `RecentClips/` (>2-3 min since newest filename-date)
  IS a real problem worth investigating.
- Stale `SentryClips/` or `SavedClips/` is NOT diagnostic on
  its own — it just means nothing has happened.
- An indexer "newest clip" age across ALL buckets blends these
  signals and is therefore non-actionable; that's why the
  system_health indexer probe was demoted to informational.
- If we ever add a real "Tesla is writing" probe, scope it to
  `RecentClips/` filename dates specifically (e.g., WARN if
  newest RecentClips filename is >5 min behind wall clock).

**Filename invariant (useful for any of these probes):**
`YYYY-MM-DD_HH-MM-SS-{camera}.mp4` — Tesla bakes the clip's
start time into the filename when it creates the file. mtime
can drift due to FAT allocation-table flushes, so always parse
the filename (not stat) when judging recency.

### nginx ↔ gunicorn timeout invariant

This was found chasing the lightshow "Bad Gateway after 100%
upload" bug. Documented in passing in commit `8685a08` but
worth restating:

`gunicorn.timeout` MUST be `>= max(nginx.proxy_read_timeout,
nginx.client_body_timeout, nginx.proxy_send_timeout)`. Otherwise
gunicorn SIGKILLs the worker mid-write, nginx sees a partial
response, and the user sees `502 Bad Gateway` after their
upload progress bar already hit 100 percent.

Both sides are now 300 s. When you bump one, bump the other in
the same commit, and add a comment on both sides cross-
referencing each other.


### Most "short-uptime boots" in journalctl are OUR dead-man timer, not a real crash

While diagnosing apparent ping drops on cybertruckusb.local, the
boot history showed a worrying pattern:

` 
journalctl --list-boots
... -6  Thu 2026-05-21 12:07:45 EDT  Thu 2026-05-21 12:18:20 EDT   (10 min!)
... -4  Thu 2026-05-21 16:20:42 EDT  Thu 2026-05-21 16:33:07 EDT   (12 min)
... -1  Fri 2026-05-22 08:05:04 EDT  Fri 2026-05-22 08:21:17 EDT   (16 min)
` 

These were NOT crashes or watchdog bites. They were the
hardware-test skill's dead-man self-reboot timer firing because
the deploy wrapper forgot to cancel it (SSH lag between deploy
and cancel, or the operator interrupting mid-script).

**To distinguish dead-man from a real crash:**

` bash
sudo journalctl -b -1 --no-pager | tail -40
` 

- Clean shutdown via dead-man = you see
  `systemd-reboot.service: Deactivated successfully` and
  `Reached target reboot.target - System Reboot`. Filesystems
  unmount cleanly. No oops/panic/watchdog trace.
- Real crash = abrupt journal cutoff, no shutdown sequence, may
  see kernel oops or `Watchdog has expired` before the gap.

**Deploy wrapper hygiene (binding rule going forward):**

1. Use a 	rap (bash) to cancel the dead-man on script exit, so
   even an aborted/interrupted deploy still cancels:
   `trap 'ssh ... "sudo systemctl stop b1-deadman.timer 2>/dev/null; sudo systemctl reset-failed b1-deadman.timer 2>/dev/null" || true' EXIT`.
2. Don't arm the dead-man at all for changes that can't break
   SSH/WiFi/boot. Editing web templates and Python blueprints
   under `/opt/teslausb/web/` and restarting `teslausb-web`
   is safe — no dead-man needed. Reserve dead-man for changes to
   `/etc/ssh/`, `/etc/NetworkManager/`, `/etc/wpa_supplicant/`,
   `/boot/firmware/cmdline.txt`, `/boot/firmware/config.txt`,
   `/etc/fstab`, kernel modules, or anything that calls
   `reboot`/`poweroff`.
3. If you DO arm it, the verification curl call must finish in
   < 30 s (so 90-120 s dead-man is fine; 180 s is overkill and
   amplifies the cost of forgetting to cancel).

**Ping drops vs reboots:** sustained ping drops without an
accompanying reboot in `journalctl --list-boots` are not
crashes. On a Pi Zero 2 W under heavy iowait (which is normal
when teslafat is serving the USB gadget) WiFi packet processing
can briefly stall. The device is fine; it's just the platform
performance ceiling.


## Phase Q — disk-backed pending spill (2026-05-24)

### `ProtectSystem=strict` + `ReadWritePaths` silently degrades pending-spill

The `teslafat@.service` unit hardens with `ProtectSystem=strict`,
which makes `/usr`, `/boot`, `/etc`, AND `/var` (the whole rest
of `/`) read-only inside the service namespace unless explicitly
listed under `ReadWritePaths=`. When we first deployed the
disk-backed `PendingSpill`, `prepare_spill_dir()` succeeded
(directory was pre-created with the right owner) but every
subsequent `append_chunk_to_disk` call hit
`Read-only file system (os error 30)`. The daemon caught the
error per its best-effort policy, logged a `WARN`, dropped the
chunk, and silently continued in legacy 16 MiB memory-cap mode —
behaviourally identical to Phase P, including the 1428
evictions/minute.

Lesson: any per-instance writable state outside `/run` and
`/srv/teslausb` (the only two paths in the original
`ReadWritePaths=`) requires either `StateDirectory=<name>` (which
creates `/var/lib/<name>` and adds it to `ReadWritePaths` for you)
or an explicit `ReadWritePaths=/var/lib/...` line. Confirmed by
running `sudo -u teslausb touch /var/lib/teslafat/spill/0/test`:
host-shell write succeeded (proving filesystem perms were fine),
but in-namespace writes from the service failed.

Fix shipped: `StateDirectory=teslafat` plus
`ReadWritePaths=/run/teslausb /srv/teslausb /var/lib/teslafat`
in `teslafat@.service`, plus a drop-in
`/etc/systemd/system/teslafat@.service.d/10-spill.conf` written
during deploy for any device that pre-dates the unit-file change.

### `setup-lib`-emitted TOML lacks newlines between sections

`B1_TESLAFAT_CONF_0_TEMPLATE` in `setup-lib/11-gadget.sh` happens
to inline `[retention]` immediately after `fs_type = "exfat"` with
no intervening newline:

```
fs_type = "exfat"[retention]
```

The Rust `toml` crate parses this correctly (it doesn't require
inter-section newlines), so it has worked silently for the whole
project. But any `sed -i "s|fs_type = \"exfat\"|fs_type = \"exfat\"\nspill_dir = ...|"`
inserts the new key BEFORE the `[retention]` token on the same
line — producing `spill_dir = "..."[retention]`, which is invalid
TOML. The daemon fails to deserialize and falls back to defaults
(no `spill_dir` = legacy memory mode).

Lesson: when editing these generated configs in place, always
preserve the trailing token by inserting a newline before the next
section header. Better: regenerate the file from the template
rather than patching it. Better still: fix the template to include
the newline (done in Phase Q's `11-gadget.sh` change, where the
new lines push `[retention]` onto its own line for both LUN
templates).

### Tesla's write pattern: data-clusters-first, dir-entry-last, ~7 GiB worst case

Sustained telemetry from cybertruckusb.local confirmed Tesla's
sentry recording pattern that motivated Phase Q:

* Cluster size on the 256 GiB exFAT TeslaCam LUN is **128 KiB**.
* A single 1.7 GB sentry clip = ~13 K data-cluster writes.
* All four cameras' data clusters arrive **before** any
  directory entry for any of those files — so the worst-case
  in-flight pre-dir-entry buffer is roughly
  `4 cameras × 1.7 GB ≈ 6.8 GiB`.
* Steady-state burst rate was **1400 unresolved clusters /
  minute = ~180 MB/min** of bytes-in-flight.

This makes any in-memory cap that fits on a 464 MiB Pi
fundamentally too small. The 4 GiB disk cap (`DEFAULT_DISK_SPILL_BYTES`)
is currently 2× headroom over the observed worst case; if a future
firmware revision pushes the working set above that we will see
non-zero evictions in the `evicted_clusters_total` counter and can
raise to 8 GiB without a code change (just an ADR).

### Two installation roots on the device — `/opt` is prod, `/home/pi` is dev

Both `/opt/teslausb/web/teslausb_web/services/mapping_queries.py`
AND `/home/pi/teslausb-b1/web/teslausb_web/services/mapping_queries.py`
exist on the live device. The `teslausb-web.service` unit
`ExecStart=/opt/teslausb/web/.venv/bin/gunicorn ... teslausb_web.wsgi:app`
imports from `/opt/teslausb/web/.venv/lib/.../teslausb_web/` —
the `/home/pi/teslausb-b1/web/` tree is the cloned source for
operator ad-hoc work and is **not** what gunicorn loads.

When deploying Python changes by `scp` + `install`, always target
`/opt/teslausb/web/...`. A successful `systemctl restart teslausb-web`
followed by behaviour that doesn't change is the canonical symptom
that you deployed into `/home/pi/teslausb-b1` instead. Verify with
`cat /proc/<gunicorn-pid>/cmdline` and follow the path.

There is no documented contract for keeping the two trees in sync;
treat `/opt/teslausb/web/` as the only authoritative deployment
location for Python and templates.

### Mapping `video_path` had a silent double-prefix

The worker stores `clips.relative_path` rooted at `backing_root`
(so every value begins with `TeslaCam/`). The videos blueprint
allow-lists `backing_root/TeslaCam` as its one root and joins
the URL `<path:filepath>` underneath. Sending the raw DB value
to the front-end made every `/videos/stream/TeslaCam/RecentClips/...`
URL resolve to `<backing_root>/TeslaCam/TeslaCam/...`, which
doesn't exist — every map-click produced a "Video file not found
— Tesla may have overwritten it" toast even though the file was
sitting on disk.

The bug shipped because:

* `_video_path_exists()` (the existence probe used by
  `_trip_is_playable`) joins against `media_root = backing_root`,
  so it correctly returns `True` for the raw `TeslaCam/...` path.
* The blueprint joins against `backing_root/TeslaCam`, so it
  needs the path *without* `TeslaCam/`.

No single test exercised the end-to-end flow, so the two
mismatched conventions never collided in CI.

Fix shipped: `mapping_queries.py::_video_url_path()` strips one
leading `TeslaCam/` at every emission point that goes to the
front-end (`_derived_event_from_row`,
`_route_waypoint_from_entry`, `_event_row_from_derived`,
`waypoints_for_video`). Raw `relative_path` is preserved for
all internal users.

Lesson going forward: when the same value is consumed by two
modules with different rooting conventions, make the boundary
explicit (a `to_url_path()` helper) and write at least one
integration test that walks the actual `clip → API JSON → blueprint
resolve → HTTP 206` round-trip. Charter pillar 1 ("primitive
obsession") would have flagged `str` for both forms; a `ClipDbPath`
+ `ClipUrlPath` newtype pair would make the conversion required
rather than implicit.

## Phase 5 — Mapping overlay SEI must be SINGLE-fetch (2026-05-26)

### The bug

The video overlay HUD on `/mapping/` displayed gear=PARK / speed=13 mph
while the video itself clearly showed the car driving forward toward a
Supercharger stall. Operator-reported, reproducible on the May 23 Bay
City clip.

### The cause

B-1's first port of v1's client-side SEI HUD shipped a "stream-then-swap"
double-load:

1. `openVideoOverlay` set `video.src = streamUrl` for instant
   playback.
2. In parallel, `loadLiveSeiForClip` called
   `fetch(streamUrl).arrayBuffer()` and parsed SEI from the bytes.
3. When SEI parse finished, the video src was swapped to a
   `URL.createObjectURL(blob)` of those same bytes.

On Pi Zero 2 W WiFi (brcmfmac), the SEI fetch takes 60-100 s for a 30 MB
clip. During that window:

* The video was already playing — driven by the streaming URL.
* The HUD fell back to the clicked DB waypoint (`onOverlayTimeUpdate`
  with empty `SEI_FRAMES`).
* The clicked waypoint was the "parked at destination" endpoint
  (`gear=P`), because that's the geographic point the user clicked.
* HUD showed PARK / 13 mph for a full minute while the video played the
  start of the clip where the car was actively driving.

Worse: the Pi served the same 30 MB file *twice* (once for the
streaming `<video>`, once for the SEI fetch), doubling RAM + WiFi
pressure for no gain.

### The fix (v1 parity)

V1's `scripts/web/templates/event_player.html` (commit `b5aeeee~1`,
`loadVideoWithCache` / `loadSEIData`) is explicit about the right
model — the in-code comment at v1 L1237 reads:

> *"In overlay mode: DON'T set video.src here - loadSEIData will fetch
> the video, parse SEI metadata, and set video.src to a blob URL. This
> avoids double download."*

So in `web/teslausb_web/templates/mapping.html`:

* `openVideoOverlay` no longer sets `video.src`. It only shows the
  loading spinner and calls `loadLiveSeiForClip`.
* `loadLiveSeiForClip` does ONE fetch via `response.body.getReader()`
  (streaming, so we can show a live `X / Y MB (Z %)` label), parses
  SEI, builds a `Blob`, sets `video.src = blobUrl` then
  `video.load()`.
* The HUD is no longer seeded with stale DB-waypoint numeric fields —
  only coordinates are seeded. Numeric fields stay blank until per-frame
  SEI drives them, so the user never sees a contradiction between video
  and HUD.
* `navigateClip` follows the same model for front clips; non-front
  cameras (no Tesla SEI) keep the streaming src since there is no HUD
  to gate on.
* Fetch/parse failures now `showToast` directly — the `<video>`
  element no longer sees them because its src is empty during loading.

### Rules for any future SEI overlay work

1. NEVER set `<video>.src` to a streaming endpoint before SEI is
   loaded. Loading indicator on the `<video>` element is fine; an
   empty src is fine.
2. Use `response.body.getReader()` to stream the MP4 and update a
   visible progress label every ~200 ms. `arrayBuffer()` blocks
   silently for 60-100 s on Pi WiFi and the spinner looks frozen.
3. Do NOT seed the HUD numeric fields from the clicked DB waypoint
   before SEI loads. Seed coordinates only. The clicked waypoint is
   chosen by geography, not by video timestamp, and is routinely off
   by tens of seconds from what the video shows.
4. Surface fetch/parse errors via `showToast` — the `<video>`
   element never sees them.
5. Non-front cameras have no Tesla SEI. Fall back to the streaming
   URL for those (there's no HUD to gate on).

### Triage checklist for "overlay HUD doesn't match video"

Before blaming SEI parsing, the protobuf shim, or the Tesla data:

1. Open DevTools → Network. Is the MP4 being fetched **twice**? If
   yes, the "single-fetch" invariant is broken. Find the second
   `video.src = streamUrl` and remove it.
2. Is `SEI_FRAMES.length > 0` in the console? If 0, SEI hasn't
   loaded yet — and the HUD should be BLANK, not showing stale
   DB-waypoint data.
3. Is `video.src` a `blob:` URL or an `http:` URL? It must be
   `blob:` for v1-parity SEI playback.
