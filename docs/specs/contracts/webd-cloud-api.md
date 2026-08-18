# Contract: webd `/api/cloud` HTTP surface

Status: **NOT FROZEN — reconciled design draft with OPEN ITEMS.** Target surface
for **P4** (webd) + **P6** (SPA). A Tier-3 review (cycle 2,
`files/cloud-p0-review-reconciliation.md`) confirmed the security prerequisite but
found the wire contract underspecified. Pinned in P4, not here.

> **OPEN ITEMS (pin in P4):**
> - **The anonymous mutation boundary** — exact Host/Origin/Sec-Fetch validation,
>   explicit confirmation fields, idempotency, target fencing, and exact
>   4xx envelopes. B-1 has no login/session layer by product decision.
> - Manual retry maps to the indexd **`cloud_queue_retry`** verb, not a plain
>   upsert. For the failed-upload parity lane, webd's future route is
>   **child-specific** and only retries rows currently in `failed`, with an
>   optional `upload_set_id` generation fence for sealed rows.
> - Redaction is enforced **before persistence** in the rclone engine, not only at
>   this edge (see the creds contract).

webd now ships a **read-only first slice**: `GET /api/cloud` (uploadd
`get_status`) + `GET /api/cloud/queue` + `GET /api/cloud/history` (durable
indexd-backed observability). The remaining
`/api/cloud/*` status/config/mutation lanes are still pending in P4.

---

## 0. Security/product boundary — anonymous local network
webd has no login/session layer by deliberate product design. The cloud surface
mutates credentials and triggers uploads, so mutating `/api/cloud/*` routes
must enforce strict Host/Origin/Sec-Fetch validation, bounded typed input,
explicit confirmation, idempotency, and durable owner-job semantics. Credential
writes additionally require provider-specific validation and redaction.
Authentication is not a prerequisite or a planned feature.

## 1. Transport & error mapping
JSON over the existing webd HTTP server. webd calls **indexd** for
state/config/history and an **uploadd control socket** for actions (D6, §4).

- indexd unavailable (non-unix stub / socket error) → **503** (mirrors the
  existing `set_pref` path).
- **Fix (m1):** `webd::indexd_client` currently maps read-timeout / connection-
  reset to `Protocol` → **500**; these are availability failures and must map to
  **`Unavailable` → 503** so the SPA shows "temporarily unavailable," not a bug.
