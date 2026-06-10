# SPEC — USB I/O architecture & guaranteed clip archiving

> Parent: [`SPEC.md`](./SPEC.md) · Owner services: [`gadgetd`](./gadgetd.md),
> [`retentiond`](./retentiond.md)
> Related: [`tesla-usb-contract.md`](./tesla-usb-contract.md) (device/partition
> model, media paths), [`storage.md`](./storage.md) (space governor),
> [`scannerd.md`](./scannerd.md) (raw reader), [`indexd.md`](./indexd.md) (catalog),
> [`uploadd.md`](./uploadd.md) (cloud archive).

This document is the authoritative design for **USB drive performance / I/O
contention** and **guaranteed RecentClips archiving**. It is the reconciled output
of two independent analyses (this session's own reading of the implemented stack +
an independent GPT-5.5 second opinion) per the repo problem-solving directive.

**Implementation is Rust-only** (see `.github/copilot-instructions.md`). The legacy
v1 Flask app is reference-only for behavior/look; no Python is used.

---

## 0. The supreme invariant (unchanged)

**The car must always be able to write TeslaCam** ([`SPEC.md` §2](./SPEC.md)). Every
decision below serves it. A design that reduces media-write latency but risks a
dashcam-recording gap is wrong.

---

## 1. Ground truth (as-implemented today)

- **One backing image, one LUN.** `gadgetd` exposes a single
  `/data/teslausb/disk.img` as `lun.0`: MBR + **p1 `TESLACAM` exFAT** + **p2
  `MEDIA` exFAT**, fully `fallocate`d. (`gadgetd/src/{provision,handoff}.rs`.)
- **Reads never eject.** `scannerd` reads the backing image **raw** (`pread`,
  `MBR → exFAT → FAT → MP4 → H.264 SEI`) and **never mounts** the Tesla filesystem.
  Consistency under concurrent car writes is handled by **stability-gating**, not by
  a kernel mount. This is the load-bearing fact for archiving (§3).
