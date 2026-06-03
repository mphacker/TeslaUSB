# ADR-0029 — Remove FAT32 entirely; the synthesized USB drive is exFAT-only

- **Status**: Accepted
- **Date**: 2026-06-03
- **Branch**: `b1-userspace-rust`
- **Driver**: Operator directive (2026-06-03): *"We want to fully remove
  FAT32 and only use exFat in this project. No need to fix anything with
  Fat32, however, we do need to remove any Fat32 code and hooks."* The
  device runs exFAT on both partitions in production; FAT32 was dead
  weight that still carried code, tests, and a config default.

## Context

The teslafat NBD daemon synthesizes the USB drive the Tesla mounts. Early
B-1 work supported two filesystem families behind a `FsType` config enum
(`Fat32` | `Exfat`), with parallel implementations:

- `teslausb-core/src/fs/fat32/` — a full FAT32 synth/parse/layout stack
  (boot sector, FSInfo, FAT table, directory LFN/SFN, geometry, decoder).
- `teslafat/src/backend/fat32_write.rs` — the FAT32 write state machine.
- Twin integration tests (`synth_write_integration.rs`,
  `fs_fat32_integration.rs`) plus FAT32 arms inside shared code
  (`SynthInner`/`WriteState`, `RegionKind`, `cluster_layout::AllocatedChains`,
  MBR partition-type selection).

FAT32 was also the **default** `fs_type` in code, despite every deployed
device running exFAT (the 32 GB+ MEDIA partition exceeds the 32 GB FAT32
comfort zone, and exFAT is what the car expects for the single
partitioned disk per ADR-0023). Maintaining two filesystem families
doubled the surface area for the deep exFAT review (ADR-0026/0027/0028)
with no production benefit.

### The deployment-safety risk

The teslafat config uses `#[serde(deny_unknown_fields)]`. The live device
config `/etc/teslausb/teslafat-0.toml` (written by
`setup-lib/11-gadget.sh`) contains `fs_type = "exfat"`. Removing the
`fs_type` field outright would make a new binary **reject the existing
on-disk config and fail to start** — i.e. the daemon would not present the
gadget, the car would stop recording. That is the #1 invariant violation
the project exists to prevent.

## Decision

**Delete all FAT32 implementation, wiring, and tests. The synthesized
drive is exFAT-only. Keep a deprecated, parse-only `fs_type` config key
that accepts `"exfat"` and explicitly rejects `"fat32"`, to preserve
back-compat with the deployed config.**

1. **Delete the FAT32 code** (`git rm`): the whole
   `teslausb-core/src/fs/fat32/` directory, `fat32_write.rs`,
   `fs_fat32_integration.rs`, `synth_write_integration.rs`.
2. **Collapse shared FAT32/exFAT branches to exFAT-only**: `SynthInner`,
   `WriteState`, `overlay_read`/`write`/`flush` matches, `RegionKind`
   variants, `cluster_layout::AllocatedChains` + its `DirTreeBackend`
   impl, and the now-orphaned `UnsupportedRegion` error variants.
3. **MBR partition type** is hardcoded to `PARTITION_TYPE_EXFAT` (0x07);
   the FAT32 LBA type (0x0C) constant and the `partition_type_for` match
   are removed.
4. **Config back-compat (Option B, chosen over full removal).** `FsType`
   becomes a single-variant `enum { #[default] Exfat }` with a custom
   `Deserialize` that accepts only `"exfat"` and returns a clear error
   for `"fat32"` ("…is no longer supported; this project is exFAT-only").
   The `fs_type` field is retained as `#[serde(default)]` so the live
   config keeps parsing under `deny_unknown_fields`, but **no production
   code reads it** — synthesis is unconditionally exFAT.
5. **Convert (not delete) the FS-agnostic test coverage** to exFAT:
   `smoke.rs` (NBD boot-sector assertions → exFAT `EB 76 90`/`EXFAT   `),
   `power_cut_harness.rs` (`.partial` crash-recovery, rewritten with exFAT
   `encode_file_entry_set` + exFAT `dir_decode`), and the 10k-file
   cold-start bench (moved to `teslafat/tests/exfat_cold_start_bench.rs`).
