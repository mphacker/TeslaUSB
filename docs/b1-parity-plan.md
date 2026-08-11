# B-1 parity remediation plan

## Objective

Make `mhackermsft/b1-clean` a practical replacement for `main`: preserve the
legacy application's important user-facing capabilities and workflows, while
implementing them in Rust/TypeScript with B-1's reliability, performance, and
safe destructive-operation boundaries.

Parity means equivalent operator outcomes and supported workflows, not
identical Flask routes, templates, database layout, or implementation.

## Engineering constraints

- No Python in the shipped solution or build/deploy surface.
- Keep daemon ownership explicit: gadgetd owns car-facing handoff, indexd owns
  catalog state, uploadd owns cloud transfer, retentiond owns archive
  deletion/space governance, wifid owns Wi-Fi, and schedulerd owns schedules.
- All destructive operations must be explicit, validated, serialized, and
  fail closed.
- Preserve same-origin/operator checks for mutations, bounded request bodies,
  path canonicalization, durable queues, leases, and job status publication.
- Do not widen archive scope or deletion eligibility without updating the
  retention contract and its safety tests.
- Use typed DTOs and API client methods; do not add screen-local untyped
  fetches for shared operations.

## Workstream 0: freeze the parity contract

**Purpose:** prevent implementation from chasing an ambiguous target.

**Changes**

- Maintain `b1-parity-matrix.md` as the tracked capability matrix with columns
  for main behavior, B-1 endpoint, B-1 screen, owner daemon, persistence,
  safety rule, status, and test.
- Maintain `b1-parity-contract-index.md` as the contract index linking each
  parity workstream to its webd, daemon, SPA, migration, and test surfaces.
- Decide and document product choices for armed retention, cloud durability
  requirements, provider support, and whether legacy configuration is imported
  automatically or only through an explicit migration command.
- **Operator security decision (2026-08-10):** the operator prefers no login and
  local-network-only access. B-1 will not enable dangerous mutations on that
  basis alone; cloud commands, deletion, Wi-Fi changes, gadget operations, and
  maintenance actions remain disabled until an authenticated session and
  request-forgery protection are approved.

**Dependencies:** none.

**Acceptance**

- Every P0/P1/P2 item in the gap report has one matrix row and one owner.
- Every intentional divergence is labeled as accepted, pending decision, or
  prohibited.

## Workstream 1: finish cloud archive parity

**Owner surfaces:** `webd`, `uploadd`, `indexd` cloud schema/RPC, SPA
`CloudArchive.tsx`, cloud contracts under `docs/specs/contracts`.

**Progress:** The read-only uploader status channel is now exposed by
`GET /api/cloud` and rendered by the Cloud Archive screen. Queue/history remain
read-only. Cloud mutations are still intentionally blocked until operator
authentication/CSRF protection and durable command/job semantics are defined.

**Implementation**

1. Define authenticated/operator-protected webd contracts for:
   - aggregate status and health;
   - Sync Now, wake, stop, and cancel;
   - queue page, item removal, and clear;
   - history and failed-item inspection;
   - connection test, remote browse, mkdir, and remote-path selection;
   - folder policy, priority order, retry limits, cleanup toggles, and reserve;
   - bandwidth test and applied throttle;
   - statistics baseline/reset.
2. Complete uploadd control-socket commands and map each command to durable
   indexd state transitions. Queue mutations must be idempotent and return a
   job ID or durable accepted state.
3. Implement provider flows for the backends supported by main:
   Google Drive, OneDrive, Dropbox, S3, B2, Wasabi, and custom rclone.
4. Keep credential handling in `teslausb-creds`; never return secrets to the
   SPA or write persistent plaintext rclone configuration.
5. Replace disabled Cloud screen controls with live data, optimistic state only
   where the operation is durable, and explicit error/retry states.

**Dependencies:** Workstream 0; existing cloud credential and indexd cloud
contracts.

**Acceptance**

- A configured provider can be tested, started, stopped, and observed from the
  SPA without shelling out from the browser.