- **Only writes eject.** Media install/remove and car-clip delete use the
  **eject-handoff**: soft-eject `lun.0` (clear `lun.0/file`) → loop-mount the target
  partition RW on the Pi → apply validated mutation → `fsync` → unmount → re-present.
  Window target ~5 s; the car buffers seconds of recording in RAM (tolerance is
  prototype-unknown #2 — measured, not assumed).
- **Consequence (the contention problem):** because the whole image is one LUN, a
  **media (p2) write soft-ejects the entire drive — including TeslaCam (p1)** — for
  the handoff window. Media writes are currently operator-gated and infrequent, so
  this is tolerable today, but it does not scale to frequent website-driven uploads.
- **retentiond is a STUB.** The verified-archive *library core* (copy + re-hash,
  per-folder policy, empirical-window measurement) exists and is unit-tested, but the
  **live `serve` loop is not running** (calibration-gated). **Therefore RecentClips
  are NOT being archived today** — the central gap this spec closes.

---

## 2. Disk-image topology: 1 image vs 2 images/LUNs

### 2.1 The decision

| | 1 image / 1 LUN (today) | 2 images / 2 LUNs (target) |
|---|---|---|
| Media write interrupts TeslaCam | **Yes** (~5 s, whole drive) | **No** (cycle `lun.1` only) |
| Car reads media from partition | **Proven** (same device, p2) | **UNPROVEN** (does Tesla scan `lun.1`?) |
| Failure isolation (fsck/corruption) | Coupled | Separated |
| I/O fragmentation domain | Shared | Separated |
| Hardware risk | Low (proven) | Gated on a spike |

Both analyses converge on the **gating fact**: per
[`tesla-usb-contract.md` §2](./tesla-usb-contract.md) (unknown #1) the car finds
media features by scanning the *same device's other partition*. A 2-LUN gadget
presents **two** drives, and it is **unproven that Tesla scans `lun.1`** for lock
chime / boombox / lightshow / wraps / plates. Splitting blindly could silently break
**all** media features.

### 2.2 Reconciled recommendation

- **North star: two images / two LUNs.** `lun.0 = teslacam.img` (sacred, continuously
  presented, never RW-mounted on the Pi while exposed) and `lun.1 = media.img`
  (independently ejectable for Pi RW updates). This gives zero-interruption media
  cycling, clean failure isolation, and an I/O-priority boundary
  (dashcam-archiving > media-write > indexing). This is GPT-5.5's primary
  recommendation and is adopted as the **target**.
- **Interim until proven: stay on one image** (hardware-proven; car definitely reads
  media from p2). Critically, **archiving is read-only and already
  interruption-free regardless of 1-vs-2** (§3), so the 1-image interim does **not**
  block the highest-value work. Cap TeslaCam interruption by **batching** media
  writes (apply a whole queue in one eject) and **quiet-window gating** (only eject
  when no active save/recording).
- **Migration is GATED on a hardware spike** (§5): prove that, with a 2-LUN gadget,
  (a) Tesla reads media features from `lun.1`, (b) TeslaCam on `lun.0` keeps
  recording while `lun.1` is cycled, and (c) which media features (if any) still
  require a full USB re-enumeration. Do not migrate until (a)+(b) pass. Per the repo
  directive, send the migration plan back to GPT-5.5 + get operator approval before
  any device disk mutation.

---

## 3. Guaranteed RecentClips archiving (the protective copy)

### 3.1 Principle: protection is a read-only copy, so it never interrupts the car

Tesla's RecentClips is a flat rotating ring with a vehicle-set **≈1–24 h** window
that is not exposed to the drive. To never lose SEI/GPS trip/event footage, the Pi
must copy each meaningful RecentClip to a **Pi-only protected archive on ext4 (a
separate filesystem)** *before* Tesla rotates it. Because:

1. the copy reads via the **raw reader** (no mount, no eject — §1), and
2. the destination `archive/` is a **different filesystem** that Tesla cannot touch,

the protective copy runs **continuously without ejecting the car drive** and is
immune to p1 rotation. **This is the key win and it is available in the 1-image
interim.** (B-1 already avoids the "RO-mount of a live exFAT" cache-incoherence
hazard a naive design would hit, because scannerd never mounts.)

### 3.2 The archive loop (retentiond `serve`, to be wired)

1. **Source of truth = the indexd catalog.** scannerd→indexd already derive clips
   (incl. RecentClips), SEI/GPS, and trip/event membership.
2. **Cadence ≪ the window.** Poll on a short cadence (target every few seconds to a
   few minutes; calibrate against the **empirically-measured** window — never assume
   1 h). Archive lag must stay well under the measured rotation interval.
3. **Completeness gate (both analyses agree):** only copy a RecentClip when
   **size + mtime are stable across consecutive polls**; copy; **re-stat / re-hash
   the source after copy**; dedup by **content hash + size + time + camera**, not
   filename alone. Optionally validate MP4 structure.
4. **Priority order:** `SavedClips` → `SentryClips` → older `RecentClips` →
   newest/still-active RecentClips last. Archiving outranks indexing/thumbnails/maps/
   media-writes for I/O.
5. **Destination:** `archive/{SavedClips,SentryClips,RecentClips,TeslaTrackMode}` on
   Pi ext4 (per [`storage.md` §2](./storage.md)); mark archived in the catalog;
   verified (copied-and-rehashed) before the row is trusted.
6. **Selectivity:** prioritize RecentClips carrying SEI/GPS and belonging to a real
   trip/event (the footage the operator cares about); the governor
   ([`storage.md`](./storage.md)) bounds total archive footprint and evicts by value,
   never deleting undurable footage.

### 3.3 What is NOT required for protection (and why)

- **Deleting archived RecentClips from p1** to reclaim space DOES need an
  eject-handoff — but it is **optional**: Tesla rotates p1 itself. So reclaim is a
  separate, gated nicety; the protective copy never depends on it. This keeps
  protection 100% read-only.

### 3.4 Honest guarantee + failure modes (no overclaiming)

A live-filesystem raw read is **not an absolute** guarantee. Tracked, surfaced
failure modes: Pi down longer than the window; Tesla rotates faster than the Pi
copies; storage stall; an active clip mistaken as complete; archive disk full. The
health UI MUST surface **archive lag, last-copied clip, and recording continuity**,
and the governor MUST **reserve archive space and block media uploads when archive
lag or free space is unsafe**. A truly absolute guarantee would need block-level
snapshot/mirror or controlled offline windows (major complexity) — out of scope
until the above is proven insufficient.

---

## 4. RO/RW switching discipline for media writes

Normal media write (install/remove a chime/boombox/lightshow/music/wrap/plate):

1. Upload into **Pi-only staging** (never directly to the LUN).
2. **Batch/queue** changes; apply many in one handoff.
3. **Preconditions:** archiver caught up; free space healthy; no media-LUN op in
   flight; preferably car parked / not actively recording (gate on the gadgetd write
   heartbeat).
4. Eject the **media LUN only** (`lun.1` in the target; `lun.0` in the interim).
5. Mount media image RW on the Pi, apply queued changes, `sync`, unmount cleanly
   (optionally `fsck`).
6. Re-present the media LUN; verify TeslaCam stayed present and new clips keep
   arriving.
7. **Avoid routine full UDC unbind/rebind** — it drops the entire USB device and can
   truncate the active dashcam clip. If a specific media feature only reloads after a
   full re-enumeration, treat **that** feature's apply as a **deferred maintenance
   action** (parked, archiver caught up, operator-gated), not a routine upload.

---

## 5. Hardware spikes required before committing (PROVE FIRST)

1. **Multi-LUN media read** — present `lun.0=teslacam.img` + `lun.1=media.img`;
   verify Tesla reads lock chime / boombox / lightshow / wraps / plates from `lun.1`.
   GO/NO-GO for the 2-image migration.
2. **Independent media cycling** — with the car recording to `lun.0`, eject/mount/
   re-present `lun.1` and confirm TeslaCam recording is uninterrupted and no clip is
   truncated.
3. **Re-enumeration matrix** — determine which media features (if any) require a full
   USB re-enumeration to be noticed, so they can be classed as maintenance-only.
4. **Window measurement** — measure the effective RecentClips rotation window on the
   actual vehicle to calibrate the archive cadence (§3.2).

Each spike runs under the `hardware-test` skill safety rails, with a GPT-5.5 pre-run
review and explicit operator confirmation; none may run autonomously (they touch the
#1-invariant write path / disk layout).

---

## 6. One-image → two-image migration runbook (GATED)

> **Software status (host-verified, NOT deployed).** Per the operator directive
> "go for 2 images", `gadgetd` has been refactored from one `disk.img` (MBR p1+p2,
> one LUN) to **two single-partition images, two LUNs**: `teslacam.img` → `lun.0`
> (sacred, seeded `TeslaCam/`) and `media.img` → `lun.1` (MEDIA). Build gates are
> green via podman (fmt, clippy `-D warnings`, `cargo test --workspace`, installer
> denylist/test matrix). **The live device still runs the single `disk.img`.** The
> data migration below is **operator + hardware-spike gated** and MUST NOT run
> autonomously — it rewrites the car-facing disk layout (#1 invariant).

### 6.1 Preconditions (all must hold before any byte is written)

- **Spikes §5 #1 and #2 PASS** on the actual vehicle: Tesla reads media features
  from `lun.1`, and TeslaCam on `lun.0` keeps recording while `lun.1` is cycled.
  Without both, do not migrate — a blind split silently breaks media and/or risks
  the dashcam.
- **Size from SOURCE, not from the defaults.** The provisioning defaults
  (`--teslacam-mib 3072`, `--media-mib 1024`) are NOT authoritative for migration:
  compute each new image from the **used bytes** of the corresponding old partition
  + exFAT metadata overhead + a growth/safety margin, and **fail closed** if source
  content cannot fit (a too-small `teslacam.img` would silently truncate clips —
  finding #7).
- **Free-space preflight with live headroom.** All three images
  (`disk.img` + `teslacam.img.new` + `media.img.new`) must be **fully preallocated,
  never sparse**, AND `/data` must retain explicit reserved headroom for the
  **still-live** `disk.img`'s ongoing car writes, the indexd/SQLite DBs, the ext4
  protected archive, and logs. Provisioning that consumes blocks the live image
  needs would itself cause car-write EIO (finding #6) — refuse unless headroom is
  proven.
- **Archiver caught up AND frozen for the copy.** retentiond archive lag = 0 and
  free space healthy; additionally, retentiond/scannerd/indexd are put into a
  **read-only maintenance freeze** for the snapshot + final delta so the source is
  not mutated mid-copy (lag 0 is not a write freeze — finding #11).
- **Operator present, vehicle parked, quiet window.** UDC state ∈ {not attached,
  suspended} or car parked with no active save; GPT-5.5 pre-run review done.
- **Off-device backup of the SACRED data, not just media.** Back up the full p1
  (`TeslaCam/` clips — `Saved/`, `Sentry/`, `Track/`, `RecentClips/`) **and the
  indexd SQLite catalog/DB state**, in addition to p2 (MEDIA) content + the
  installed-media catalog. p1 clips + the index are the irreplaceable data
  (finding #8); media is re-seedable.
- **Migration sentinel (not just a procedure).** A migration is permitted only when
  a sentinel file records the §5 spike evidence + an explicit operator
  acknowledgement; `gadgetd`/installer/systemd MUST refuse to perform or
  auto-trigger the migration without it (finding #18). The gate is enforced, not
  documentary.

### 6.2 Ordering invariant (never drops TeslaCam)

The migration is staged so that **at every step a power loss / Pi crash leaves a
bootable gadget exposing valid TeslaCam footage** and is reversible to the proven
single-`disk.img` state until the operator's final commit:

1. Build the two new images **out of band at `*.new` paths**
   (`teslacam.img.new`, `media.img.new`) — the live gadget keeps serving the old
   `disk.img` the whole time. Nothing at the final `teslacam.img`/`media.img` paths
   exists yet, so a crash cannot expose a half-built image (finding #3).
2. **Bulk copy from a quiesced/raw source** (see 6.3) into the `*.new` images.
3. **Final delta + verify under freeze:** with the car quiesced and services
   frozen, re-sync any clips the car wrote during the bulk copy, then verify against
   a source manifest (6.3).
4. **Cutover atomically:** `sync` + `fsync` files and parent dir, `fsck` the
   unmounted images, detach all loop devices, assert no open writers/mounts, then
   atomically rename `*.new` → final paths and flip a **single fsynced mode
   pointer**; `gadgetd down` old → `gadgetd up` two-image, TeslaCam LUN (`lun.0`)
   presented first.
5. **Verify on the vehicle** that TeslaCam records to `lun.0` and media is read
   from `lun.1` BEFORE the old `disk.img` is eligible for deletion.

The boot default flips to the two-image mode **only via the single atomic mode
pointer, and only after** step 5 verifies. Between any intermediate edits a crash
must still boot the proven old single image — so the known-good
`gadgetd up --image disk.img` unit/binary stays the default until the one atomic
commit (findings #3, #4, #9).

### 6.3 Content copy — raw-read or quiesced, NEVER mount the live image

The car-served `disk.img` MUST NOT be loop-mounted (even RO) while the gadget
exports it: a concurrent kernel mount can observe stale/inconsistent exFAT metadata
while the car writes through USB, and copying an actively mutated filesystem can
miss or half-copy clips — both violate the "a Pi crash looks like a clean unplug,
never corrupt/EIO" model (findings #1, #2). Two acceptable source strategies:

- **Preferred — reuse the raw reader.** Copy clips out of the old image with the
  same no-mount raw exFAT reader scannerd already uses (`MBR → exFAT → FAT → file`),
  which is the project's existing safe path for reading a live image, then do a
  short final delta after a clean eject.
- **Or — quiesce first.** Cleanly soft-eject the old LUN (car parked, no active
  save), confirm no car writer, and only then mount RO / raw-copy the now-static
  image.

Mechanics:

- Do **not** `dd` old-p1 bytes onto `teslacam.img` — geometry differs (old p1
  started at LBA 2048 in a 2-partition MBR; the new image is a single partition
  spanning `1MiB..100%`). Provision the `*.new` images first (MBR + one exFAT
  partition each, seed `TeslaCam/` on `teslacam.img.new`).
- Copy **all of p1's content**, not just `TeslaCam/` — any root-level files, volume
  metadata, or Tesla-created artifacts outside `TeslaCam/` must be preserved, or
  the root explicitly asserted to contain only known-safe entries (finding #13).
  Likewise old p2 → `media.img.new`. (The ext4 protected archive is Pi-side and
  untouched.)
- **Verify completeness with a manifest, not just fsck.** `fsck.exfat -n` proves
  structure, not that every clip copied. Build a source manifest after quiescence
  (file counts, sizes, mtimes, and hashes where practical) and compare the
  destination against it (finding #12). Assert exFAT volume sectors == MBR
  partition sectors before either image is exposed as a LUN.
- **Preserve Tesla-visible disk identity.** Carry over (or explicitly validate)
  labels, MBR partition type, the active/boot bit, exFAT cluster size, and volume
  serial, plus the gadget's removable flag and vendor/product/serial and LUN order
  — any of these can change Tesla's behavior (finding #14). Validate across cold
  boot / replug / sleep-wake, not just one cycle.
- **fsync scope is explicit:** fsync the copied files, the image files themselves,
  the indexd DB/config files, AND their parent directories before cutover
  (finding #17). Discover loop partitions via `losetup --show -P` and verify the
  partition device maps to the intended image rather than assuming `loopXp1`
  (finding #16).

### 6.4 Never destroy `disk.img`; rollback is phase-dependent

`disk.img` stays on `/data` as the rollback anchor through the entire vehicle
verification, deleted **only** after a human confirms on the car that clips keep
arriving on `lun.0` and media reads on `lun.1` across at least one
park→drive→park cycle. Rollback is **not** a single command at all times
(finding #5):

- **Before cutover (steps 1–3):** rollback is trivial — discard the `*.new` images;
  the old single-image gadget was never interrupted.
- **After cutover (steps 4–5), before final delete:** the car may have recorded NEW
  clips to `teslacam.img`. Rolling straight back to `disk.img` would orphan those
  clips. Rollback in this phase MUST first copy/reconcile the new `lun.0` clips back
  into `disk.img` (or keep both images and mark rollback as requiring a clip merge),
  THEN repoint the atomic mode pointer to the old single image and reboot.
- Rollback also assumes the **deployed `gadgetd` can still run single-image mode**:
  keep the known-good old binary + unit + config available, or prove the new
  binary's `--image disk.img` path on hardware before migrating (finding #15).

### 6.5 Downstream services migrate ATOMICALLY with the gadget (no mixed mode)

scannerd/indexd/retentiond/uploadd and the installer must never run half in
one-image and half in two-image mode — disagreement about image paths/layout can
produce duplicate, missing, or wrongly-deleted catalog rows (finding #10). Freeze
the dependent services, back up the DB, migrate their config in lockstep with the
gadget cutover, and restart them together.

- **scannerd reads two images.** Today scannerd opens one `disk.img` (both
  partitions). With two single-partition images it must open **both**
  `teslacam.img` (clips → trips/events) **and** `media.img` (media catalog), still
  read-only / raw / never mounting. **DONE (committed, host-verified):** the
  `scannerd-two-image` lane added an `ImageSource` abstraction that stamps a
  logical slot per image (`teslacam.img` → slot 0, `media.img` → slot 1) so
  downstream classification is unchanged; `produce()` merges records across both
  sources and aborts the batch on any structural error. `scannerd.service`
  `ExecStart` now serves `teslacam.img --media media.img`; `indexd`'s in-process
  `run_scan_pass` wraps its single reader as a native source. Back-compat: with no
  `--media`, scannerd serves the single combined `disk.img` (native MBR slots).
  Workspace gate green (fmt, clippy `-D warnings`, `cargo test --workspace`). This
  must still cut over in the same atomic window as the gadget.
- **Installer disk.img name/sentinel.** `setup-lib/common.sh` (`TESLAUSB_DISK_IMG=
  …/disk.img`) and the deploy-app symlink/sentinel guards protect the single image
  by name. **DONE (committed):** `common.sh` now defines `TESLAUSB_TESLACAM_IMG` +
  `TESLAUSB_MEDIA_IMG` and a `TESLAUSB_LUN_IMAGES` set (legacy `disk.img` kept so a
  mid-migration device is still guarded); a new `is_lun_image()` resolves symlinks
  and the rollback guards + `assert_safe_dest()` refuse a write-through / restore-
  over for **any** backing image. Installer suite extended (47/0) with cases proving
  deploy-app refuses a symlink resolving to `teslacam.img`/`media.img` and rollback
  ignores a planted sidecar for each; denylist/symlink-refusal tests stay green.
- **systemd units** are already updated to the two-image CLI
  (`gadgetd{,-provision,-control}.service`); the provision unit takes
  `--teslacam-mib`/`--media-mib`.

### 6.6 Strengthened spike probes (GPT-5.5, fold into §5 execution)

Before trusting the split on the vehicle, the §5 spikes must additionally check:

1. **Capacity re-read on geometry change.** The split changes per-LUN capacity;
   confirm Tesla re-reads geometry on re-present (removable=1 + unbind/rebind) and
   does not cache the old single-drive size.
2. **Per-LUN eject independence.** Clearing `lun.1/file` must leave `lun.0`
   enumerated/configured (verify `udc state` + new p1 clips during the `lun.1`
   cycle), proving the eject is scoped to one LUN, not the whole gadget.
3. **Boot recovery per-LUN.** After an unclean reset mid-`lun.1`-handoff, recovery
   must re-present each LUN's **own** image (never cross-wire `media.img` onto
   `lun.0`) and `lun.0` must come back first.
4. **Media feature coverage on `lun.1`.** Probe every feature (lock chime,
   boombox, lightshow, music, wraps, plates) — some may need a full
   re-enumeration; class those as maintenance-only.
5. **Two-drive enumeration stability.** Confirm the car tolerates a 2-LUN
   composite device (some hosts dislike multi-LUN mass storage) across sleep/wake.
6. **Write-amplification / `/data` headroom.** Two fully-allocated images + the
   ext4 archive must not exhaust `/data`; preflight + governor reserve.
7. **fsck cadence per image.** Independent images mean independent corruption
   domains; confirm the RW-handoff `fsck.exfat -n` runs per `media.img` cycle
   without touching `teslacam.img`.
8. **Recording continuity metric.** Capture dashcam clip timestamps across a
   `lun.1` cycle to prove zero-gap (the whole point of the split).
9. **Rollback rehearsal.** Before the real migration, rehearse the
   repoint-to-`disk.img` + reboot rollback so it is a single proven command.

Each remains `hardware-test`-railed, GPT-5.5-pre-reviewed, and operator-gated.

---

## 7. Implementation roadmap (Rust-only)

Ordered by value / independence; none changes the on-device disk layout without the
§5 spikes + operator approval.

1. **retentiond archive loop (read-only, highest value).** Wire the `serve` loop:
   catalog-driven selection + stability gate + raw-read copy to ext4 `archive/` +
   verify + catalog-mark. Host-testable behind the existing I/O traits; deploy is a
   read-only daemon (no eject) → low blast radius. **Closes the RecentClips gap.**
2. **Archive-lag + recording-continuity health** surfaced via webd → settings/storage
   UI (pillar 3 health).
3. **Media-write batching + quiet-window gate** in the webd→gadgetd handoff path
   (caps interim 1-image interruption).
4. **be-toybox-endpoints list/read** (catalog-path pattern, no p2 mount) → FE wiring
   for media screens (pillar 3).
5. **uploadd cloud archive (rclone)** + be-cloud-config (pillar 3 cloud).
6. **2-image migration** — ONLY after §5 spikes #1+#2 pass: provision `teslacam.img`
   + `media.img`, two LUNs in `gadgetd`, migrate media content, update the handoff to
   cycle `lun.1` only. GPT-5.5-reviewed + operator-gated. See the §6 runbook.

---

## 8. Provenance

Reconciled from this session's independent reading of the implemented stack and an
independent GPT-5.5 second opinion (model `gpt-5.5`), per the repo's mandatory
parallel-second-opinion directive. Points of agreement and the one divergence
(1-vs-2-image default, resolved as "2-image north star, gated on a hardware spike;
1-image safe interim") are recorded in §2. The §6 migration runbook and its
strengthened spike probes (§6.6) were authored for the "go for 2 images" directive
and hardened by a second, independent GPT-5.5 **adversarial** review (18 findings on
snapshot consistency, copy-from-live-image hazards, atomicity, sized-from-source,
p1+index backup, phase-dependent rollback, and an enforced migration sentinel — all
folded into §6.1–6.5). They likewise carry an operator + hardware gate; the `gadgetd`
two-LUN software refactor is host-verified (podman: fmt, clippy `-D warnings`,
`cargo test --workspace`, installer matrix) but deliberately undeployed. Before executing the §5
spikes or the §6 migration, the concrete plan is sent back to GPT-5.5 for adversarial
review and to the operator for approval.
