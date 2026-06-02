# ADR-0023 — Single USB LUN with a partitioned disk (two exFAT partitions)

**Status:** Proposed (2026-06-01). Awaiting operator approval before
implementation. Supersedes the dual-separate-LUN presentation introduced
in Phase 6.11 (`setup-lib/11-gadget.sh`).

**Related:** ADR-0004 (backend trait shape — `BlockBackend`), ADR-0005 /
ADR-0006 (NBD wire + lifecycle), ADR-0018 (LUN-aware cleanup pressure),
ADR-0021 (disk-backed pending spill), the SIGHUP live-reload work
(`backend/reloadable.rs`), `setup-lib/11-gadget.sh`,
`web/teslausb_web/services/lock_chime_service.py`.

## Context

### The bug that forced this decision

The operator reported the Tesla kept playing the **old** lock chime after
activating a new one. Hardware testing on `cybertruckusb.local`
(2026-06-01) proved, via a full-reboot test (the strongest possible USB
re-enumeration), that:

> **Tesla reads its "special" host-managed files — `LockChime.wav`,
> `LightShow/`, `Boombox/` — only from the partitions of the ONE physical
> USB device it uses for dashcam. It does not scan a second, separate USB
> device / LUN for them.**

Evidence: with `LockChime.wav` present only on the media LUN (lun.1) and
absent from the CAM LUN (lun.0), a full reboot still played the chime that
had previously been cached from lun.0. The media LUN's chime was never
read.

### Why v1 did not have this bug

v1 presents **one** physical USB disk with an MBR partition table:

```
[ MBR ][ part1: TeslaCam (dashcam) ][ part2: media (Chimes/, LightShow/, LockChime.wav) ]
```

Tesla scans the partitions of that single device and finds `LockChime.wav`
on part2. v1's "second partition" is the **2nd partition of the dashcam
device**, not a second device. (See `main:scripts/web/services/
lock_chime_service.py::set_active_chime`, which writes the chime to part2
root and then rebinds the gadget.)

### What B-1 does today (the root cause)

B-1 (Phase 6.11, `setup-lib/11-gadget.sh`) presents **two separate LUNs**:

| | LUN 0 | LUN 1 |
|---|---|---|
| nbd device | `/dev/nbd0` | `/dev/nbd1` |
| teslafat instance | `teslafat@0` | `teslafat@1` |
| backing dir | `/srv/teslausb/teslacam` | `/srv/teslausb/media` |
| filesystem | exFAT, label `TESLACAM` | FAT32, label `MEDIA` |
| gadget | `mass_storage.usb0/lun.0` | `mass_storage.usb0/lun.1` |
| config | `/etc/teslausb/teslafat-0.toml` | `/etc/teslausb/teslafat-1.toml` |

`/etc/modprobe.d/teslausb-nbd.conf` sets `nbds_max=2 max_part=0` (each
nbd device is a whole-disk filesystem with no partition table). To Tesla
these are two independent USB drives. The chime, light shows, and boombox
sounds placed on the media LUN are therefore never read by the car. Only
dashcam (which Tesla writes to whatever drive has `TeslaCam/`) and the USB
music player (which scans any drive) work across two separate LUNs.

### Current sizing / resize mechanism (must be preserved)

`teslausb-resize-lun --lun {teslacam|media} --size-gb N` rewrites the
target instance's `volume_size_gb` in its TOML and restarts it. Because
the synthesized volume is **virtual and sparse** (real bytes live in the
backing directory on the SD card; the FAT/exFAT geometry is computed, not
allocated), a resize is just a change of reported geometry — no data is
moved. The operator drives this from the web UI via a narrow sudoers
entry. **Any new design must keep per-partition resize with these same
semantics.**

## Decision

Present **one** USB mass-storage LUN backed by a **single synthesized disk
that carries an MBR partition table with two partitions, both exFAT**:

