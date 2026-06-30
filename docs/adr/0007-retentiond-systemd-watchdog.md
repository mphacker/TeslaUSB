# ADR-0007 — retentiond systemd service watchdog (hang detection)

> Note: this is a systemd **service** watchdog (`sd_notify("WATCHDOG=1")`), not
> the hardware `/dev/watchdog` timer. "Watchdog" below always means the former.

- Status: Accepted
- Date: 2026-06-30
- Supersedes: none (extends the resilience posture established alongside ADR-0005/0006)
- Tier: 3 (no-loss recording path; live-hardware deploy)

## Context

The operator mandate is binding: **"the archiver should not be dependent on
anything else and should always be operational."** `retentiond` is the no-loss
recording path — it copies dashcam clips off the car-visible LUN into the Pi-side
archive. Losing footage is the worst outcome.

The code is already self-sufficient (no `indexd`/`scannerd` runtime dependency,
ADR-0005) and the marker journal makes restart cheap and bounds growth
(ADR-0006). The `retentiond.service` unit already has `Restart=always`,
`RestartSec=5s`, idle I/O (`IOSchedulingClass=idle`, `IOWeight=10`),
`CPUWeight=10`, `OOMScoreAdjust=100`, `LimitCORE=0`, and `panic=abort`.

**The remaining gap:** `Restart=always` only catches a process that *exits*
(crash/panic). It does NOT catch a process that is *hung* — deadlocked,
livelocked, or wedged in a blocking syscall that never returns. A hung archiver
keeps the service `active` while silently archiving nothing, which is exactly the
silent-footage-loss failure mode the self-sufficiency work targets.

A systemd service watchdog closes this gap: the daemon must periodically send
`sd_notify("WATCHDOG=1")`; if it fails to within `WatchdogSec`, systemd treats
the service as failed, kills it (SIGABRT), and `Restart=always` restarts it. On
restart the daemon re-enumerates RecentClips and resumes from durable markers
(ADR-0006), so a restart loses no footage.

This design was reviewed by a parallel GPT-5.5 Tier-3 second opinion
(APPROVE-WITH-CHANGES); its corrections are folded in below.

## Decision

### 1. systemd unit (`deploy/systemd/retentiond.service`)

Keep `Type=simple` and `Restart=always`. Add:

```
WatchdogSec=240
NotifyAccess=main
```

- `Type=notify` / `READY=1` are **NOT** required: `WatchdogSec=` arms the
  watchdog and sets `NOTIFY_SOCKET` independently of the service type; `READY=1`
  is only consumed by `Type=notify`. `NotifyAccess=main` lets the main process
  send notifications.
- Keep `Restart=always` (not `Restart=on-watchdog`): a watchdog timeout is a
  failure, and `always` already restarts on failure; this also keeps crash and
  hang recovery on one policy.

### 2. `WatchdogSec = 240s`

