# `schedulerd` + chime enforcement — spec

> **Status:** draft for the A3d enforcement slice (`docs/status.md` §4.5).
> Covers the chime *scheduler state owner* (`schedulerd`, already built) **and**
> the missing **enforcement/actuation layer** that makes schedules + random-mode
> actually change the car's `LockChime.wav`.

## 1. Problem

The chime *config/state* layer is fully functional and tested: schedules
(weekly/date/holiday/recurring), chime groups, and a random-on-boot
`RandomMode {enabled, group_id}` all persist in `schedulerd` and round-trip
through the `webd` proxy to the SPA. The pure rule engine
(`teslausb_core::chime::resolve_active`) is implemented + host-unit-tested.

But **nothing ever changes `LockChime.wav` based on a schedule or random mode.**
Verified gaps:

1. `schedulerd`'s `Evaluate` IPC command has **zero callers** — no per-minute tick.
2. `store.evaluate()` never passes `random_mode`/`groups` into the engine → "random
   on boot" has no runtime effect.
3. `Interval::OnBoot` recurring schedules are a no-op (`trigger_today` returns `None`).
4. Nothing enqueues the `LockChime.wav` swap when a rule fires.

Only the manual **Set Active** button changes the chime today.

## 2. Ownership & where the tick lives (decision)

**The enforcement tick lives in `webd`, not `schedulerd`.** Deciding reasons:

- `webd` already owns the **actuation path**: `chime_library::activate` reads a
  library WAV from the MEDIA partition (`media_ro_root()/Chimes/<name>`),
  validates it, and calls `route::run_install(state, kind, P2, "LockChime.wav",
  bytes)` to enqueue the gadgetd swap. This path is built + live-proven.
- `webd` already owns the **real chime library** (`list_chime_library`, the
  media-backed `Chimes/*.wav` catalog) and a `SchedulerClient`, and has a
  tokio runtime + a background-task precedent (`media_events`).
- `schedulerd` has **no** media access and **no** gadgetd client. Giving it those
  would duplicate webd's activate logic and cross the single-writer ownership
  boundary.

So: `schedulerd` stays the pure **state owner + decision engine** (via its
`Evaluate`/`EvaluateBoot` IPC). `webd` runs the **tick** and performs the
**actuation** by reusing the proven Set-Active path. `gadgetd` remains the sole
partition writer.

```
 webd chime_enforcer (per-minute + at-boot)
   │  list real library (catalog)         ┌─────────────┐
   ├─ Evaluate{now,tz,active,library} ───▶│ schedulerd  │ pure resolve_active /
   │  ◀── pick (concrete filename) ───────│ (state only)│ resolve_boot
   │                                       └─────────────┘
   └─ if pick != last_enforced:
        read Chimes/<pick> → validate WAV → run_install(P2,"LockChime.wav") ──▶ gadgetd
```

## 3. Design

### 3.1 Core engine (`teslausb_core::chime`) — pure, no new deps

Add a boot resolver that stays I/O-free and never sees the `RandomMode`/group
structs (those live in `schedulerd::model`); the caller resolves the configured
group to a plain `&[String]` of member filenames.

```rust
/// Resolve the chime that should be active at device boot.
/// Same evaluation as `resolve_active`, EXCEPT `Interval::OnBoot` recurring
/// schedules are treated as triggered at boot (trigger minute 0). If that yields
/// no winner AND `random_members` is Some(non-empty), pick one of those members
/// (excluding `active_chime` when another exists) as the lowest-priority "random
/// on boot" default, returned as a synthetic Pick (schedule_id =
/// "random-on-boot", schedule_name = "Random on boot"). The random pick is
/// seeded by `boot_seed` so it is STABLE across webd restarts within one device
/// boot (no churn on a crash-restart) but rotates on a real reboot.
pub fn resolve_boot(
    now: CivilTime,
    schedules: &[Schedule],
    active_chime: Option<&str>,
    library: &[String],
    random_members: Option<&[String]>,
    boot_seed: u64,
) -> Option<Pick>
```

