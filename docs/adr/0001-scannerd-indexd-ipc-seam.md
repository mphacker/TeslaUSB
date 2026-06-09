# ADR 0001 — The `scannerd → indexd` IPC seam (privilege/fault isolation of untrusted parsing)

- **Status:** Accepted
- **Date:** B-1 reset
- **Deciders:** B-1 service-layer lane, integrator
- **Scope:** `rust/crates/scannerd`, `rust/crates/indexd`, `deploy/systemd`

This is the first Architecture Decision Record in the repository; it
establishes the `docs/adr/NNNN-title.md` convention (sequential number,
present-tense decision, security rationale, alternatives, consequences).

## Context

TeslaUSB B-1 indexes the car's recorded dashcam media by reading the **raw**
USB mass-storage backing image (`/data/teslausb/disk.img`) directly — `MBR →
exFAT → FAT chain → MP4 → H.264 SEI` — and never mounting the Tesla
filesystem, so it can never interfere with the car's writes (the **#1
invariant**, SPEC.md §2).

The specs split this work across two daemons:

- **`scannerd`** (docs/specs/scannerd.md §2.5) parses the raw bytes,
  stability-gates clips, extracts SEI, groups clips, and **emits facts over
  a local IPC seam**. "scannerd derives nothing about trips/events — it only
  produces facts."
- **`indexd`** (docs/specs/indexd.md §1/§3) **consumes scannerd output**, is
  the **sole SQLite writer**, owns the schema, and derives trips/events.
  Explicitly: **"No raw parsing or SEI decoding (that is scannerd)."**

The implementation had drifted from this design. `indexd`'s process opened
the raw image itself (`INDEXD_IMAGE` → a `BlockReader` over `disk.img`) and
ran the entire untrusted-byte pipeline **in-process**, in the same process
that owns the database, before writing SQLite. The parsing primitives were
correctly reused from the `scannerd` *library*, but the `indexd` *process*
was doing `scannerd`'s job.

This collapses a privilege/fault-isolation boundary. The bytes being parsed
are **attacker-controllable**: a malicious or corrupt exFAT directory, MP4
box tree, or H.264 SEI payload is car-written input that a determined
adversary (or simple corruption) controls. Parsing it in the trusted,
sole-DB-writer process means any parser memory-safety bug, panic, or
resource exhaustion happens **inside the process that owns the durable
database**.

## Decision

Make `scannerd` the process that touches untrusted bytes, and `indexd` a
pure consumer that only ever sees typed, validated facts.

1. **Split the existing `run_scan_pass` along its internal seam.** Everything
   *before* the DB calls (open image → parse → walk → stability-gate → read
   clip bytes → SEI walk → normalize) becomes `scannerd::produce`, which
   returns a versioned `ScanBatch` of facts and owns no DB. The DB calls
   (`upsert/ensure clip → replace waypoints → upsert angle → prune → derive →
   rebuild → commit`) become `indexd::apply`, which writes SQLite in one
   transaction. The in-process composition `run_scan_pass = produce + apply`
   is retained for tests, so parity is structural: the in-process path and
   the cross-process path share the exact same two halves.

2. **Run them as two processes over a Unix-domain-socket seam.** `scannerd
   serve` binds `/run/teslausb/scannerd.sock` and answers one
   `Request::Scan{generation, resync}` per pass by running one `produce`
   pass and streaming back a single length-prefixed JSON `ScanBatch` frame
   (framing matches the `gadgetd` precedent: 4-byte LE length + JSON).
   `indexd` is the client: it owns the 30 s cadence and a monotonic
   generation, reads the batch, and `apply`s it.

3. **`indexd` drops `INDEXD_IMAGE` entirely.** It never opens the raw image
   and never parses raw media. `scannerd` holds the *only* read-only image
   fd.

4. **Recovery is by re-emit / resync, not a durable queue.** `scannerd`'s
   stability tracker lives for its **process lifetime** and is **never reset
   on connect** (a reset would zero the quiescence window so nothing would
   ever emit). On first connect, on reconnect, and after any `apply` failure,
   `indexd` sends `resync=true`; `scannerd` re-arms its emitted flags and
   replays **all currently-stable clips**, recovering any batch that was
   produced (tracker advanced) but not durably committed. This leans on the
   fact that the SQLite DB is fully **rebuildable and idempotent**.

5. **The consumer treats facts as untrusted data.** `apply` deserializes
   typed JSON (data, never code), enforces total/per-field caps
   (records/batch, waypoints/clip, string lengths, a 64 MiB frame ceiling and
   a 64 KiB request ceiling), recomputes trust-sensitive fields
   (`is_front` from the camera angle, `view_kind` from the bucket) rather
   than trusting forgeable wire fields, and prunes **only** when the batch is
   `complete` (a flag set only when every structural parse step succeeded).

## Security rationale

- **Fault isolation.** A parser crash, panic, or memory-safety failure now
  occurs in `scannerd`, a disposable process that owns no durable state.
  systemd restarts it; its in-memory tracker re-accumulates after one
  quiescence window. The database-owning process is never in the blast
  radius.
- **Privilege isolation.** `scannerd` is the *only* process holding a fd on
  `disk.img`, and it holds it **read-only**, at idle I/O priority. For this
  slice it runs as `root` like the other non-gadgetd units — a dedicated
  unprivileged `User=`, cgroup `MemoryMax`, and a `ProtectSystem` sandbox are
  **device-calibrated in migration M5 / Task 7.4c** (matching the existing
  `indexd.service` convention), not set here. The OOM order (SPEC.md §7) is
  encoded now via `OOMScoreAdjust`: `scannerd` (`300`) holds only disposable
  in-memory state and dies before the catalog-owning `indexd` (`-100`), which
  in turn stays well above `gadgetd`, the write-path guardian (`-1000`).