```
LBA 0:        Protective/standard MBR (one 512-byte sector, 2 partition entries)
part1:        exFAT — TeslaCam (dashcam)            label TESLACAM
part2:        exFAT — media (Chimes/, LightShow/,   label TESLAMEDIA
              Boombox/, Music, LockChime.wav, ...)
```

This mirrors v1's proven structure, so the car reads `LockChime.wav`,
`LightShow/`, and `Boombox/` from part2. Both partitions are exFAT per the
operator's 2026-06-01 decision (no remaining reason to keep FAT32; the
chime/lightshow features are what mattered and they are a function of
*being on the dashcam device's partition table*, not of the filesystem
flavour).

### Why exFAT for both (operator decision, recorded)

- Tesla's modern firmware reads exFAT for both dashcam and the host-file
  features. FAT32's only historical advantage here was caution; the
  decisive variable proven on hardware is *device/partition topology*, not
  FAT32-vs-exFAT.
- One filesystem flavour simplifies the codebase: a single write path
  (the exFAT `ExfatWriteState`) is exercised on both partitions. Once both
  partitions are exFAT and verified on hardware, the **entire FAT32 engine
  is decommissioned and deleted** — not retained as dead code. The charter
  forbids dead code (Pillar 5), and the operator has directed that this
  restructure must not leave stale FAT32 paths behind. See
  "FAT32 decommissioning" below.

### FAT32 decommissioning (no dead code left behind)

Going all-exFAT makes the FAT32 filesystem engine dead code. Per the
charter ("No dead code") and the operator's explicit direction
(2026-06-01), it is removed — but **only after** the all-exFAT gadget is
deployed and verified on hardware, because the live device's media LUN
runs FAT32 *today* and removing the engine earlier would break the
running system and the build.

**FAT32-only code to delete (safe to remove once exFAT-both ships):**

- `rust/crates/teslausb-core/src/fs/fat32/` — the whole module
  (`boot_sector`, `chain`, `directory`, `dir_decode`, `fsinfo`,
  `fat_table`, `synth`, `parse`, `layout`, `geometry`, `mod`).
- `rust/crates/teslafat/src/backend/fat32_write.rs` — the FAT32 write
  state machine.
- The `FsType::Fat32` enum variant (`teslafat/src/config.rs`) and its
  `Default` impl; `FsType` collapses to exFAT-only. Decide whether to keep
  the enum as a single-variant forward-compat seam or drop `fs_type`
  entirely from the config — leaning toward dropping it, since a
  single-variant enum is itself a mild smell.
- FAT32 branches in `teslafat/src/backend/synth.rs` (the `match fs_type`
  arms selecting the FAT32 layout/synth/write path).
- FAT32-only tests: `teslausb-core/tests/fs_fat32_integration.rs`, the
  FAT32 arms of `teslafat/tests/synth_write_integration.rs` and
  `power_cut_harness.rs`, and FAT32 cases in `cold_start_bench.rs`.
- `fs_type = "fat32"` in the media TOML template + the `fat32` mentions in
  `setup-lib/11-gadget.sh` and `setup-lib/03-data-roots.sh`.
- Web/UI FAT32 references: `web/teslausb_web/blueprints/system_health.py`,
  `services/storage_health_service.py`, and the `templates/index.html` /
  `templates/storage_settings.html` FAT32 strings.
- FAT32-specific doc comments in `fs/mod.rs`, `lib.rs`, `messages.rs`, etc.

**Shared code to KEEP (used by exFAT too — not FAT32-specific despite the
name/comments):** `fs/cluster_layout.rs`, `fs/cluster_map.rs`,
`fs/data_cluster_source.rs`, `fs/backing_tree.rs`, `fs/geometry.rs` (the
shared geometry traits), and the backend infrastructure
(`dir_tree`, `dirty_map`, `pending_spill`, `reloadable`, `retention`).
These appear in the FAT32 grep only via shared comments or the common
synthesis primitives. Each will be re-checked with `cargo machete` +
clippy `dead_code` after the FAT32 deletion to confirm nothing is
orphaned.

