# ADR-0026 — Reject out-of-heap exFAT directory entries before allocation

- **Status**: Accepted
- **Date**: 2026-06-03
- **Branch**: `b1-userspace-rust`
- **Driver**: Deep exFAT-implementation review (2026-06-03, reconciled
  with a parallel GPT-5.5 second opinion). Finding ranked **BLOCKER 2**:
  a malformed `NoFatChain` directory entry can drive an unbounded
  allocation in the teslafat write path, aborting the daemon and
  dropping the USB gadget — which stops TeslaCam recording. Standing
  operator invariant: *the device must ALWAYS allow USB writes to
  TeslaCam when it is powered on.*

## Context

The exFAT write-state machine
(`rust/crates/teslafat/src/backend/exfat_write.rs`) resolves a file or
directory from a decoded directory entry's `FirstCluster` and
`DataLength`. For a contiguous (`NoFatChain`) entry it materializes the
cluster run directly from the entry, without walking the FAT:

```rust
let cluster_count = self.clusters_for_data_length(data_length); // capped at u32::MAX
// ...
(first_cluster..first_cluster.saturating_add(cluster_count)).collect::<Vec<u32>>();
// directories additionally do: vec![0u8; chain_vec.len() * bytes_per_cluster]
```

`clusters_for_data_length` only saturates at `u32::MAX`; it does **not**
bound the count to the actual data heap (`ClusterCount` clusters
starting at `FIRST_DATA_CLUSTER`). The decoder
(`teslausb-core/src/fs/exfat/dir_decode.rs`) deliberately still emits a
`File` entry even when its `SetChecksum`/`NameHash` fail validation
(test `checksum_mismatch_is_reported_but_entry_still_emitted`), so a
garbage or torn-write `DataLength` reaches the write path intact.

**Failure mode**: a directory entry with a garbage `DataLength` (up to
`u64::MAX`) yields `cluster_count == u32::MAX`. The `collect::<Vec<u32>>()`
demands ~16 GiB and the directory buffer `vec![0u8; ...]` demands far
more. On a 512 MB Pi Zero 2 W the allocation aborts the process. When
`teslafat@0` dies, the gadget LUN keeps `/dev/nbd0` open, the stack
cannot self-heal, and Tesla loses USB visibility and stops recording —
the exact cascade documented in ADR-0020, reached by a different
trigger.

This is reachable from untrusted on-disk bytes: any directory cluster
the host writes is decoded and fed to `handle_child_seen`.

## Decision

**Validate cluster geometry against the data heap at the single funnel
where pending entries are created (`handle_child_seen`), and reject
entries whose cluster run escapes the heap — before any allocation.**

- A new `heap_end_cluster_exclusive()` returns
  `FIRST_DATA_CLUSTER + geometry.cluster_count()` — the first cluster
  index past the heap (exFAT spec §3.1.6).
- A new `contiguous_run_in_heap(first, count)` returns whether
  `[first, first + count)` lies entirely within
  `[FIRST_DATA_CLUSTER, heap_end_cluster_exclusive())`, using
  `checked_add` so an overflowing run is rejected rather than wrapped.
- In `handle_child_seen`, after computing `cluster_count`:
  - empty files (`cluster_count == 0`, not a directory) are allowed
    through unchanged (no run to materialize — they only record a size);
  - `NoFatChain` entries must pass `contiguous_run_in_heap`;
  - FAT-chained entries need only an in-heap *start* cluster (the chain
    itself is already bounded by `MAX_CHAIN_LENGTH` in `try_walk_chain`).
  - On failure the entry is **skipped** (`tracing::warn!` + `return
    Ok(())`), exactly like the existing unresolved-chain skip path.

Because `handle_child_seen` is the *only* insertion point for
`pending_files`, every downstream consumer (`try_resolve_file`,
`ensure_directory_registered`, `grow_directory_if_needed`,
`adopt_directory_chain`) reads an already-validated `cluster_count`, so
no second guard is needed at each `collect`/`vec!` site.

### Why reject, not clamp

Clamping `cluster_count` to the heap end would fabricate a plausible-but-
wrong file whose extents map clusters the entry never legitimately
claimed, risking cluster-map overlaps with real files. A directory entry
that points outside the heap is garbage; the kernel re-emits a corrected
entry on the next directory write (entries are written incrementally and
`handle_child_seen` runs again), so skipping loses nothing real while a
clamp would invent state.

### Why guard in the write path, not the decoder

The decoder's job is faithful structural decoding; it intentionally
surfaces checksum failures without dropping entries so higher layers can
decide policy (see its existing tests). Heap bounds are a property of the
*synthesized volume geometry*, which the decoder does not know. The write
path owns the geometry and the allocation, so the bound belongs there.

### Trade-off acknowledged

A legitimate entry observed mid-write with a transiently out-of-range
`DataLength` is skipped until the corrected entry arrives. This matches
the existing `try_walk_chain` "not yet resolvable → skip and retry"
behavior and is strictly safer than the alternative (abort → recording
stops). The skip is logged with the offending cluster, count, and heap
end for diagnosability.

## Consequences

- **Bounded allocation**: the write path can no longer be driven to a
  multi-GiB allocation by a malformed/garbage directory entry; the
  daemon stays alive and the gadget stays presented.
- **Behaviour change (protocol-facing)**: teslafat now silently skips
  directory entries whose contiguous cluster run leaves the heap. The
  skip is logged and counted via the existing warn path; it is additive
  (previously such entries either OOM'd or produced corrupt extents).
- No IPC/schema change; no new dependency.

## Alternatives considered

1. **Clamp `cluster_count` to the heap.** Rejected — fabricates wrong
   extents and risks cluster-map overlaps with real files (see above).
2. **Cap `clusters_for_data_length` at a fixed maximum (e.g. 1 M).**
   Still allocates a large Vec for a benign-looking but wrong entry and
   does not tie the bound to the real volume geometry. Rejected as a
   magic-number half-measure.
3. **Classify malformed entries in the decoder and stop emitting them.**
   A larger, riskier change to shared `teslausb-core` decoding that
   other consumers (worker indexer, tests) depend on for fidelity.
   Rejected in favor of the localized, geometry-aware guard; the decoder
   keeps emitting faithfully and the write path enforces its own
   invariant.
4. **Return `EIO` to the host on a bad entry.** Tesla's response to a
   mid-write `EIO` is to flag the drive and stop recording — the exact
   failure we prevent. Rejected on safety grounds (same reasoning as
   ADR-0020).

## Validation

- Regression test
  `out_of_heap_data_length_is_rejected_without_unbounded_allocation`
  in `exfat_write.rs`: feeds a file entry with `DataLength == u64::MAX`
  (`cluster_count == u32::MAX`) and a directory entry starting past the
  heap; asserts no extent/directory is registered, nothing is retained
  as pending, and the call returns `Ok` (pre-fix this path allocated a
  `u32::MAX`-element `Vec` and aborted).
- `cargo test -p teslafat --lib` → all pass.
- `cargo fmt -p teslafat -- --check` clean.

## References

- Review report: session `files/exfat-deep-review-20260603.md`
  (BLOCKER 2).
- Code: `rust/crates/teslafat/src/backend/exfat_write.rs`
  (`heap_end_cluster_exclusive`, `contiguous_run_in_heap`,
  `handle_child_seen` guard).
- Related: ADR-0020 (bounded pending-data spill) — same OOM-cascade
  failure surface, different trigger; ADR-0023 (single-LUN partitioned
  disk) — the shared backend that makes any teslafat abort stop
  recording.
