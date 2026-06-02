# ADR-0025 — Bucket map days by the operator's timezone (`zoneinfo` + `tzdata`)

- **Status**: Accepted
- **Date**: 2026-06-02
- **Branch**: `b1-userspace-rust`
- **Relates to**: ADR-0017 (mapping single source of truth),
  ADR-0019 (materialise trips and `detected_events`),
  ADR-0024 (raw `event.json` `clip_events` table).
- **Driver**: Operator report (2026-06-02): an evening drive plus a
  horn-honk on June 1 (America/Detroit, EDT = UTC−4) filed under
  June 2 on the map. Root cause: the web layer bucketed "days" by the
  UTC calendar date of each timestamp, so any activity after ~20:00
  local crossed midnight UTC and landed on the next day.

## Context

Every timestamp in the worker DB is a true UTC epoch (worker schema
v6, the already-landed `event.json` tz-correction). A "day" on the
maps page, however, is a *presentation* concern: the operator wants
trips and events grouped by **their own local calendar day**, not by
UTC. The vehicle is stationary in one timezone, but viewers may load
the page from any device, so the day boundary must be resolved from a
timezone the operator controls — not hardcoded to UTC and not assumed
from the server clock.

The web read path is pure-DB and bounded (ADR-0017/0019). Day
bucketing therefore has to be expressed either as SQL range bounds on
the UTC epoch columns or as a post-fetch Python bucketing step — never
as a per-request filesystem or wall-clock dependency.

## Decision

Bucket days by an **IANA timezone** resolved at the request boundary,
using Python's standard-library `zoneinfo` (DST-correct) backed by the
`tzdata` package.

**Resolution order (web blueprint):**

1. An explicit operator **Settings override** (`display_timezone`,
   an IANA name) — wins so the map is deterministic across every
   viewing device.
2. The browser's reported zone, sent as a `tz` query param
   (`Intl.DateTimeFormat().resolvedOptions().timeZone`).
3. **UTC fallback** when neither is present or the value is unknown.

All three funnel through `normalize_tz`, which validates against
`ZoneInfo` and degrades any unknown/invalid value to UTC so a
malformed timezone can never break a query.

**Single source of truth.** All timezone math lives in one new module,
`web/teslausb_web/services/mapping_tz.py`:

- `day_bounds_utc(date, tz)` → `[start, end)` UTC epoch seconds for a
  local calendar day. DST-safe: the span is local-midnight to the next
  local-midnight, each resolved to its own UTC instant (23 h or 25 h
  on transition days — never a hardcoded 86 400).
- `local_date_of(epoch, tz)` / `local_date_of_iso(iso, tz)` →
  `YYYY-MM-DD` local day for a UTC epoch / ISO timestamp.
- `normalize_tz(name)` → validated IANA name or `"UTC"`.

**Query strategy.** Range predicates on the indexed UTC epoch columns
(`col >= start AND col < end`) replace the old
`date(col,'unixepoch') = ?` equality, so the existing worker indexes
(`trips_by_start_utc`, `events_by_ts`, `clip_events_by_timestamp`) are
used. The days *aggregation* (one row per day) cannot be a SQL
`GROUP BY` because the boundary is timezone-dependent, so those rows
are bucketed in Python over the retention-bounded result set.

**Override storage.** `display_timezone` lives in
`MapViewPreferencesService` / `map_view_prefs.json` (web-only,
worker-decoupled — the home of `speed_units`), default empty = "Auto
(browser)". It is **not** placed in the worker-shared
`mapping_settings.json`.

**Cache keys.** The day-payload and playable-trips caches key on the
resolved tz (`f"{tz}\x1f{date}"`) so a UTC viewer and an EDT viewer
never share a cached payload.

**New dependency.** `tzdata>=2024.1` is added to `web/pyproject.toml`.
Raspberry Pi OS ships the system zoneinfo, but bundling `tzdata`
guarantees the IANA database is present regardless of host packaging,
so `ZoneInfo("America/Detroit")` cannot raise `ZoneInfoNotFoundError`
on a minimal install.

## Alternatives considered

### A. Keep bucketing by UTC

**Rejected.** This is the bug. Any operator east or west of UTC whose
activity crosses local midnight sees trips/events on the wrong day.

### B. Server-side fixed timezone (single configured zone, no browser)

Configure one zone for the device and bucket everything by it.

**Rejected as the *only* mechanism, adopted as the *override*.** A
fixed zone is correct for the stationary vehicle but requires manual
configuration before the map is right, and a zero-config default is
preferable. So the browser zone is the default and the fixed zone is
the optional override (resolution order above) — the best of both.

### C. Do the bucketing in the browser (ship UTC, group in JS)

Return UTC-bucketed data and regroup client-side.

**Rejected.** It would push day-boundary logic, pagination, and the
"latest day with data" union into JS, duplicating the materialised
model's query logic and breaking the bounded-payload contract. The
day boundary belongs in one place on the server.

### D. Hardcode offsets / arithmetic instead of `zoneinfo`

Compute `±HH:MM` offsets by hand (as the worker does for `event.json`).

**Rejected for the web layer.** The web layer must handle named zones
and DST transitions for arbitrary viewers; hand-rolled offsets do not.
`zoneinfo` is standard-library and DST-correct. (The worker's
hand-rolled approach in ADR-0024 remains appropriate there because
Tesla only ever emits fixed offsets, never named zones.)

## Consequences

### Positive

- Trips and events bucket under the operator's local calendar day; the
  June-1-evening honk + drive file under June 1 in EDT.
- Zero-config by default (browser zone) with a deterministic override.
- Read path stays pure-DB and bounded; range predicates use existing
  indexes.
- All timezone math is in one module, validated, and DST-safe.

### Negative

- One new runtime dependency (`tzdata`). Mitigation: standard,
  widely-trusted, data-only package; pinned to a floor version.
- Days aggregation moves from SQL `GROUP BY` to Python bucketing.
  Mitigation: the result set is retention-bounded (a few thousand
  rows) and aggregated before the `limit` is applied.
- Every cache key and query now carries a tz. Mitigation: a
  module-level `UTC_TZ_NAME` default preserves UTC behaviour for any
  caller that omits the tz (existing tests unchanged).

### Migration

No schema change — DB timestamps are already true UTC. This is a
web-only presentation change. Reverting the web layer restores UTC
bucketing with no data migration. `map_view_prefs.json` gains an
additive `display_timezone` field (default empty = Auto); older files
without it read back as Auto.

## References

- ADR-0017 — mapping single source of truth.
- ADR-0019 — materialise trips and `detected_events`.
- ADR-0024 — raw `event.json` `clip_events` table (worker tz handling).
- Module: `web/teslausb_web/services/mapping_tz.py`.
- Resolver: `web/teslausb_web/blueprints/mapping.py` (`_resolve_display_tz`).
- Override: `web/teslausb_web/services/map_view_prefs_service.py`
  (`display_timezone`).
