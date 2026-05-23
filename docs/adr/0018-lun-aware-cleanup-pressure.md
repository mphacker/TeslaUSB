# ADR-0018 — LUN-aware cleanup pressure

- **Status**: Accepted (supersedes ADR-0012)
- **Date**: 2026-05-23
- **Branch**: `b1-userspace-rust`
- **Driver**: 2026-05-23 outage on `cybertruckusb.local` —
  `teslafat@0.service` crash-looped with `exFAT cluster
  allocator out of capacity: requested 205, available 154`
  while the worker reported "no free-space pressure". Operator
  had to delete 28.7 GB of RecentClips by hand to recover.
  Standing operator directive applies: *"don't take shortcuts.
  remember that. and don't forget to do code reviews and update
  documents."*

## Context

The B-1 cleanup subsystem has two pressure-triggered paths,
both of which need to know "how full is the volume Tesla sees":

1. **`cleanup::Cleanup::measure_pressure`** — gates the
   age-broadening behaviour. When free is below
   `min_free_pct` (from `worker.toml`), the cutoff widens to
   `now`, making every no-GPS RecentClips clip immediately
   eligible regardless of `retention_days`.

2. **`cleanup_sweep::sweep_to_target`** — the tier-aware
   continuous sweep introduced in AC.5/AC.7. Runs until
   either every Tier-A/B/age-Tier-C candidate is deleted or
   free reaches `storage_config.cleanup.target_free_pct`.

Both paths historically called `statvfs(3)` on `backing_root`
(`/srv/teslausb/teslacam`). That measurement reports the
**host SD-card root filesystem's** free space — NOT the
LUN-visible fill.

### Why this is wrong

`teslafat` synthesises a fixed-size LUN of
`volume_size_gb × 1 GiB` and presents it to the Tesla USB
gadget driver as a single exFAT volume. The backing tree under
`backing_root` is what fills that LUN. The SD card holding the
backing tree is BIGGER than the LUN (the live device has a
470 GiB SD with a 256 GiB LUN), so the two free-space metrics
are unrelated:

| | SD root (statvfs) | LUN visible to Tesla |
|---|---|---|
| Capacity | 470 GiB | 256 GiB |
| Used | 290 GiB (62%) | 266 GiB (104%!) |
| Free | 176 GiB (38%) | **0 GiB (overflow)** |

The 2026-05-23 outage was the exact pathology: cleanup
reported 38% free → "no pressure" → no broadening, no sweep.
teslafat's exFAT cluster allocator then ran out of clusters
mid-write and crash-looped.

### Why the existing tier-A sweep didn't save us

`sweep_to_target` uses the SAME statvfs measurement and would
have triggered for the same reason — except in this case it
also auto-tunes a tiny target like 0.5% based on
`measure_total_bytes(backing_root)` (the SD card's reported
total), so even when it did wake up it thought "we're 38% free
of 470 GB, way past the 0.5% target, no-op." Bug compounded.

## Decision

Replace every `statvfs(backing_root)` call with a LUN-fill
measurement:

```text
lun_used_bytes  = sum of file sizes recursively under backing_root
lun_size_bytes  = storage_config.storage.teslacam_gb * 1 GiB
lun_free_pct    = (lun_size_bytes - lun_used_bytes) / lun_size_bytes * 100
                  (saturated to 0..100)
```

* New module `lun_pressure.rs` owns this computation as three
  pure-ish helpers: `lun_size_bytes(u32) -> u64`,
  `lun_used_bytes(&Path) -> io::Result<u64>` (recursive
  `std::fs::read_dir` walk), and `lun_free_pct(used, size) ->
  f64` (pure math).
* `Cleanup::run_once` and `sweep_to_target` / `sweep_to_target_now`
  gain a `lun_size_bytes: u64` parameter. The supervisor loads
  `StorageConfig` once per tick and passes the resulting size
  to both phases so they agree.
* Passing `lun_size_bytes = 0` disables LUN-aware pressure
  (the cleanup floor short-circuits, the sweep no-ops). This
  is the safe back-compat path when `/etc/teslausb/teslausb.toml`
  is absent or unparseable.
* All measurement code is now platform-agnostic
  (`std::fs::metadata` + `read_dir`), so the dev-workstation
  test suite exercises the same code path as the live Pi —
  no more `cfg(target_os = "linux")` gate around the
  pressure measurement, no more `Ok(100.0)` stub.

