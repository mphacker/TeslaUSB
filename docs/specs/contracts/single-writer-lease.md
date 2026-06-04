# Contract D3 — single-writer lease + single-deleter

```
Contract-Version: 0.1 (DRAFT)
Lease store:    indexd            (sole SQLite writer — all lease mutations funnel here)
Lease holders:  webd (playback)   uploadd (upload)
Sole deleter:   retentiond        (only service that unlinks Pi-side archive files)
Binds tasks:    4.2 (leases table) · 5.1b (playback lease) · 6.3 (upload lease) · 6.1e (governor honors)
```

**Derives from:** [`storage.md §4.1,§5,§5.1,§5.2,§5.3`](../storage.md) ·
[`indexd.md §2.1,§2.4,§4`](../indexd.md) · [`webd.md §2.3`](../webd.md) ·
[`uploadd.md §2.2,§2,§3`](../uploadd.md) · [`SPEC.md §6.1,§7`](../SPEC.md) ·
[`tasks.md` 4.2 / 5.1b / 6.1e / 6.3](../../tasks/tasks.md).

> Two invariants this contract guarantees:
> **(L)** an in-flight read/upload is **never** evicted (a held lease blocks
> deletion); **(D)** exactly **one** service (`retentiond`) unlinks archive files,
> via a crash-safe rename-then-unlink protocol idempotent across power loss.

---

## 1. Why a lease (and why a single deleter)

`retentiond`'s space governor evicts the least-valuable safe archived item when the
card gets low ([`storage.md §4`](../storage.md)). Meanwhile `webd` may be streaming
a clip and `uploadd` may be transferring one. Deleting a file mid-read/upload would
corrupt the operation. Conversely, letting `uploadd` or `webd` *also* delete files
creates upload/delete races and stale index rows
([`storage.md §5`](../storage.md)). So:

- **Leases** make in-use items un-evictable, with a **TTL** so a crashed holder
  can't pin a file forever.
- **Single deleter** (`retentiond`) plus a **single DB writer** (`indexd`) gives one
  global ordering and no two-services-each-assume-the-other races.

The `leases` table and `archive_items.delete_state` columns are defined in
[D1 §2](./indexd-schema.md); this contract defines the **protocol** over them.

---

## 2. The single-writer rule (how holders mutate state)

`indexd` is the **sole SQLite writer** ([`indexd.md §2.1`](../indexd.md),
[`storage.md §5.2`](../storage.md)). Therefore **`webd` and `uploadd` do not write
the `leases` table directly.** They call an `indexd` **lease IPC** (acquire / renew
/ release); `indexd` performs the transaction. Likewise `retentiond` asks `indexd`
to advance `delete_state` (`DELETE_CLAIMED → DELETING → DELETED`); it never writes
those rows itself.

> **OPEN (OQ-2): the IPC wire transport is unspecified by the specs.** `SPEC.md §6.1`
> lists `/run/teslausb/*.sock` (Unix domain sockets) and `gadgetd.md §4` sketches a
> typed request/response over a UDS — the natural model. Candidate: a small typed
> UDS RPC on `indexd` (`/run/teslausb/indexd.sock`). Confirm transport + framing
> (length-prefixed JSON vs. a binary codec) at freeze.

### 2.1 Lease IPC (proposed shape)

The canonical subject is an **`archive_item`** (RECOMMENDED RESOLUTION of OQ-1; see
[D1 §6 OQ-1](./indexd-schema.md)). A `webd` playback/export request on a *clip*
expands to leases on **every** backing `archive_item` (join `archive_item_clips`)
in one transaction — all-or-nothing, so a partially-protected clip can't stream.

```
acquire(archive_item_id, kind, holder, ttl_s)
        -> Granted { lease_id, gen, expires_mono_ms }
         | Denied  { reason }            # item already DELETE_CLAIMED+, or doesn't exist
acquire_for_clip(clip_id, kind, holder, ttl_s)        # convenience: leases ALL backing items
        -> Granted { leases:[{archive_item_id, lease_id, gen, expires_mono_ms}] }
         | Denied  { reason }            # atomic: if ANY backing item is unclaimable, none granted
renew(lease_id, gen, ttl_s)
        -> Renewed { expires_mono_ms }
         | Stale  { reason }             # see 2.2 — gen mismatch OR already-expired OR not LIVE
release(lease_id, gen)
        -> Released | NoOp
```