- Queue/history/statistics match durable indexd/uploadd state after restart.
- A disconnect or uploadd restart does not lose queue entries or lease state.
- Provider-specific secrets never appear in API responses, logs, or screenshots.
- Playwright covers provider setup, status, queue, history, error, retry, and
  reset flows at both required viewports.

## Workstream 2: retention, cleanup, and archive deletion

**Owner surfaces:** `retentiond`, `indexd`, `webd`, `spa` cleanup/settings UI,
`docs/specs/retention-eviction.md`.

**Progress:** The storage UI is reachable through the restored `/storage` alias
and now explicitly tells operators that armed local cleanup does not wait for
cloud upload confirmation. A read-only `GET /api/retention/status` now exposes
the validated retention governor when available, asks indexd for the same
bounded eviction-candidate view used by retentiond, reports estimated
reclaimable bytes, and includes bounded recent cleanup/freed-byte history from
persisted catalog rows when present. The Storage diagnostics card now renders
that operator-facing retention report (including explicit cloud-durability
disclosure) without enabling policy edits or cleanup execution. The same status
response now also includes an additive optional `exclusion_report` (for example
`too_recent`, `pinned`, `linked_sentry`, `lease_active`) produced by an indexd
classifier over a bounded sample, derived from the same server-side eligibility
and claim gates used by retention/indexd candidate selection. An active lease is
reported as claim-blocked even if the row appears in the read-only candidate
list. A read-only
`GET /api/retention/preview?limit=` continues to return a capped, redacted
candidate summary. B-1 now also exposes a read-only
`GET /api/retention/policy` typed snapshot sourced from the live
`retentiond.governor.json` facts (effective mode, thresholds/floor, per-cycle
caps, and a stable source tag), with explicit `unavailable`/`snapshot:null`
degradation when the governor is absent or malformed. Storage diagnostics now
renders that policy snapshot with explicit read-only wording and no policy
controls. Policy editing and deletion execution remain pending.

**Implementation**

1. Add a read-only retention status API backed by retentiond health/governor
   status files and indexd candidate counts.
2. Add a validated policy API for thresholds, recency floor, caps, dry-run,
   and operator-visible mode. Persist policy in a B-1-owned format rather than
   silently reusing arbitrary legacy YAML.
3. Add preview/report endpoints that return candidates, exclusions, estimated
   bytes, and reasons without deleting.
4. Define the archive deletion protocol between webd, retentiond, and indexd:
   validate the clip/event set, acquire the proper lease, delete through the
   single writer, reconcile catalog rows, and publish a terminal job state.
5. Implement `target=archive` and `target=both`; retain explicit target
   selection and reject ambiguous requests.
6. Surface the armed-without-durability behavior prominently and provide a
   dry-run/operator confirmation path where product policy requires it.

**Dependencies:** Workstream 0; existing retention and indexd delete code;
cloud semantics from Workstream 1 if cloud-gated deletion is approved.

**Next safe slice:** keep cleanup control read-only, then add a low-space/no-progress
operator signal that combines storage pressure with retention non-progress
without enabling deletion actions.

**Acceptance**

- Preview and execute produce the same candidate ordering, with execution
  refusing stale or changed candidates.
- Saved/Sentry/pinned/recent clips remain protected by server-side checks.
- Interrupted deletes reconcile safely and never report success before durable
  completion.
- `archive` and `both` deletion are covered by unit, IPC, and Playwright tests.
- A low-space condition is visible even when retention cannot make progress.

## Workstream 3: Wi-Fi onboarding and captive portal

**Owner surfaces:** `wifid`, `webd` Wi-Fi modules, `CaptivePortal.tsx`, SPA
client/types, captive probe routes.

**Progress:** Webd now handles the Apple, Android, Windows, and generic legacy
probe paths explicitly and redirects them to `/captive-portal`. Live access-point
validation is pending. The captive screen now consumes the existing read-only
Wi-Fi status, discovered-network, and saved-profile APIs; connect, forget, scan,
and AP mutations remain disabled pending operator-gated mutation validation.