**Sequencing (critical):** the FAT32 deletion is the LAST migration phase
(see P7), gated on a green hardware verification of the all-exFAT gadget.
Until then the FAT32 engine stays compiled and live so the current device
keeps working and any rollback target remains buildable.

### The clean insertion point: a new `BlockBackend`

The NBD server already serves *any* `BlockBackend`
(`teslausb-core::backend::BlockBackend`):

```rust
pub trait BlockBackend {
    fn size(&self) -> u64;
    async fn read(&self, offset: u64, buf: &mut [u8]) -> BackendResult<()>;
    async fn write(&self, offset: u64, buf: &[u8], flags: WriteFlags) -> BackendResult<()>;
    async fn flush(&self) -> BackendResult<()>;
}
```

We introduce a new **`PartitionedDiskBackend`** (in the `teslafat` crate,
Layer-1-adjacent backend module) that implements `BlockBackend` and
**composes** the two existing per-filesystem backends without changing
them:

- Holds a generated 512-byte **MBR** (LBA 0) plus an ordered list of
  partitions, each `{ start_lba, sector_count, child: ReloadableBackend }`.
- `size()` = `(total_sectors) * 512`, where `total_sectors` covers the MBR,
  alignment gaps, and both partitions.
- `read` / `write` route by offset:
  - offset within the MBR/gap region → serve generated MBR bytes (reads);
    writes to the table are accepted-and-ignored or rejected (Tesla does
    not repartition; see Risks).
  - offset within a partition's LBA range → subtract `start_lba*512` and
    delegate to that partition's child backend (`read`/`write`/`flush`).
- `flush` fans out to all children.

The children remain `ReloadableBackend<SynthBackend>` exactly as today, so
the **SIGHUP live-reload** mechanism is unchanged — re-walking a backing
dir still atomically swaps that partition's view, and the chime/lightshow
refresh path keeps working per-partition.

**No changes are required to `teslausb-core::fs::{exfat,fat32}` or to the
existing `SynthBackend` synth/write state machines.** This satisfies the
charter's layering rule: the partition router is a pure composition over
the existing block abstraction.

### Topology after the change

| | Single LUN |
|---|---|
| nbd device | `/dev/nbd0` only |
| teslafat instance | one process serving the partitioned disk |
| backing dirs | `/srv/teslausb/teslacam` (part1) + `/srv/teslausb/media` (part2) — **unchanged on disk** |
| gadget | `mass_storage.usb0/lun.0` only |
| modprobe | `nbds_max=1` (or keep 2, only nbd0 used); host-side `max_part` irrelevant — Tesla parses the MBR itself |

The two backing directories stay separate on the SD card (preserving
ADR-0018 LUN-aware cleanup, the indexer's CAM-only assumptions, and write
isolation between the car's high-churn dashcam writes and web-managed
media). Only the *presentation* collapses from two LUNs to one
partitioned LUN.

### Resize semantics (preserved, generalized to partitions)

`teslausb-resize-lun --lun {teslacam|media} --size-gb N` continues to
exist. Instead of rewriting one of two independent TOMLs, it rewrites the
**partition geometry** of the single disk config:

- Each partition keeps a `size_gb`. The MBR partition entry's
  `sector_count` and the child `SynthBackend`'s reported volume size are
  derived from it.
- A resize regenerates the MBR (partition 2's `start_lba` shifts when
  partition 1 grows/shrinks) and re-presents the disk (SIGHUP re-walk +
  UDC rebind, the existing mechanism).
- Still virtual/sparse: no bytes move; only reported geometry changes.
- The same `os_reserve_gb` / total-capacity guardrails in the resize
  helper apply to the **sum** of the two partitions.

## Alternatives considered

1. **Single device, single partition, one exFAT filesystem for
   everything.** (Operator initially asked, then revised to two
   partitions.) Rejected because:
   - It would force the car's continuous high-churn dashcam writes to
     share one filesystem with web-managed media, enlarging the
     corruption blast radius.
   - It breaks ADR-0018 LUN-aware cleanup and the indexer/SEI assumption
     of a CAM-only tree; the two backing trees would have to be merged.
   - Independent per-area resize becomes "one knob" instead of two.
   - It fixes the chime bug no better than two partitions (both present a
     single device Tesla scans).

