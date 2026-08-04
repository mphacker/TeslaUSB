# Retention & eviction (B-1) — what actually deletes footage

**Status:** describes the configuration running on the live B-1 device as of
2026-08-04. Every claim below was verified against source or the device, not
inferred from comments.

This document exists because the repository previously implied the opposite of
the truth. The shipped unit
[`deploy/systemd/retentiond.service`](../../deploy/systemd/retentiond.service)
carries `--no-delete`, and several working notes recorded "eviction is inert" as
the *safety basis* for other design decisions. That was wrong, and any durability
argument that rested on it must be re-derived.

## 1. The effective command is not the shipped one

`retentiond` on the device is launched by a systemd drop-in that **clears** the
shipped `ExecStart` and substitutes its own:

```
ExecStart=
ExecStart=/usr/local/bin/retentiond serve --archive-recent-only --enable-eviction \
  --recency-floor-secs 3600 --allow-permanent-loss \
  --archive-root /data/teslausb/archive --volume-image /data/teslausb/teslacam.img
```

Always confirm with `systemctl show retentiond -p ExecStart --value`. Reading
`retentiond.service` alone is not sufficient and has produced wrong conclusions.

The drop-in is recorded at
[`deploy/systemd/retentiond.service.d/10-eviction.conf`](../../deploy/systemd/retentiond.service.d/10-eviction.conf).

## 2. Mode resolution

`resolve_eviction_mode` (`retentiond/src/main.rs`) is the whole decision:

| `--enable-eviction` | `--dry-run` | `--allow-permanent-loss` | mode |
|---|---|---|---|
| no | — | — | `Inert` |
| yes | yes | — | `DryRun` |
| yes | no | no | `DryRun` |
| yes | no | **yes** | **`Armed`** |

The device passes `--enable-eviction --allow-permanent-loss` with no
`--dry-run`, so it runs **`Armed`**. Deletion is real and irreversible. The
governor logs its mode as `[ARMED]`.

## 3. ⚠ Durability is not required for deletion

This is the single most important property, and the least obvious.

`retentiond/src/main.rs` sets the delete-cycle context with
`allow_undurable = parsed.enable_eviction` — the flag is wired to *whether
eviction is on*, not to whether anything is backed up. Its own comment says so:

> `allow_undurable` is wired to `enable_eviction`, so armed eviction currently
> bypasses indexd's `PROVEN_DURABLE` gate.

In `indexd`'s `list_eviction_candidates` (`db/reads.rs`), `allow_undurable = 1`
short-circuits the `PROVEN_DURABLE_PROOF_SQL` term entirely.

**Consequence:** archived clips are permanently deleted with no proof they exist
anywhere else. Cloud upload is *not* a precondition. Do not describe local
eviction as "cloud-gated" — that is true of the cloud-durability design
(`indexd-cloud-schema.md`), which fails closed, but it is **not** what is
running.

The coupling is deliberate and load-bearing: flipping `allow_undurable` to
`false` today would stop eviction freeing space at all, because `uploadd` does
not yet reliably write finalized COMPLETE parent upload sets. Do not "fix" it in
isolation.

## 4. What still protects footage

With durability bypassed, these are the *only* remaining guarantees. All are
enforced in SQL in `list_eviction_candidates`, so they fail closed:

- **`SavedClips` and `SentryClips` are never candidates** — `folder_class =
  'RecentClips'` on the item, plus `HAVING MIN(... c.folder_class = 'RecentClips')
  = 1` and `MAX(c.is_sentry) = 0` across every linked clip.
- **Pinned rows are never candidates** — `ai.pinned = 0`.
- **Recency floor** — the newest linked clip must be older than the floor
  (3600 s). Gated on `clips.started_at` (the true recording instant from the
  Tesla filename/mvhd), *not* `archived_at`, whose wall-clock value is unreliable
  on a clock-less device.
- **Known start time required** — every linked clip needs `started_at > 0`.
- **Suppression window** — `suppress_until` must be null or in the past.
- **Per-cycle caps** — 8 GiB, 256 items, 5000 ms per pass
  (`TargetDrainConfig::default`), bounding damage from any single bad cycle.

## 5. Value ordering — what goes first

Within eligible candidates, `ORDER BY` a computed tier, then oldest
`clips.started_at`, then `id`:

| tier | meaning | evicted |
|---|---|---|
| 0 | `front_parse_attempts.parse_state = 'no_waypoints'` — no GPS/SEI telemetry | first |
| 1 | parsed with waypoints | next |
| 2 | has a row in `events` | last |

A multi-clip archive item is only tier 0 if *every* linked clip is; a mixed item
falls to a higher tier. Verified by `parsed_with_waypoints_never_tier0`,
`multiclip_mixed_item_not_tier0`, and `within_tier_oldest_first`.

## 6. Thresholds

From `TargetDrainConfig::default` (`retentiond/src/config.rs`):

| knob | value | meaning |
|---|---|---|
| `target_free_frac` | `0.08` | act when free space is below 8% |
| `target_exit_frac` | `0.10` | stop once 10% free is restored (hysteresis) |
| `recency_floor_secs` | `3600` | never touch anything recorded in the last hour |
| `per_cycle_evict_bytes` | 8 GiB | per-pass byte cap |
| `per_cycle_evict_count` | 256 | per-pass item cap |
| `per_cycle_wall_ms` | 5000 | per-pass wall-clock cap |

The governor logs one line per pass including `free_before`, `free_after`,
`target_free`, `gap_to_target`, and a `stop=` reason.

## 7. Reimage hazard

Neither the release builder nor the installer descends into `*.service.d/`:

- `release/build-release.sh` — `find "$units_dir" -maxdepth 1 -type f -name '*.service'`
- `setup-lib/units.sh` `install_unit_files` — globs `"$src"/*.service`

So a reimage or reinstall restores `retentiond.service` with `--no-delete` and
**no drop-in**, silently returning eviction to `Inert`. On a card that is
currently 90% full, that means free space is no longer defended and the archive
fills.

This cuts both ways, and both directions are dangerous:

- Forgetting to reapply the drop-in ⇒ **disk fills, archiving stops.**
- Auto-installing drop-ins ⇒ **every fresh install silently arms permanent
  deletion.**

The second is why `10-eviction.conf` is checked in but deliberately left outside
the install path. Changing that is a Tier-3 decision requiring explicit operator
sign-off, not a packaging cleanup.

## 8. Operating it

```sh
# What is actually running?
systemctl show retentiond -p ExecStart --value

# Current governor state (root-only; /run/teslausb is mode 0750 —
# a plain `cat` fails with a *silent* empty result if stderr is discarded).
sudo cat /run/teslausb/retentiond.governor.json
sudo cat /run/teslausb/retentiond.health.json
```

De-arm to `DryRun` (keeps candidate selection observable, deletes nothing):
remove `--allow-permanent-loss` from the drop-in, `daemon-reload`, restart.

Disable entirely: delete the drop-in, `daemon-reload`, restart — the shipped
`--no-delete` unit takes over.

Both are Tier-3 changes to a recording-adjacent daemon; use the hardware-test
skill.
