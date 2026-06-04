# SPEC — `retentiond` (archiving + retention rules)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable · Language: Rust
> External contract: [`tesla-usb-contract.md`](./tesla-usb-contract.md)
> Reference behavior: existing `web/teslausb_web/services/cloud_archive/`
> (`discovery.py`, `paths.py`, `cloud_cleanup.py`).

## 1. Objective

Make sure **footage the user wants is never lost**, while keeping the device
from filling up. `retentiond` copies clips off the car-visible volume into the
**Pi-side archive directory** (on the Linux ext4 data filesystem — *not* a
separate partition, *not* inside the car's `disk.img`; see
[`SPEC.md` §6.1](./SPEC.md)) and prunes per a configurable, **per-folder**
policy. Every change to the Tesla-visible volume goes through the `gadgetd`
eject-handoff — never a direct RW mount of the live FS, never during an active
save. **Read-only mirroring is not a "change"**: copying clips *out* of
`disk.img` via the raw read path requires **no** handoff — only car-visible
mutations (deletes, media writes) use the `gadgetd` handoff.

The three TeslaCam folders behave **differently** and therefore have
**different policies** (see §3). Conflating them is the main correctness bug this
spec exists to prevent.

---

## 2. The rotation reality (do not assume "1 hour")

`RecentClips` is a **rolling buffer the car overwrites continuously**. The
retention window is a **vehicle setting (≈1–24 h)** that is **not exposed to the
drive** — we cannot read it. Any hard-coded "~1 hour" assumption is wrong.

Therefore `retentiond` **measures the effective window empirically** instead of
assuming it:

- Track the newest and oldest *complete* `RecentClips` segment visible across
  successive `scannerd` passes, and observe when previously-seen segments
  **disappear** (the car overwrote them).
- Estimate the window as the age of the oldest still-visible complete segment.
- Treat the estimate as **advisory only** (for surfacing "archive is keeping up"
  health), never as a hard guarantee. We win by mirroring **continuously and
  promptly**, not by racing a known deadline.

> **Observability limit.** Loss can only be detected for segments we **already
> observed**. While the Pi is offline, `scannerd` is down, or the scan cadence
> lags behind the (unknown) rotation window, segments may be created **and**
> overwritten between scans — we can never know they existed. When offline time
> or scan lag exceeds the estimated window, `retentiond` raises an explicit
> **"unobserved-gap"** health state instead of implying full coverage.

`SavedClips` and `SentryClips` are **event folders** the car does **not** rotate
(per [`tesla-usb-contract.md`](./tesla-usb-contract.md) §4). They persist until
something deletes them — so the risk there is **volume pressure**, not the car
overwriting them.

---

## 3. Per-folder archiving policy

Folder identity and structure come from
[`tesla-usb-contract.md`](./tesla-usb-contract.md). Do **not** hard-code the
camera set (models differ — Cybertruck/newer add pillar/extra repeater cams);
group by event timestamp folder and copy whatever camera files are present.

