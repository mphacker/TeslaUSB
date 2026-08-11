# Contract: indexd cloud-sync persistence (migration v6 + RPCs; **v7 sealed-set durability**)

Status: **v6 NOT FROZEN — reconciled design draft with OPEN ITEMS; §7 (migration v7
sealed-upload-set durability, scope B1) is FROZEN** (two independent Tier-3 designs
converged — Opus + GPT-5.5; trail in `files/sealed-set-durability-design.md`). Target surface
for **P1** (indexd), consumed by **P3**/**P4**. A twice-run Tier-3 adversarial
review overturned code-level premises in the first draft (see
`files/cloud-p0-review-reconciliation.md`, cycle 2); those are corrected here, and
the exact RPC **wire envelopes remain OPEN** — they are pinned in P1 against the
real `proto.rs`/`server.rs` `status`-tagged framing, not invented here.

> **⚠️ Corrected premises (verified against code):**
> - **Leases are boot-scoped monotonic ms, NOT unix seconds.** The v5 `leases`
>   table is `boot_id TEXT` + `expires_mono_ms INTEGER` (index
>   `idx_leases_expiry(boot_id, expires_mono_ms)`). The target is **RTC-less**;
>   wall-clock epoch expiry is unsafe. Lease RPCs use `ttl_ms` + return
>   `{boot_id, expires_mono_ms}`.
> - **The durable queue keys on a single `ArchiveItemId(i64)`** (`queue.rs`).
>   Per-child rows require a queue-identity change (§2.1) — this schema *defines*
>   the child key; the Rust `QueueItem`/`UploadQueue` types must change to match.
> - **The archive driver is `RecentClips`-only** (`folder_class:"RecentClips"`
>   hardcoded). Sentry/Saved *event* rows are **not produced today** (§4); cloud
>   upload of events is gated on that registration path.
>
> **OPEN ITEMS (pin in P1):** exact request/response JSON per verb incl. the
> `status`-tagged success/error envelope + error codes; cursor encoding + order +
> stability; the manual-retry verb (`cloud_queue_retry`, resolving parked
> collisions — not a plain upsert); collision storage (a differing-hash offer
> cannot share the one PK row with the existing object); `destination_id`
> lifecycle + **byte-exact `remote_key` normalization** (shared by dedup across
> all lanes).

indexd is the **single SQLite writer**. Cloud state consolidates here — **no
separate `cloud_sync.db`**.

> **Addendum (v9 scaffolding, no route enable):** migration v9 adds
> `cloud_failed_upload_retry_requests` for failed-upload retry request identity
> persistence (stable `job_id`/`request_id`, idempotency key/hash, owner/kind/state,
> target `archive_item_id`/`child_key`/`upload_set_id`, sanitized status metadata,
> timestamps, scoped uniqueness). indexd additionally has an internal IPC-only
> child-specific `cloud_failed_upload_retry` verb (`accepted`/`replay`/`conflict`/
> `refused`) built on this durable storage. Public retry/delete HTTP mutation
> routes remain disabled.

---

## 1. Reuse what already exists at v5 — do NOT re-add

Verified in `indexd/src/db/migrations.rs` (`LATEST_VERSION = 5`):

- **`leases`** with `kind IN ('upload','playback')` — the **upload lease already
  exists**. v6 adds only *verbs*, not the table.
- **`archive_items.durable`** — the durability flag. Cloud completion sets it
  (§3, D5); do not add a parallel `uploaded_verified` column.
- **`prefs(key, value)`** — the KV store webd already validates/forwards
  (`set_pref`). **Non-secret** cloud config lives here (§5).
- **`archive_items.id`** (i64) is the `ArchiveItemId` = the **parent event**.

Migration **v6 is additive-only** (forward-only invariant, `migrations.rs`): new
tables + indexes; no destructive change to v1–v5. It must be **idempotent** and
pass the existing up/forward-only migration tests.

## 2. New tables (v6) — DDL hardened (M7)

All columns `NOT NULL` unless a nullable is deliberate; every count/size has a
`CHECK (… >= 0)`; hashes `CHECK(length(hash)=64)` lowercase-hex; enums are
`CHECK(x IN (…))`; text fields length-capped.

### 2.1 `cloud_upload_queue` — one row per **child object** (D2, D7)
```
archive_item_id  INTEGER  -- FK archive_items(id); the PARENT event
child_key        TEXT     -- child discriminator (relative path within the event)
destination_id   TEXT     -- which configured remote (D7 identity component)
remote_key       TEXT     -- canonical destination object key (D7)
category         TEXT     CHECK(category IN ('event_sentry','trip','bulk'))
seq              INTEGER  CHECK(seq >= 0)          -- FIFO tiebreak
total_bytes      INTEGER  CHECK(total_bytes >= 0)
bytes_uploaded   INTEGER  CHECK(bytes_uploaded >= 0 AND bytes_uploaded <= total_bytes)
expected_hash    TEXT     -- backend verification value (algorithm-specific length); NULL until derived
verify_alg       TEXT     CHECK(verify_alg IN ('sha256','md5','crc32c','sha1','quickxor','dropbox','none'))
content_sha256   TEXT     CHECK(length(content_sha256) = 64)  -- always-present local identity (reused from archive copy)
state            TEXT     CHECK(state IN ('queued','in_progress','done','failed','parked'))
attempts         INTEGER  CHECK(attempts >= 0)
not_before       INTEGER  -- unix secs; retry backoff gate (M2), NULL = ready
last_error       TEXT     -- sanitized, capped (D8); NULL when none
PRIMARY KEY (destination_id, remote_key)          -- = the D7 dedup identity
```
`parked` = differing-hash collision on an existing key (D7); needs operator
action, never auto-overwritten.

### 2.2 `cloud_synced_files` — dedup oracle / durability cache (D7)
```
destination_id  TEXT
remote_key      TEXT
archive_item_id INTEGER
child_key       TEXT
content_sha256  TEXT CHECK(length(content_sha256)=64)  -- dedup identity component
verify_alg      TEXT
verify_value    TEXT  -- backend hash actually compared (algorithm-specific length); NULL if none
size_bytes      INTEGER CHECK(size_bytes >= 0)
synced_at       INTEGER
completion_seq  INTEGER CHECK(completion_seq >= 0)  -- monotonic; stats baseline (M1)
PRIMARY KEY (destination_id, remote_key)
```
Identity is `(destination_id, remote_key)` **+ hash/size** match. This table is a
**cache/oracle**, not the source of truth for a *file's* durability — the parent
event's `archive_items.durable` is (set in §3). A same-key/different-hash offer
does not dedup; it parks (§2.1).

### 2.3 `cloud_sync_history` — completed/failed transfer log (M1, M5)
```
id              INTEGER PRIMARY KEY
completion_seq  INTEGER CHECK(completion_seq >= 0)  -- shared monotonic counter (M1)
archive_item_id INTEGER
child_key       TEXT
destination_id  TEXT
outcome         TEXT CHECK(outcome IN ('uploaded','failed'))
size_bytes      INTEGER CHECK(size_bytes >= 0)
at              INTEGER
error_class     TEXT    -- sanitized class only on failure (D8); NULL otherwise
```

### 2.4 `cloud_meta` — cumulative counters + reset baseline (M1)
Single-row (or KV) table holding the **monotonic `completion_seq` allocator** and
the reset **baseline** (`stats_baseline_seq`, `stats_baseline_at`). Cumulative
Synced/Transferred are **derived** as `COUNT(*)/SUM(size_bytes)` over
`cloud_sync_history WHERE outcome='uploaded' AND completion_seq >
stats_baseline_seq` — **not** an incrementable integer (removes the D5
double-count race). "Reset counters" sets `stats_baseline_seq = current seq`.

### 2.5 `cloud_provider_config` — **non-secret** config (secrets: creds store)
Folders enabled, per-folder priority, `reserve_gb`, retry policy
(`max_attempts`, base backoff), toggles (`keep_until_backed_up`, auto-sync).
May be a typed projection of `prefs` keys rather than a new table — P1 decides,
but the **key set is frozen here**. **No credential material** ever lands here.

Indexes: `cloud_upload_queue(state, category, seq)` (ready-work scan),
`cloud_sync_history(completion_seq)`, `cloud_synced_files(archive_item_id)`.

## 3. RPC verbs (framed-JSON over the indexd control socket) — D6

**These do not exist in `proto.rs`/`server.rs` today.** P1 adds each as a
`{"cmd":…}` request with a **`status`-tagged** response envelope (matching the
existing indexd framing — the exact JSON per verb is an **OPEN item** pinned in
P1), mirroring `set_pref`. Frame cap = **64 KiB** (`MAX_FRAME`), 15 s timeout,
non-unix stub → `Unavailable`. **Lease time is boot-scoped monotonic ms**
(`ttl_ms` in, `{boot_id, expires_mono_ms}` out — never epoch); other ages are
unix seconds; all sizes bytes.

| cmd | args | returns | notes |
|---|---|---|---|
| `cloud_candidates` | `folders[]`, `after_cursor?`, `limit` | `{items[], next_cursor?}` | **paginated** (D6); ready-view over **already-seeded** queue rows; items carry `archive_item_id`, `child_key`, source path, size, **local `content_sha256`** (backend hash derived later by uploadd) |
| `cloud_discover` | `after_cursor?`, `limit` | `{items[], next_cursor?}` | **P1.5 — catalog seed.** Paginated list of **parent `archive_items`** still needing sync: `durable = 0`, `delete_state = 'LIVE'`, `folder_class` in the **config-enabled** set (from `cloud_provider_config`). Items carry `archive_item_id`, `folder_class`, `path`, `category`. indexd has **no FS access** so it never enumerates children — uploadd enumerates+hashes each parent's children and `cloud_queue_upsert`s them. Uses `idx_archive_candidate`. Re-emitting a parent is harmless (per-child upsert dedups). |
| `cloud_queue_load` | `after_cursor?`, `limit` | `{items[], next_cursor?}` | **paginated**; resume the durable queue on boot. **P1.5:** items MUST include `expected_hash` + `verify_alg` (write-only in v6) so uploadd can re-verify after a restart |
| `cloud_queue_upsert` | `item` | `{state}` | idempotent on `(destination_id, remote_key)`; returns `parked` on hash-collision |
| `cloud_queue_retry` | `archive_item_id`, `child_key?`, `resolution` | `{state}` | **manual retry** — resets attempts/backoff and resolves a `parked` collision (`keep_existing`/`rekey`/`replace`); NOT expressible via upsert |
| `upload_lease_acquire` | `archive_item_id`, `ttl_ms` | `{granted, token, boot_id, expires_mono_ms}` | `kind='upload'` on the existing `leases` table |
| `upload_lease_renew` | `token`, `ttl_ms` | `{ok, expires_mono_ms}` | called from uploadd's renewal thread (D1) |
| `upload_lease_release` | `token` | `{ok}` | |
| `cloud_upload_commit` | `queue_pk`, `attempt_id`, `hash`, `hash_alg`, `size` | `{ok, durable_parent}` | **one transaction (D5)**; commit fields **derived from the locked queue row**, keyed by `attempt_id` for idempotency — see §3.1 |
| `cloud_upload_fail` | `queue_pk`, `attempt_id`, `error_class`, `not_before`, `terminal` | `{ok, state}` | **idempotent on `attempt_id`** (a retried fail must not double-count attempts/history) |
| `cloud_stats_get` | — | `{synced_count, synced_bytes, since_at}` | derived vs baseline (M1) |
| `cloud_stats_reset` | — | `{ok, baseline_seq}` | sets baseline = current seq (M1) |
| `cloud_config_get` / `cloud_config_put` | (config) | (config) | non-secret only; validate like `set_pref` |
| `cloud_history_load` | `after_cursor?`, `limit` | `{items[], next_cursor?}` | **paginated**, capped limit (m2) |

There is **no** `cloud_stats_bump` verb (D5 — it was non-idempotent and
double-counted on retries; stats are derived).

### 3.1 `cloud_upload_commit` — the atomic transaction (D5)
**P1.5 — enforce the queued expectation (the commit is the durability authority,
so it must not blindly trust the client's verify).** The locked queue row carries
`expected_hash` + `verify_alg`; before recording durability, reject the commit
(`invalid_input`) unless the supplied `(hash, hash_alg)` satisfies it:
`verify_alg = 'none'` ⇒ require `hash = ''` (copy-integrity backends); otherwise
require `hash_alg == verify_alg` and, when `expected_hash` is non-NULL,
`hash == expected_hash`. The idempotent-replay guard must compare **per-alg** (the
v6 guard only compared `prior_hash` when `hash_alg = 'sha256'`). `content_sha256`
stays the dedup key and `attempts.hash` still stores it (its CHECK allows only
empty|sha256); the **backend** verify result is recorded in
`cloud_synced_files.{verify_alg, verify_value}` (already the case — do **not** try
to store a non-sha256 backend hash in `attempts.hash`). Then, in **one SQLite
transaction**, idempotent on `(destination_id, remote_key, hash)`:
1. allocate the next `completion_seq` from `cloud_meta`;
2. upsert `cloud_synced_files` (dedup row + `completion_seq`);
3. insert `cloud_sync_history(outcome='uploaded', completion_seq, …)`;
4. set `cloud_upload_queue.state='done'` for `queue_pk`;
5. **if every child of `archive_item_id` is now `done`**, set
   `archive_items.durable = 1` — else leave it 0.
Re-running with the same key is a no-op returning the prior result (crash-safe).
`durable_parent` in the reply tells uploadd/retentiond the event is fully backed
up.

## 4. Folder → category map — **based on real `folder_class`, gated** (M6)

Maps the **registered** `folder_class` (indexd `FolderClass`: `RecentClips`,
`SavedClips`, `SentryClips`, …) → `UploadCategory`. **Reality check:** the
Phase-1 archive driver **only ever registers `RecentClips`**
(`archive_driver.rs` hardcodes it), so today only the `RecentClips` row is
reachable. `SavedClips`/`SentryClips` are valid `FolderClass` values indexd can
*parse*, but **no archive rows are produced for them yet** — those rows depend on
the event-registration prerequisite (uploadd.md §2). `ArchivedClips` is a
retentiond **destination** concept, **not** a source `folder_class`, so it is
**not** a candidate key.

| registered `folder_class` | category | available today? |
|---|---|---|
| SentryClips | `event_sentry` | **no** — needs event registration |
| SavedClips  | `event_sentry` | **no** — needs event registration |
| RecentClips | `bulk` (or per-config) | **yes** (Phase-1) |

`cloud_auto_delete_old` semantics are **deferred** (M6): they conflict with the
"no unrequested delete" retention invariant and are out of P1 scope.

**P1.5 — `cloud_discover` (§3) filters on this map.** It seeds the queue only from
`archive_items` whose `folder_class` is in the **config-enabled** set
(`sentry_enabled`/`saved_enabled`/`recent_enabled` in `cloud_provider_config`),
mapped to `category` per the table above. `RecentClips` is a *valid* source when
`recent_enabled = 1` (Phase-1 archive rows exist for it today); the earlier
"never RecentClips" framing is retired. `ArchivedClips` remains a retentiond
**destination**, never a discovery key.

## 5. Retention coupling (P5)
`retentiond`'s `cloud_policy_satisfied` reads `archive_items.durable` (already the
gate model). v6 changes nothing structural — P5 wires the read + reconciles the
v1 `keep_until_backed_up` toggle into `cloud_provider_config`. No eviction-policy
redesign.

## 6. Tests (P1 acceptance)
Migration v5→v6 up + **idempotent re-run** + forward-only guard; every RPC
round-trip; `cloud_upload_commit` **idempotency** (double-commit = single history
row, `durable` flips only on last child) + **partial-children** (parent stays 0);
dedup match vs same-key-different-hash **park**; pagination cursor stability under
concurrent inserts; stats derivation vs a mid-stream `cloud_stats_reset`; DDL
`CHECK` rejections (negative size, `bytes_uploaded > total`, bad hash length, bad
enum).

## 7. Migration v7 — sealed-upload-set durability (Option B / B1) — FROZEN

**Supersedes §3.1 step 5 and the naive §5 `durable` read.** v6 flips
`archive_items.durable = 1` the moment every currently-associated child queue row
is `done` (`maybe_flip_parent_durable`, `cloud.rs:584-607`). That is a **footage-loss
bug**: it flips on a *count over the mutable queue*, so a partially-enumerated set, a
superseded generation, or a per-child mark can authorize eviction of footage whose
off-device copy is incomplete or stale. v7 rebinds `durable` to a **sealed upload
set** proven complete against an **immutable generation**. Two independent Tier-3
designs (Opus + GPT-5.5) converged on this; full trail + failure-mode table in
`files/sealed-set-durability-design.md`. **Additive/forward-only/idempotent** like v6
(`db/mod.rs` runs migrations transactionally, only above the recorded version).

**Scope B1 (operator-chosen):** build the protocol + shared-v7 schema + 3 RPCs +
uploadd sequencing, **fully unit/integration-tested now** (tests populate
`manifest_digest` by calling `finalize_event_archive` directly). Production **event
population** (the retentiond event-arm daemon: FolderFact producer + live catalog
seam) stays P0.5's separate lane. Until it lands, event rows have
`manifest_digest = NULL`, `cloud_prepare_parent_upload` **fails closed**, and no
event is cloud-evictable in production — this is intended, not a regression.

### 7.1 Two-axis separation (event-archive-registration.md §5.4)
- **Verification** (`verified_pass_id`, `manifest_digest`): the Pi-side copy is
  trustworthy. Owned by `finalize_event_archive`. Sets `durable = 0` on a new/changed
  generation; **never rewrites `durable` on idempotent replay**.
- **Durability** (`archive_items.durable`): a durable **off-device** copy exists →
  `retentiond` may evict the local copy. Owned **only** by
  `cloud_finalize_parent_upload`'s predicate (§7.4).

### 7.2 Shared v7 DDL (one v7; do NOT create a competing migration)
`archive_items` gains (all nullable; existing rows → NULL, never authorize durability):
- `manifest_digest TEXT` — **FNV-1a-128, 32 lowercase hex** (matches P0.5
  `manifest.digest()`; do NOT change that module). CHECK: NULL OR (len=32 ∧ lower ∧
  `NOT GLOB '*[^0-9a-f]*'`).
- `verified_pass_id TEXT` — 32-hex, same CHECK shape.
- `source_generation TEXT` (1–256) — **opaque, boot-qualified; NEVER lexically
  ordered** (scanner counters reset to 0 each reboot). Concurrency ordering is the
  CAS field in §7.3, never this.
- `source_event_key TEXT` (1–512), `source_volume_id TEXT` (1–128),
  `segment_set_digest TEXT` (64-hex SHA-256; P0.5 event identity — **reserved, NOT
  part of the durability predicate**).
- Partial `UNIQUE(source_volume_id, source_event_key) WHERE source_event_key IS NOT
  NULL`, **plus BEFORE INSERT/UPDATE guard triggers** rejecting a duplicate
  `source_event_key` when either side has `source_volume_id = NULL` (SQLite treats
  NULLs as distinct, so the index alone under-protects single-volume rows). Same key
  on two *known distinct* volumes is permitted.

New `cloud_parent_upload_sets`:
`upload_set_id TEXT PK` (random 128-bit, 32-hex), `archive_item_id INTEGER NOT NULL
REFERENCES archive_items(id) ON DELETE CASCADE`, `destination_id TEXT` (1–128),
`source_manifest_digest TEXT NOT NULL` (32-hex FNV), `request_digest TEXT NOT NULL`
(64-hex SHA-256 prepare idempotency key, §7.3), `expected_child_count INTEGER CHECK
>0`, `created_at`, `finalized_at`, `superseded_at`.
`UNIQUE(upload_set_id, destination_id)`; partial `UNIQUE(archive_item_id) WHERE
superseded_at IS NULL` = **one current set per parent**; index
`(archive_item_id, request_digest)`.

New `cloud_parent_upload_set_children`:
`upload_set_id`, `child_key` (1–512), `destination_id` (1–128), `remote_key`
(1–1024), `category IN ('event_sentry','trip','bulk')`, `seq ≥ 0`, `total_bytes ≥ 0`,
`manifest_mtime_ms`, `content_sha256` (64-hex SHA-256), `expected_hash` (1–256),
`verify_alg IN ('sha256','md5','crc32c','sha1','quickxor','dropbox')` — **`'none'`
forbidden for sealed children** (v6 commit validates `none` with only empty-hash+size,
insufficient to authorize deletion). `PK(upload_set_id, child_key)`,
`UNIQUE(upload_set_id, destination_id, remote_key)`, `FOREIGN KEY(upload_set_id,
destination_id) REFERENCES cloud_parent_upload_sets(upload_set_id, destination_id) ON
DELETE CASCADE`.

`cloud_upload_queue` gains `upload_set_id TEXT REFERENCES
cloud_parent_upload_sets(upload_set_id) ON DELETE SET NULL`; partial
`UNIQUE(upload_set_id, child_key) WHERE upload_set_id IS NOT NULL`; index on
`upload_set_id`. `cloud_upload_attempts` gains `upload_set_id TEXT` (same FK) so
attempt replay binds to the issuing generation (queue PK alone is unsafe — a
superseding prepare can retag the same `remote_key`).

**DB-level guard:** a `BEFORE UPDATE OF durable ON archive_items` trigger rejecting
`0→1` unless the §7.4 predicate holds (defense-in-depth against any stray future
setter). Existing `durable = 1` rows are not rewritten but are **not** eviction-
authorized until re-proven — see §7.6.

### 7.3 `finalize_event_archive` (P0.5 §5.2 D1–D7; the manifest_digest persistence)
Registers a whole verified event as ONE `archive_items` row. Single ≤64 KiB frame
(oversize ⇒ deterministic `event_too_large`, no DB write). Physical generation is
published+fsynced to a **fresh immutable dir BEFORE** the tx (a pre-commit crash
leaves only a GC-able orphan). Request carries `pass_id`, `source_event_key`,
nullable `source_volume_id`, opaque `source_generation`, `expected_prior_manifest_digest`
(**CAS**), `manifest_digest`, `segment_set_digest`, `expected_segment_count`,
`size_bytes`, `file_count`, `archived_at`, `generation_dir_path`, folder/partition +
sidecar facts, and the authoritative segment/clip/angle records.
- **Absent row:** require `expected_prior_manifest_digest = NULL`; create one complete
  `LIVE`, `durable = 0` row + links.
- **Exact replay** (stored `manifest_digest == incoming`, all aggregates/links/path
  match): return existing id, **write nothing — preserve `durable`**.
- **Conflict** (same digest, different path/aggregates/segment set/verification
  tuple): reject, do not "repair".
- **Changed generation:** require `expected_prior_manifest_digest == stored`, row
  `LIVE`, no unexpired current-boot upload/playback lease. Then: **mark any current
  cloud set superseded**, set `durable = 0`, atomically replace links+angles, write
  the verification tuple + aggregates, switch `path` to the new generation.
`register_archived_clip` is **UNCHANGED** (RecentClips carries no digest; see §7.6).

### 7.4 `cloud_prepare_parent_upload` / `cloud_finalize_parent_upload`
**prepare** `(archive_item_id, destination_id, source_manifest_digest, children[])`
→ `{upload_set_id, already_prepared}`. One tx:
1. **Reconstruct** the FNV-1a-128 manifest digest from the child array
   (`child_key`+`total_bytes`+`manifest_mtime_ms`+`content_sha256`, normalized like
   `manifest.rs`) and require **triple-equality**: `reconstructed ==
   source_manifest_digest == archive_items.manifest_digest`. This defeats
   "echo the digest, omit a child" independent of FNV's non-cryptographic nature
   (completeness ≠ collision-resistance; byte-integrity is the per-child SHA-256 in
   finalize).
2. Parent `LIVE`; digest non-NULL; child keys relative/traversal-free/unique;
   `(destination_id, remote_key)` unique; all children use the request destination;
   `verify_alg != 'none'`; `expected_hash` present; frame ≤64 KiB.
3. Compute SHA-256 `request_digest` over a domain tag + parent id + source digest +
   destination + the sorted length-prefixed child records. **Idempotent:** if the
   current set has the same `request_digest` and identical membership, return its
   `upload_set_id` (even if already finalized).
4. Else require `durable = 0`; reject any `remote_key` owned by another parent's
   current set; **mark a different current set superseded** and park its unfinished
   queue rows; insert the set + members + tag every corresponding
   `cloud_upload_queue` row `upload_set_id` **in the same tx** (sealing+enqueue are
   atomic — there is no separate enqueue phase). Dedup may init `state='done'` only
   when `cloud_synced_files` matches hash **and** size **and** alg **and** verify
   value (v6's hash/size-only dedup is too weak for a sealed row).

**finalize** `(upload_set_id)` → `{ok, durable_parent, already_finalized}`. Flip
`durable = 1` **iff** `COMPLETE(a, s)`:
```
COMPLETE(a,s) := a.delete_state='LIVE'
  AND s.archive_item_id=a.id AND s.superseded_at IS NULL
  AND a.manifest_digest IS NOT NULL AND a.manifest_digest=s.source_manifest_digest
  AND s.expected_child_count>0
  AND s.expected_child_count = COUNT(children WHERE upload_set_id=s.id)
  AND s.expected_child_count = COUNT(queue    WHERE upload_set_id=s.id)
  AND NOT EXISTS (member m LEFT JOIN queue q
        ON q.upload_set_id=m.upload_set_id AND q.destination_id=m.destination_id
       AND q.remote_key=m.remote_key
      WHERE m.upload_set_id=s.id AND (
        q.upload_set_id IS NULL OR q.state<>'done'
        OR q.content_sha256<>m.content_sha256 OR q.verify_alg<>m.verify_alg
        OR COALESCE(q.expected_hash,'')<>m.expected_hash
        OR q.child_key<>m.child_key OR q.category<>m.category
        OR q.seq<>m.seq OR q.total_bytes<>m.total_bytes))
```
In one tx set `s.finalized_at`, then conditionally `a.durable=1`. Incomplete-but-
current ⇒ `{ok:true, durable_parent:false}` (no mutation). Already-finalized+durable ⇒
read-only replay. Unknown/superseded/wrong-parent/non-LIVE/digest-mismatch ⇒
deterministic rejection, no mutation. **`cloud_upload_commit`/dedup/retry/fail NEVER
flip `durable`.**

### 7.5 Neutralize the two premature-flip paths + RPC table additions
- **DELETE** `maybe_flip_parent_durable` and its calls in `cloud.rs`
  (`~1005-1047` dedup upsert, `~1170-1172` retry, `~1511` commit). `cloud_upload_commit`
  keeps returning `durable_parent` **temporarily for wire compat but always `false`**
  (deprecated).
- **Remove/private** `set_durable` (`mutations.rs:734-747`); no generic durable setter
  survives. Replace uploadd's per-child `DurabilityClient::mark_uploaded_verified`
  (`durability.rs`, called `engine.rs:388`/`rclone.rs:359`) with the prepare/finalize
  seam; `commit`/`fail`/`retry` must carry `upload_set_id`; `KeepExisting`/retry must
  stop marking `done` unconditionally (`cloud.rs:1092-1105`) — only on matching synced
  evidence.

New RPC verbs (additive to `proto.rs` `Request`, `#[serde(tag="cmd")]`;
deploy indexd before its callers so older indexd rejects the unknown verb → fail closed):
| cmd | args | returns |
|---|---|---|
| `finalize_event_archive` | (§7.3 payload) | `{archive_item_id, already_finalized}` |
| `cloud_prepare_parent_upload` | `archive_item_id, destination_id, source_manifest_digest, children[]` | `{upload_set_id, already_prepared}` |
| `cloud_finalize_parent_upload` | `upload_set_id` | `{ok, durable_parent, already_finalized}` |

`cloud_discover` (§3) gains the `manifest_digest`/`path` needed to prepare; a
"current prepared sets needing finalization" read supports post-reboot resume.

### 7.6 Retention coupling — PROVEN_DURABLE (supersedes §5)
Eviction authorization becomes `PROVEN_DURABLE(a) OR allow_undurable`, where
`PROVEN_DURABLE(a) := a.durable=1 AND EXISTS current s (s.finalized_at IS NOT NULL AND
COMPLETE(a,s))`. Pre-v7 `durable=1` rows (written by the old buggy flip) keep the
boolean but are **not** eviction-authorized until a finalized set re-proves them —
fail-safe (temporary capacity starvation, never loss). `allow_undurable=true` remains
an explicit operator bypass this protocol cannot protect.

### 7.7 v7 acceptance tests (must pass before the lane is done)
Migration v6→v7 up + idempotent re-run + forward-only; the guard triggers
(NULL-volume duplicate rejected, two known volumes allowed; `durable` 0→1 blocked
unless COMPLETE). `finalize_event_archive`: absent/exact-replay(preserve durable)/
conflict/changed-generation(supersede+durable→0)/CAS-stale-reject/oversize-fail-closed.
prepare: triple-digest-equality (omitted child rejected), idempotent replay, supersede
+ park, `verify_alg='none'` rejected, remote-key-owned-by-another-parent rejected.
finalize: COMPLETE true→durable; every FALSE arm (missing/extra/swapped/mismatched/
non-done child, superseded, wrong-parent, non-LIVE, digest-mismatch) → no flip; replay
idempotent; commit/retry/fail never flip. Interleavings: reboot mid-upload (set/queue
persist, resume, finalize), event grows mid-upload (new generation supersedes, stale
finalize refuses), superseded worker's late commit (set-id mismatch rejects), crash
between publish and tx (orphan GC, no partial row), legacy `upload_set_id=NULL` rows
can never satisfy COMPLETE.