- `gen` is a 128-bit token returned at acquire; renew/release must present it so a
  delayed message from a crashed-then-restarted holder can't extend or drop a lease
  that was already reaped and re-granted.
- `holder` identifies the service + instance (`uploadd`, `webd:<conn-id>`), purely
  for diagnostics/`/api/storage` reporting ([`storage.md §6`](../storage.md)).
- The **governor preemption** verbs (`request_release` / `cancel`) are in §5 — they
  let `retentiond` reclaim a low-value upload lease under Emergency rather than
  waiting out the TTL ([`storage.md §3.1`](../storage.md)).

### 2.2 Heartbeat / TTL (renew is strictly conditional)

The holder **renews while the operation is active**
([`storage.md §4.1`](../storage.md), [`webd.md §2.3`](../webd.md),
[`uploadd.md §2.2`](../uploadd.md)): `webd` renews while a stream/export is in
flight; `uploadd` renews while a transfer runs. If a holder crashes, its lease is
**not renewed**, the monotonic deadline passes, and the item frees — no manual
cleanup.

**`renew` is a single `indexd` transaction that returns `Stale` unless ALL hold:**
1. `lease_id` + `gen` match an existing row, **and**
2. the lease is **not already past its deadline** (`expires_mono_ms > mono_now`),
   **and**
3. the subject `archive_item` is still `LIVE` (not `DELETE_CLAIMED`+).