- **Precedence (v1 parity):** a real schedule that is active at boot (any of
  weekly/date/holiday/recurring, *including* OnBoot recurring) **wins** over the
  random-on-boot default. Random-on-boot is only the fallback when no schedule
  decides the chime. (Rationale: random-on-boot sets the *default* lock chime at
  startup; explicit schedules override it.)
- **OnBoot eligibility:** internally, `resolve_boot` makes `Interval::OnBoot`
  recurring schedules eligible with trigger minute 0 (and a stable boundary, e.g.
  0) so `resolve_chime`'s existing random library pick applies. The minute-driven
  `resolve_active` is unchanged (OnBoot still returns `None` there).
- **Determinism / no restart churn:** the random-on-boot pick is seeded by the
  `boot_seed` argument (mixed via the existing `seed_for` helper). `webd` derives
  `boot_seed` from the kernel boot id (`/proc/sys/kernel/random/boot_id`, hashed;
  fall back to `/proc/stat` `btime`, then `0`). Result: every webd restart inside
  the same device boot resolves the SAME random-on-boot chime (no eject-handoff
  churn on a crash-restart loop), while a genuine reboot rotates it. It need not
  be cryptographically random — v1 "random" is a rotation, not a security
  primitive. `active_chime` is excluded when ≥1 other candidate exists.

  `EvaluateBoot` carries `boot_seed` from webd into the engine; schedulerd does
  not invent it (schedulerd has no notion of the device boot).

`resolve_active` is **unchanged** (regression-protected by existing tests).

### 3.2 `schedulerd`

1. **`store.rs`**
   - `evaluate(now, active_chime, library)` — unchanged signature; already takes
     the real `library`. (Schedule-level `RANDOM`/recurring already resolve from
     it.)
   - **New** `evaluate_boot(&self, now, active_chime, library, boot_seed) ->
     Option<Pick>`: resolve `random_mode` → if `enabled` and `group_id` names an
     existing group, compute that group's member filenames **intersected with
     `library`** (a member that no longer exists is skipped); pass `Some(&members)`
     (or `None` when random mode is off / group empty) plus `boot_seed` into
     `teslausb_core::chime::resolve_boot`.
2. **`ipc.rs`**
   - Extend `Evaluate` with an optional `library: Option<Vec<String>>` field
     (`#[serde(default)]`). A **supplied** list (`Some`, even when empty) is used
     verbatim — an empty supplied list legitimately means "no installable
     candidates" and must **not** fall back to the stale local scan. Only an
     **omitted** field (`None`, legacy callers) triggers `library::scan`. Factor
     this into a `resolve_eval_library(supplied, dir)` helper shared by both
     handlers.
   - **New** `EvaluateBoot { unix_secs, tz_offset_secs, active_chime?, library?,
     boot_seed }` → call `store.evaluate_boot(now, active_chime, library,
     boot_seed)` (the store resolves the random-mode group internally) →
     `{ "pick": … | null }`, identical pick JSON shape as `Evaluate`.
   - No new auth/framing — reuse the existing length-prefixed JSON + `0o660`
     socket.

### 3.3 `webd` — `chime_enforcer.rs` (new module)

A background task that drives enforcement. **Started only in the production
binary path**, never in handler unit tests.

- **Start hook:** `router_with_clients` (the single place that constructs
  `AppState`), right after building `state`, spawns the enforcer **iff** env
  `WEBD_CHIME_ENFORCER` is set, via `crate::chime_enforcer::spawn(state.clone())`.
  The systemd unit sets `WEBD_CHIME_ENFORCER=1`; handler tests never set it, so no
  enforcer runs under test even though they go through `router_with_clients`. This
  keeps `AppState` private (no need to thread it out to `main`). `tokio::spawn` is
  valid here because the router is always built inside a Tokio runtime (`#[tokio::
  main]` in production, `#[tokio::test]` in tests).
- **State:** an in-memory `last_enforced: Option<String>` (the last filename the
  *scheduler* applied). Not persisted — see restart behavior below.
