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

## 6. Implementation roadmap (Rust-only)

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
   cycle `lun.1` only. GPT-5.5-reviewed + operator-gated.

---

## 7. Provenance

Reconciled from this session's independent reading of the implemented stack and an
independent GPT-5.5 second opinion (model `gpt-5.5`), per the repo's mandatory
parallel-second-opinion directive. Points of agreement and the one divergence
(1-vs-2-image default, resolved as "2-image north star, gated on a hardware spike;
1-image safe interim") are recorded in §2. Before executing the §5 spikes or the §6.6
migration, the concrete plan is sent back to GPT-5.5 for adversarial review and to the
operator for approval.
