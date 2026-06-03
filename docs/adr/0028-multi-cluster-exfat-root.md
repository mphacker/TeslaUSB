# ADR-0028 — Multi-cluster exFAT root directory via a FAT chain

- **Status**: Accepted
- **Date**: 2026-06-03
- **Branch**: `b1-userspace-rust`
- **Driver**: Deep exFAT-implementation review (2026-06-03, reconciled
  with a parallel GPT-5.5 second opinion). Finding ranked **MAJOR 4**:
  the synthesized exFAT root directory was hard-capped at a single
  cluster, so a backing tree with enough top-level entries to overflow
  one root cluster failed layout planning outright (`RootOverflow`
  error) — the volume could not be presented at all. Standing operator
  invariant: *the device must ALWAYS allow USB writes to TeslaCam when
  it is powered on.*

## Context

The exFAT root directory begins at a fixed cluster — the boot sector
carries only `FirstClusterOfRootDirectory` (exFAT spec §3.1.7), set by
`ExfatGeometry::first_root_directory_cluster()` to cluster 2. Unlike
subdirectories, the root cannot use a `NoFatChain` contiguous run that
the parent's directory entry sizes, because there is no parent entry for
the root — its size is implied by walking its FAT chain from the fixed
first cluster to end-of-chain.

The previous planner (`teslausb-core/src/fs/exfat/layout.rs`,
`render_root_directory`) rendered the root into exactly one cluster and
returned a hard `LayoutError::RootOverflow` when the three special
entries (allocation bitmap, upcase table, volume label) plus every
top-level child's entry set exceeded one cluster. The read synth
(`synth.rs`) and the write resolver (`backend/exfat_write.rs`) both
assumed a single-cluster root:

- `fat_entry_value()` returned end-of-chain for cluster 2 unconditionally.
- `read_data_cluster_chunk()` served root bytes only for cluster 2.
- `with_directory_seed()` seeded the write resolver's root
  `DirectoryState` with a one-cluster `chain` and buffer, and mapped only
  cluster 2 in `cluster_to_directory`.

**Failure mode**: cluster 2 also neighbours the bitmap and upcase
clusters (3..3+B+U), so the root physically *cannot* extend contiguously
even if the format allowed it. A backing tree whose root holds enough
top-level files/dirs to need a second root cluster (large flat TeslaCam
layouts, many top-level lightshow/wrap/chime folders, etc.) hit
`RootOverflow` and the synth refused to build the volume — Tesla sees no
drive and cannot record. This is the most severe outcome of all: not a
mid-write fault but a total non-presentation.

## Decision

**Synthesize a multi-cluster root as a real FAT chain: the fixed first
root cluster followed by a contiguous overflow extent allocated from the
heap, linked cluster→cluster→end-of-chain in the FAT. Bound the resident
root buffer with a clean error instead of an unbounded allocation.**

This is a single atomic, cross-layer change (read planner + read synth +
write resolver + write FAT mirror + backend wiring), because a partial
fix — e.g. a planner that emits a chain a single-cluster reader cannot
serve — would be worse than the current clean refusal.

### Planner (`layout.rs`)

- `render_root_directory` computes the total root bytes (3 special
  entries + every top-level child entry set), then
  `root_cluster_count = ceil(total / bytes_per_cluster)`.