2. **Keep two separate LUNs; publish chime/lightshow/boombox onto the CAM
   LUN root.** Rejected as a permanent design: it puts host-managed files
   on the dashcam volume (operator disliked), splits related content
   across LUNs, and diverges from the v1-proven structure. Retained only
   as a possible *interim stopgap* if this ADR's migration is deferred.

3. **Two partitions, exFAT CAM + FAT32 media (v1-faithful filesystems).**
   Superseded by the operator's "exFAT for both" decision; carried no
   functional advantage once topology (not FS flavour) was identified as
   the cause.

## Consequences

### Positive

- The car reads `LockChime.wav`, `LightShow/`, and `Boombox/` — the
  reported bug and a class of latent bugs (light shows / boombox would
  have failed identically) are fixed at the root.
- v1-faithful topology; the web `lock_chime_service` activation flow maps
  cleanly (write to part2 root → re-present).
- Backing-dir layout, ADR-0018 cleanup, indexer, SEI, and write isolation
  all unchanged.
- Smaller gadget surface: one LUN, one nbd device, one teslafat process.

### Negative / cost

- New Layer-1 code: `PartitionedDiskBackend` + an MBR generator/parser
  module, with unit tests (charter coverage ≥ 80% on `fs/`-adjacent code).
- A net **reduction** in total code once P7 lands: the entire FAT32 engine
  (`fs/fat32/`, `fat32_write.rs`, the `fs_type` config seam, FAT32 tests,
  and UI/setup mentions) is deleted, leaving a single exFAT write path.
- Migration on the live device must collapse two LUNs → one without losing
  the 256 GB of CAM data or the 32 GB of media data (see migration plan).
- `setup-lib/11-gadget.sh`, `teslausb-resize-lun`, the nbd-attach/gadget
  units, and the web `gadget_rebind` / `lock_chime` services must be
  reworked from a two-instance model to a one-instance partitioned model.
- A single teslafat process now serves both partitions: a crash takes both
  down together (today a media-LUN crash leaves CAM up). Mitigation: the
  process already restarts on-failure; CAM write quiescence gating still
  applies; and in practice the car tolerates a brief whole-device stall
  exactly as it tolerates the UDC rebind today.

## Implementation plan (NOT YET STARTED — for review)

Phased, each phase independently testable; CAM-write safety gated
throughout (hardware-test rails: dead-man timer, snapshots, quiescence
checks).

**P0 — Design lock (this ADR).** Operator approves topology + exFAT-both.

**P1 — `PartitionedDiskBackend` + MBR module (pure Rust, no hardware).**
- New `teslafat/src/backend/partitioned.rs`: `PartitionedDiskBackend`
  implementing `BlockBackend`, composing N children with `{start_lba,
  sectors}`.
- New MBR generate/parse (likely in `teslausb-core::fs` as a small
  `mbr` module, since it is filesystem-adjacent and library-shareable):
  classic 512-byte MBR, 2 primary partition entries, type `0x07`
  (exFAT/IFS), CHS filled with the LBA-overflow sentinel, correct
  `start_lba` / `sector_count`, `0x55AA` signature.
- Unit tests: byte-exact MBR for known geometries; offset routing
  (MBR/gap/part1/part2 boundaries, cross-partition reads rejected);
  `size()` math; `flush` fan-out. Property test: every byte offset maps to
  exactly one region.
- Validate the synthesized disk with `fdisk -l` / `sfdisk --dump` against
  a loopback of the exported image in a host test.

**P2 — Config + single-instance wiring.**
- Extend `teslafat` config: a `[[partition]]` array (`backing_root`,
  `size_gb`, `volume_label`, `fs_type`, `spill_dir`, `retention`) plus a
  top-level disk section. Keep `deny_unknown_fields`; bump the schema doc
  header.