6. **Hooks/docs**: stale "FAT32" comments in `setup-lib/03-data-roots.sh`
   and `scripts/check.sh` corrected to "exFAT"; the web `system_health`
   partition probe defaults an absent `fs_type` to exFAT and drops the
   FAT32 display mapping.

### Why keep a rejecting `fs_type` key instead of removing it

Removing the field is the cleaner end state but is unsafe to land before a
lockstep config regen on every device — and deploys are hardware-gated and
asynchronous. Keeping a parse-only key that *rejects* `"fat32"` gives us
the FAT32 code deletion immediately with zero deployment risk: the worst
case (a stale config naming `"exfat"`) parses and is ignored. A future ADR
may drop the field once all devices are known to be regenerated.

## Consequences

- **Recording invariant upheld across upgrade:** a new binary starts
  cleanly against the existing `fs_type = "exfat"` config.
- **Roughly halved filesystem surface area**: one synth/parse/write
  family to maintain, review, and harden. The deep-review hardening
  (ADR-0026/0027/0028) no longer needs a FAT32 twin.
- **Protocol/default change**: `fs_type = "fat32"` is now a hard config
  error. This is intentional and loud (not silent).
- **No new dependency; no IPC/schema change.**

## Alternatives considered

1. **Keep FAT32 behind the enum "just in case."** Rejected — it is dead
   weight that doubles review/hardening cost for a mode no device uses,
   violating the charter's "no dead code" pillar.
2. **Remove the `fs_type` field outright now.** Rejected for deployment
   safety (see above); revisit once all devices are regenerated.
3. **Default the field to exFAT but still accept `"fat32"` as an alias.**
   Rejected — silently accepting a value whose implementation no longer
   exists would synthesize an exFAT drive while the operator believes
   FAT32 is in use. A hard, explanatory rejection is safer.

## Validation

- `cargo test -p teslafat -p teslausb-core` green on Windows host AND
  Linux (WSL): teslafat 264 lib + teslausb-core 537 lib + all integration
  suites (`smoke` 7, `power_cut_harness` 9, `synth_exfat_write_integration`
  31, `fs_exfat_integration` 11, `exfat_cold_start_bench`, `sentinel`).
- `config.rs::rejects_fat32_fs_type_with_clear_error` regression test:
  a config with `fs_type = "fat32"` fails to parse with the exFAT-only
  message.
- `smoke.rs::daemon_serves_exfat_boot_sector_via_nbd_handshake_and_read`:
  the running daemon (default config, no `fs_type`) serves a valid exFAT
  boot sector over the NBD wire.
- `test_system_health.py::test_partition_block_defaults_absent_fs_type_to_exfat`:
  an absent `fs_type` surfaces as exFAT, never "?" or FAT32.
- `cargo fmt`/`clippy` clean (advisory pedantic only); shellcheck clean
  (info-level only) on the touched hooks; ruff clean on the touched web
  files.

## References

- Code removed: `rust/crates/teslausb-core/src/fs/fat32/` (dir),
  `teslafat/src/backend/fat32_write.rs`,
  `teslausb-core/tests/fs_fat32_integration.rs`,
  `teslafat/tests/synth_write_integration.rs`.
- Code edited: `teslafat/src/{config.rs,main.rs}`,
  `teslafat/src/backend/{synth.rs,mod.rs,reloadable.rs}`,
  `teslausb-core/src/fs/{mod.rs,mbr.rs,geometry.rs,cluster_layout.rs}`,
  `teslausb-core/src/fs/exfat/{synth.rs,parse.rs}`,
  `teslafat/tests/{smoke.rs,power_cut_harness.rs,exfat_cold_start_bench.rs}`.
- Related: ADR-0023 (single-LUN partitioned disk — why one exFAT disk),
  ADR-0026/0027/0028 (exFAT deep-review hardening this removal builds on).