- The `root_cluster_count - 1` overflow clusters are allocated as a
  **single contiguous extent**, from the heap allocator, **after** every
  child is placed, so child cluster numbers are undisturbed. (The
  overflow run is therefore non-adjacent to the fixed first root
  cluster — cluster 2's neighbours are the bitmap/upcase.)
- The overflow extent is pushed into `allocated_extents` so the
  allocation bitmap marks it in-use.
- A new `root_cluster_chain()` accessor returns the full chain
  (`[first_root_cluster] ++ overflow_run`), consumed by both the synth
  and the write seed.
- `DataClusterSource::read_cluster_bytes` maps each root-chain cluster to
  its slice of the rendered root buffer via `root_chain_index`.

### Bounded failure (no OOM)

- `MAX_ROOT_DIRECTORY_BYTES = 16 MiB` caps the resident root buffer.
- The old `LayoutError::RootOverflow` is replaced by
  `LayoutError::RootTooLarge { needed_bytes, max_bytes }`, returned
  *before* any allocation when the root would exceed the cap. 16 MiB is
  far beyond any realistic TeslaCam/media top-level fan-out (tens of
  thousands of entries) while still preventing a pathological tree from
  OOMing a 512 MB Pi — consistent with the bounded-allocation posture of
  ADR-0026 and ADR-0020.

### Read synth (`synth.rs`)

- Stores `root_overflow: Option<(first, count)>` from
  `root_cluster_chain().split_first()`.
- `fat_entry_value()` now chains the root: fixed cluster → first overflow
  cluster → … → end-of-chain.
- `read_data_cluster_chunk()` serves the correct buffer slice for any
  root-chain cluster via a new `root_chain_index` helper.
- `with_layout()` relaxes the single-cluster length check to
  `chain.len() * bytes_per_cluster`.

### Write resolver (`backend/exfat_write.rs`)

- `with_directory_seed` gains a `root_chain: &[u32]` parameter. It seeds
  the root `DirectoryState` with the **full** chain, a buffer
  materialized per-cluster from the layout source, the decoded
  pre-existing child set, and a `cluster_to_directory` entry for **every**
  root cluster — so a host directory write into a root overflow cluster
  resolves to the root directory. The existing
  `apply_directory_cluster_write` is already multi-cluster-generic
  (`chain.iter().position()` + lazy chain materialization), so no change
  was needed there.
- A new `set_fat_base(cluster, value)` seeds the **base** FAT mirror for
  the root chain (cluster→next … →EOC) **without** marking those bytes
  dirty, so `try_walk_chain(root)` stays consistent and a host write that
  extends the root resolves correctly. The bytes are not dirtied because
  FAT-region reads are served by the read synth; the write overlay must
  surface only genuinely host-written FAT bytes.

### Backend wiring (`backend/synth.rs`)

- Snapshots `layout.root_cluster_chain()` **before** `with_layout` moves
  the layout, and passes it to `with_directory_seed`.

## Consequences

- **The volume always presents.** A root that needs more than one cluster
  is synthesized correctly instead of failing layout — closing a total
  non-presentation hole (worse than any mid-write fault).
- **Protocol-facing change** to the synthesized on-disk image: the root
  directory may now span multiple clusters linked through the FAT. This
  is standard exFAT (the spec mandates exactly this); it is what
  `fsck.exfat` and the Linux/Windows/Tesla exFAT drivers expect.
- **Bounded**: pathological trees fail cleanly with `RootTooLarge`
  (≤16 MiB resident root) rather than OOMing.
- No IPC/schema change; no new dependency.

## Alternatives considered

1. **Keep the single-cluster cap; document the limit.** Rejected — a
   real backing tree can exceed one root cluster, and the result is
   total non-presentation (no recording at all). Unacceptable against
   the standing invariant.
2. **Pre-pack the root and refuse trees that don't fit one cluster at
   walk time.** Same non-presentation outcome, just detected earlier;
   does not let the device serve a legitimate large root.
3. **Allocate the overflow contiguously adjacent to cluster 2 and use
   `NoFatChain`.** Impossible: cluster 2's neighbours are the
   bitmap/upcase clusters, and the root has no parent entry to carry a
   `NoFatChain` data length. The format requires a FAT chain for a
   multi-cluster root.
4. **Unbounded root buffer.** Rejected — a garbage/pathological tree
   could OOM the Pi. The 16 MiB cap with a clean `RootTooLarge` error
   preserves the no-OOM invariant.

## Validation

- **Planner** (`layout.rs`):
  - `plan_root_spanning_multiple_clusters_builds_fat_chain` — asserts the
    chain length > 1, the overflow run is contiguous and non-adjacent to
    the fixed root cluster, the buffer length matches the chain, the
    overflow extent is in `allocated_extents`, and `DataClusterSource`
    serves each chain cluster's slice.
  - `plan_rejects_root_exceeding_resident_buffer_ceiling` — asserts
    `RootTooLarge` for a root past the 16 MiB cap.
- **Read synth** (`tests/fs_exfat_integration.rs`):
  - `root_spanning_multiple_clusters_chains_fat_and_serves_all_entries`
    — builds a 200-file root, walks the root FAT chain end to end
    (head → non-adjacent contiguous overflow → EOC), and counts the
    `File` primary entries across every chain cluster, proving the data
    reads serve the spilled directory bytes (not zero-fill).
- **Write resolver** (`exfat_write.rs`):
  - `seeded_multicluster_root_resolves_clip_in_overflow_cluster` — seeds
    a two-cluster root chain (non-adjacent overflow), asserts the base
    FAT mirror is walkable (`try_walk_chain` == full chain), writes a
    top-level clip's directory entry into the **root overflow cluster**
    plus its data, and asserts the clip materializes at the volume root.
    Pre-fix the overflow cluster was unmapped, the write spilled to
    `pending_data`, and the clip was silently dropped.
- `cargo test -p teslafat -p teslausb-core` → all pass (849 core lib +
  278 teslafat + integration suites).
- `cargo fmt` and `cargo clippy --lib --all-features` clean on both
  crates.

### Hardware verification still required

Per the operator's explicit acceptance, this change is **not trusted
on-device until** a hardware run confirms `fsck.exfat /dev/nbd0` is clean
and the volume mounts read/write on Linux with a root large enough to
span multiple clusters. The software gates above are necessary but not
sufficient for a protocol-facing image change.

## References

- Review report: session `files/exfat-deep-review-20260603.md` (MAJOR 4).
- Code: `rust/crates/teslausb-core/src/fs/exfat/layout.rs`
  (`render_root_directory`, `root_cluster_chain`,
  `MAX_ROOT_DIRECTORY_BYTES`, `RootTooLarge`),
  `rust/crates/teslausb-core/src/fs/exfat/synth.rs`
  (`root_overflow`, `root_chain_index`, `fat_entry_value`),
  `rust/crates/teslafat/src/backend/exfat_write.rs`
  (`with_directory_seed` root chain, `set_fat_base`),
  `rust/crates/teslafat/src/backend/synth.rs` (root-chain snapshot).
- Related: ADR-0026 (bounded out-of-heap directory entries) and ADR-0020
  (bounded pending-data spill) — same no-OOM posture; ADR-0023
  (single-LUN partitioned disk) — the shared backend whose non-
  presentation stops recording.
