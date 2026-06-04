# SPEC — `indexd` (trips / events / clips derivation → SQLite)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable · Language: Rust
> Consumes `scannerd` output and builds the rebuildable side-state that the UI
> reads. Owns the SQLite schema.

## 1. Objective

Turn the raw facts produced by `scannerd` (stable clips + SEI sample streams)
into the **derived domain model** the web app needs — **trips by day**, **event
bubbles** (honk, Sentry, hard brake/accel), per-event front-cam frame mapping,
and clip metadata — persisted in **SQLite (WAL)** on the **Pi-side data
filesystem** (Linux ext4), never inside the car's `disk.img` LUN.

## 2. Responsibilities

1. **Own the SQLite schema** (clips, angles, sei_samples or compacted track,
   trips, events, archive_items, leases, settings/prefs). WAL mode. Migrations
   versioned. **Sole DB writer**: also performs WAL **checkpoints/truncate** on
   request from `retentiond`'s space governor ([`storage.md` §5.2](./storage.md)),
   so WAL growth can't silently eat the reserve.
2. **Trip derivation:** segment SEI/location samples into trips by day, matching
   today's behavior (reuse existing thresholds/logic; see `web/.../mapping_trip_derivation.py`
   as the behavioral reference, not as code to keep).
3. **Event derivation:** detect honk / Sentry / hard-brake / hard-accel events
   from SEI + clip context using the **existing, hard-won thresholds** (reference:
   `mapping_event_derivation.py`). Each event records time, location, type, and
   the clip + the **front-cam frame/offset** to jump to.
4. **Clip metadata + retention/value signals:** persist the angle grouping,
   durations, availability, and the data `retentiond` needs for value scoring and
   safe deletion ([`storage.md`](./storage.md)): per item a **delete-state**
   (`LIVE`/`DELETE_CLAIMED`/`DELETING`/`DELETED`/`QUARANTINED`), **durability**
   (uploaded + remotely-verified), **pin/favorite**, event severity/telemetry/geo
   presence, size, archived-at (grace), and active **upload/playback leases**
   (with TTLs) so deletion can never race an in-flight read.
5. **Incremental + idempotent:** re-running over already-seen clips must not
   duplicate; pruning removes rows for clips that no longer exist
   (reference: `mapping_index_prune.py`).
6. **Rebuildable:** the entire DB can be dropped and regenerated from the media;
   it is never authoritative over the video itself and never lives on the Tesla
   volume.

## 3. Non-responsibilities

- No raw parsing or SEI decoding (that is `scannerd`).
- No HTTP/UI (that is `webd`); `indexd` only writes the DB and may expose a small
  status/IPC for "index progress".
- No deletion of video files (that is `retentiond` via the `gadgetd` handoff).

## 4. Data model (indicative)

| Table | Key fields |
|-------|-----------|
| `clips` | id, started_at, partition, sentry flag, duration, availability |
| `angles` | clip_id, camera (front/back/left/right/repeater), file ref, offsets |
| `trips` | id, day, start/end time, bbox, distance, point count |
| `trip_points` | trip_id, t, lat, lon, speed, heading |
| `events` | id, trip_id, clip_id, type, severity, t, lat, lon, front_frame_offset |
| `archive_items` | id, folder_class, path, size, archived_at, delete_state, durable (uploaded+verified), pinned, value_score (derived), suppress_until |
| `leases` | item_id, kind (upload/playback), holder, expires_at (TTL) |
| `prefs` / `settings` | map view prefs, speed units, thresholds overrides, storage reserves/quotas/value-weights |

Schema is the single source of truth for the UI; finalize during build with the
existing DB as the behavioral reference.

## 5. Acceptance criteria

- [ ] For a known media fixture, produces the same trips/events a user would see
      today (parity on counts, types, and jump-to-frame targets).
- [ ] Idempotent re-index produces no duplicates; pruning removes vanished clips.
- [ ] DB lives on the Pi-side ext4 filesystem in WAL mode, outside the car's `disk.img`; never on the Tesla volume.
- [ ] Full rebuild from scratch reproduces the model.
- [ ] Runs within `MemoryMax`; bounded query/derivation memory.

## 6. Testing

- Golden-file tests: media fixture → expected trips/events JSON (parity with
  current behavior).
- Migration tests (forward-only) and idempotency/prune tests.
- Threshold tests pinned to the existing event-detection constants.

## 7. Boundaries

**ALWAYS** keep the DB rebuildable and Pi-only; preserve today's
trip/event/threshold behavior; keep migrations versioned and idempotent.
**ASK FIRST** before changing any user-visible derivation (trip segmentation,
event thresholds) — that changes product behavior.
**NEVER** put the DB on the Tesla volume; never treat derived state as
authoritative over the media; never delete media (delegate to handoff).
