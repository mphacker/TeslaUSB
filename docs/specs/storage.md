# SPEC — Storage-space governance (the SD-card space governor)

> Parent: [`SPEC.md`](./SPEC.md) · Owner service: [`retentiond`](./retentiond.md)
> Related: [`indexd`](./indexd.md) (persists value signals + delete state),
> [`uploadd`](./uploadd.md) (durability + upload leases),
> [`webd`](./webd.md) (playback leases + storage UI),
> [`tesla-usb-contract.md`](./tesla-usb-contract.md) (folder semantics).

This document is the authoritative design for keeping the SD card from filling
up. It is **implemented inside `retentiond`** (see that spec for the per-folder
archiving policy); this file specifies the *space-management mechanism* that runs
across all archived data.

---

## 1. Framing: what actually consumes the card

The supreme invariant is **the car must always be able to write TeslaCam**
([`SPEC.md` §2](./SPEC.md)). Storage governance serves that invariant indirectly:

- The car-facing LUN is a **single fixed-size, fully-preallocated `disk.img`**.
  The car's incoming video **rotates inside** that image and **does not grow**
  ext4 usage. `disk.img` is accounted at its **full nominal size at all times**
  (even if the file is momentarily sparse). Provisioning MUST `fallocate` it
  fully so car writes never depend on ext4 free space.
- The **unbounded** consumers of the SD card are **our own Pi-side data**: the
  `archive/` tree, the SQLite index + **WAL**, upload **staging**, **thumbnails**,
  and **journald/logs**.
- Therefore the governor's job is to **bound our own footprint** so the OS, our
  services, and especially `gadgetd` never starve. A starved `gadgetd` is the
  *principal* path by which low disk space could endanger car writes — which is
  why this is a **safety function**, not housekeeping. (A second, indirect path:
  if Pi ext4 is **exhausted**, archiving — and therefore the car-side cleanup
  handoffs — stall, so the **car's** exFAT volume could itself fill over time;
  that is `retentiond`'s car-side concern ([§3](./retentiond.md)), and sustained
  Pi-Exhaustion is surfaced as an explicit blocker, §3.1.)

> **Corollary (do not confuse the two spaces).** Deleting files *inside* the
> exFAT volume (a car-side handoff delete, [`retentiond` §3](./retentiond.md))
> does **not** reclaim host ext4 space, because `disk.img` stays fixed-size.
> Car-side deletes exist only to keep the **car's** TeslaCam partition from
> filling. **Host-space relief comes exclusively from evicting Pi-side files.**
> The governor never mutates `disk.img` to reclaim host space.

---

## 2. Filesystem model & reserve tiers

On startup and each cycle, `statfs` the relevant paths and **group by device id
(`st_dev`)**: `/` (OS root), `/srv/teslausb` + `archive/{SavedClips,SentryClips,
RecentClips,TeslaTrackMode}`, upload staging, thumbnail/cache dir, `/var/lib/
teslausb` (SQLite + WAL + SHM), and the log/journald dir.

- If `/` and the data dir **share `st_dev`** → one combined budget, enforce the
  **strictest** reserve. Archive growth can then kill SSH/journald/SQLite/
  `gadgetd`, so the root reserve dominates.
- If **separate** → independent root and data budgets. A **root-FS** reserve
  breach is handled by **root-specific** mitigation (cap journald, clear temp,
  alert) — **never** by evicting the data archive, which frees **zero** root
  bytes; only shared-device or data-FS pressure evicts archive.

**Reserve tiers, highest protection first:**

1. **OS/app reserve (sacrosanct).** Root-FS headroom for the OS (journald, apt,
   tmp) and our runtime (WAL/checkpoint budget, sockets, logs). **Never**
   consumed by the archive. If breached, the governor enters at least
   **Critical** even if archive usage looks fine.
2. **Data-FS operating reserve.** Headroom so archive copies, copy temp files,
   WAL checkpoints, and uploads never hit zero.
3. **Archive soft cap → hard cap.** The target ceiling for `archive/` as a whole,
   on top of the per-folder quotas in [`retentiond` §3](./retentiond.md).

Budgets when root and data share a filesystem:

```
usable_for_archive = free_bytes
                   - os_reserve - data_operating_reserve
                   - (disk_img_nominal - disk_img_allocated)   # sparse-image guard
```

The **sparse-image guard** subtracts any not-yet-allocated `disk.img` blocks
(`disk_img_allocated = st_blocks × 512`) so a momentarily sparse image can't be
mistaken for archive headroom and let the archive eat blocks the car will later
need. Steady state it is ~0 (image fully `fallocate`d, §1); a large value raises
the sparse-image alert (§6/§8).

Enforce **inodes** as a parallel budget — thumbnails and Recent segments can
exhaust inodes before bytes.

### 2.1 Default thresholds (advisory — **tune on hardware**, §7)

Use **both** a percentage and an absolute floor (`max(pct, bytes)`): percentages
fail on small cards, absolutes fail on large ones. Hysteresis: separate **enter**
(high-water) and **exit** (low-water) marks so tiers don't flap.

**256 GB card (free-space enter / exit):**

| Tier | Enter below | Exit above |
|------|------------:|-----------:|
| Healthy | — | `max(8%, 20 GiB)` |
| Low | `max(6%, 16 GiB)` | `max(8%, 20 GiB)` |
| Critical | `max(3%, 8 GiB)` | `max(6%, 16 GiB)` |
| Emergency | `max(1.5%, 4 GiB)` | `max(3%, 8 GiB)` |
| Exhausted | `max(0.75%, 2 GiB)` **or no safe candidates** | safe candidate appears / Healthy |

**32 GB card:**

| Tier | Enter below | Exit above |
|------|------------:|-----------:|
| Healthy | — | `max(8%, 2.5 GiB)` |
| Low | `max(6%, 2 GiB)` | `max(8%, 2.5 GiB)` |
| Critical | `max(3%, 1 GiB)` | `max(6%, 2 GiB)` |
| Emergency | `max(1.5%, 512 MiB)` | `max(3%, 1 GiB)` |
| Exhausted | `max(0.75%, 256 MiB)` **or no safe candidates** | safe candidate appears / Healthy |

**Inodes:** Low `<3%` free · Critical `<1.5%` · Emergency `<0.75%`.
**Root reserve floor:** shared-fs ≥ `max(5%, 2 GiB)` (256 GB) / `max(5%, 512 MiB)`
(32 GB); separate small root ≥ `max(10%, 512 MiB)`.

All thresholds are **persisted settings** surfaced in the storage UI (extends
today's `cloud_reserve_gb` / `cloud_auto_cleanup` pattern to the **local**
archive).

---

## 3. The continuous Space Governor (the watchdog)

A **dedicated, always-on task inside `retentiond`**, on its own thread/async
task — **independent of the archive copy/verify pipeline** so it keeps running
and can **preempt** that pipeline even when copying is busy or wedged. It is
**index-driven** (queries candidates from SQLite) — it must **not** recursively
walk millions of files every cycle.

**Cadence:** cheap periodic `statfs` (source of truth), faster under pressure —
Healthy ~60 s, Low ~30 s, Critical ~15 s, Emergency ~5–10 s — plus **event
wakeups** (archive copy done, staging created/removed, upload done, WAL crossed a
threshold, deletion done, a producer requested write budget). `inotify` is a
hint only; it misses free-space changes from journald, WAL, rclone temp files,
deleted-open files, and unrelated root consumers.

**Producers acquire a write budget** from the governor before large writes
(archive import, staging). Below Healthy the governor can deny/queue budget.

### 3.1 Tier actions (additive)

- **Healthy:** full operation. Opportunistic maintenance: trim consumers over
  soft quota, opportunistic WAL checkpoint (via `indexd`), delete old temp.
- **Low:** stop nonessential thumbnail creation; trim cache; trim Recent mirror
  to quota; evict **durable, low-value** items (start with flood Sentry); ask
  `indexd` to checkpoint WAL; reduce upload-staging concurrency; warn UI.
  Archiving continues but must acquire budget.
- **Critical:** **pause** Recent mirroring and bulk Sentry archiving (finish an
  almost-complete in-flight event only); stop thumbnails; stop **new** upload
  staging; evict durable items aggressively (ascending value, §4); force WAL
  **truncate** via `indexd`; cap journald via its config (never raw-delete active
  logs). Red UI state.
