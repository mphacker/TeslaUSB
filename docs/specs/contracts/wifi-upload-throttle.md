# Contract D4 — wifi ↔ upload throttle

```
Contract-Version: 0.1 (DRAFT — PROVISIONAL)
Cap owner:   wifid     (owns + enforces the TX cap; publishes throttle state)
Consumer:    uploadd   (subscribes; self-limits; pauses on demand)
Binds:       6.2 (wifid) ↔ 6.3 (uploadd)
```

> **PROVISIONAL:** the contract *shape* is fixed and bindable now, but the TX cap /
> chunk-ceiling numbers depend on the **measured SDIO-deadlock TX threshold**
> (Phase 2 hardware spike #4) and stay `TUNABLE` placeholders until then. **Ratified
> at Phase 6** (the `wifid`/`uploadd` build). See [`README.md` maturity table](./README.md).

**Derives from:** [`wifid.md §1,§2.3,§2.4,§3`](../wifid.md) ·
[`uploadd.md §2.3,§3,§4`](../uploadd.md) · [`SPEC.md §2`](../SPEC.md) ·
[`tasks.md` 6.2 / 6.3](../../tasks/tasks.md) · spike #4 ([`SPEC.md §9`](../SPEC.md)).

> WiFi is **convenience, not a reliability dependency**
> ([`wifid.md §1`](../wifid.md)). This contract keeps cloud upload **under the
> BCM43436 SDIO-deadlock threshold** so a saturated WiFi link can never wedge the
> chip and endanger the #1 invariant ([`SPEC.md §2`](../SPEC.md)), and makes uploads
> **pause cleanly** when the link can't carry them (AP mode, chip recovery,
> governor backpressure).

---

## 1. The problem this seam solves

`uploadd` wants to push archived clips as fast as possible; the Pi Zero 2 W's
Broadcom chip shares one **SDIO bus** with the SD card, and sustained TX can trip a
**deadlock** ([`wifid.md §2.3`](../wifid.md)) — the documented May-11 crash class.
So upload bandwidth must be **capped** and **coordinated**:

- `wifid` **owns** the cap (it knows link state, enforces a `tc`/token-bucket TX
  limit, and runs the SDIO chip-reset watchdog).
- `uploadd` **respects** the cap (it must never *rely* on the kernel cap alone —
  `uploadd.md §2.3` says "respect the WiFi TX rate cap coordinated with `wifid`").
- Neither may exceed the cap; neither may saturate the link
  ([`uploadd.md §4,§6`](../uploadd.md)).

---

## 2. Authority model (proposed)