> **"Complete event folder" / verified archive — strict definition.** An event
> folder counts as *complete* only when its **full directory manifest** (the set
> of files, plus each file's size, mtime, and content hash) is **unchanged across
> consecutive `scannerd` passes** — not merely when an individual MP4's tail is
> stable. This guards against late-arriving `event.json`, thumbnails, or extra
> camera angles. A *verified archive pass* binds to that manifest: every file in
> it is copied and **re-hashed at the destination**, and the source identity is
> **re-validated after copy** (still present, same size/hash). Only then is the
> event eligible for car-side deletion. If the manifest changes mid-copy, the
> pass is restarted, not marked verified.

### 3.1 `SavedClips` — highest value
- **Trigger:** user pressed Save (or honk) — explicitly wanted footage.
- **Archive:** copy each complete event folder to the archive **and verify**
  (checksum) before it is eligible for any car-side deletion.
- **Delete from car:** default **keep on the car** unless volume pressure is
  high; only after **one verified archive pass** (and cloud policy satisfied, if
  configured) may a copy be removed via handoff. The Saved local **archive** copy
  is the **last of the no-loss class** to be pruned, and only once **durable**
  (see [`storage.md` §3.2/§4](./storage.md)); an **undurable** Saved copy is
  **never** auto-evicted.
- **Local-archive durability floor:** a verified-archived Saved event is **never
  deleted from the Pi-side archive** unless its configured durability policy is
  satisfied (e.g. uploaded to cloud or copied off-device). If ext4 is exhausted
  and no Saved item is durability-eligible for eviction, `retentiond` **fails
  closed** — it stops accepting new lower-priority work and raises a **critical**
  health alert rather than deleting undurable Saved footage. (See §5.)

### 3.2 `SentryClips` — archive after Saved, can flood
- **Trigger:** Sentry events — can be **numerous** and fill the volume fast.
- **Archive:** copy + verify, scheduled **after** `SavedClips` in priority.
- **Delete from car:** default **delete after a verified archive pass** once the
  car-visible free space drops below a configurable threshold (mirrors the
  existing cleanup behavior). This keeps the Tesla volume from filling while
  preserving the footage in the archive.
- **Car-side delete safety gate:** `retentiond` only *requests* the delete;
  `gadgetd` owns the final gate — it refuses the handoff unless the LUN has been
  write-idle for a defined quiet period and no active save is indicated, and
  after remounting RW it **re-validates the target folder still matches the
  verified manifest** before deleting (never delete a folder the car has since
  changed). The local-archive durability floor of §3.1 also applies to Sentry by
  **default** (an undurable Sentry archive copy is not evicted merely to reclaim
  space). **Unlike `SavedClips`**, however, undurable Sentry **may** be evicted
  under **Emergency** tier **and** explicit user opt-in (Class-B, permanent-loss —
  [`storage.md` §3.2/§4.3](./storage.md)): Sentry can flood, so the operator may
  choose bounded permanent loss over letting the card fill, whereas undurable
  Saved is **never** auto-evicted.

### 3.3 `RecentClips` — NEVER delete from the car; bounded rolling mirror
- **Never** issue car-side deletes of `RecentClips`; the car owns and rotates it.
- **Default = bounded rolling mirror:** continuously copy **all complete**
  `RecentClips` segments into the archive, prioritizing **event-adjacent** and
  **oldest-visible** segments first (oldest are closest to being overwritten).
  The mirror is **quota-capped**; when full, **evict the oldest non-pinned**
  archived segments. Segments near a Saved/Sentry event (or otherwise pinned by
  policy/telemetry) are **pinned** and not evicted by the rolling cap.
- **Eviction grace window:** a segment becomes evictable only after a
  configurable **grace window** since it was first archived — long enough to
  cover the worst-case delay between a Recent segment's capture and a
  Saved/Sentry event materializing and being indexed. This stops us evicting
  context that *becomes* event-adjacent shortly after capture. Pins are
  re-evaluated whenever a new event is indexed.
- **Lighter option (not default):** *telemetry-only* mirror — archive only
  segments that carry telemetry/are flagged interesting (the existing
  `sync_recent_with_telemetry` behavior). Offered as a low-footprint mode, but
  the safe default is the bounded full mirror above.

### 3.4 `TeslaTrackMode` — treated like a non-rotated event folder
`TeslaTrackMode/` (per [`tesla-usb-contract.md`](./tesla-usb-contract.md)) is
**not rotated** by the car. Default policy: **archive + verify like
`SentryClips`** (copy out, then car-side delete only after a verified pass under
free-space pressure, via handoff), at a configurable priority **between Sentry
and RecentClips**. It must not be silently excluded (which would let it fill p1)
nor hard-coded out.

### 3.5 Priority / backpressure order
Under contention (CPU, I/O, free space, WiFi), honor this strict order — higher
wins:

1. **Car writes / FS integrity** (the #1 invariant — never compromised)
2. **`SavedClips`** archive + verify
3. **`SentryClips`** archive + verify
4. **`TeslaTrackMode`** archive + verify
5. **`RecentClips`** event-adjacent windows
6. **`RecentClips`** generic rolling mirror
7. **Cloud upload** (handled by `uploadd`, lowest)

---

## 4. The honest guarantee (surface this in the UI)

- **`SavedClips` / `SentryClips`:** once the event folder is complete and one
  verified archive pass succeeds, the footage is **preserved in the archive and
  not deleted from the car until that verified copy exists**. The local archive
  copy is then only removed when its **durability policy is satisfied** (cloud /
  off-device) — never merely to reclaim space (§3.1 floor). Practically: given
  working scans and adequate space, Saved/Sentry are kept. The honest caveat is
  that preservation is bounded by **archive space + a working scan/copy path** —
  if ext4 is exhausted with no durability-eligible eviction candidate, we **fail
  closed and alert** rather than silently dropping footage. (The one exception is
  **explicitly opt-in**: an operator may enable Class-B Emergency eviction of
  undurable **Sentry** to avoid filling the card — [`storage.md` §3.2](./storage.md)
  — which trades this guarantee for that folder. It is **off by default**, and
  undurable **Saved** is never included.)
- **`RecentClips`:** **best-effort, raced** against an unknown car-controlled
  window. We mirror continuously and oldest-first, but if archiving cannot keep
  up (slow I/O, archive quota too small, device offline), some segments **may be
  overwritten by the car before we copy them**. For segments we **previously
  observed**, a drop is **never silent** — `retentiond` raises a health warning
  ("RecentClips archiving is falling behind; increase quota or reduce load") via
  `webd`. For segments created-and-overwritten **between scans / while offline**,
  we surface the **"unobserved-gap"** health state (§2) — we cannot detect
  individual losses we never saw, and we say so rather than implying coverage.

This honesty is deliberate: we must not imply a guarantee for `RecentClips` that
the car's rotation physically prevents, nor imply Saved/Sentry survive space
exhaustion when their only copy is undurable.

---

## 5. Deletion, free space, and disk safety

The **continuous SD-card space governor, reserve tiers, value-scoring eviction,
single-deleter authority, and crash-safe deletion** are specified in full in
**[`storage.md`](./storage.md)** (implemented inside `retentiond`). The
essentials that bind this spec:

- **Two different spaces — don't conflate them.** Host ext4 free space is
  reclaimed **only** by evicting **Pi-side archive/cache/staging** files (a plain
  unlink; no handoff). Deleting *inside* the exFAT volume does **not** free host
  space (`disk.img` is fixed-size); car-side deletes exist only to keep the
  **car's** TeslaCam partition from filling.
- **All Tesla-volume (car-side) deletions go through the `gadgetd` eject-handoff**
  — never a direct RW mount of the live FS, never during an active Sentry/honk
  save.
- **`disk.img` is fully preallocated** (`fallocate`d at provisioning) and
  accounted at full nominal size, so car writes never depend on ext4 free space
  and a full archive can never corrupt the LUN.
- **Reserves are tiered and the OS/`gadgetd`/SQLite reserve is sacrosanct**
  ([`storage.md` §2](./storage.md)): archive growth may **never** starve them.
  Eviction deletes the **least-valuable safe** item first ([`storage.md`
  §4](./storage.md)), never undurable Saved/Sentry, never pinned/leased items.
- **Emergency vs. "car must always write":** if the **car-visible** volume is
  critically full and archiving can't keep up, never delete undurable user
  footage to make room; raise a **critical "recording-at-risk"** alert and only
  delete **verified-archived, durable** folders via handoff. `retentiond` never
  blocks/holds a handoff and never starves `gadgetd`.

---

## 6. Responsibilities

1. **Archive selection & copy:** per §3, using `scannerd`'s **stable-clip** facts
   (only copy files proven complete) and copying via the **raw read path** —
   never mounting the Tesla FS RW.
2. **Verification:** checksum each archived event before it counts as a "verified
   archive pass" that unlocks car-side deletion.
3. **Empirical rotation tracking:** maintain the `RecentClips` window estimate and
   "keeping up?" health signal (§2).
4. **Run the SD-card space governor + value-based eviction** ([`storage.md`](./storage.md)):
   continuously watch free space/inodes on both filesystems, enforce reserve
   tiers, and evict the **least-valuable safe** archived item first — never
   undurable Saved/Sentry, never pinned/leased/in-grace items — via the
   crash-safe delete protocol. `retentiond` is the **sole** deleter of Pi-side
   archive files.
5. **Deletion via handoff (car-side):** request `gadgetd` eject-handoff for any
   car-side delete (§5); `gadgetd` is the final safety gate (idle/quiet + manifest
   re-validation); never during a save.
6. **Coordinate with `uploadd`:** mark verified-archived items for cloud upload;
   don't prune local copies that haven't satisfied their cloud policy (unless the
   policy says otherwise).
7. **Expose status/policy** to `webd`: per-folder archive progress, window
   estimate, "keeping up" health, free-space headroom, and the configured policy.

## 7. Non-responsibilities

- No cloud transfer (that is `uploadd`).
- No raw parsing/indexing (consumes `scannerd`/`indexd` outputs).
- No direct Tesla-FS writes/deletes (always via `gadgetd` handoff).
- Does not read the car's rotation setting (it isn't exposed) — it measures.

## 8. Acceptance criteria

- [ ] `SavedClips`/`SentryClips`/`TeslaTrackMode` complete event folders are
      archived **and checksum-verified** against a stable directory manifest; no
      verified-archived event is lost.
- [ ] A verified-archived event's local copy is removed only when its durability
      policy is satisfied; under ext4 exhaustion with no eligible candidate,
      `retentiond` **fails closed** and raises a critical alert (never deletes
      undurable user footage).
- [ ] `RecentClips` is **never** deleted from the car by `retentiond`.
- [ ] `RecentClips` default mode mirrors all complete segments oldest/event-near
      first, within quota, evicting oldest non-pinned **only after the grace
      window**.
- [ ] When `RecentClips` archiving falls behind, a **health warning** is raised
      for observed segments; an **"unobserved-gap"** state is raised when scan
      lag/offline exceeds the estimated window (no false coverage claim).
- [ ] `SentryClips`/`TeslaTrackMode` are deleted from the car only **after** a
      verified archive pass and only under the configured free-space threshold,
      via handoff; `gadgetd` re-validates the folder against the manifest and
      refuses during/near a save.
- [ ] `SavedClips` are kept on the car by default; pruned only under high pressure
      after verified archive (+ cloud policy if set).
- [ ] All Tesla-volume deletions go through the handoff; none during a save.
- [ ] Hard ext4 reserve + preflight accounting enforced before each archive write;
      `disk.img` is fully preallocated.
- [ ] The continuous space governor ([`storage.md`](./storage.md)) reacts to
      low-space independently of the copy pipeline and evicts least-valuable-safe
      first; full governor/value/crash-safety criteria live in `storage.md §8`.
- [ ] Backpressure honors the §3.5 order (car writes always win).
- [ ] Camera set is **not** hard-coded; events with extra/missing cameras archive
      correctly.
- [ ] Runs within `MemoryMax`; copies stream within the I/O cap.

## 9. Testing

- Per-folder policy tests over **synthetic timelines** (Saved/Sentry/Recent) with
  injected rotation (segments disappearing) and volume-pressure scenarios.
- "Falling behind" test: Recent mirror cannot keep up → warning raised, no silent
  loss claim; offline-gap test → "unobserved-gap" state, no false coverage.
- Verification gating: deletion refused until a verified archive pass against a
  **stable directory manifest** exists; manifest-change-mid-copy restarts the pass.
- Durability-floor test: ext4 exhausted with only undurable Saved → fails closed +
  critical alert, no deletion of undurable footage.
- Handoff-delegation tests (delete routes through `gadgetd`, refused during/near a
  save, manifest re-validated after remount).
- Variable-camera-set fixtures (Cybertruck/newer) archive without hard-coding.
- Interaction test with `uploadd` (no premature prune before cloud policy met).
- Space-governor tests live in [`storage.md` §8](./storage.md) (tier transitions
  with hysteresis, value ordering, crash-safe delete recovery, delete-vs-lease
  races, Sentry-flood, shared-partition, sparse-`disk.img` accounting).

## 10. Boundaries

**ALWAYS** treat each TeslaCam folder by its distinct policy; archive + verify
Saved/Sentry/TrackMode against a stable manifest before any car-side delete;
mirror RecentClips continuously without ever deleting it from the car; run the
space governor to keep the OS/`gadgetd`/SQLite reserve sacrosanct and evict
least-valuable-safe first ([`storage.md`](./storage.md)); delete car-side only
via handoff (with `gadgetd` re-validating the manifest); honor the §3.5
backpressure order.
**ASK FIRST** before changing user-visible retention semantics, default per-folder
modes, or quotas.
**NEVER** delete on the live Tesla FS directly; never during a save; never delete
`RecentClips` from the car; never delete undurable user footage to reclaim space
(fail closed instead); never claim `RecentClips` is guaranteed; never fill
the disk; never starve car writes.