- `main.rs` builds a `PartitionedDiskBackend` from N partitions, each a
  `ReloadableBackend<SynthBackend>`; serves it on the single socket.
- SIGHUP handler re-walks all partitions (or a targeted one).
- `--check-config` validates partition sizes sum within the disk and
  alignment is sane.

**P3 — setup + gadget + resize rework.**
- `setup-lib/11-gadget.sh`: one `teslafat.toml` with two partitions; one
  nbd device; one gadget LUN; `nbds_max=1`. Update unit topology
  (`teslafat.service` non-templated or `@disk`; one `nbd-attach`;
  `usb-gadget` requires one attach).
- `teslausb-resize-lun`: operate on partition geometry; regenerate MBR;
  re-present. Preserve sudoers narrowness and `os_reserve_gb` guardrails.
- `tesla_gadget_rebind.sh` / `gadget_rebind.py`: SIGHUP the single
  instance (re-walk both partitions) → UDC rebind. Simpler than today.
- `lock_chime_service` (B-1): write `LockChime.wav` to the **media
  partition backing root** (`/srv/teslausb/media`), which is now part2 of
  the single device Tesla scans — v1-faithful. Keep the
  delete→sync→tmp→rename→utime→sync robustness.

**P4 — Charter review + security review.**
- Full `charter-review` on the diff (new Layer-1 code, gadget/sudoers,
  cache-invalidation, file deletion → triggers `security-review`).
- `cargo fmt`/`clippy -D warnings`/tests/coverage/`deny`/`machete`/`doc`;
  Python ruff/mypy/pytest for the web changes.

**P5 — Hardware migration (the careful part).**
- Pre-flight: snapshot configs/units/binaries; confirm CAM write-idle;
  arm dead-man.
- Because backing dirs are unchanged on disk, migration is a
  *presentation* swap, not a data move:
  1. Install new single-partition teslafat binary + config + units.
  2. Tear down the old two-LUN gadget; bring up the one-LUN partitioned
     gadget.
  3. UDC rebind; Tesla re-enumerates one disk with two partitions.
- Verify with `fdisk -l` on a host mount of `/dev/nbd0` (read-only) that
  the MBR + two exFAT partitions are correct and CAM data is intact.
- Operator locks/unlocks the parked car → confirms the new chime plays
  (use a sound audibly distinct from the cached Portal).
- Confirm dashcam still records (clip count climbs) throughout.
- Rollback: re-install the snapshotted two-LUN config + units + binary and
  rebind. Backing data is untouched either way.

**P6 — Docs.**
- `docs/06-OPERATIONS.md`, `docs/02-LEARNINGS.md` (the topology finding),
  and this ADR flipped to **Accepted, hardware-verified**.

**P7 — FAT32 decommission (LAST; gated on green P5 hardware verify).**
- Delete the FAT32 engine and all FAT32-only paths per the
  "FAT32 decommissioning" inventory above (module, write state machine,
  enum variant, synth match arms, tests, TOML/setup mentions, UI strings,
  doc comments).
- Run `cargo machete` + clippy `dead_code` + `cargo +nightly udeps` (if
  available) to confirm no orphaned shared code remains; re-run the full
  exFAT test suite + coverage gate.
- Charter-review the deletion diff (Pillar 5 — no dead code) before commit.
- NOT done earlier: removing FAT32 before the all-exFAT gadget is verified
  on hardware would break the live media LUN and destroy the buildable
  rollback target.

## Hardware-proven finding to record in LEARNINGS

> Tesla reads `LockChime.wav`, `LightShow/`, and `Boombox/` only from the
> partitions of the single USB device it uses for dashcam. Presenting them
> on a *separate* USB LUN does not work, even after a full reboot. The fix
> is one device with a partition table (v1 topology), not two devices.

## Invariant (unchanged)

TeslaCAM must ALWAYS be writable when the device is powered on. The
migration is a presentation swap over unchanged backing data; CAM write
quiescence is gated before any re-present, exactly as the existing
rebind/SIGHUP flow already does.