- **Boot step (once, at start):**
  1. `tz_offset_secs` from `local_offset_secs()` (see §3.5).
  2. `boot_seed` from the kernel boot id (§3.1).
  3. List the real library (`list_chime_library` filenames). **If the list errors
     OR is empty (media not mounted/catalog not ready yet) → skip the boot step**;
     the per-minute tick converges once the library is readable.
  4. `EvaluateBoot{ now=unix_now(), tz_offset_secs, active_chime=None, library,
     boot_seed }`.
  5. If it returns a pick **and that pick is in `library`** → `apply(pick)`
     (install + `last_enforced = pick`). A pick naming a since-deleted file is
     skipped silently (self-heals when it returns) rather than looping.
- **Tick step (every 60 s):**
  1. List the real library. **On list error or empty → skip this tick** (leave
     `last_enforced` unchanged; retry next minute).
  2. `tz_offset_secs = local_offset_secs()` (re-read each tick so a DST change /
     env update takes effect without a restart).
  3. `Evaluate{ now, tz_offset_secs, library }` — **`active_chime` is deliberately
     omitted (None).** The core resolver EXCLUDES `active_chime` from a random
     pool, so feeding `last_enforced` back would make a RANDOM/recurring schedule
     resolve a *different* file every tick (a gadgetd handoff per minute).
     Idempotency is enforced entirely by `next_action` (step 4) over a stable,
     seed-deterministic pick — not by the resolver's active-exclusion.
  4. Decide via the pure helper `next_action(pick, last_enforced)`:
     - `Some(name)` when `pick` is `Some` and `pick.chime_filename != last_enforced`.
     - `None` otherwise (no pick, or pick already enforced).
  5. On `Some(name)`, **iff `name` is in `library`** → `apply(name)`.
- **`apply(name)`** = the shared install helper (below): read+validate
  `Chimes/<name>`, `run_install(state, "chime_scheduler_enforce", P2,
  "LockChime.wav", bytes)`. **`run_install` already publishes a `JobStatus` to the
  jobs hub**, so an enforcement install/failure is visible on the existing job
  SSE / Failed Jobs surface — no separate health channel is built. On success
  (queued or done) set `last_enforced = Some(name)`. On failure: log, **do not**
  update `last_enforced` (so the next tick retries — an implicit ~60 s backoff).
- **Shared install helper:** factor the read+validate+`run_install` body out of
  `chime_library::activate` into `install_library_chime_as_active(state, name)`
  so the manual `activate` handler **and** the enforcer call the exact same code
  path (single source of truth for "make this library file the active chime").
- **Concurrency:** the enforcer is a single task; ticks never overlap (it awaits
  each `apply`). A manual `POST …/activate` racing a tick is fine — both go
  through the serialized gadgetd durable queue; last-writer-wins, and the next
  tick reconciles only on a *pick change*.

### 3.4 Idempotency & restart (decision)

- Compare the resolved pick **only against `last_enforced`** (the last value the
  scheduler applied), **not** the on-disk `LockChime.wav` (which has no embedded
  name and is resolved heuristically by the UI). This means the scheduler
  **applies a change only when its pick transitions**, and does **not** fight a
  manual Set-Active between two identical scheduled windows. This is the intended
  product behavior (manual override respected until the next schedule change),
  and it bounds gadgetd eject-handoffs to **one per actual pick change**, not one
  per minute.
- **webd restart** loses `last_enforced` (`None`). The first boot/tick after
  restart therefore re-applies the currently-resolved pick **once** (idempotent
  convergence — the car ends in the correct state). Acceptable: one redundant
  handoff at most per restart, only if a schedule is currently active.