**Implementation**

1. Make `/captive-portal` use the existing Wi-Fi status, scan, saved-profile,
   connect, forget, priority, and AP APIs.
2. Reuse the same mutation confirmation, origin checks, serialization, timeout,
   rollback, and link-loss recovery already implemented for Settings.
3. Add the legacy probe endpoints and redirect behavior in webd, returning the
   appropriate success payload for Apple, Android, Windows, and generic clients.
4. Keep the onboarding screen honest when wifid is unavailable, but do not
   replace a failed live read with a fabricated “Not connected” state.

**Dependencies:** Workstream 0; existing wifid APIs.

**Acceptance**

- Scan, join, forget, reorder, AP mode, and AP configuration work from the
  onboarding screen.
- Failed joins roll back the prior connection and clearly report the reason.
- Captive probe requests receive the expected non-SPA responses.
- Playwright verifies no console errors and exercises both live and unavailable
  wifid states.

## Workstream 4: operator maintenance and administration

### Failed jobs

**Progress:** A shared durable-mutation job foundation is now defined in
`specs/contracts/durable-mutation-jobs.md` and wired into typed validation/state
helpers. The slice covers idempotency keys, job-id shape, lifecycle/cancellation
states, restart recovery projection, bounded-redacted error text, Host/Origin/
Sec-Fetch non-GET same-origin validation helpers for new durable mutation routes
(CSRF hardening, not auth), and explicit recording/gadget-handoff exclusion
semantics. Legacy Wi-Fi mutation checks still permit missing `Origin` and are
disclosed as such. B-1 now also exposes
read-only `GET /api/jobs/capabilities` to disclose this contract and current
limits (`JobHub` remains in-memory).

The `/api/jobs` + `/api/jobs/failed` path remains read-only, and B-1 now adds
read-only durable failed-upload history at `GET /api/jobs/failed/uploads`
(indexd-backed `cloud_sync_history` rows with `outcome='failed'`) so operators
can triage upload failures without mutating queue state. Retry/delete commands
remain intentionally disabled pending operator-approved auth/CSRF and daemon
ownership wiring. The pending retry contract is now explicit: child-specific
targeting (`archive_item_id` + `child_key`), `failed`-only eligibility, and
deterministic reject for `done|queued|in_progress|parked`, with optional
`upload_set_id` generation-fence semantics (sealed rows require match;
unsealed rows require omission).

indexd now also persists failed-upload retry request identity (migration v9
`cloud_failed_upload_retry_requests`) and carries the generation fence through
failed history (migration v10). The public retry route is enabled for local
network users with strict same-origin checks; delete remains disabled.

Next step: finish the full retry validation/UAT gate, then add typed delete
commands only after their durable ownership and recovery contract is complete.
Retry creates a new durable attempt rather than mutating history invisibly.

### Advanced settings

**Progress:** B-1 now exposes a bounded, read-only advanced-settings snapshot at
`GET /api/settings/advanced` and renders it in the Settings dashboard Advanced
Settings section. The slice intentionally includes only settings keys B-1
already validates for writes (`trip_gap_minutes`, `speed_limit_mph`,
`speed_unit`, `display_timezone`, `clock`) and reports whether each value is
stored or defaulted (missing/invalid).

Inventory every main tunable, assign it to the owning B-1 daemon, define range
and restart semantics, persist it in a versioned B-1 configuration store, and
expose only validated fields. Calibration-gated retention values must not be
presented as production guarantees.

### Gadget/mode controls

**Progress:** B-1 now exposes a bounded, read-only gadget/mode snapshot at
`GET /api/gadget/mode-status` and renders it in the Settings USB Drive panel.
The panel now shows current gadget mode, handoff state, LUN/image load status,
and recovery/banner facts sourced only from existing `gadgetd` runtime/persisted
status (`last_result`, queue counts, media read-mount health, and
re-enumeration state). Mutation controls (present/edit/recover/recent-archive)
remain intentionally out of scope.