**Two distinct control planes — keep them separate** (GPT-5.5 #3 + rubber-duck #9).
WiFi-link throttle and storage backpressure are *different* concerns and must not be
funneled through one service:

- **Link plane — `wifid` owns it.** `wifid` is the single source of truth for the
  *network* throttle (cap + chunk + link mode) and publishes `ThrottleState`.
  Enforcement is **belt-and-braces** (resolved, see OQ-4):
  - **Belt — `uploadd` self-throttle:** `uploadd` paces its own transfer (token
    bucket / rclone `--bwlimit` / chunked reads + inter-chunk sleeps) to the
    published `max_tx_bytes_per_s` and honors `max_chunk_bytes` (which `tc` *cannot*
    enforce — chunking is necessarily `uploadd`'s job).
  - **Braces — `wifid` kernel `tc`:** `wifid` also enforces a `tc` egress cap so a
    misbehaving/outdated `uploadd` still cannot exceed the hard limit.
- **Storage plane — `retentiond` owns it.** Governor backpressure ("stop staging /
  cancel low-value upload under Emergency", [`storage.md §3.1`](../storage.md))
  flows **directly `retentiond` → `uploadd`** (and via the lease-preemption path
  [D3 §5](./single-writer-lease.md)) — **not** through `wifid`. Over-coupling a WiFi
  service to storage safety would be wrong.

`uploadd` computes its **effective** go/no-go as `wifi_allows && storage_allows`. The
`/api/cloud/*` UI ([D2 §2.4](./webd-api.md)) may *aggregate* both for display, but
the two signals have separate owners and channels.

This matches both specs: `wifid.md §2.3` ("enforce a token-bucket / `tc` TX cap")
and `uploadd.md §2.3` ("respect the WiFi TX rate cap coordinated with `wifid`").

> **OQ-4 — RESOLVED (pending ratify):** keep **both** — `wifid` owns/enforces the
> hard cap (`tc`), `uploadd` cooperatively self-paces to the published state. The
> specs decide this (`wifid.md §2.3` *enforces*; `uploadd.md §2.3` *respects*); "only
> one of the two" is **not** spec-compliant, so it is no longer an open choice. The
> only remaining detail is implementation (rclone `--bwlimit` vs. a Rust token
> bucket), which is `uploadd`'s call ([`uploadd.md §2.2`](../uploadd.md)).

---

## 3. Throttle state (the published contract)

`wifid` publishes a small, versioned **`ThrottleState`** (link plane only) over IPC;
`uploadd` subscribes (push) and may also poll (pull) on (re)connect. Storage
backpressure is a **separate** `StoragePressure` signal from `retentiond`.

```rust
// teslausb-core::contracts::throttle  (doc-only proposal; no .rs from this lane)
pub enum LinkMode { Sta, Ap, Down }      // never Sta+Ap concurrently (wifid.md §2.1)

pub struct ThrottleState {               // OWNED BY wifid — link plane only
    pub seq:                 u64,        // monotonic sequence counter; bumps on every change (staleness guard)
    pub link_mode:           LinkMode,
    pub uploads_allowed:     bool,       // false ⇒ no usable STA path right now
    pub max_tx_bytes_per_s:  u64,        // 0 when !uploads_allowed; TUNABLE (spike #4)
    pub max_chunk_bytes:     u32,        // per-write ceiling under the deadlock threshold; TUNABLE
    pub action:              PauseAction,// HOW uploadd must yield (see §3.1)
    pub reason:              PauseReason,// WHY (for /api/cloud + diagnostics)
}
pub enum PauseReason {                   // link-plane reasons only
    None,            // STA up, full cap
    ApMode,          // AP onboarding active — STA down, no cloud path
    LinkDown,        // not associated / no reachability
    ChipRecovery,    // SDIO watchdog resetting brcmfmac
    NearDeadlock,    // wifid backing off to stay under the SDIO threshold
}

pub struct StoragePressure {             // OWNED BY retentiond — storage plane (separate channel)
    pub seq:            u64,
    pub uploads_allowed: bool,           // false at Emergency/Exhausted "stop dequeue"
    pub action:         PauseAction,
}
```

### 3.1 Pause actions — `uploads_allowed=false` is not one thing

Both reviews flagged that "finish/park the current transfer cleanly" is too vague:
for `ChipRecovery` (SDIO bus wedged) or storage Emergency, *continuing* the current
TX or disk read for minutes is exactly wrong. So the signal carries an explicit
**`PauseAction`**:

```rust
pub enum PauseAction {
    Run,                 // proceed at max_tx_bytes_per_s
    DrainNoNew,          // finish the in-flight file, then stop dequeuing new work
    PauseAtCheckpoint,   // checkpoint the current transfer ASAP and park (resumable)
    AbortResumeLater,    // stop now, even mid-file; rely on resumable queue to retry
}
```

Mapping: `NearDeadlock` → keep `Run` at a lower cap; `ApMode`/`LinkDown` →
`DrainNoNew` (the link just went away; no need to abort a nearly-done file unless TX
is impossible); `ChipRecovery` → **`AbortResumeLater`** (the bus is wedged — stop
touching it); storage Emergency → `PauseAtCheckpoint` (be quick but resumable). The
held **upload lease** is renewed while parked and released on full suspension /
governor preemption ([D3 §2.2,§5](./single-writer-lease.md)).

- **`seq`** lets `uploadd` ignore a stale state if messages arrive out of order.
- **`max_tx_bytes_per_s` / `max_chunk_bytes`** are the HW-measured spike-#4 outputs;
  the contract carries **placeholders**, never guessed numbers
  ([`wifid.md §2.3`](../wifid.md), [`SPEC.md §9 #4`](../SPEC.md)).

> **OPEN (OQ-5):** the cap value + chunk size are **TUNABLE**, set by spike #4
> ("exact Mbps/chunk size from prototype unknown #4" — [`wifid.md §2.3`](../wifid.md)).
> Until measured, the contract is shape-only.

---

## 4. State transitions that force a pause

`wifid` republishes `ThrottleState` (new `seq`) on each of these — and `uploadd`
must react to all of them:

| Trigger ([`wifid.md`](../wifid.md)) | New state | `uploadd` action |
|---|---|---|
| STA → AP fallback (home WiFi unreachable) (§2.1) | `link_mode=Ap, uploads_allowed=false, action=DrainNoNew, reason=ApMode` | finish in-flight, stop dequeue; no cloud path in AP mode |
| Link down / reachability probe fails (§2.1) | `Down, false, DrainNoNew, LinkDown` | finish in-flight if possible, then pause |
| SDIO chip-reset watchdog fires (§2.4) | `Down/Sta, false, AbortResumeLater, ChipRecovery` | stop immediately (bus wedged); resumable queue retries |
| Sustained TX nearing the deadlock threshold (§2.3) | `Sta, true, Run, max_tx ↓, NearDeadlock` | reduce rate to the new cap |
| Recovered, STA reachable | `Sta, true, full cap, None` | resume dequeue at full cap |

**Never AP+STA concurrently** ([`wifid.md §2.1`](../wifid.md)) — so AP mode always
means `uploads_allowed=false` (there is no STA path to the cloud while the AP is up).

---

## 5. Transport (proposed)

A UDS at `/run/teslausb/wifid.sock` ([`SPEC.md §6.1`](../SPEC.md)) exposing:

```
subscribe()            -> stream of ThrottleState   (push on change; first msg = current)
get_throttle()         -> ThrottleState             (pull, e.g. on uploadd (re)connect)
```

`webd` also reads link/throttle status from `wifid` for the cloud-archive UI
([`wifid.md §6`](../wifid.md), [D2 §2.4](./webd-api.md) `/api/cloud/*`,
`/api/wifi`). `webd` is **read-only** here; it never sets the cap.

**`wifid` → `gadgetd` read-only dependency (the reboot gate).** When a chip wedge
can only be cleared by a Pi reboot ([`wifid.md §2.4`](../wifid.md)), `wifid` must
**not** reboot while the car is mid-write — that would violate invariant #1 (a reboot
must look like a clean unplug, never an EIO mid-write). So `wifid` reads `gadgetd`'s
write-heartbeat before any reboot:

```
gadgetd.sock: gadget_status() -> { write_heartbeat_mono_ms: i64, usb_state: enum }
```

`wifid` reboots only when the heartbeat shows **USB idle** for a freshness window
(no host writes for ≥ `reboot_idle_grace_ms`, TUNABLE). The heartbeat is
**monotonic** (boot-scoped, no RTC — consistent with [D3 §4.2](./single-writer-lease.md));
a stale/absent heartbeat is treated as **"car may be writing" ⇒ do not reboot**
(fail-safe). This is a *read* dependency only — `gadgetd` still solely owns the write
path; `wifid` never touches the gadget.

> Mirrors [D3 OQ-2](./single-writer-lease.md): same UDS + framing question
> (length-prefixed JSON vs. binary). Keep the choice consistent across all
> `/run/teslausb/*.sock` IPC.

---

## 6. Invariants this contract upholds

- **Write-path safety (the #1 invariant).** TX stays under the measured
  SDIO-deadlock threshold under sustained upload
  ([`uploadd.md §4`](../uploadd.md), [`wifid.md §4 acceptance`](../wifid.md)); a
  wedge is recovered by **chip reset**, not a Pi reboot — and a Pi reboot, if ever
  needed, is gated on **USB-idle** via `gadgetd`'s write-heartbeat
  ([`wifid.md §2.4`](../wifid.md), [`SPEC.md §2 invariant 4`](../SPEC.md)). Upload
  throttling is a *contributor* to staying under the threshold, not the recovery
  mechanism.
- **Uploads never block car I/O** ([`uploadd.md §6`](../uploadd.md)); pausing is
  always safe because the queue is durable and resumable
  ([`uploadd.md §2.1,§4`](../uploadd.md)).
- **OOM order.** `uploadd` sheds **first**, `wifid` second
  ([`SPEC.md §7`](../SPEC.md)); if `uploadd` is killed, `wifid`'s `tc` cap and state
  simply have no consumer — no inconsistency.

---

## 7. OPEN QUESTIONS

1. **(OQ-4) Enforcement boundary — RESOLVED (pending ratify).** Keep **both**:
   `wifid` owns/enforces the hard `tc` cap, `uploadd` cooperatively self-paces. The
   specs decide this (`wifid.md §2.3` *enforces*, `uploadd.md §2.3` *respects*), so it
   is no longer a free choice; only the `uploadd` self-pacing implementation (rclone
   `--bwlimit` vs. Rust token bucket) is left to the builder.
2. **(OQ-5) Cap + chunk values** — HW-measured (spike #4); contract is shape-only
   until then.
3. **(OQ-2) IPC transport/framing** — shared with D3; keep `/run/teslausb/*.sock`
   consistent (length-prefixed JSON vs. binary).
4. **Backpressure routing — RESOLVED toward two planes (pending ratify).** Governor
   backpressure flows **`retentiond` → `uploadd` directly** (a `StoragePressure`
   signal, §3), **not** through `wifid`. `uploadd` ANDs the link-plane and
   storage-plane go/no-go. Confirm the operator agrees storage safety should not be
   coupled into the WiFi service.
5. **Reboot-gate freshness window.** `reboot_idle_grace_ms` (how long the gadget
   write-heartbeat must read idle before `wifid` may reboot to clear a chip wedge)
   is **TUNABLE** — needs a HW-measured value, and confirmation that `gadgetd`
   exposes a write-heartbeat in its status RPC ([`gadgetd.md`](../gadgetd.md)).
