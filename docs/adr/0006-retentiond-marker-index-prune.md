# ADR 0006 — `retentiond` in-memory marker index + bounded marker prune

- **Status:** Proposed
- **Date:** 2026-06-30
- **Builds on:** [ADR 0005](0005-retentiond-self-sufficient-archive-read.md)
- **Scope:** `retentiond` dedup marker lifecycle — restart cost, unbounded growth,
  and the non-destructive re-copy property that makes prune safe
- **Review:** Design reconciled with an independent GPT-5.5 second opinion, which
  rejected an earlier "prune is inherently safety-neutral" framing (see
  *Safety*). The reconciled design adds a copy-path hardening (Phase C) so the
  claim holds unconditionally.

## Problem statement

ADR-0005 introduced per-clip durable dedup **markers**: for every archived
clip-group, `retentiond` writes one JSON file
(`<archive>/.retentiond/markers/<hash16(canonical_key)>.json`) via atomic
temp-file + rename. The dedup gate `marker_is_complete_live` consults that file
every cycle to decide whether a clip is already archived.

This proven mechanism has two efficiency liabilities on the resource-constrained
Pi (one shared microSD with the live recorder):

1. **Per-cycle file I/O for dedup.** Every cycle re-reads + re-parses one marker
   file per live candidate. The live RecentClips buffer holds ~tens to ~hundred
   clip-groups, so dedup costs O(live-window) `read_to_string` + serde parses
   every loop, forever — wasted microSD reads contending with recording.
2. **Unbounded marker growth.** One marker file accumulates per clip-group ever
   archived, with **no prune**. Measured: 98 files / 396 KB after ~1 day; grows
   ~100+/day of driving. Over months/years this bloats a single ext4 directory
   (inode + dir-entry growth) and lengthens the boot-time marker scan.

The operator asked specifically: *"is there a journal … so that upon restart we
can look at that and not have to try looking at each file … make this work well
with low resource utilization."* The markers already **are** that journal; what
is missing is (a) reading them **once** into memory instead of every cycle, and
(b) bounding their count.

## Decision

Keep the per-file markers as the **durable source of truth** (unchanged on-disk
format, unchanged atomic write). Add two things:

### 1. In-memory marker index (Phase A — the operator's primary ask)

`DriverState` gains an index loaded **once** at startup:

```
markers: HashMap<String /* canonical_key */, MarkerSummary>,
markers_loaded: bool,
```

where `MarkerSummary { source_fingerprint: String, status: MarkerStatus,
last_seen_epoch: i64 }`.

- **Lazy load-once**, mirroring the existing `load_outbox_if_needed` /
  `outbox_loaded` pattern: on first use, scan `MARKER_DIR`, read + parse each
  marker file, and insert `canonical_key -> summary`. Files that fail to parse or
  whose `schema`/`canonical_key` don't validate are **skipped** (not inserted) —
  exactly as `read_marker` already rejects them today.
- `marker_is_complete_live` consults the **map** (`status == CompleteLive &&
  source_fingerprint == candidate.source_fingerprint`) — **zero per-cycle file
  reads**.
- `write_marker` writes the **file first** (atomic, as today); only on write
  success does it upsert the map entry. The index therefore can never report
  `CompleteLive` for a clip whose marker isn't durably on disk.
- `last_seen_epoch` is seeded at load time from the marker's `updated_at`.

### 2. Conservative, source-aware prune (Phase B)

The earlier draft pruned on a pure wall-clock grace and assumed re-copy was
harmless. The GPT-5.5 second opinion showed both assumptions were unsafe (see
*Safety*). The reconciled prune is **scan-absence based with a wall-clock floor**,
and only the **physical** candidate source drives it:

- Each `MarkerSummary` tracks `missed_scans: u32` and `last_seen_epoch: i64`.
- On every **successful** candidate enumeration, for each observed
  `canonical_key`: reset its `missed_scans = 0` and set `last_seen_epoch = now`.
  For every map entry **not** observed this scan: `missed_scans += 1`. (A failed
  enumeration is skipped entirely — it must not age markers.)
- A marker is eligible to prune only when **both**
  `missed_scans >= PRUNE_MIN_MISSED_SCANS` (default **40**, ≈ tens of minutes of
  absence at the cycle cadence — robust to clock skew and transient stability-gate
  gaps) **and** `now - last_seen_epoch >= PRUNE_GRACE_SECS` (default **3600 s**
  wall floor, guards against pathologically fast cycles).
