# Contract: shared durable mutation job foundation (B-1)

Status: **Foundation + failed cloud-upload retry enabled (no delete/destructive mutations).**

This contract defines the shared job/idempotency/error semantics that
state-changing APIs must use. It enables only the child-specific failed
cloud-upload retry route; it does **not** enable deletion, cloud control,
Wi-Fi mutation, gadget mode mutation, or fsck start/cancel endpoints.

## 0. Security/product boundary (operator decision)

- B-1 currently remains **local-network-only** and **no-login** by operator
  decision (2026-08-10).
- Do **not** infer that this authorizes destructive mutations.
- Until an explicit operator-approved auth/CSRF design lands, dangerous
  mutation routes stay disabled/read-only.

## 1. Shared durable envelope fields

Every durable mutation job surface (HTTP response, SSE status, or daemon status
RPC) uses:

- `requestId` — logical request identity.
- `requestHash` — canonical request hash (`sha256` lowercase hex).
- `jobId` — stable job identity for polling/correlation.
  - accepted formats in this slice: `m-<digits>` (gadget queue ids),
    `<digits>` (legacy webd in-process ids).
- `owner` — owning subsystem (`gadgetd`, `indexd`, `uploadd`, `retentiond`,
  or `webd` adapter route).
- `kind` — mutation kind discriminator.
- `state` — one of:
  - `queued`, `running`, `done`, `failed`, `refused`, `busy`,
    `cancel_requested`, `cancelled`.
- `cancelRequested` — explicit cancellation flag, independent of terminal state.
- `sanitizedError` — bounded public error detail (max 160 chars).
- `statusUrl` — canonical polling URL.
- `idempotencyKey` — caller dedupe key (8..128 chars, ASCII alnum + `-_.:`).
  - required on retryable write commands once those commands are enabled.

No raw daemon stderr, backend secret material, or unbounded payload echo may be
surfaced in any envelope field.

## 2. Lifecycle and cancellation semantics

Valid transitions:

- `queued -> running | done | failed | refused | busy | cancel_requested | cancelled`
- `running -> done | failed | busy | cancel_requested | cancelled`
- `cancel_requested -> done | failed | busy | cancelled`
- terminal states (`done|failed|refused|busy|cancelled`) are replay-idempotent
  (same-state updates only).

Cancellation semantics for this foundation:

- cancellation is represented explicitly via `cancel_requested`/`cancelled`.
- subsystems that do not support cancellation yet must fail closed with a
  deterministic refusal code (`cancellation_unsupported`) and no state change.

## 3. Restart recovery semantics

- In-flight durable jobs (`running`/`cancel_requested`) recover to `queued`
  after restart unless the owning daemon can prove terminal completion.
- Existing implementations this contract aligns with:
  - `gadgetd` queue recovery requeues interrupted `applying` entries.
  - `indexd` cloud attempt ids remain idempotent across retry/replay so restart
    does not double-apply commit/fail rows.

## 4. Idempotency replay semantics

For a previously-seen `idempotencyKey`:

- same key + same `requestHash` => replay original accepted/result envelope.
- same key + different `requestHash` => deterministic **409 conflict** (no new
  mutation side effects).

## 5. Non-GET CSRF hardening checks (not authentication)

For non-GET mutation routes, enforce Host/Origin/Sec-Fetch-Site same-origin
evidence:

- `Host` is required and syntactically valid.
- `Origin` is required and must match `Host` authority.
- `Sec-Fetch-Site`, when present, must be `same-origin`, `same-site`, or `none`.

This hardening is **not** an auth substitute. It reduces cross-site trigger
risk while the operator-approved auth/CSRF session model is still pending.

Legacy pre-foundation mutation routes may still be on older per-route checks
(for example, host-required with optional `Origin`). Those routes are not this
contract's compatibility target; new durable mutation routes must enforce the
strict checks above.

## 6. Recording and gadget-handoff exclusion

Destructive/write mutations that can affect Tesla-facing media state must not
start while either condition is true:

- recording activity is active, or
- a gadget handoff is active.

When blocked, return a deterministic refusal (no side effects). Queue-only
acceptance is allowed only when ownership rules guarantee deferred safe apply.

## 7. Read-only contract visibility endpoints

Read-only disclosure endpoints are allowed in this foundation slice:

- `GET /api/jobs`
- `GET /api/jobs/failed`
- `GET /api/jobs/failed/uploads`
- `GET /api/jobs/capabilities`

They must not trigger mutations. `capabilities` should explicitly disclose that
current `JobHub` retention is in-memory and not restart durable.

## 8. Required tests for this foundation slice

- state transition allow/deny coverage (including cancellation transitions);
- restart-recovery projection coverage;
- `requestId`/`requestHash`/`jobId` validation coverage;
- idempotency replay/conflict behavior coverage;
- non-GET same-origin Host/Origin/Sec-Fetch check coverage;
- bounded/sanitized public error coverage;
- recording/handoff exclusion gate coverage.

## 9. Failed cloud-upload retry contract (route enable: retry only)

- Public delete mutation routes remain disabled in this slice.
- `POST /api/cloud/queue/{archive_item_id}/retry` is enabled for local-network
  operators with strict Host/Origin/Sec-Fetch-Site checks.
- The planned cloud retry command is **child-specific**:
  `(archive_item_id, child_key, upload_set_id?)` identifies one queue row.
- `upload_set_id` is an optional generation fence with explicit semantics:
  - sealed row: `upload_set_id` is required and must match exactly;
  - unsealed row: `upload_set_id` must be omitted;
  - shape: 32-char lowercase hex.
- Eligible source state is **only** `failed`.
- Deterministic rejects (no queue mutation) for source states:
  `done`, `queued`, `in_progress`, `parked`.
- `parked` remains a separate collision-resolution flow (`cloud_queue_retry`
  resolution modes), not a failed-upload retry.
- Idempotency persistence for retry commands stores request identity and target
  shape so same key+same hash can replay deterministically and same key+different
  hash yields `409`. Target identity includes `upload_set_id`, so retrying with a
  different fence is a deterministic conflict.
- indexd now carries persistence scaffolding (`cloud_failed_upload_retry_requests`,
  migration v9) for this idempotency lane: stable `job_id`/`request_id`,
  idempotency scope, owner/kind/state, target fence identity, sanitized
  response/status metadata, and timestamps.
- indexd now also exposes an **internal IPC-only** `cloud_failed_upload_retry`
  command that uses that durable envelope/persistence lane and returns explicit
  `accepted` / `replay` / `conflict` / `refused` outcomes with stable
  `job_id`/`request_id` identities.
- indexd remains the single writer for queue-state mutations; groundwork here is
  validation/contract/persistence scaffolding only.
- request hash semantics for this route:
  - webd computes canonical `request_hash` server-side from target + envelope;
  - request body `requestHash` is compatibility-only (accepted if present,
    validated for shape, ignored for idempotency decisions).