- **Redacted errors (D8):** responses **never** echo raw rclone/backend output or
  submitted secret values. Errors are a **stable code + sanitized, length-capped
  message** (mirroring wifid ipc's "reason never echoes the submitted value").
  Test-connection returns a **classified** result (`ok | auth_failed |
  unreachable | config_invalid | timeout`), not verbatim stderr.

## 2. Endpoints

### Reads
- `GET /api/cloud` → **minimal read-only status slice** from uploadd control
  `get_status`: `{ configured, provider_type, uploader_state, sync_now_state }`.
  No secrets, no mutations. The full dashboard counters/queue summary are still
  pending in P4.
- `GET /api/cloud/config` → non-secret config (`cloud_config_get`): folders,
  priority, `reserve_gb`, retry, toggles. **Never** returns secrets.
- `GET /api/cloud/queue?cursor=&limit=` → paginated queue snapshot
  (`cloud_queue_load`); `limit` **server-capped** (m2).
- `GET /api/cloud/history?cursor=&limit=` → paginated history
  (`cloud_history_load`); `limit` server-capped (m2).
- `GET /api/jobs/failed/uploads?cursor=&limit=` → paginated **failed-upload
  history** only (`cloud_failed_history_load`), derived from durable
  `cloud_sync_history` rows where `outcome='failed'`; same cursor/limit rules
  and redaction as `/api/cloud/history`.

### Mutations (all anonymous-local-network safety gated — §0)
- `PUT /api/cloud/config` → validate (like `set_pref`) → `cloud_config_put`.
  Rejects unknown keys / out-of-range values with 400 + field error.
- `POST /api/cloud/provider` → set credentials for one flow (OAuth token paste /
  S3 form / NAS form or `rclone.conf` paste). webd **validates + type/‌key
  allow-lists** (`cloud-provider-creds.md` §4) **before** handing to the creds
  store; a rejected paste (multi-section, banned key, bad type) → **400 with a
  sanitized reason**. On success the blob is (re)written and uploadd is signaled
  to reload (D9, §4).
- `POST /api/cloud/provider/test` → `rclone lsd teslausb:` (or `rclone about`)
  via the uploadd control socket; returns the **classified** result (§1), never
  raw stderr.
- `POST /api/cloud/sync-now` → asks **uploadd** (control socket, §4) to run a
  candidate/enqueue/drain pass now. 202 + current status; **never** blocks on the
  transfer.
- `POST /api/cloud/reset-counters` → `cloud_stats_reset` (sets the stats baseline
  — M1). Returns the new baseline.
- `POST /api/cloud/queue/{archive_item_id}/retry` (**enabled for local-network
  operators**) → retry exactly one failed child via indexd
  `cloud_failed_upload_retry`. Path segment is
  the **numeric `archive_item_id`** (M5) — **not** a slash-bearing remote key.
  Body includes a **required** `child_key` plus durable-mutation envelope fields
  (`requestId`, `idempotencyKey`) and an **optional compatibility**
  `requestHash` field. webd computes the canonical request hash server-side for
  idempotency persistence; if the client sends `requestHash`, webd validates
  shape only and ignores it for idempotency decisions. Body also supports the
  optional `upload_set_id` fence (`32`-char lowercase hex). For sealed rows
  `upload_set_id` must be present and match; for unsealed rows it must be
  omitted. Retry request identity includes `(archive_item_id, child_key,
  upload_set_id)` so a fence change is a deterministic idempotency conflict.
  Route rejects source states `done|queued|in_progress|parked`; delete stays
  disabled.

## 3. FailedJobs / JobHub wiring (M5)
Upload failures already have a home: `JobHub` + `FailedJobs.tsx` + the
`upload_queue` SSE seam (present, unfed). P4 feeds them from the **persistent**
`cloud_upload_queue` / `cloud_sync_history` (not an in-memory ring that dies on
restart):
- failed/parked children surface as `JobStatus` entries with a **sanitized**
  `error_class` (no raw stderr — D8),
- the SSE `upload_queue` topic emits state transitions,
- the manual-retry action maps to the
  `POST /api/cloud/queue/{archive_item_id}/retry` contract above (child-specific,
  failed-only).

## 4. uploadd control socket (D6)
`sync-now`, `provider/test`, and the provider-reload signal need a **live uploadd
process**, not indexd. The first implemented control-socket slice is read-only:

- **Path:** `/run/teslausb/uploadd.sock` (overridable by daemon/env config)
- **Framing:** 4-byte little-endian length + JSON payload (same family as
  indexd/wifid/gadgetd), frame cap **64 KiB**
- **Timeout:** 15 s socket read/write
- **Verb:** `{"cmd":"get_status"}`
- **Success envelope:** `{"status":"uploadd_status","configured":bool,
  "provider_type":string|null,"uploader_state":string,"sync_now_state":"unsupported"}`
- **Error envelope:** `{"status":"error","message":"..."}`

Mutation verbs (`sync_now`, `test_remote`, `reload_credentials`) remain pending
and are explicitly deferred until the anonymous-local-network request-forgery
boundary is in place and each mutation has durable accepted/job semantics.
Until uploadd is enabled (Phase 8 gate), webd returns **503 "uploader offline"**
rather than a hang or a 500.

## 5. Deferred (post-P0)
- A remote **browse** endpoint (list objects on the remote) — **post-P0** (m2);
  not needed for parity MVP and adds bandwidth/enumeration cost.

## 6. Tests (P4 acceptance)
Accept/reject/persist per endpoint; **request-forgery boundary**: every
mutating route rejects missing or mismatched Host/Origin/Sec-Fetch metadata with
400/403 and **does not** touch creds/indexd;
provider paste rejection (multi-section, banned key, `type=wasabi` normalized to
`s3`) → 400 sanitized; **no endpoint ever returns raw rclone stderr or a secret**
(assert on redaction); indexd-down → 503 (incl. the m1 timeout/reset remap);
uploadd-offline → 503 "uploader offline"; retry route accepts the numeric
`archive_item_id` and rejects a non-numeric segment; stats reflect a
`reset-counters` baseline.