- Prune runs **every `PRUNE_EVERY_CYCLES`** (default **5**), not every cycle, and
  removes at most `PRUNE_MAX_DELETIONS_PER_CYCLE` (default **16**) markers per pass
  (file via `fs::remove_file` + map entry), avoiding a delete storm.
- **Source-aware:** prune is only enabled for the deployed `VolumeCandidateSource`,
  which re-offers every physically-present stable clip each scan
  (`volume_source.rs:71-89`). The legacy `SqliteCandidateReader` *filters out*
  already-archived clips (`candidates.rs:114-120`), so an archived-but-live clip
  would falsely age out under it — prune MUST be disabled for that source.

This bounds the resident marker set to ≈ the live-window size (~100) in steady
state, regardless of total clips ever archived, while guaranteeing a marker is
pruned only well after its clip has truly left the live buffer.

### 3. Non-destructive staged-promote copy (Phase C)

For prune to be genuinely loss-safe, re-copying a clip must never destroy bytes
already archived. Today it can: each angle is copied straight to its final path
(rename-over, `live.rs:218`), and a mid-clip failure rolls back by **deleting**
the angles already copied this attempt (`archive_driver.rs:248-258` →
`remove_dest`). If those destinations held a previously-complete archive, the
rollback deletes good footage. (This is a **pre-existing** hazard, reachable today
on any genuine fingerprint-mismatch re-copy; prune would make it more reachable.)

The fix makes a clip copy **all-or-nothing**:

1. Stage every angle into `<archive>/.retentiond/staging/<item>/<file>` (reusing
   `copy_and_hash_dest` to a staging path; hash is computed on the staged bytes).
2. Only after **all** angles stage successfully, **promote** each staged file to
   its final path via a new `ArchiveStore::promote_dest(staging_rel, final_rel)`
   (a rename on the same filesystem, content-preserving so the staged hash stays
   valid).
3. On any staging failure, **discard** the staged files (`remove_dest` on the
   staging paths) and write a `Partial` marker. **No final destination is ever
   touched**, so a previously-complete archive is never damaged.

A crash mid-promote leaves a clip with a mix of new-complete and old-complete
individual angle files (never a truncated file, since each was fully written +
synced before any promote) — self-heals on the next pass. Orphaned staging files
from a crash are harmless (outside the archive, unregistered) and are best-effort
cleaned at startup.

## Why pruning is now safe

With Phase C, a marker is **only a dedup hint** and removing one cannot lose
footage:

- It never deletes archived footage or a source clip (retentiond runs
  `--no-delete`; markers and staging live under `.retentiond/`, not the archive
  items).
- It never *prevents* archiving. Pruning a marker can at most cause a clip to be
  re-evaluated and **re-copied** — now a **non-destructive** operation (Phase C)
  bounded to `MAX_COPIES_PER_CYCLE` (4) per cycle.
- Disk remains the source of truth; a crash between file-delete and map-update (or
  the reverse) self-heals on the next boot, which rebuilds the index from whatever
  marker files survived. Surviving-file/missing-map → safe re-archive;
  deleted-file/surviving-map cannot persist (the map is in-memory, gone on
  restart).
- The conservative scan-absence + wall-floor trigger means prune targets only
  clips that have demonstrably left the live buffer, so in practice the re-copy
  path is not even reached for pruned clips.

The prune's only residual cost is **wasted re-copy I/O** if the trigger is too
aggressive — never data loss, never corruption, never serving wrong bytes.

## Alternatives considered

- **Single compacted journal file** (replace N marker files with one JSON map or
  append-only log loaded once at boot). Rejected: it discards the just-proven
  per-file atomic isolation (one bad write risks **all** dedup state instead of
  one clip's), enlarges the change to the no-loss path, and the in-memory index
  already delivers the "read once at restart" property the operator wanted without
  touching the durable write path.
- **Pure wall-clock prune grace, assuming idempotent re-copy.** Rejected after the
  GPT-5.5 review surfaced the destructive-rollback path above; replaced by the
  scan-absence trigger + Phase C copy hardening.
- **Index only, defer prune.** Considered (it delivers the per-cycle I/O win with
  zero added risk) but the operator elected to also bound growth now and fix the
  pre-existing copy hazard in the same change.

## Invariants preserved

- On-disk marker format, schema, and atomic write are unchanged.
- `marker_is_complete_live` semantics are identical (status + fingerprint match);
  only the **source** of the lookup changes (map instead of file).
- Index is a cache: files are authoritative; the index is rebuilt from files on
  every start, never trusted across a restart.
- Dedup never returns `CompleteLive` unless a matching durable marker exists.