This closes the resurrection race (rubber-duck #2): a holder cannot "renew back to
life" a lease the governor already treated as expired/abandoned. Past-deadline ⇒
`Stale` ⇒ the holder must stop the operation and (if still wanted) `acquire` afresh,
which will be `Denied` if the item is now being deleted.

**Deadlines are boot-scoped monotonic** (D1 `leases.expires_mono_ms` + `boot_id`),
not wall-clock — the Pi has **no RTC**, so a clock jump must never pin a live lease
forever nor reap an active one (same rationale `storage.md §5.1` uses to forbid
wall-clock in trash gens). On a new `indexd` boot, every lease from a prior `boot_id`
is stale by definition and reaped at startup (§4.2).

> **OPEN (OQ-5): TTL + renew interval are HW-tunable.** Proposed defaults (flag
> `TUNABLE`, validate on hardware per [`storage.md §7`](../storage.md)):
> `ttl_s = 60`, renew every `ttl_s/3 ≈ 20 s`. Long enough to survive a GC pause /
> brief stall, short enough that a crashed holder frees the item within ~1 min.

---

## 3. The eviction gate (how the governor honors leases)

Before evicting a candidate, `retentiond` treats it as a **hard exclusion** if it
has any **unexpired** lease ([`storage.md §4.1`](../storage.md)). "Unexpired" =
`boot_id == current && expires_mono_ms > mono_now`. A lease past its deadline (or
from an older boot) is ignored (and reaped by `indexd`). Pinned / undurable-Saved /
in-grace / `QUARANTINED` items are likewise excluded — leases are one gate among
those.

Because both the lease check and the delete claim run **through `indexd`** in a
transaction, the check-then-claim is atomic: an item cannot be leased *and* claimed
for delete at the same instant.

```
retentiond: pick lowest-value candidate
            -> indexd: BEGIN;
                 if EXISTS unexpired lease on subject  -> ABORT (skip candidate)
                 else set delete_state = DELETE_CLAIMED
               COMMIT
```

A new playback/upload lease **cannot** be granted on an item already
`DELETE_CLAIMED`+ (the `acquire` returns `Denied`) — so a stream can't start on a
file the governor just claimed. (See [D1 §2 leases / archive_items](./indexd-schema.md).)

---

## 4. The single-deleter protocol (crash-safe, `retentiond` only)

Verbatim alignment with [`storage.md §5.1`](../storage.md). **Only `retentiond`
unlinks**; `webd`/`uploadd`/`scannerd` never unlink archive content
([`storage.md §5`](../storage.md), [`uploadd.md §2.2,§3`](../uploadd.md)).

Delete-state column lives on `archive_items` (D1): `LIVE → DELETE_CLAIMED →
DELETING → DELETED` (`DELETE_FAILED` / `QUARANTINED` for anomalies).

1. `retentiond` asks `indexd` for candidates (value-sorted, lease-excluded).
2. `indexd` marks the row `DELETE_CLAIMED` in a transaction (the gate in §3).
3. `retentiond` **renames within the same filesystem** into a trash dir:
   `archive/<…>/event123` → `archive/.retention-trash/event123.<gen>.deleting`
   where `<gen>` is a **random 128-bit token, never wall-clock** (the Pi has no RTC;
   a clock reset must not collide trash names), then **fsync the source parent dir**.
4. `indexd` marks `DELETING`.
5. Recursively delete the trash entry; **fsync the trash parent**.
6. `indexd` marks `DELETED(bytes_freed)`.

### 4.1 Startup recovery matrix (verbatim, [`storage.md §5.1`](../storage.md))

| DB state | FS state | Action |
|----------|----------|--------|
| `DELETE_CLAIMED` | original present | retry or release → `LIVE` |
| `DELETE_CLAIMED` | trash present | continue delete |
| `DELETING` | trash present | finish delete |
| `DELETING` | neither present | mark `DELETED` |
| `LIVE` | trash present | `QUARANTINED` (investigate) |
| `DELETED` | original present | `QUARANTINED` |
| (any) | file with no row | orphan → safe to remove |
| row | no file | mark `DELETED` |

This FS↔DB reconciliation runs **once at startup**, scoped to `.retention-trash` and
non-`LIVE`/anomalous rows — not a per-cycle walk
([`storage.md §5.1`](../storage.md)).

**Who drives it (recovery ownership — rubber-duck #3).** `retentiond` owns the
recovery sweep, but since it cannot write the DB it drives each matrix action
through idempotent `indexd` RPCs:

```
retentiond startup:
  scan .retention-trash/  +  ask indexd for rows in {DELETE_CLAIMED, DELETING,
                              DELETE_FAILED} (and orphan check)
  for each: compute (DB state, FS state) -> matrix action -> call the matching
            idempotent indexd RPC:
              release_delete_claim(item_id)     # -> LIVE
              mark_deleting(item_id)            # continue
              mark_deleted(item_id, bytes)      # finish
              quarantine(item_id, reason)       # anomaly
```

Each RPC is idempotent (safe to re-apply if power is lost again mid-recovery).
`indexd` and `gadgetd` are **never** restarted to unblock this; only the offending
*reader* may be ([`storage.md §5.2`](../storage.md)).

### 4.2 Lease recovery at startup

Leases are **boot-scoped** (D1 `leases.boot_id`). On `indexd` startup it mints a new
`boot_id`; **every lease carrying a prior `boot_id` is stale and reaped** — operations
(stream/upload) restart from scratch and re-`acquire`, so nothing is "continued"
across a restart. Within a live boot, a lease is reaped when
`expires_mono_ms <= mono_now`. Because deadlines are monotonic, a wall-clock jump
(no RTC) can neither pin a dead lease forever nor reap a live one.

---

## 5. Governor preemption of low-value leases (Emergency)

[`storage.md §3.1`](../storage.md) lets the Emergency tier **"cancel low-value upload
leases if safe."** Acquire/renew/release alone can't do that — the governor would
have to wait out the TTL while the card is critically full. So the lease IPC adds a
**cooperative preemption** path (rubber-duck #4 + GPT-5.5 #2):

```
retentiond -> indexd:  request_release(lease_id, reason)     # sets leases.preempt_req=1
indexd     -> (holder observes preempt_req on next renew, OR via a push notify)
holder (uploadd): checkpoint the transfer (resumable), release(lease_id, gen)
retentiond:        only after the lease is released/expired -> proceed to DELETE_CLAIMED
```

Rules:
- **Cooperative first.** `request_release` asks; it does **not** force-delete a
  leased file. The holder gets a bounded window to checkpoint and release.
- **Bounded.** If the holder doesn't release within a `preempt_grace` (TUNABLE,
  proposed = one TTL), the lease lapses by its monotonic deadline anyway (the holder
  must keep renewing to hold it, and a well-behaved holder honoring `preempt_req`
  stops renewing). The governor never unlinks a still-leased file.
- **Only low-value, only when safe.** `retentiond` preempts an **upload** lease only
  for an item whose durability/value makes eviction safe per
  [`storage.md §4`](../storage.md); a **playback** lease (active user stream) is not
  preempted — the user is watching it.
- **Resumable.** `uploadd` checkpoints so the durable queue resumes the transfer
  later ([`uploadd.md §2.1,§4`](../uploadd.md)); preemption is a pause, not a loss.

---

## 6. Interaction with `uploadd` durability (no double authority)

`uploadd`, on a verified upload, asks `indexd` to set `archive_items.durable = 1`
(`UPLOADED_VERIFIED`) — this is what later lets `retentiond` treat the local copy as
safe to evict ([`uploadd.md §2.2`](../uploadd.md)). `uploadd` **never deletes** the
local file ([`uploadd.md §2.2,§3`](../uploadd.md)); it only (a) holds an upload lease
while transferring and (b) flags durability. Deletion remains `retentiond`'s alone.

The WAL-blocked-checkpoint escalation ([`storage.md §5.2`](../storage.md)) is
consistent with leases: an active reader registers its open read txn / lease so the
blocker is identifiable; if a truncate stays blocked past Critical the governor may
restart the offending **reader** (`webd`/`uploadd`) — **never** `indexd` or
`gadgetd`.

---

## 7. Proposed shared Rust types (illustrative — `teslausb-core::contracts`)

```rust
// teslausb-core::contracts::lease  (doc-only proposal; no .rs produced by this lane)
pub enum LeaseKind { Upload, Playback }

// Canonical subject is an archive_item (OQ-1 recommended resolution). A clip-level
// request is expanded server-side to one LeaseRequest per backing archive_item.
pub struct LeaseRequest {
    pub archive_item_id: i64,
    pub kind:            LeaseKind,
    pub holder:          String,
    pub ttl_s:           u32,
}
pub enum LeaseGrant {
    Granted { lease_id: i64, gen: u128, expires_mono_ms: i64 },  // monotonic, boot-scoped
    Denied  { reason: String },
}
pub enum RenewResult {
    Renewed { expires_mono_ms: i64 },
    Stale   { reason: String },          // gen mismatch | past deadline | subject not LIVE
}
pub enum DeleteState {
    Live, DeleteClaimed, Deleting, Deleted, DeleteFailed, Quarantined,
}
```

Exact placement (`teslausb-core` vs a new `contracts` crate) is the integrator's
call; `teslausb-core` is the natural home since it is the pure-logic shared-types
crate ([`SPEC.md §6`](../SPEC.md)) and already declares "shared domain types".

---

## 8. OPEN QUESTIONS

1. **(OQ-1) Lease subject identity — RECOMMENDED RESOLUTION pending freeze.** Both
   contract reviews converged on **`archive_item` as the only leasable/evictable
   subject**, with a clip-level request expanding (atomically) to leases on all
   backing `archive_items` via `archive_item_clips`, and **non-archived live/RO clips
   explicitly not retention-leasable** (best-effort playback only — the car may
   rotate them; an unbounded snapshot is rejected by
   [`SPEC.md §3`](../SPEC.md)). Encoded in §2.1 + [D1 §2](./indexd-schema.md);
   **operator/integrator to ratify**.
2. **(OQ-2) Lease/delete IPC transport** — UDS + framing (length-prefixed JSON vs.
   binary). Proposed: typed UDS RPC on `/run/teslausb/indexd.sock`, mirroring
   `gadgetd.md §4`. Keep consistent with D4's `wifid.sock`.
3. **(OQ-5) TTL + heartbeat + `preempt_grace` defaults** — proposed `ttl_s=60`,
   renew ~20 s, `preempt_grace=one TTL`, all flagged TUNABLE; validate on HW
   ([`storage.md §7`](../storage.md)).
4. **Trash dir location & quota.** `archive/.retention-trash/` is named in
   `storage.md §5.1`; confirm it shares the archive filesystem (must, for rename to
   be atomic) and whether trash bytes count against the governor budget until
   unlinked.
5. **Preemption notify mechanism.** §5 lets the holder observe `preempt_req` on its
   next `renew` **or** via a push notify. Confirm whether a push (lower latency, but
   needs a holder→indexd subscription) is wanted, or poll-on-renew suffices given
   the ~20 s renew cadence.