- **Recurring/random cadence note:** a `Recurring` schedule with a short interval
  (e.g. 15 min) resolves a *new* random pick each interval window → that is a
  real eject-handoff per window **by design** (it's a rotation feature). Document
  this in the unit/UI so an operator choosing 15 min understands the wear/USB
  re-enumeration cost. (Hard re-enumeration for an active-chime change is the
  §1.1/C6 car-side concern, A3d.5.)

### 3.5 Timezone — real local offset (`local_offset_secs()`)

Schedules are wall-clock (a "weekly 08:00" must fire at 08:00 *local*), so a fixed
UTC offset would mis-fire them across DST. The workspace denies `unsafe_code`, so
`libc::localtime_r` is out and `chrono`/`time` local detection is unreliable in a
multithreaded process. The cheapest **safe** way to get the real, DST-correct
offset with no new dependency is to shell out to coreutils `date`:

```
local_offset_secs():
  1. if env WEBD_TZ_OFFSET_SECS is set → parse i32 seconds, use it (test/override).
  2. else run `date +%z` (std::process::Command, no shell) → parse "±HHMM" → secs.
  3. else → 0 (UTC).
```

`date +%z` honors `/etc/localtime` including DST, costs one tiny subprocess per
tick (60 s cadence — negligible), and needs no unsafe and no crate. The env var
stays as a deterministic override for tests and for a headless/`date`-less image.
This **removes** the earlier fixed-offset limitation.

### 3.6 Reconciliation with the GPT-5.5 design review

GPT-5.5 independently agreed "tick in webd" is right for B-1. Its P0 findings are
**adopted**: real local TZ via `date +%z` (§3.5), a boot-stable random seed so
restarts don't churn (§3.1), library-readiness skip + failure visibility via the
jobs hub (§3.3), and a hardware "no-spam / only-lun.1-cycles" proof (§4.4).

The following GPT-5.5 suggestions are **declined for this LEAN v1 slice** (with
rationale), to avoid gold-plating; each is a noted follow-up, not a blocker:
- **Durable/persisted enforcement state + content-hash dedupe.** In-memory
  `last_enforced` + idempotent restart convergence (one redundant handoff at most
  per restart, only if a schedule is active) is sufficient for v1. The
  same-name/new-bytes case is anomalous (WAVs are validated on upload).
- **A separate app-level activation lock + queue-coalescing layer.** The gadgetd
  **durable queue is already the serialization point**, and the enforcer is a
  single task that awaits each `apply`, so manual + scheduled activations can't
  interleave mid-handoff. Coalescing is only relevant under rapid pick churn
  (e.g. a 15-min recurring schedule) and is a follow-up if observed in practice.
- **A dedicated `chime-enforcerd` daemon.** webd is the correct home for B-1
  (owns the actuation path + library + scheduler client); a separate daemon is a
  long-term refactor, not a v1 need.
- **Clock-not-ready at boot:** the per-minute tick self-corrects once NTP sets the
  clock, so no explicit gate is built; noted.

## 4. Acceptance tests (the implementation contract)

### 4.1 Core (`teslausb-core/src/chime.rs`, host unit tests)
- `resolve_boot_fires_on_boot_recurring`: one enabled `Recurring{OnBoot}` schedule
  + library `["A.wav","B.wav"]` → returns a `Pick` with a library filename.
- `resolve_boot_random_default_when_no_schedule`: no schedules, `random_members =
  Some(["X.wav","Y.wav"])` → returns a `Pick` whose `chime_filename` ∈ members and
  `schedule_id == "random-on-boot"`.
- `resolve_boot_schedule_beats_random_default`: a weekly schedule already
  triggered today **and** `random_members` present → the weekly schedule's chime
  wins (not the random default).
- `resolve_boot_random_excludes_active`: `random_members=["A.wav","B.wav"]`,
  `active_chime=Some("A.wav")` → picks `"B.wav"`.
- `resolve_boot_none_when_nothing`: no schedules, `random_members=None` → `None`.
- `resolve_boot_seed_is_stable`: same `boot_seed` + same library → same random
  pick across calls; a different `boot_seed` may rotate it.
- `resolve_active` existing tests still pass (OnBoot still `None` there).

### 4.2 `schedulerd` (host unit tests)
- `evaluate_boot_uses_random_group`: add a group `{members:["G1.wav","G2.wav"]}`,
  enable random mode for it → `store.evaluate_boot(now, None, &["G1.wav","G2.wav"],
  boot_seed)` returns a pick ∈ the group members.- `evaluate_boot_skips_missing_members`: group member not in `library` is skipped;
  if none remain and no schedule applies → `None`.
- `ipc Evaluate honors supplied library`: `Evaluate` with `library:["L.wav"]` and a
  `RANDOM` weekly schedule active → pick is `"L.wav"` (proves webd's library, not
  the local scan, is used).
- `ipc EvaluateBoot round-trips`: returns the boot pick over the socket.

### 4.3 `webd` (host unit tests)
- `next_action` truth table: `(Some("A"), None)→Some("A")`; `(Some("A"),
  Some("A"))→None`; `(Some("B"), Some("A"))→Some("B")`; `(None, Some("A"))→None`;
  `(None, None)→None`.
- `activate` handler still passes its existing test after the helper extraction
  (no behavior change to the manual path).

### 4.4 End-to-end (Playwright + hardware) — the GA proof
Full UI test of the Lock Chimes page (`docs/status.md` requires this for any UI
change), on the live device under the hardware-test rails:
- Scheduler/groups/random-mode CRUD round-trip through the real UI (create a
  schedule, see it listed; create a group; toggle random mode), console clean,
  mobile (375) + desktop (1280).
- **Enforcement proof:** create a weekly schedule whose trigger time is ≤ now for
  today, pointing at a known library chime; within ~2 ticks the enforcer installs
  it → `LockChime.wav` on the MEDIA mount becomes byte-identical to that library
  chime (verified on-device), and the Active Lock Chime card reflects it without a
  manual reload. Evidence appended to `files/hw-results.md` + screenshots.
- **No-spam / single-writer proof (GPT-5.5 P0):** after the schedule applies once,
  subsequent ticks do **not** re-enqueue (assert the `chime_scheduler_enforce` job
  count stays at 1 over several minutes — `last_enforced` dedupe holds), and the
  handoff cycled **only `lun.1`** (`lun.0`/teslacam.img untouched, `ro=0`
  preserved) — same evidence discipline as F5.
- Active-chime card / audio player keep working; no regression to manual Set
  Active or library CRUD.

## 5. Out of scope (tracked elsewhere)
- **A3d.5** car-side **full USB re-enumeration** so the *car* re-reads a changed
  `LockChime.wav` (vs. soft medium-change) — §1.1 / C6, Tier-C/at-vehicle.
- Persisting `last_enforced` across restarts (current convergence behavior is
  acceptable — §3.4/§3.6).
- Durable enforcement state, content-hash dedupe, an activation-coalescing layer,
  and a dedicated `chime-enforcerd` daemon — declined for v1 (§3.6), follow-ups.

## 6. Files touched
- `rust/crates/teslausb-core/src/chime.rs` — `resolve_boot` + tests.
- `rust/crates/schedulerd/src/store.rs` — `evaluate_boot` + tests.
- `rust/crates/schedulerd/src/ipc.rs` — `Evaluate.library`, `EvaluateBoot` + tests.
- `rust/crates/webd/src/chime_enforcer.rs` — **new** task + `next_action` + tests.
- `rust/crates/webd/src/chime_library.rs` — extract `install_library_chime_as_active`.
- `rust/crates/webd/src/lib.rs` / `main.rs` — `spawn_chime_enforcer` start hook
  behind `WEBD_CHIME_ENFORCER`.
- `rust/crates/webd/src/scheduler.rs` — `evaluate` / `evaluate_boot` client
  helpers if needed (or reuse the generic `call`).
- `deploy/systemd/webd.service` — `Environment=WEBD_CHIME_ENFORCER=1` (+ optional
  `WEBD_TZ_OFFSET_SECS`); update `schedulerd.service` header scope note.
- `spa/test/uat/chime-scheduler.spec.ts` — extend the UAT for the E2E proof.