- **Emergency:** stop **all** optional Pi-side writers (import, Recent mirror,
  thumbnails, new staging, nonessential logging); cancel low-value upload leases
  if safe; if policy permits, evict **undurable low-value** non-Saved (Class-B,
  §4); **preserve** `gadgetd`, `indexd`, SSH/health UI, and the root reserve;
  high-severity alert. **Never** mutate `disk.img`.
- **Exhausted:** below Emergency **and** no safe candidates remain (only pinned /
  undurable-Saved / leased bytes left). Keep optional writers stopped, keep
  `gadgetd` alive, and **surface exact blockers** (pinned bytes, leased bytes,
  unuploaded-Saved bytes, WAL/log usage, shared-device condition). **Exit is
  dynamic, not latched:** the moment any candidate becomes safe (a lease lapses
  un-renewed, an item is marked durable by `uploadd`, journald/WAL is reclaimed,
  or free space recovers), drop to the space-appropriate tier and **resume
  eviction** — manual override is only needed when nothing changes. **Do not
  spin-delete.**

### 3.2 Explicit order of sacrifice (first → last)

1. Incomplete temp / copy scratch
2. Deletion trash (already-renamed, mid-delete)
3. Thumbnails / regenerable cache
4. **RecentClips** Pi-side mirror (best-effort by design), oldest/lowest-score
5. Durable-verified **flood** Sentry
6. Durable-verified normal Sentry
7. Durable-verified TeslaTrackMode
8. Durable-verified **SavedClips** local copy (after grace/policy) — *last of the
   no-loss class*
9. *(Emergency + policy only)* undurable flood Sentry
10. *(Emergency + policy only)* undurable normal Sentry
11. *(Emergency + policy only)* undurable TeslaTrackMode
12. **Never automatic:** undurable SavedClips
13. **Never automatic:** pinned / favorited / leased / in-grace items
14. Manual operator override only

Steps 1–8 are **Class-A (no permanent loss)**; 9–11 are **Class-B (permanent
loss)** and gated to Emergency + explicit policy; 12–13 are never auto-deleted.
The car's RecentClips on the volume itself is **never** a target — only **our**
mirror is.

The governor evicts the Pi-side archive with a plain ext4 unlink (no handoff).
It **never** blocks/holds a `gadgetd` handoff to free space.

---

## 4. Value-scoring model (delete least-valuable safe item)

`indexd` persists the per-item signals; `retentiond` computes a comparable score.
**Hard exclusions are applied first** (outside the score); remaining candidates
are split into Class-A/Class-B (§3.2) and sorted **ascending by value**.

### 4.1 Hard exclusions (never auto-delete)

- Pinned / favorited / user-marked-keep
- **Undurable** SavedClips (no verified durable copy)
- Item with an in-flight **upload lease** or active **playback/download lease**.
  Leases carry **TTLs**, and the holder **renews (heartbeats)** while the
  operation is active — so an in-progress upload/stream is **never** evicted; only
  a lease left un-renewed past its TTL (crashed/abandoned holder) frees the item.
