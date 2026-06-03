# ADR-0027 — In-bounds write/flush faults must never become NBD EIO

- **Status**: Accepted
- **Date**: 2026-06-03
- **Branch**: `b1-userspace-rust`
- **Driver**: Deep exFAT-implementation review (2026-06-03, reconciled
  with a parallel GPT-5.5 second opinion). Finding **MAJOR 3**: a
  backing-disk fault during `apply_write`/`flush` propagated to the
  host as NBD EIO, which makes the Tesla flag the drive and stop
  recording. Standing operator invariant: *the device must ALWAYS
  allow USB writes to TeslaCam when it is powered on.*

## Context

`SynthBackend` (`rust/crates/teslafat/src/backend/synth.rs`) serves
BOTH the FAT32 TeslaCam partition (the car writes continuously) and the
exFAT MEDIA partition through one `BlockBackend`. Its `write`/`flush`
implementations propagated `apply_write`/`flush` errors with `?`:

```rust
state.apply_write(offset, buf)?;            // ENOSPC / EACCES / missing .partial
if flags.contains(WriteFlags::FUA) { state.flush()?; }
```

`ExfatWriteError`/`Fat32WriteError` `From`-convert into
`BackendError::Io`, which the NBD transmission layer returns to the host
as error code 5 (EIO). The Tesla treats any drive EIO as a fault, flags
the device, and stops recording until a vehicle power-cycle — the exact
catastrophic outcome the project exists to prevent.

A second, independent defect surfaced during review: `flush()` did
`self.in_flight_files.drain().collect()` and then `?`-returned on the
first `finalize_with_replace` failure. Every still-pending path (and the
failing one) was thereby removed from `in_flight_files` and never
retried — silent data loss distinct from the EIO issue.

## Decision

**For in-bounds writes, never surface a backing fault as EIO. Swallow
it (log + count) and keep the gadget alive. Keep finalize
state-preserving so a swallowed flush is retried, not lost.**

1. **Boundary swallow (`SynthBackend::write`/`flush`).** `check_bounds`
   still rejects genuinely out-of-bounds requests (a real protocol
   error that cannot occur for a correctly-sized gadget). For in-bounds
   requests, an `apply_write`/`flush` error is routed to
   `tolerate_write_fault`, which increments a `write_faults` counter,
   logs the first fault at `warn` and the rest at `trace` (to avoid
   flooding at host write rate), and returns `Ok(())`. The dropped
   write degrades to zero-fill on read via the tolerant overlay
   (ADR/Phase-A behavior), never to wrong data.
2. **State-preserving finalize.** Both `flush()` implementations now
   iterate best-effort: a path whose `finalize_with_replace` fails is
   re-inserted into `in_flight_files` (retried on the next flush), every
   path is attempted, and the first error is returned for internal
   callers/tests — but the NBD boundary swallows it per (1).
3. **Telemetry.** `SynthBackend::write_fault_count()` exposes the dropped
   fault count for future `system_health` surfacing, so a degraded but
   alive recording path is observable.

### Why swallow rather than surface

The operator invariant ranks "gadget stays alive / recording continues"
strictly above MEDIA write fidelity. A dropped MEDIA write yields at
worst an incomplete media file (read as zero-fill); a surfaced EIO stops
ALL recording. For TeslaCam writes the same logic holds: a corrupt or
truncated clip is strictly better than the car abandoning the drive.

### Why uniform, not transient-vs-permanent

Distinguishing ENOSPC (may clear) from EACCES (won't) at the NBD wire
boundary buys nothing: either, surfaced as EIO, stops recording.
Internally the finalize re-queue already gives transient faults a retry;
permanent faults simply keep being logged and counted.

### Internal-state safety (reviewed)

The rubber-duck flagged the risk that swallowing could preserve a
corrupted write-state machine and serve WRONG reads (worse than EIO).
This is mitigated: data-cluster writes mutate the backing `.partial`
last (so a backing failure leaves in-memory extents pointing at an
incomplete file, which the tolerant overlay reads as zero-fill, not
garbage); and the finalize path is now state-preserving. The remaining
internal errors (cluster-map/dir-tree) were already handled with
warn-and-skip paths before this change.

## Consequences

- **Recording invariant upheld:** a backing ENOSPC/EACCES/missing-partial
  can no longer stop the Tesla from recording.
- **Behaviour change (protocol-facing):** teslafat now silently drops
  in-bounds host writes on backing failure (logged + counted). This
  extends the precedent set by ADR-0020 (bounded pending-data spill —
  the first place the B-1 write path silently dropped host writes) to
  the `apply_write`/`flush` fault path.
- **No IPC/schema change; no new dependency.**

## Alternatives considered

1. **Keep propagating EIO.** Rejected — directly violates the core
   invariant (this is the bug).
2. **Surface only "permanent" faults as EIO.** Rejected — any EIO stops
   recording; the distinction is meaningless at the wire boundary.
3. **Block the write until space frees.** Rejected — blocking the NBD
   I/O loop stalls the car's writes (the failure we prevent), same as
   ADR-0020's reasoning.

## Validation

- `flush_requeues_in_flight_path_when_finalize_fails` regression tests
  in BOTH `exfat_write.rs` and `fat32_write.rs`: seed an in-flight file,
  delete its `.partial` to force `finalize_with_replace` to fail, assert
  `flush()` surfaces the error AND the path remains in `in_flight_files`
  (pre-fix the path was drained and lost → `in_flight_file_count() == 0`).
- `poisoned_locks_recover_instead_of_returning_eio` (ADR-adjacent,
  BLOCKER 1) exercises the same never-EIO boundary for lock poison.
- `cargo test -p teslafat --lib` → all pass; `cargo fmt`/`clippy` clean.

## References

- Review report: session `files/exfat-deep-review-20260603.md` (MAJOR 3).
- Code: `rust/crates/teslafat/src/backend/synth.rs`
  (`tolerate_write_fault`, `write`, `flush`),
  `backend/exfat_write.rs` + `backend/fat32_write.rs` (`flush`).
- Related: ADR-0020 (bounded pending-data spill — silent-drop
  precedent); ADR-0026 (reject out-of-heap dir entries — same review).
