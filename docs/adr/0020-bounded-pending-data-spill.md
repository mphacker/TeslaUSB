# ADR-0020 — Bounded pending-data spill in teslafat write path

- **Status**: Accepted
- **Date**: 2026-05-24
- **Branch**: `b1-userspace-rust`
- **Driver**: 2026-05-24 outage on `cybertruckusb.local` —
  `teslafat@0.service` was OOM-killed twice (RSS 346 MB then
  357 MB on a 512 MB Pi Zero 2 W). When `teslafat@0` died, the
  USB-gadget LUN file kept `/dev/nbd0` open, so `nbd-attach@0`
  could not reattach; after 10 retries systemd refused to restart
  it, and `usb-gadget.service` failed in turn. Tesla lost USB
  visibility and stopped recording dashcam video. Standing
  operator directive applies: *"don't take shortcuts. remember
  that. and don't forget to do code reviews and update documents."*

## Context

Both write-state machines (`backend/exfat_write.rs` and
`backend/fat32_write.rs`) used an unbounded
`HashMap<u32, Vec<PendingDataChunk>>` named `pending_data` to
stash write payloads addressed to clusters whose owning directory
entry / FAT chain had not yet arrived from the host. The pattern
is fundamental to how Tesla writes:

1. Tesla writes data clusters first (large bursts).
2. Tesla then writes the directory entry that names the file.
3. Tesla finally writes the FAT chain linking those clusters.

The materializer cannot replay a data write until the cluster's
owner is known, so the bytes sit in `pending_data` waiting.

**Failure mode**: if Tesla never finalises the file (mid-write
power loss, fsck event, gadget reset, rapid burst that hits a
LUN swap), the orphan clusters stay in the map forever. Each
`push` does `bytes.to_vec()`, so every leaked write is a heap copy.
On a 512 MB Pi, sustained dashcam bursts accumulated 350+ MB of
orphan chunks within hours, triggering the OOM cascade above.

## Decision

Introduce a **shared bounded FIFO spill buffer**
(`backend/pending_spill::PendingSpill`) used by both write-state
machines. Properties:

- **Cap**: 16 MiB total bytes across all clusters
  (`DEFAULT_MAX_SPILL_BYTES`). Generous for healthy Tesla bursts
  (60 s @ ~10 Mbps ≈ 75 MB total, of which only a small fraction
  is unresolved at any instant) and tight enough that worst-case
  daemon RSS stays well under the Pi Zero 2 W's budget.
- **Eviction policy**: FIFO at the cluster level. When
  `total_bytes > max_bytes`, the oldest cluster's entire chunk
  list is dropped and a `tracing::warn!` is emitted with the
  cluster number, evicted byte count, and lifetime totals.
- **Observability**: `evicted_clusters_total`, `evicted_chunks_total`,
  `evicted_bytes_total` counters are exposed on the type for future
  surfacing in `system_health`.
- **Shared module**: the previously duplicated `PendingDataChunk`
  struct is now defined once in `pending_spill.rs`, satisfying the
  charter's no-duplication rule (Pillar 1).

### Why FIFO, not LRU

A cluster that keeps accumulating writes without resolution is
*more* suspicious than one that arrived early and sat quietly.
Promoting on each write would hide a runaway loop. FIFO surfaces
the pathological case in the warn-log first.

### Why drop, not block

The teslafat write path is invoked from the NBD I/O loop. Blocking
on a full spill would stall Tesla's USB writes, which is the exact
failure we are preventing (video loss). Dropping the orphan
candidates (which were never going to materialise anyway, by
hypothesis — that's why they're old) preserves recording continuity.

### Trade-off acknowledged

If a directory entry *does* arrive after its data clusters have
been evicted, the resulting file will be truncated / contain zeros
for the evicted ranges. This is acceptable versus the alternative
(daemon OOM → gadget loss → all recording stops for minutes). The
warning log will make the trade-off visible; tuning the cap upward
is a one-line change if real-world telemetry shows benign clusters
being evicted.

## Consequences

- **Bounded RSS**: teslafat steady-state RSS stays well below the
  OOM threshold under sustained Tesla load.
- **Cascading-recovery gap remains**: this ADR does *not* fix the
  separate problem that when teslafat dies for *any* reason, the
  LUN file keeps `/dev/nbd0` open and the rest of the gadget stack
  cannot self-heal. That is tracked as a follow-up
  (`nbd-attach@.service` needs `ExecStopPost=` to clear the LUN
  file and release the nbd binding).
- **Behaviour change**: this is the first place in the B-1 write
  path that *silently drops* host writes. The behaviour is logged,
  counted, and reversible (raise the cap), but it must be called
  out in PROGRESS and surfaced in any future system_health work.

## Alternatives considered

1. **Cgroup memory cap on `teslafat@.service`** (e.g.,
   `MemoryMax=240M`). Would still kill the process — same OOM
   cascade, just at a lower threshold. Rejected: treats the
   symptom, not the cause.
2. **TTL-based eviction** ("drop chunks older than 5 minutes").
   Requires a clock per chunk, more state. FIFO with a byte cap
   gives the same protection more cheaply.
3. **Per-cluster cap** ("no cluster may hold more than 1 MB").
   Doesn't bound total memory if many clusters leak simultaneously.
4. **Refuse writes when full** (return `EIO` to the gadget).
   Would surface the problem to Tesla immediately, but Tesla's
   response to mid-write `EIO` is unknown and the device would
   likely stop recording entirely. Rejected on safety grounds.

## Validation

- 9 unit tests in `pending_spill.rs`, including
  `regression_2026_05_24_unbounded_growth_does_not_recur`.
- `cargo test --workspace` → all 1342 tests pass.
- `cargo clippy --workspace --all-targets -- -D warnings` clean.
- Hardware validation (post-deploy):
  1. Watch RSS over 30+ min of Tesla driving.
  2. Look for `pending-data spill: evicted` warnings in journal.
  3. If frequent under benign load, raise `DEFAULT_MAX_SPILL_BYTES`
     and re-deploy.

## References

- Incident timeline: `docs/01-PROGRESS.md` Phase P entry.
- Code: `rust/crates/teslafat/src/backend/pending_spill.rs` and
  the call sites in `exfat_write.rs` / `fat32_write.rs`.
- Related: ADR-0018 (LUN-aware cleanup pressure) — same operator
  directive, different subsystem.
