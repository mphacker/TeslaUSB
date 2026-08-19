# B-1 parity capability matrix

This matrix is the execution checklist for `docs/b1-parity-plan.md`. A row is
complete only when the B-1 endpoint, owning daemon, persistence behavior, UI
surface, safety rule, and validation target are all present. `Planned` means
the gap is known but not implemented; `Partial` means the core path exists but
does not replace the full `main` workflow.

| Priority | Capability | Main reference | B-1 endpoint/surface | Owner | Persistence | Safety rule | Status | Acceptance test |
|---|---|---|---|---|---|---|---|---|
| P0 | Cloud credentials | `cloud_archive.py` provider setup | `/api/cloud/credentials`, `CloudArchive.tsx` | webd + teslausb-creds | encrypted credential blob | same-origin; secrets never returned | Partial | credential CRUD/API and SPA cloud setup |
| P0 | Cloud status/control | `cloud_archive.py` sync/status routes | `/api/cloud` via uploadd `get_status`; `CloudArchive.tsx` status banner; mutations pending | webd + uploadd | uploader status snapshot | local-socket read-only status; mutations require strict same-origin/request-forgery checks, explicit confirmation, and durable jobs | Partial | status route/socket tests and Cloud Archive UAT; later Sync Now, wake, stop, cancel |
| P0 | Cloud queue/history | queue/history APIs and page | `/api/cloud/queue`, `/api/cloud/history` | webd + indexd | upload queue and history tables | redacted DTOs; bounded cursors; no lost rows on restart | Partial | route redaction/unavailable tests and cloud archive UAT |
| P0 | Cloud policy | folders, priority, reserve, retry, cleanup | Planned `/api/cloud/settings` | webd + uploadd | versioned B-1 config | validated ranges and provider allow-list | Planned | save/reload policy and priority ordering |
| P0 | Cloud providers | OAuth, S3-style, custom rclone | credential flow plus provider adapters | teslausb-creds + uploadd | encrypted credentials; transient rclone config | no plaintext persistent secrets | Partial | provider contract tests and connection tests |
| P0 | Cleanup control | cleanup status/policy/preview/execute/report | Read-only `/api/retention/status` (governor, bounded candidate totals/bytes, additive `operator_signal` low-space/no-progress diagnostic, optional bounded `exclusion_report` from indexd classifier incl. lease gate, bounded recent cleanup history, cloud-durability disclosure), read-only `/api/retention/policy` typed snapshot (`effective_mode`, thresholds/floor, per-cycle caps, source, explicit unavailable status), capped `/api/retention/preview`, and Storage diagnostics retention report wiring; policy/execute remain planned | webd + retentiond + indexd + SPA | retention policy and job history | shared server-side eligibility predicate; caps are explicit; cloud durability disclosure is explicit; policy snapshot is read-only and never fabricates defaults; operator signal degrades explicitly when diagnostics are absent; mutations require protected jobs | Partial | retention status/preview/policy Rust tests + Storage Health UAT; execute, cancel, report pending |
| P0 | Archive deletion | archive event/clip deletion | `DELETE /api/clips/:id?target=archive` | retentiond + indexd | catalog reconciliation | lease + stale-plan check + terminal job | Missing | archive delete IPC and UAT |
| P0 | Combined deletion | delete car and archive together | `target=both` | gadgetd + retentiond + indexd | handoff and catalog state | ordered handoffs; no partial success claim | Missing | failure injection and recovery test |
| P1 | Wi-Fi onboarding | captive portal status/connect/saved profiles | `/captive-portal` consumes live status, scan, connect/select, forget, priority, and safe AP controls; all mutations are operator-confirmed | wifid + webd + SPA | NetworkManager profiles | serialized mutation, rollback, link-loss recovery, same-origin AP/STA mutation checks, no AP force-off control | Partial | focused desktop UAT and webd AP-origin tests pass; live onboarding mutation UAT pending |
| P1 | Captive probes | Apple/Android/Windows/generic probe routes | Explicit webd probe routes redirecting to `/captive-portal` | webd + wifid | none | probe paths never fall through to SPA shell | Partial | HTTP probe compatibility tests; live AP validation still pending |
| P1 | Advanced settings | `/settings_advanced` tunables | read-only `GET /api/settings/advanced`; Settings dashboard Advanced Settings section | webd + indexd prefs | existing prefs rows for supported keys | bounded allow-list; invalid/missing values default with source status; no mutations | Partial | webd defaulting/validation tests, typed SPA client wiring, and Settings dashboard UAT |
| P1 | Failed-job retry/delete | jobs counts/retry/delete + `specs/contracts/durable-mutation-jobs.md` | `/api/jobs`, `/api/jobs/failed`, `/api/jobs/failed/uploads`, `/api/cloud/queue/{archive_item_id}/retry`, `/api/jobs/capabilities`; shared mutation envelope/validation helpers | webd + indexd + owning daemon | durable attempt history + indexd v9 retry-request identity and v10 history-fence migrations | requestId/idempotencyKey/requestHash/jobId envelope validation; idempotency same key+hash replay + different-hash `409`; strict Host/Origin/Sec-Fetch checks on retry; bounded redacted errors; recording/handoff exclusion; failed-upload retry is child-specific and `failed`-only (reject `done|queued|in_progress|parked`) with optional `upload_set_id` generation fence semantics (sealed rows require match, unsealed rows require omission); retry enabled, delete still disabled | Partial | durable-mutation validation tests + capabilities route test + failed-upload history and retry route tests + indexd retry-request persistence tests + Failed Jobs retry UAT |
| P1 | Gadget/mode operations | present/edit/recover/recent archive | read-only `/api/gadget/status` + bounded `/api/gadget/mode-status`; Settings USB panel mode/handoff/LUN/recovery status; mutation commands still planned | gadgetd + webd | durable handoff/job state | no direct LUN mutation outside gadgetd | Partial | gadget-status route/unit tests and Media Hub + shell operation-banner UAT |
| P1 | Mapping administration | index controls, diagnostics, charts, Sentry detail | `GET /api/index/status`, `GET /api/index/lifecycle`, `GET /api/index/driving-stats`, `GET /api/index/event-chart`, `GET /api/events/:id/detail`, `GET /api/trips/page?playable=true`, `GET /api/clips/:id/waypoints`, plus catalog reads; lifecycle routes planned | indexd + webd | catalog/index jobs | bounded queries; async rebuild jobs | Partial | index-status + lifecycle + driving-stats + event-chart + event-detail query/HTTP tests, playable-trip cursor filtering, bounded clip-waypoint read, and Trip Map desktop/mobile UAT (including marker/timeline event detail); lifecycle and mapping UAT pending |
| P2 | FSCK | FSCK start/status/cancel/history | Read-only `GET /api/fsck/status`, `GET /api/fsck/history`, `GET /api/fsck/last-check/:partition`; Settings dashboard visibility; start/cancel/repair still pending | maintenance owner + webd | persisted fsck status/history snapshots | read-only; no start/cancel/repair mutations exposed | Partial | webd fsck route tests, typed SPA client coverage, and Settings dashboard UAT |
| P2 | Main-to-B1 migration | N/A for the B-1 feature-parity release | Optional read-only `teslausb-migrate` diagnostics only; no migration workflow required | Optional maintenance utility | N/A | Must not modify device state | Out of scope | Explicit operator decision: B-1 does not need to migrate existing `main` installations |
| P2 | Retention disclosure | configurable cleanup/cloud behavior | storage UI explicitly distinguishes armed local eviction from cloud durability | retentiond + webd + SPA | policy and governor status | never describe armed eviction as cloud-gated | Partial | armed/dry-run UAT; policy controls still pending |

## Status definitions

- **Implemented:** the user workflow is live and covered by the relevant
  contract and end-to-end test.
- **Partial:** a meaningful B-1 slice is live, but main users cannot complete
  the whole workflow.
- **Planned:** contracts or code may exist in pieces, but no complete operator
  workflow is available.
- **Missing:** no supported B-1 replacement exists.

## Completion rule

Before parity sign-off, replace every `Partial`, `Planned`, and `Missing`
status with either `Implemented` or an explicitly approved `Deferred` status
with an operator-visible limitation and a linked issue.