The tension: too short → **false kills** when `retentiond` is legitimately
blocked in a slow copy/hash/fsync syscall under idle-I/O starvation (the car is
writing and retentiond's idle I/O class yields); too long → slow hang detection.

A single camera-angle `.mp4` is tens of MB; under SD writeback pressure +
idle-I/O starvation, one file's copy+hash+fsync+rename can plausibly stall
80–200s. The copy is now petted internally (per-window read pets, per-block hash
pets, boundary pets before fsync/rename), so no single *un-petted* span
approaches that figure — only an individual blocking syscall (one `sync_all`,
one `read` window) could, and a single such syscall exceeding the deadline means
effectively-dead storage. `60s`/`120s` are unsafe/borderline; `180s` is
defensible for the petted design, but a Tier-3 deploy-plan review re-derived the
~200s worst-case whole-op figure, so we set **`240s`** to clear it with ~20%
margin. The cost is negligible: a true hang is still detected within ~4 min
(plus `TimeoutStopSec` ≈ 90s to recover ≈ 5.5 min total), far inside Tesla's
multi-hour RecentClips buffer, while false kills become even less likely. A
false kill is bounded anyway (copies are non-destructive staged-promote,
ADR-0006 — no footage loss).

### 3. Process-global `watchdog` module (best-effort, rate-limited)

New library module `rust/crates/retentiond/src/watchdog.rs`:

- `init()` — once, at startup, reads `NOTIFY_SOCKET`, `WATCHDOG_USEC`,
  `WATCHDOG_PID` from the environment into a `static OnceLock<Option<Watchdog>>`.
  Enabled only if `NOTIFY_SOCKET` is set AND (`WATCHDOG_PID` unset OR equal to
  our PID). Parses the socket address as a filesystem path or an abstract socket
  (leading `@` or NUL) via `std::os::linux::net::SocketAddrExt::from_abstract_name`.
  Derives the minimum pet interval from `WATCHDOG_USEC / 2` (systemd's
  recommended cadence), clamped to a sane floor (~1s) so per-chunk calls are cheap.
- `pet()` — best-effort keepalive callable from anywhere, any thread: if enabled
  and at least the min interval has elapsed (tracked via an `AtomicI64`
  last-pet-millis with a compare-and-set), send the `WATCHDOG=1` datagram. No-op
  if disabled, not yet elapsed, or the send errors. **Never panics.**
- Non-unix builds: `init()`/`pet()` are no-ops so host-platform `cargo build`
  and the pure unit tests stay green.

Why a process global rather than threading a callback through the `ArchiveStore`
trait and the chunked-I/O helpers: there is exactly **one** notify socket per
process, so the watchdog is genuinely process-singleton state. A global keeps the
`ArchiveStore` trait and `read_full_file_to_writer`/`hash_reader_sha256`
signatures unchanged and lets the deep copy/hash loops pet without plumbing. The
tradeoff (ambient state, slight coupling of the read/hash helpers to the watchdog
module) is accepted and localized; the env-parsing logic is extracted into a pure
`Config::from_env_parts(...)` for unit testing without real sockets.

### 4. Pet placement (closing the false-kill gap)

`pet()` is called:

- once at startup (after `init()`),
- inside the per-candidate `on_progress` callback (already exists),
- at the end of every cycle (both `Ok` and `Err` arms),
- on every 1-second tick of `sleep_interruptible` (so a 20s idle sleep never
  starves the watchdog), AND
- **inside the two chunk loops** — `read_full_file_to_writer` (per read window)
  and `hash_reader_sha256` (per read chunk).

The per-chunk pets are the critical addition from the GPT-5.5 review: per-candidate
pets alone leave a single slow multi-angle clip able to exceed `WatchdogSec`
under starvation. Internal rate-limiting in `pet()` makes per-chunk calls
effectively free (most are no-ops between intervals).

## Consequences

- A hung `retentiond` is now detected and restarted within ~240s (plus
  `TimeoutStopSec`); combined with durable markers + non-destructive copy,
  recovery loses no footage.
- New ambient process-global state (the notify socket), localized to one module
  and best-effort; pure env-parse logic is unit-tested.
- `read_full_file_to_writer` and `hash_reader_sha256` gain a cheap best-effort
  `watchdog::pet()` call per chunk — a small, intentional coupling.
- No change to the archive decision/copy semantics; the watchdog only affects
  liveness signalling and the unit's failure policy.

## Alternatives considered

- **`sd-notify` crate** instead of hand-rolled std datagram: avoided to keep the
  aarch64 cross-build dependency surface minimal; the std `UnixDatagram` +
  `SocketAddrExt` path is small and fully under our control. (GPT-5.5: std-only
  acceptable provided the helper is tested — it is.)
- **`Type=notify` + `READY=1`**: unnecessary for a watchdog and would change
  startup semantics (systemd would block on readiness).
- **Callback threaded through `ArchiveStore`**: more invasive (trait + helper
  signature changes) than a process-global for genuinely process-singleton state.
- **Shorter `WatchdogSec` (60/120s)**: rejected — false-kill risk under idle-I/O
  starvation during a legitimate slow single-file copy.
- **`Restart=on-watchdog`**: rejected — `Restart=always` already covers it and
  keeps one recovery policy for crashes and hangs.

## Review reconciliation (GPT-5.5, Tier-3)

A parallel GPT-5.5 second opinion reviewed the design (APPROVE-WITH-CHANGES) and
two adversarial passes reviewed the implementation. Neither pass found a Critical
or any footage-loss path (copies are non-destructive staged-promote and markers
are durable, so a watchdog restart re-enumerates and resumes with no loss). The
findings concerned **false-kill avoidance**, and were reconciled as follows:

- **Resolved:** non-blocking notify socket (`set_nonblocking(true)`, ignore
  `WouldBlock`); init+first pet moved **before** the volume-image open (so slow
  storage at startup can't kill us pre-pet); pet-interval **capped at 10s** (was
  effectively `WatchdogSec/2`) so the hot-loop pets keep the 240s deadline
  fresh; boundary pets added around the discrete blocking steps that sit between
  the instrumented read/hash loops (`copy_and_hash_dest` start, before
  `sync_all`, before `rename`; `promote_dest` start); startup pets in
  `load_markers_if_needed` (before the staging wipe + per marker entry) and
  before the per-cycle `list_candidates()` scan.
- **Consciously NOT instrumented (bounded stop):** `volume_source` readdir
  internals, the tiny durable marker/outbox JSON fsync+rename
  (`durability.rs`), and the metadata `sync_dir`/`sync_dir_chain` calls. These
  move small/metadata-only data; a single such call blocking longer than the
  ~170s residual budget would mean effectively-dead storage, in which case a
  watchdog restart is the *correct* response, not a false kill. Instrumenting the
  crash-safety `durability` core with watchdog calls would also couple a pure
  module to liveness signalling for negligible benefit. The residual false-kill
  cost is bounded (a restart + re-enumerate, no footage loss), so the review loop
  was stopped here by orchestrator judgment rather than chasing asymptotically
  implausible syscall stalls.