- Item inside the stability/**grace window** ([`retentiond` §3.3](./retentiond.md))
- Item whose index row is in an inconsistent/`QUARANTINED` state
- Anything inside `disk.img` (car-visible)

### 4.2 Eviction granularity

| Type | Unit |
|------|------|
| SavedClips / SentryClips / TeslaTrackMode | **whole event folder** (never a single camera mp4 — camera sets vary by model) |
| RecentClips mirror | per timestamp/camera **segment** |
| thumbnails / cache | individual file |
| upload staging | whole staging job |

### 4.3 Signals → score (lower deletes first; weights configurable, **tune on HW**)

**Base by class** (durability is the dominant axis — a durable copy means
deletion is *not* loss):

| Class | Base |
|------|-----:|
| temp / trash | −1000 |
| thumbnails / cache | −900 |
| RecentClips mirror | 0 |
| durable flood Sentry | 150 |
| durable normal Sentry | 300 |
| durable TrackMode | 450 |
| durable SavedClips | 650 |
| undurable flood Sentry | 700 |
| undurable normal Sentry | 800 |
| undurable TrackMode | 900 |
| undurable SavedClips | **protected** |
| pinned | **protected** |

**Modifiers** (clamped, normalized to a comparable integer):

| Signal | Effect |
|--------|-------:|
| uploaded + remotely verified (durable) | −300 |
| only locally present (undurable) | +300 |
| user Save/honk | +300 |
| within grace window / leased / pinned | excluded |
| recency | +0..150 (newer) / −0..150 (very old) |
| Sentry-flood classification | −250 |
| impact/alarm/severe event (from `indexd`) | +150..300 |
| has `event.json` / telemetry / geo waypoints | +50..150 |
| adjacent to a Saved event (context) | +100..250 |
| duplicate / same-time-place cluster | −100..250 |
| large size **under pressure** | −1 per 100 MiB (capped) — **efficiency tie-breaker only**, never to prefer a high-value large item over a low-value small one |
| user-marked disposable | −500 |

**Eviction loop:** candidate set = units passing all §4.1 gates → sort by
(class, ascending value, then size desc, then oldest) → delete in **batches**
until the tier's **exit (low-water)** mark is met (not merely above the entry
mark — prevents flap) or candidates are exhausted (→ escalate tier).

Under **inode** (not byte) pressure, the loop **pivots** to file-count-heavy
low-value classes first — thumbnails, regenerable cache, RecentClips mirror
segments — regardless of byte size, since freeing one large event folder reclaims
bytes but barely moves the inode budget.

### 4.4 Sentry-flood detection (advisory defaults)

Flood mode when, per hour, Sentry exceeds **256 GB:** 20 events or 4 GiB · **32
GB:** 10 events or 512 MiB. Flooded Sentry loses relative value; try to keep the
newest window (256 GB ≈ last 24 h, 32 GB ≈ last 6 h) unless Critical/Emergency
overrides. Prevents one noisy parking session from evicting weeks of context.

---

## 5. Single deletion authority + crash safety

**Exactly one service deletes Pi-side archive/cache/staging files: `retentiond`.**
`uploadd`/`scannerd`/`webd` never unlink archive content — they **lease** or
**report** (e.g. `uploadd` marks `UPLOADED_VERIFIED`; it must **not** delete
after upload). One deleter prevents upload/delete races, stale index rows, and
two services each assuming the other freed space, and enables a single global
value ordering.

### 5.1 Crash-safe deletion protocol (idempotent across power loss)

Index delete-state column: `LIVE → DELETE_CLAIMED → DELETING → DELETED`
(`DELETE_FAILED` / `QUARANTINED` for anomalies).

1. `retentiond` asks `indexd` for candidates.
2. `indexd` marks the row `DELETE_CLAIMED` in a transaction.
3. `retentiond` **renames within the same filesystem** into a trash dir:
   `archive/<…>/event123` → `archive/.retention-trash/event123.<gen>.deleting`
   (where `<gen>` is a **random 128-bit token**, never a wall-clock value — the
   Pi has no RTC, so a clock reset must not collide trash names), then **fsync the
   source parent directory**.
4. `indexd` marks `DELETING`.
5. Recursively delete the trash entry; **fsync** the trash parent.
6. `indexd` marks `DELETED(bytes_freed)`.

**Startup recovery matrix:**

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

This full FS↔DB reconciliation (including the orphan sweep) runs **once at
startup**, scoped to `.retention-trash` and rows in a non-`LIVE`/anomalous state —
**not** a per-cycle walk of the whole archive (the steady-state governor stays
index-driven, §3).

### 5.2 SQLite WAL handling

`retentiond` **monitors** `index.sqlite3{,-wal,-shm}` size but **only `indexd`
checkpoints** (single DB writer). Thresholds (advisory): warn at 128 MiB (256 GB)
/ 32 MiB (32 GB); request **truncate** checkpoint at 256 MiB / 64 MiB. A stuck
reader can block checkpoint and grow WAL. Active readers register their open read
txn (and hold a lease) so the blocker is identifiable; if a **truncate** stays
blocked past **Critical**, the governor may **restart the offending reader
service** (`webd`/`uploadd` — **never** `indexd` or `gadgetd`) to release the
lock.

### 5.3 Anti-thrash

On eviction, record a **tombstone**: source path/identity (mtime/size/hash if
known), reason, generation, **suppress-until** time, durability state. Rules: do
not re-import an event just because `scannerd` still sees it; Recent mirror uses
a strict quota + oldest-first replacement + a "don't re-fetch below horizon"
mark; flood-Sentry evicted under pressure stays suppressed until **sustained**
Healthy; a high-value Saved may re-import later **only** if its durable copy was
lost **and** space is Healthy. Batch deletes to the exit mark (hysteresis)
prevent tier flapping.

---

## 6. Reporting (to `webd` / storage UI — parity with `storage_settings`, `cloud_archive`)

Expose: current governor tier; per-device free **bytes and inodes**; root-reserve
status; `disk.img` logical vs allocated blocks (**sparse-image warning**);
archive usage by folder; staging/thumbnail/WAL/log usage; **pinned** bytes;
**leased** bytes; reclaimable bytes by class; last eviction batch (what/why/how
much freed); next candidate classes; whether undurable footage is being
sacrificed; and which writers are paused. Keep the two health signals **distinct**:

```
TeslaCam USB (car writeability): OK / Not OK     <- the invariant
Pi archive storage:  Healthy / Low / Critical / Emergency / Exhausted
```

All thresholds editable in the UI.

---

## 7. Prototype/measure on hardware before trusting (gates defaults)

`statfs` cost & cadence impact · recursive-delete latency for large event trees ·
SD-card `fsync` latency · WAL growth/checkpoint under scanner+upload load ·
rclone staging size patterns · journald growth during failures · candidate-query
memory · deleted-open-file behavior · **actual `disk.img` allocation
verification** · governor reaction under a synthetic **Sentry flood** · UI
responsiveness during Critical/Emergency cleanup.

---

## 8. Acceptance criteria

- [ ] A dedicated governor task runs independently of the copy/verify pipeline and
      keeps reacting (tier transitions) even when the pipeline is stalled.
- [ ] Free **space and inodes** are watched on both root and data filesystems;
      shared-`st_dev` collapses to one budget with the strictest reserve.
- [ ] OS/root reserve is sacrosanct: breaching it forces ≥ Critical regardless of
      archive usage.
- [ ] `disk.img` is accounted at full nominal size; a sub-99% allocated image
      raises a sparse-image Critical alert; no car-side delete is ever used for
      host-space relief.
- [ ] Tiers use hysteresis (enter≠exit) and do not flap under steady pressure.
- [ ] Eviction deletes the **least-valuable safe** item first per §4, respecting
      all hard exclusions; undurable SavedClips and pinned/leased items are never
      auto-deleted; Class-B (permanent-loss) deletion only at Emergency + policy.
- [ ] Exactly one service (`retentiond`) unlinks archive files.
- [ ] Deletion is crash-safe: power loss mid-delete reconciles to a consistent
      DB+FS state via the recovery matrix (no half-deleted event shown as
      complete; no stale rows).
- [ ] Deleting an item with an active upload or playback lease is impossible;
      leases expire by TTL.
- [ ] `Exhausted` surfaces exact blockers and stops, rather than spin-deleting,
      and **auto-exits** to the space-appropriate tier once any safe candidate
      reappears (no manual latch).
- [ ] Evicted items are not immediately re-archived (tombstone/suppress-until).
- [ ] The governor never blocks/holds a `gadgetd` handoff and never starves
      `gadgetd`.

## 9. Boundaries

**ALWAYS** protect OS/`gadgetd`/index before archive copies; bound our own
footprint; evict least-valuable-safe first; delete via the crash-safe
rename-then-unlink protocol; honor leases and grace; keep the two health signals
distinct.
**ASK FIRST** before changing default reserves, tier thresholds, value weights,
or enabling automatic Class-B (permanent-loss) eviction.
**NEVER** delete undurable user footage or pinned/leased items automatically;
never use a car-side (exFAT) delete for host-space relief; never mutate
`disk.img` to reclaim space; never let archive growth starve the OS, `gadgetd`,
or SQLite; never spin-delete in `Exhausted`.