Add explicit webd/gadgetd operations for present/edit/recover/recent-archive
flows where the B-1 architecture supports them. If an operation requires
eject or reboot, return a durable handoff/job and show the operation banner
until terminal state.

### FSCK

**Progress:** B-1 now exposes read-only filesystem-check visibility from
persisted snapshots: `GET /api/fsck/status`, `GET /api/fsck/history`, and
`GET /api/fsck/last-check/:partition`. The Settings dashboard renders current
state, per-partition last-check summaries, and recent check history without
offering start/cancel/repair actions.

Implement a bounded fsck job owned by the appropriate daemon, with status,
cancel, history, and last-check APIs. It must never run concurrently with a
car-facing write or gadget handoff.

**Dependencies:** Workstream 0; daemon ownership decisions. Gadget and FSCK
work must not bypass gadgetd or the single-writer rules.

**Acceptance**

- Each operation has typed request/response DTOs, authorization/origin checks,
  timeout behavior, and durable terminal status.
- UI controls cannot issue duplicate operations and remain correct after
  refresh/reconnect.
- Unit and IPC tests cover unavailable daemon, timeout, cancellation, retry,
  and restart recovery.

## Workstream 5: mapping administration parity

**Owner surfaces:** `indexd`, `webd` query/routes, `TripMap.tsx`, analytics and
event-detail SPA components.

**Progress:** B-1 now exposes bounded read-only mapping diagnostics (`/api/index/status`,
`/api/index/driving-stats`, `/api/index/event-chart`) and a bounded per-event
detail read (`/api/events/:id/detail`) used by Trip Map marker/details actions.
These slices have query-level and HTTP-level Rust coverage plus affected desktop/mobile
Trip Map UAT. Lifecycle commands remain pending.

**Implementation**

- Add typed endpoints for index status, trigger, rebuild, cancel, diagnose,
  playable trips, all routes, driving stats, event charts, Sentry-event
  details, event clips, and waypoints-for-clip.
- Reuse existing catalog queries where possible; add indexes and bounded
  pagination before exposing large result sets.
- Keep map rendering read-only and make indexing operations asynchronous jobs.
- Add explicit stale-index/partial-data indicators instead of silently
  omitting events.

**Current slice:** bounded read-only mapping diagnostics and playable-route
reads backed by existing catalog rows. The slice now includes:

- `GET /api/trips/page?playable=true` for cursor-paginated playable trips
  (overlap with `clips.availability = 'present'`);
- `GET /api/clips/:id/waypoints` for capped (20,000-row) clip telemetry;
- `GET /api/index/driving-stats` for read-only driving aggregates (distance,
  drive time, warning count, sentry count, avg/max speed) used by Trip Map
  diagnostics.
- `GET /api/index/event-chart` for read-only event distribution aggregates
  (indexed total, top types, recent per-day buckets including sentry and
  warning slices) used by Trip Map diagnostics.
- `GET /api/index/lifecycle` for read-only lifecycle/index-health diagnostics
  from persisted catalog state (lifecycle state, stale/error/retry counts, and
  last derived/parse-attempt timestamps) used by Trip Map diagnostics.
- `GET /api/events/:id/detail` for read-only event drill-down (base event row,
  linked clip metadata, and optional nearest `clip_events` sidecar context when
  available, with graceful fallback when `clip_events` is absent) used by Trip
  Map popup and timeline detail actions.

These reads remain separate from future rebuild/repair lifecycle jobs.

**Dependencies:** Workstream 0; indexd schema/query review.

**Acceptance**

- Main's mapping workflows can be completed from B-1 without direct database
  access.
- Large date ranges remain bounded and responsive on the Pi profile.
- Rebuild/cancel/restart behavior preserves catalog consistency.

## Workstream 6: main-to-B-1 migration (out of scope)

**Owner surfaces:** `setup.sh`, `setup-lib`, a Rust migration utility or
  migration subcommand, credential and catalog crates, migration docs/tests.

