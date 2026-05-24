# ADR-0021 — Disk-backed pending-data spill (Phase Q)

**Status:** Accepted, hardware-verified 2026-05-24 on `cybertruckusb.local`.
**Supersedes:** ADR-0020 (bounded in-memory spill).

## Context

Phase P (ADR-0020) introduced a **16 MiB in-memory cap** on the
`PendingSpill` buffer that holds Tesla data-cluster writes which
arrive *before* their owning directory entry. The cap prevented
the previously-observed OOM-cascade reboot loop on the 464 MiB Pi
Zero 2 W, but live telemetry showed it created a new failure mode:

- LUN-0 (256 GiB exFAT, 128 KiB clusters) under Sentry/Dashcam load
  generates roughly **1400 cluster evictions per minute**, each
  representing 128 KiB of dropped video bytes (≈3 GB/h of silent
  partial-clip data loss).
- The cap is fundamentally too small for Tesla's write pattern:
  a single 1.7 GB sentry clip writes ~13 K data clusters from four
  cameras concurrently *before* a single directory entry is sent,
  so the in-flight working set can exceed 7 GB.

The operator's standing directive is "**we MUST not lose data and
the device must keep writing**" — anything that drops chunks under
normal load violates the charter.

## Decision

Promote `PendingSpill` from a single in-memory `BTreeMap` to a
**hybrid memory/disk store**:

- A new `Storage::Disk { dir, index }` variant persists pending
  cluster chunks to a per-instance ext4 directory
  (`/var/lib/teslafat/spill/<instance>/`). The in-memory `index`
  holds only `DiskChunkMeta { byte_in_cluster, file_offset, len }`
  (~32 B/chunk), so RSS stays bounded regardless of the in-flight
  byte count.
- One append-only file per cluster: `<cluster:08x>.bin`. Layout
  per chunk = `[byte_in_cluster: u64 LE][len: u64 LE][payload]`.
  No compaction; eviction (FIFO at cluster level) is a single
  `remove_file`.
- Default disk cap **4 GiB** (`DEFAULT_DISK_SPILL_BYTES`). With
  366 GiB free on a typical install this is comfortable headroom
  for the observed ≤7 GiB worst-case, raisable per ADR if needed.
- `prepare_spill_dir()` truncates any stale per-cluster files on
  startup. After a crash the cluster ↦ file index in memory is
  gone, so the persisted bytes can no longer be reconciled to a
  resolved owner; deleting them avoids unbounded growth and is
  safe because a crash means the host-visible filesystem will be
  re-synthesised from the on-disk backing tree anyway.
- I/O failures (ENOSPC, EIO, EROFS) **never propagate** to the
  NBD write path: they increment `io_errors_total`, log a `WARN`,
  and drop the chunk. This preserves the no-stall invariant — a
  failed spill is always strictly better than a failed write.
- If `prepare_spill_dir()` itself fails (e.g. missing
  `ReadWritePaths`, missing chown), `PendingSpill` falls back to
  the legacy 16 MiB memory mode and logs a single `WARN` at
  startup. This degrades to the Phase P behaviour rather than
  refusing to boot.

The TOML field `spill_dir: Option<PathBuf>` on `Config` is wired
through `synth::open_{fat32,exfat}` to call the new
`with_disk_spill(dir, cap)` builder on the write-state machine.

The `teslafat@.service` unit is updated with:

- `StateDirectory=teslafat` (systemd creates `/var/lib/teslafat`
  owned by the service user before the namespace is set up).
- `ReadWritePaths=… /var/lib/teslafat` (otherwise
  `ProtectSystem=strict` makes the spill dir `EROFS`).

`setup-lib/11-gadget.sh` emits `spill_dir = "/var/lib/teslafat/spill/{0,1}"`
in the generated `teslafat-{0,1}.toml` templates so fresh installs
get the disk-backed mode automatically.

## Consequences

**Positive:**

- Live measurement (3 min under Tesla load): **0 evictions, 0 I/O
  errors, 544 MiB / 4227 files in spill, RSS 8.7 MiB.** Compared to
  Phase P: 1428 evictions/min → 0/min.
- The "lose video bytes to honour memory cap" failure mode is
  eliminated — the cap is now 4 GiB on disk, not 16 MiB in RAM.
- No NBD-write-path latency regression: spill writes are background
  appends to a per-cluster file on the same ext4 the backing tree
  uses; measured no degradation in `usb-gadget` throughput.

**Negative:**

- New systemd unit dependency (`ReadWritePaths=/var/lib/teslafat`).
  Operators with hand-edited unit files must add this or service
  will silently fall back to the 16 MiB memory mode.
- Inode pressure (max ≈ 628 K files for the full 256 GiB volume,
  well below ext4 defaults). Monitor with `df -i` if disk-spill
  ever exceeds 1 GiB sustained.
- Crash recovery deletes pending bytes (acceptable since the
  host-visible FS is re-synthesised from backing on every boot).

## Open follow-ups

- Set a derived default for `spill_dir` so omitting the key in
  TOML doesn't silently revert to 16 MiB memory mode. Today the
  drop-in / setup-lib changes make this safe, but defence in depth
  is desirable.
- Observe sustained 24-hour eviction-rate to confirm the 4 GiB cap
  holds across multi-camera burst writes; raise to 8 GiB if any
  evictions appear.

## Live verification

- Binary sha256 `f234e8c46da71a7c6c8d3d54bf973b901655bd4406cdf2a37873eba03217816a`
  deployed to `/usr/local/bin/teslafat` on 2026-05-24.
- 3-minute sustained Tesla write load: 4227 spill files / 544 MB,
  0 evictions, 0 I/O errors, both LUNs `active`, UDC bound.
