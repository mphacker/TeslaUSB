# ADR 0002 — `uploadd` cloud-upload backend: rclone subprocess vs. native Rust

- **Status:** Proposed
- **Date:** B-1 reset
- **Deciders:** TeslaUSB B-1 service-layer lane, integrator
- **Scope:** `rust/crates/uploadd`, `deploy/systemd/uploadd.service`,
  `docs/specs/uploadd.md`, `rust/crates/uploadd/src/transfer.rs`

This ADR resolves the **"choose at build"** open question in
[`docs/specs/uploadd.md` §2.2](../specs/uploadd.md) and the `svc-uploadd-decide`
tracker item in [`docs/tasks/plan.md` §9 / §11.3](../../tasks/plan.md). It was
also acknowledged in `rust/crates/uploadd/src/transfer.rs` (the `Uploader`
seam module doc, which carries a provisional forward-recommendation to the
integrator). The decision is: **shell out to `rclone` as the initial transfer
backend, keeping the `Uploader` trait as the swap seam.**

---

## Context

`uploadd` must transfer archived dashcam footage from the Pi-side archive
directory (`/srv/teslausb/archive/`) to a user-configured cloud remote. The
daemon runs on a **Raspberry Pi Zero 2 W: aarch64, 512 MB RAM, glibc 2.41**
([`SPEC.md §1`](../specs/SPEC.md)).  It is the **most-disposable service** in
the system — `OOMScoreAdjust=900`, killed first in the OOM order
(`uploadd → wifid → webd → scannerd → retentiond → indexd → NEVER gadgetd`,
[`SPEC.md §7`](../specs/SPEC.md)) — and its cgroup `MemoryMax` is
deliberately the tightest of the seven daemons.

The transfer layer is fully abstracted behind the `Uploader` trait in
`rust/crates/uploadd/src/transfer.rs`:

```rust
pub trait Uploader {
    fn put_chunk(&self, remote_key: &str, offset: u64, data: &[u8])
        -> Result<(), TransferError>;
    fn finalize(&self, remote_key: &str, total_bytes: u64)
        -> Result<ContentHash, TransferError>;
}
```

The transfer backend selection is already encoded as a `TransferBackend` enum
(`Rclone` | `RustUploader`); no choice is silently defaulted. The WiFi throttle
"belt" (`uploadd::throttle::Pacer` — a pure-integer token bucket) and the wifid
`tc` "braces" are fully implemented and are independent of which backend is
chosen (`docs/specs/contracts/wifi-upload-throttle.md §2`).

The **reference Python implementation** already uses rclone throughout:
`cloud_rclone_service.py`, `cloud_oauth_service.py`. The `SPEC.md §3` system
diagram explicitly labels the cloud path `Cloud[(rclone remote)]`.

### Options under consideration

**Option A — `rclone` subprocess.** Shell out to the `rclone` binary.
`RcloneUploader` stages the archive file (already on local disk) and drives
`rclone copyto --checksum --bwlimit` per transfer, capturing the exit status
and remote digest.

**Option B — Native Rust uploader.** Use `object_store`, `opendal`, hand-rolled
`reqwest`/`tokio` provider clients, or a combination, to implement
multipart/resumable uploads entirely in-process.

---

## Decision

**Use rclone (Option A) as the initial transfer backend.**

The `Uploader` trait seam is retained unchanged so a native Rust backend can
replace it in a future ADR without touching the engine, queue, throttle, or
durability logic (`rust/crates/uploadd/src/transfer.rs`).

The implementation:

1. **`RcloneUploader` wraps `rclone copyto --checksum --bwlimit`.**
   `put_chunk` writes incoming chunks into a scoped staging file under the
   configured staging path (already under `retentiond`'s space accounting).
   `finalize` invokes `rclone copyto --checksum <staging> <remote>:<key>`,
   parses the remote-computed digest from rclone's output, cleans up the
   staging file on both success and error, and returns `ContentHash` to
   `verify_digest` in the engine. The staging file is bounded in size by the
   archive item itself; no unbounded buffering.

2. **Bandwidth throttle — belt and braces preserved.**
   `rclone --bwlimit` is set to the current `ThrottleState.max_tx_bytes_per_s`
   published by `wifid` (`wifi-upload-throttle.md §2`). The `Pacer`
   token-bucket in `uploadd::throttle` remains the primary self-pacer (the
   "belt"); `wifid`'s kernel `tc` cap is the hard "braces". The `--bwlimit`
   flag gives rclone its own belt-level assurance so even mid-chunk bursts stay
   bounded inside the rclone process. The chunk ceiling (`max_chunk_bytes`) is
   enforced by `Pacer::try_consume` before any bytes reach rclone.

3. **Resumable transfers.** rclone's provider-specific multipart resume
   semantics (S3 multipart, B2 large-file, etc.) handle mid-transfer restarts
   natively. The `uploadd` queue persists the staging path; on restart the
   engine re-presents the same staging file and rclone resumes or restarts
   cleanly without duplication.

4. **Checksum integrity.** `rclone copyto --checksum` instructs rclone to
   verify the uploaded object's remote digest. `finalize` passes that digest to
   `verify_digest` for comparison against the local `ContentHash` computed at
   archive time (`uploadd.md §2.2`). A mismatch returns `Integrity::Corrupt`
   and the engine requeues without flagging durability.

5. **Child-process lifecycle under the WiFi pause protocol.** On an
   `AbortResumeLater` signal from `wifid` (SDIO chip recovery — the bus is
   wedged), `uploadd` sends `SIGTERM`/`SIGKILL` to the active rclone child
   process and returns the item to the queue. The wifid `tc` braces ensure no
   further TX is possible even if the signal races. The staged file is retained
   for the next attempt.

6. **rclone installed by `setup.sh`** as a pinned aarch64 binary from
   `rclone.org` (or via apt), alongside the Rust service binaries. Its
   filesystem path is injected into `UploaddConfig` and never hardcoded in the
   crate. The `uploadd.service` unit (currently `STAGED`) must add a
   `LoadCredential=` for the rclone config path when graduated to
   `TESLAUSB_APP_SERVICES`.

7. **`TransferBackend::Rclone`** is the explicit selection in
   `uploadd/src/main.rs` when the live binary is wired up;
   `TransferBackend::RustUploader` remains a reserved variant with no
   implementation yet.

---

## Rationale

### 1. Provider breadth is the binding constraint

Existing TeslaUSB users configure a wide variety of remotes: Amazon S3 and
S3-compatible stores (Wasabi, Cloudflare R2, MinIO), Backblaze B2, Google
Drive, Microsoft OneDrive, Dropbox, SFTP, WebDAV, and others. The reference
Python code (`cloud_rclone_service.py`, `cloud_oauth_service.py`) has always
relied on rclone for this reason.

No single mature Rust crate matches this breadth today:

| Crate | Providers | Notes |
|---|---|---|
| `object_store` (v0.12) | S3-compatible, Azure Blob, GCS, local | No B2, no Drive, no OneDrive |
| `opendal` (v0.52) | ~40 backends | No first-class B2; large dep tree; experimental |
| `aws-sdk-rust` | S3 only | +~15–20 MB aarch64 binary; S3-only |
| rclone | 70+ remotes | Battle-tested; handles OAuth, B2 large-file, GDrive, etc. |

Backblaze B2 is the most popular budget cloud target in the existing user base.
It is absent from every mature Rust object-storage crate. Implementing B2,
Google Drive OAuth 2 PKCE, OneDrive, Dropbox, and SFTP natively would be a
multi-sprint effort with an indefinite ongoing maintenance surface on a
volunteer-maintained project.

### 2. Memory footprint on a 512 MB Pi is acceptable

The `MemoryMax` concern is real but does not favor the native option in
practice:

- **rclone is transient.** The child process exists only during an active
  transfer and exits when `rclone copyto` completes. Between transfers `uploadd`
  idles at a few MB of stack + queue state. Peak concurrent RSS in the uploadd
  cgroup ≈ `uploadd RSS (~10–20 MB) + rclone RSS (~40–60 MB during an active
  transfer)` ≈ 60–80 MB total. This easily fits in a ≥128 MB `MemoryMax`
  ceiling to be calibrated at M5 / Task 7.4c.

- **A full multi-provider native implementation is not materially smaller at
  scale.** Bringing in tokio + reqwest + TLS + provider-specific SDKs for S3,
  B2, GDrive, and OneDrive would add 20–35 MB of in-process binary and heap
  during a transfer, narrowing the gap significantly while still not covering
  the full provider matrix.

- **uploadd is first in the OOM kill order (`OOMScoreAdjust=900`).** If the Pi
  runs low on RAM during an active upload, the kernel kills uploadd — and its
  rclone child, which inherits the cgroup — first. This is the correct
  behavior: the upload queue is durable and the transfer resumes on restart.
  The native option does not improve this; the OOM kill order is the same.

- **The `MemoryMax` value** for uploadd's cgroup is set in migration M5 /
  Task 7.4c after on-device calibration. The recommended floor is **≥128 MB**
  to cover the rclone child during an active transfer; this still leaves
  ≥380 MB for the remaining six daemons, the kernel, and OS overhead.

### 3. SDIO deadlock safety is fully preserved

The SDIO deadlock concern (`SPEC.md §9 #4`, `wifid.md §2.3`) is addressed by
the belt-and-braces model already implemented independently of backend choice:

- `uploadd`'s `Pacer` token-bucket caps the rate at which bytes are consumed
  from the staging file before any are handed to rclone.
- `rclone --bwlimit` provides a second, rclone-internal belt against its own
  burst behavior.
- `wifid`'s kernel `tc` egress cap is the hard, kernel-enforced braces.
- On `ChipRecovery` / `AbortResumeLater`, `uploadd` terminates the rclone
  child immediately (`SIGTERM`/`SIGKILL`); the wifid `tc` cap simultaneously
  blocks further TX regardless.

All three layers are independent of whether the backend is rclone or native
Rust. Choosing rclone introduces no new SDIO risk.

### 4. Parity with the reference implementation

The behavioral reference for `uploadd` is `cloud_rclone_service.py`,
`cloud_oauth_service.py`, and `cloud_cleanup.py` (`uploadd.md §1`). All three
delegate to rclone for transfer, auth, and remote-retention operations. The
`SPEC.md §3` system diagram labels the cloud target `Cloud[(rclone remote)]`.
Matching the reference directly minimizes behavioral divergence and makes the
parity acceptance criteria (`uploadd.md §4`) straightforward to verify.

### 5. The `Uploader` seam protects future reversibility

The `Uploader` trait was designed as an explicit swap point
(`transfer.rs:57–73`). If binary-size constraints tighten further, the project
narrows to a small fixed provider set (e.g. S3/B2 only), or a mature
multi-provider native Rust library emerges, a `NativeUploader` implementing the
same trait can replace `RcloneUploader` in a future ADR with zero changes to
the engine, queue, throttle, or durability logic. The on-disk cost of rclone
(~60 MB aarch64 binary) is the trigger condition for that follow-up; if the
full card-space budget (`SPEC.md §9 #8`) cannot accommodate it, the integrator
escalates before M5.

---

## Credential handling and security

- **rclone config** (containing OAuth refresh tokens, provider secrets) is
  stored at a root-only `0600` path (`/etc/teslausb/rclone.conf`), consistent
  with `SPEC.md §7` and `webd.md §3.1`. It is injected into `uploadd.service`
  via `LoadCredential=`; never world-readable, never logged, never in the SPA
  bundle or on the Tesla volume.
- `webd` reads cloud status and queue state **from `uploadd` via IPC
  (`/run/teslausb/uploadd.sock`)** and forwards to `/api/cloud/*`
  (`webd.md §2.4`). It never reads the rclone config directly.
- **Initial OAuth flows** (`rclone authorize`) are driven once at setup time
  through a `webd`-mediated flow (the `be-cloud-config` work item) and write
  the resulting token to the restricted config path. Thereafter `uploadd` uses
  rclone non-interactively.

---

## Progress surfacing to `webd`

The `upload_queue` SSE event (`{queued, in_progress, done, failed, current}`,
`webd.md §3`) is produced from `uploadd`'s SQLite queue state, updated at
each engine step — independent of the transfer backend. rclone's `--progress`
flag can optionally be parsed to provide finer-grained byte-level progress
within a single file, but the queue-level granularity is sufficient for parity
with the existing cloud-archive UI (`cloud_archive.py`). Finer progress is a
nice-to-have enhancement, not a gate.

---

## Alternatives considered

- **`object_store` (S3/GCS/Azure only).** *Rejected for initial deployment.*
  Covers only three providers and lacks Backblaze B2, the most popular
  budget target among existing users. Viable as a future `Uploader`
  implementation if the project decides to support only these three providers
  in a binary-size-optimised track. Not ruled out permanently.

- **`opendal`.** *Rejected.* Covers more providers than `object_store` but
  introduces a very large dependency tree (multiple async runtimes, openssl/ring
  duplication) that conflicts with the workspace philosophy of small,
  single-purpose binaries (`SPEC.md §10` — "Introducing a new heavyweight
  dependency … ASK FIRST"). B2 support is experimental; OAuth flows are not
  managed. Revisit if it matures and slims significantly.

- **Hand-rolled provider clients (reqwest + tokio per provider).** *Rejected.*
  Re-implements everything rclone already provides; creates a permanent
  per-provider maintenance obligation for every OAuth token-refresh cycle, API
  version bump, and multipart protocol change. The cost/benefit is
  unambiguously negative on a volunteer-maintained project.

- **rclone called via stdin pipe (`rclone rcat`) without staging.** *Considered
  as an integration variant; staging-file model preferred.* Streaming via
  `rclone rcat` avoids a temporary file but prevents rclone from using
  provider-native multipart resume semantics (rcat is not resumable mid-stream).
  The staging-file approach lets rclone's native resume work correctly after an
  `AbortResumeLater` kill/restart; the staging cost is bounded by the size of
  the archive item being transferred, which is already on-disk.

---

## Consequences

- **Installer (`setup.sh`).** Must install `rclone` for aarch64 (apt package
  or pinned binary from `rclone.org`). The binary (~60 MB on-disk) is added to
  the release artifact checklist alongside the Rust service binaries. A
  first-boot preflight check must confirm rclone is present and executable
  before enabling `uploadd`.

- **`uploadd.service` graduation from STAGED.** When moved from
  `TESLAUSB_STAGED_SERVICES` to `TESLAUSB_APP_SERVICES`:
  - Add `LoadCredential=rclone.conf:/etc/teslausb/rclone.conf`.
  - The `MemoryMax` cgroup value (set in M5 / Task 7.4c) must be calibrated on
    hardware to cover both the uploadd process and its rclone child process
    (recommended floor: **128 MB** pending measurement).
  - The `OOMScoreAdjust=900` is unchanged; uploadd remains the first OOM kill
    candidate.

- **Task 6.3 — `RcloneUploader` implementation.** Implement the `Uploader`
  trait using `rclone copyto --checksum --bwlimit`, with staging-file lifecycle
  management, child-process SIGTERM on `AbortResumeLater`, and digest extraction
  from rclone's output. Wire it as `TransferBackend::Rclone` in
  `uploadd/src/main.rs`. Unit-test with a mock rclone binary that can simulate
  failure mid-transfer, wrong digest, and clean success.

- **`TransferBackend::RustUploader`** remains in the enum as a reserved variant
  with no implementation. Its doc comment already marks it as the future swap
  point (`transfer.rs:44–48`).

- **Deferred / follow-up (flagged for the integrator):**
  - **Binary-size / card-budget audit (M5).** After the card-space-budget
    measurement (`SPEC.md §9 #8`), confirm rclone's ~60 MB fits within the
    `card_total ≥ disk.img + OS + archive + reserves` budget. If it does not,
    escalate for a provider-narrowing decision (a native `object_store` backend
    for S3/B2 via the `Uploader` seam is the most likely path).
  - **Native Rust backend ADR (future).** If the project narrows its required
    provider set or a mature multi-provider Rust crate emerges, the `Uploader`
    seam makes migration surgical — only the `RustUploader` impl and the
    `setup.sh` rclone dependency change; the engine, queue, throttle, and
    durability logic are untouched.
  - **rclone OAuth flow UX (`be-cloud-config`).** How `webd` drives
    `rclone authorize` for the initial provider setup (subprocess + redirect or
    a `webd`-proxied OAuth callback) is a `webd`↔`uploadd` IPC detail to be
    finalized in Task 6.3 / `be-cloud-config`, within the secrets model of
    `webd.md §3.1`.
  - **rclone version pinning.** The rclone binary version should be pinned in
    `setup.sh` and the release manifest to ensure reproducible installs.
    Provider API changes in rclone minor versions can change `--checksum`
    output formats; pin and test before releasing upgrades.