**Decision:** B-1 is not required to migrate existing `main` installations.
Migration is not part of the feature-parity target and must not block release.
The optional `teslausb-migrate` read-only utility may remain available for
diagnostics, but no apply, credential conversion, database conversion, or
installer migration workflow will be pursued for parity.

**Historical progress:** `teslausb-migrate discover --root <path>` now provides the first
safe read-only discovery slice from
`docs/specs/contracts/migration-discovery.md`: it inventories bounded likely
legacy paths, reports installation detection, and emits explicit blockers for
unreadable files plus unsupported/unknown mapping databases. Apply/migrate/
verify/rollback modes remain pending. `setup.sh discover --root <path>` now
delegates to the installed Rust utility without performing any mutation. The
bounded inventory now also checks common legacy systemd unit paths without
executing or parsing them as configuration, and reports only the names of
bounded top-level `config.yaml` keys. `teslausb-migrate plan --root <path>`
continues to map detected sections to destination owners. A new read-only
`teslausb-migrate convert --root <path>` dry-run now normalizes a bounded set of
supported non-secret scalar settings, quarantines unknown/unsafe/non-scalar
values, omits secrets, and never writes files.

**Historical implementation (not a release dependency)**

1. Inventory legacy paths, config sections, SQLite databases, credential files,
   archive/media roots, and systemd state.
2. Add a read-only discovery mode that reports detected versions and migration
   blockers without changing the device.
3. Convert supported `config.yaml` settings into versioned B-1 configuration,
   preserving unknown fields in a quarantine/report rather than silently
   dropping them.
4. Migrate mapping/cloud/index state with schema checks, row counts, checksums,
   and idempotent markers.
5. Convert credentials only through a documented secure path; never log token
   contents.
6. Provide dry-run, apply, verify, and rollback/recovery modes. Preserve
   `disk.img`, archive media, and car-facing data unless an explicit operation
   owns them.
7. Update release/setup docs with upgrade sequencing and recovery steps.

**Dependencies:** None for the parity release. Workstreams 1-5 must define their destination contracts
before converters are finalized.

**Discovery contract:** `docs/specs/contracts/migration-discovery.md` now
defines the first safe migration slice. It is intentionally read-only and
reports missing, unreadable, and unsupported legacy inputs without applying
changes.

**Destination contract:** `docs/specs/contracts/migration-destination.md`
defines the owner and safety boundary for each legacy configuration section.
This completes the design prerequisite for a future dry-run converter without
enabling writes or destructive migration.

The first implementation step is now available as
`teslausb-migrate plan --root <path>`, which reports supported section ownership
and quarantines unknown sections without converting or writing values.

The next implementation step is now available as
`teslausb-migrate convert --root <path>`, which performs read-only dry-run
normalization for supported non-secret scalar settings and quarantines unknown,
invalid, secret, and unsafe values without emitting secret material.

**Acceptance (not required for the parity release)**

- A representative main installation can be discovered, dry-run, migrated,
  verified, and restarted without data loss.
- Running migration twice is a no-op after the first successful marker.
- Row/file counts, checksums, preferences, credentials state, and service
  health are verified after migration.
- Failure at every phase leaves the prior installation recoverable.

## Validation and release gates

Run gates from cheapest to most expensive:

1. Rust formatting, clippy, targeted unit tests, and TypeScript type/build
   checks.
2. Daemon IPC/integration tests for each changed contract.
3. Targeted Playwright specs with `UAT_FAST=1` during iteration.
4. Full SPA UAT at 375px and 1280px before each UI milestone, including
   console/page-error, network, wiring, performance, and screenshots.
5. Migration dry-run/apply/rollback tests against fixtures derived from main.
6. Release manifest, installer, setup-lib, and denylist tests.
7. Hardware validation on B-1 only for changes involving gadget handoff,
   retention/deletion, Wi-Fi, boot, or deployment.

The final parity sign-off requires every gap-report row to be marked
implemented, intentionally divergent with approval, or explicitly deferred
with an operator-visible limitation.