### Source of truth for LUN size

`storage_config.storage.teslacam_gb` is canonical. The AC.3
resize helper writes `teslafat-0.toml`'s `volume_size_gb` from
that same field, so the worker and `teslafat` are always
reading consistent values (one config, one round-trip via the
web UI).

### Why a `du`-style walk is acceptable

The live backing tree is ~6,000 files. On the Pi Zero 2 W,
`std::fs::read_dir` + `Metadata::len` traverses that in roughly
50-200 ms with warm cache, ~1-2 s cold. We already do a similar
O(N) walk every tick in `Cleanup::gc_orphans` (`list_all_clips`
+ `try_exists`), so adding a second walk for the size sum
roughly doubles the work — still well within the 5-minute
cleanup tick budget. If the device ever grows to 100k+ files
we'll add incremental size tracking via the indexer, but that
would be premature optimisation today.

### Why not extend `teslausb-core::storage_config`

Considered but rejected. `storage_config.rs` already lives in
the worker crate and the Flask web has its own
`storage_config.py` mirror. Promoting it to `teslausb-core`
would force the worker to depend on a config schema that the
web crate also imports through FFI — wide blast radius for a
narrow fix. The current shape (worker reads its own copy of
the schema, fields match the Python one by review) stays.

## Alternatives considered

### A. Status quo: keep `statvfs` measurement

* **Pros:** No code change.
* **Cons:** Loses videos. Already did. Rejected.

### B. Add a periodic `du -sb backing_root` snapshot

Cache the result for, say, 30 seconds.

* **Pros:** Cheaper than per-tick walk if the tick rate
  exceeds 1/30s.
* **Cons:** Worker tick is 5 minutes; the cache adds zero
  value and one source of staleness bugs. Rejected.

### C. Have `teslafat` report fill back to the worker over IPC

Add a new IPC message `LunFillQuery → LunFillResponse`.

* **Pros:** Single owner of LUN state. No worker-side
  filesystem walk.
* **Cons:** Adds an IPC dependency to the cleanup loop. If
  `teslafat` is wedged (which is what the outage looked like),
  the cleanup loop can't make progress either — same outcome
  as today. Rejected for now; revisit if walk cost ever becomes
  measurable on the Pi.

### D. Track size deltas incrementally in the indexer

The indexer already runs on every CLOSE_WRITE; it could
maintain a running `total_bytes` counter in SQLite and bump it
on every record/delete.

* **Pros:** O(1) read at cleanup time.
* **Cons:** Drifts if anything writes the backing tree
  out-of-band (the `recovery-deleted-*.txt` audit shows
  exactly that — the operator deleted files by hand during the
  outage). Reconciling drift requires the same full walk we'd
  do anyway. Deferred.

## Consequences

* **Bug fix:** cleanup now triggers when Tesla actually
  approaches a full LUN, not when the SD card approaches full.
  The 2026-05-23 outage shape becomes self-healing.
* **Removed dependency:** `rustix` is no longer used (it was
  only there for `statvfs(3)`). Cargo lock and `Cargo.toml`
  shrink one entry. **Supersedes ADR-0012**, which selected
  `rustix` for the statvfs path that no longer exists.
* **Tests:** new pure tests in `lun_pressure::tests` cover
  the 256 GiB / 257 GiB overflow scenario the operator hit;
  new tests in `cleanup::tests` and `cleanup_sweep::tests`
  cover the `lun_size_bytes = 0` no-op path.
* **Cross-platform tests:** the dev workstation now exercises
  the real pressure path (it used to short-circuit on
  non-Linux). One less platform-conditional behavioural
  difference between dev and Pi.
* **Performance:** one extra recursive directory walk per
  cleanup tick (the existing `gc_orphans` already does the
  equivalent). At current scale this is unmeasurable. If
  scale grows by 100×, revisit alternative D.
* **Operator action required:** `/etc/teslausb/teslausb.toml`
  must exist and have a sane `[storage].teslacam_gb` — which
  is the case on the live device today. Devices that have not
  yet been initialised will see `lun_size_bytes = 0` (default
  StorageConfig) and pressure-based sweeping will be disabled
  on those devices, falling back to pure age-based retention.
  This is the back-compat shape AC.1 already documented.