- **Data, not code, crosses the boundary.** The consumer can only ever
  receive a capped, typed, validated `ScanBatch`. A weaponized clip can at
  worst crash/OOM the producer; it can never reach the DB writer.
- **#1 invariant preserved.** `scannerd` opens the image read-only, never
  mounts, and runs at idle I/O priority, so the car's writes always win —
  exactly as the in-process reader did.

## Parity guarantee

Behavior is preserved for the same media: same trips, events, clips, and
waypoints; idempotent re-index; intact prune semantics. This is enforced by:

- the verbatim code-move split (`produce` + `apply` are the two halves of
  the original `run_scan_pass`, unchanged);
- exact `ScanReport` counter reconstruction across the producer/consumer
  boundary (DB-success counters in the consumer, DB-free diagnostics in the
  producer, merged in `run_scan_pass`), including the corner cases (phantom
  no-`started_at` clips, mid-record DB errors, partial-write-then-continue
  with no per-record savepoint);
- a load-bearing test that the final `DeriveWaypoint` is identical on both
  the wire path (`apply_map(wire_waypoint_from_walk(...))`) and the legacy
  path (`waypoint_from_walk(...)`), plus a serde round-trip-before-apply
  test;
- the full `cargo test --workspace` suite staying green.

## Alternatives considered

- **Crash-safe reset-on-connect protocol (first draft).** *Rejected.* Both an
  independent rubber-duck review and a GPT-5.5 second opinion caught that
  resetting the stability tracker per connection zeros `held_secs`, so the
  quiescence gate never fires and **nothing is ever emitted**. The tracker
  must persist for the process lifetime.
- **Durable spool / maildir of batches on disk.** *Rejected.* The DB is fully
  rebuildable and re-emit/resync already recovers every crash path; a spool
  adds disk I/O and flash wear on a 512 MB / SD-card Pi for no correctness
  gain. (`scannerd` is also OOM-killed *first*, so in-memory retry state is
  not durable anyway — recovery has to come from the consumer side, which it
  does.)
- **Fully stateless `scannerd` with stability persisted in `indexd`'s DB.**
  *Rejected.* Stability gating is `scannerd`'s spec'd responsibility;
  persisting fingerprints in the DB would push parse-derived state back
  across the boundary and complicate the schema. The tracker's ephemeral
  in-memory state is legitimately disposable.
- **`indexd` as the socket server.** *Rejected.* `scannerd` is the natural
  server (it owns the resource being read); `indexd` owns cadence + durable
  state and drives. This also matches the `gadgetd` precedent and the
  `indexd After=scannerd` ordering.
- **Binary wire format / systemd socket activation.** *Deferred.* JSON
  matches the `gadgetd` precedent and keeps the contract legible; it can be
  swapped later behind the versioned schema.

## Consequences

- `indexd` no longer reads `INDEXD_IMAGE`; it consumes facts over
  `/run/teslausb/scannerd.sock` (0660, in a 0750 dir). Both daemons run as
  `root` for this slice (a dedicated `teslausb` user is part of the M5
  hardening below).
- `deploy/systemd/scannerd.service` graduates from **STAGED to enabled** (it
  now runs `scannerd serve` and is the IPC producer) and
  `deploy/systemd/indexd.service` drops `INDEXD_IMAGE`, adds
  `INDEXD_SCANNERD_SOCKET`, and gains `After=/Requires=scannerd.service`.
- **Installer wiring (done).** `setup-lib/common.sh` moves `scannerd` from
  `TESLAUSB_STAGED_SERVICES` to `TESLAUSB_APP_SERVICES` (listed before
  `indexd` so the producer restarts before its consumer), so the installer
  now enables/starts `scannerd` alongside `indexd`. The installer test suite
  (`setup-lib/tests/run-all.sh`) stays green.
- **Deferred / follow-up (flagged for the integrator):**
  - **M5 hardening (Task 7.4c).** A dedicated unprivileged `User=teslausb`,
    cgroup `MemoryMax`, and `ProtectSystem`/sandboxing for both daemons are
    device-calibrated in migration M5, matching the existing convention in
    these units. Running `scannerd` as non-root then also requires making
    `disk.img` readable by the `teslausb` user/group.
  - **Exact-UID `SO_PEERCRED` authz.** Desired but not reachable in safe
    stable Rust on the current toolchain (`UnixStream::peer_cred` is
    unstable and the workspace denies `unsafe_code`). Authorization is
    filesystem-permission based (0660 socket in a 0750 dir), matching every
    other local socket in the system including `gadgetd`'s mutate socket. Add
    exact-UID via a vetted safe wrapper if the threat model later warrants.
  - **Pre-existing parser-gate gaps (out of scope — "move behavior, don't
    change the gate"):** a clip marked `emitted` whose bytes then fail to
    read is not retried until its content changes (identical ordering to the
    original `run_scan_pass`); the stability tracker's per-path state grows
    over the process lifetime (identical to the original `indexd`); the
    quiescence window uses wall-clock `SystemTime` rather than a monotonic
    clock; and per-record DB errors tolerate partial writes within the pass
    (no per-record savepoint, matching the original). MP4 content-digest
    stability is likewise an existing gap, not introduced here.
