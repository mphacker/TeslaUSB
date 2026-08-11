# Contract: shared durable mutation job foundation (B-1)

Status: **Foundational slice only (no new user mutations enabled).**

This contract defines the shared job/idempotency/error semantics that future
state-changing APIs must use. It does **not** enable deletion, cloud control,
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
