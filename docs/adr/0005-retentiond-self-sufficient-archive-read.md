# ADR 0005 — `retentiond` self-sufficient archive read path (no `indexd`/`scannerd` runtime deps)

- **Status:** Accepted
- **Date:** 2026-06-29
- **Supersedes:** [ADR 0004](0004-retentiond-archive-read-path.md)
- **Scope:** `retentiond` runtime wiring, archive durability markers/outbox, systemd unit isolation

## Incident and problem statement

The ADR-0004 architecture still required two daemon runtime dependencies:

1. archive candidate enumeration from `indexd` SQLite,
2. source-byte reads through `scannerd`'s Unix `ReadFile` socket.

When either daemon was unavailable, `retentiond` could stall even though the backing image and archive storage were healthy. This violated the B-1 resilience goal for archive continuity under partial service outages.

## Decision

`retentiond` is now self-sufficient at runtime:

1. **Candidate seam swap:** `retentiond` enumerates only `TeslaCam/RecentClips` directly from `teslacam.img` using linked `scannerd` library APIs (`parse_mbr`, `parse_boot_sector`, `Volume::new`, exFAT dir decode) and groups `<timestamp>-<camera>.mp4` into clip candidates.
2. **Read seam swap:** `retentiond` uses a local `VolumeReadFileClient` (linked scannerd parser + retentiond `pread` block reader) instead of the `scannerd-read.sock` dependency.
3. **Eligibility invariant:** copy is allowed only when:
   - `valid_data_length == data_length`,
   - `set_checksum_ok == true`,
   - stability gate (`required_stable_scans >= 2`, `quiescence >= 60s`) passes.
4. **Dedup/source identity invariant:** dedup is archive-local and content-addressed using durable markers carrying at least:
   - volume serial,
   - partition,
   - valid data length,
   - set checksum status,
   - destination SHA-256.
5. **Registration durability invariant:** index registration remains best-effort and non-blocking; durable bytes + marker are committed first, and a durable on-disk outbox is the source of truth for pending registrations across restarts.

## Invariants preserved

- No TeslaCam mount path is introduced.
- Per-clip failures are isolated (log + skip/retry/quarantine), not fatal for the daemon loop.
- `--archive-recent-only` and `--no-delete` safety gates remain mandatory.

## Crash isolation and restart behavior

- Marker statuses:
  - `complete_live`: suppress recopy for same source fingerprint,
  - `quarantined`: retry allowed,
  - `partial`: retry allowed.
- Registration failures do not roll back archived bytes.
- Restart replays pending registrations from durable outbox before processing new copies.
- The stability tracker is in-memory; restart resets settle history, so clips re-enter a short re-quiescence window before eligibility.

## Service-level mitigations

The systemd unit is updated to reflect daemon independence and low-priority background behavior:

- no `Wants=`/`After=` dependencies on `indexd`/`scannerd`,
- startup gated only by backing-image/archive availability (`RequiresMountsFor` + `ConditionPathExists`),
- `IOSchedulingClass=idle`, low `CPUWeight`/`IOWeight`,
- `Restart=always` with short retry delay.
