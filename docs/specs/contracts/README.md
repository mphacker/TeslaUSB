# Cross-service contracts (B-1 reset)

> Status: **DRAFT** — these are seam contracts derived from the spec set in
> [`docs/specs/`](../README.md). They are drafted by the Contracts lane and are
> **not frozen**: the integrator reviews, reconciles the OPEN QUESTIONS with the
> operator, and bumps each to `1.0 (FROZEN)` before the dependent build tasks
> (4.2, 5.1, 6.1e, 6.2, 6.3) bind to them.

## Why this directory exists

`docs/tasks/plan.md §8` calls out the work that is **"needs coordination
(contract-first)"**: the `indexd` SQLite schema must be fixed before
`webd`/`retentiond` read it; the `webd` REST/SSE API shape must be fixed before
the SPA screens fan out; the cross-cutting **lease** store must stay consistent
across `webd` (playback), `uploadd` (upload) and `retentiond` (governor); and the
`wifid`↔`uploadd` throttle must be one agreed shape. Those seams were described
prose-style across the per-service specs. This directory pulls each into **one
versioned contract** the parallel builders bind to.

`SPEC.md` stays the index and the shared invariant; these contracts are the
detailed seams it points at. They never override `SPEC.md` — when a contract and
the #1 invariant ([`SPEC.md §2`](../SPEC.md)) conflict, the invariant wins.

## The four contracts

| # | Contract | Owns the seam between | Maturity | Binds tasks |
|---|----------|-----------------------|----------|-------------|
| D1 | [`indexd-schema.md`](./indexd-schema.md) | `indexd` (writer) ↔ `webd`/`retentiond`/`uploadd` (readers) | **DRAFT — PROVISIONAL** | 4.2 → 5.1, 6.1, 6.3 |
| D2 | [`webd-api.md`](./webd-api.md) | `webd` (server) ↔ SPA (client) | DRAFT (near-final) | 5.1 → 5.2–5.x |
| D3 | [`single-writer-lease.md`](./single-writer-lease.md) | `webd`/`uploadd` (lease holders) ↔ `indexd` (lease store) ↔ `retentiond` (deleter) | DRAFT (near-final) | 4.2, 5.1b, 6.1e, 6.3 |
| D4 | [`wifi-upload-throttle.md`](./wifi-upload-throttle.md) | `wifid` (cap owner) ↔ `uploadd` (consumer) | **DRAFT — PROVISIONAL** | 6.2 ↔ 6.3 |

D1 owns the *shape* of the `leases` + `archive_items.delete_state` columns; D3
owns the *protocol* over them. D2 references D1's read shapes and D3's lease
lifecycle; D4 references D2's `/api/cloud/*` + storage-health surfaces.

### Maturity & ratification (integrator disposition, 2026-06-04)

All four are **DRAFT** (`0.1`) — none frozen. Two are additionally **PROVISIONAL**:
their *shape* is stable but specific values/columns may be **amended** before they
are ratified at the wave that implements them:

- **D1 (indexd schema) — PROVISIONAL.** May be amended by the storage-governor
  calibration (delete / fsync / WAL latencies — [`storage.md §7`](../storage.md))
  before the schema is locked. **Ratified at Phase 4** (the `indexd` build, task 4.2).
- **D4 (wifi↔upload throttle) — PROVISIONAL.** The TX cap / chunk ceiling depend on
  the **measured SDIO-deadlock TX threshold** (Phase 2 hardware spike #4); the shape
  is fixed but the numbers are placeholders. **Ratified at Phase 6** (the
  `wifid`/`uploadd` build, tasks 6.2/6.3).
- **D2 (webd API) and D3 (lease / single-deleter)** are parity- and spec-driven (no
  pending HW dependency) and are treated as **near-final DRAFT**. Ratified at their
  implementing waves: **D2 → Phase 5**, **D3 → Phases 4–6** (lease store at 4, holders
  at 5–6).

Provisional ≠ unstable: builders may bind to the shapes now; only the flagged
`TUNABLE` numbers and the storage-governor-dependent D1 details can still move.

## Conventions

- **Versioning.** Each contract has a `Contract-Version: X.Y (DRAFT|FROZEN)`
  header. Drafts are `0.y`. The integrator freezes to `1.0`. Post-freeze, additive
  changes bump the minor; a breaking change bumps the major and updates every
  dependent task card. **PROVISIONAL** (D1, D4) is an orthogonal flag meaning
  "DRAFT, and may still be amended by a pending HW spike / calibration before
  ratification" — see the maturity table above.
- **Spec citations.** Every non-obvious decision cites the exact `SPEC.md`/service
  spec section it derives from. Where the specs are silent or ambiguous, the
  decision is **not invented** — it is listed under **OPEN QUESTIONS** for the
  operator/integrator.
- **Rust signatures are illustrative.** Proposed types (e.g. a
  `teslausb-core::contracts` module) appear as markdown code blocks to pin the
  shape; **no `.rs` files and no `Cargo.toml`/`Cargo.lock` edits** are produced by
  this lane — the integrator wires the actual crate.
- **HW-measured values are placeholders.** Anything `SPEC.md §9` marks
  "measure on hardware" (lease TTLs, throttle Mbps/chunk) appears as a flagged
  `TUNABLE` placeholder, never a guessed constant baked into the contract.

## OPEN QUESTIONS roll-up (full detail in each doc)

Two of the original questions reached a **recommended resolution** during the
rubber-duck + GPT-5.5 review and are flagged *pending operator ratify* rather than
open-ended; the integrator still owns the final ratification.

1. **Lease subject identity** (D1/D3) — **RESOLVED (pending ratify):** the
   `archive_item` is the sole leasable/evictable subject; a playback request leases
   **all** backing archive_items via the new `archive_item_clips` map
   (`acquire_for_clip`). Non-archived live/RO clips are not retention-leasable (the
   car rotates them).
2. **Lease/IPC transport** (D2/D3/D4) — **OPEN:** wire format (UDS at
   `/run/teslausb/*.sock`; length-prefixed JSON vs. binary) for lease mutations and
   throttle/heartbeat reads. One consistent choice across all services.
3. **`trip_points` storage** (D1) — **OPEN:** per-point rows vs. compacted polyline
   blob; also gates the D2 `/api/clips/:id/telemetry` source (persisted vs.
   re-extracted).
4. **Throttle authority boundary** (D4) — **RESOLVED (pending ratify):**
   belt-and-braces — `wifid` owns the hard `tc` cap, `uploadd` cooperatively
   self-paces. Storage backpressure is a **separate** `retentiond → uploadd` plane,
   not routed through `wifid`.
5. **TTL/heartbeat + throttle + reboot-gate defaults** (D3/D4) — **OPEN (TUNABLE):**
   lease TTL/renew, throttle Mbps/chunk, and `reboot_idle_grace_ms` are all
   HW-measured; contract carries flagged placeholders.
6. **SSE vs long-poll** (D2) — **OPEN:** propose SSE primary with a `GET
   /api/handoff/:id` poll fallback; confirm.
7. **Delete `target` + authority split** (D2/D3) — **OPEN:** confirm default
   `target=car` (gadgetd handoff) and that Pi-archive delete (`target=archive`) is
   `retentiond`-only (single-deleter), never a handoff.
