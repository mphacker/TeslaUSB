# Retention & eviction (B-1) — what actually deletes footage

**Status:** describes the shipped configuration as of 2026-08-05. Every claim
below was verified against source or the live device, not inferred from comments.

`retentiond` ships with automatic permanent deletion **armed**. That is a
deliberate product decision, not an accident or a local override.

**Why.** The archive directory is the only unbounded-growth consumer on the card.
`teslacam.img` is a fixed-size image that Tesla ring-buffers internally; the OS
and `media.img` are effectively constant. Only `archive/` grows, and it grows
with every clip recorded. Without a space governor the card reaches 100%, writes
fail, and the appliance stops recording. **An appliance that stops recording in
order to preserve old footage has failed at its primary job.** Tesla solves this
for its own RecentClips with a ring buffer; TeslaUSB does the same for the
archive.

The tradeoff is accepted explicitly: **footage is deleted whether or not it was
ever backed up.** Backing up before eviction is the owner's responsibility. What
the appliance guarantees is that it keeps recording and deletes the *least
valuable* footage first — never `SavedClips`, never `SentryClips`, never pinned
items, never anything from the last hour.

## 1. The shipped command is the effective command

```
ExecStart=/usr/local/bin/retentiond serve --archive-recent-only \
  --enable-eviction --recency-floor-secs 3600 --allow-permanent-loss \
  --archive-root /data/teslausb/archive --volume-image /data/teslausb/teslacam.img
```

`--recency-floor-secs` is passed **explicitly** even though `3600` is also the
`TargetDrainConfig::default` value. It is the safety floor that keeps
just-recorded footage out of the candidate set, so it must not ride on an
implicit default: changing that default would silently alter deletion
eligibility on every device with no reviewable diff to the shipped command.
The cost of stating it — one number in one file — is smaller than the cost of a
deletion boundary moving invisibly.

A second reason, learned the hard way: a unit file and the binary it launches
can be at **different versions** on a live device. Reading the current source to
predict an already-deployed binary's default is not evidence. An explicit flag
makes the effective command provable by string comparison instead.

Because a systemd drop-in can still override this, confirm the effective command
with `systemctl show retentiond -p ExecStart --value` before reasoning about
durability on any specific device.

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

Two startup guards make the mode unambiguous rather than defaulted-into:
`validate_phase1_mode` refuses to start unless exactly one of `--no-delete` /
`--enable-eviction` is present (they are mutually exclusive), and `retentiond`
also refuses to start without `--archive-recent-only`. The second guard is what
keeps archive scope and eviction scope in sync — see §4.

## 3. ⚠ Durability is not required for deletion

This is the least obvious consequence of shipping armed, and the one most likely
to be mis-stated in downstream design work.

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
- **Per-episode blast radius** — `cumulative_evict_budget` bounds one
  unhealthy→healthy episode to the deficit observed when the episode started plus
  one per-cycle slack. Exceeding that without ever reaching a verified-healthy
  checkpoint latches the drain off, on the assumption that `statfs` is lying.

**Archive scope and eviction scope must stay matched.** Eviction can only remove
`RecentClips`. If the archive ever gained content outside that class, those bytes
would be permanently un-evictable and the card would fill despite an armed
governor. This cannot currently happen: `retentiond` refuses to start without
`--archive-recent-only`, so nothing else is ever archived. Any future work that
widens archive scope **must** widen the eviction gates in the same change, or
replace that startup guard with something equally load-bearing.

### What this does *not* guarantee

Armed eviction is **best-effort space defense over indexed `RecentClips`**, not a
hard guarantee that the card can never fill. It frees nothing when:

- `retentiond` is not running, or `indexd` is unreachable (the delete protocol is
  IPC to `indexd`);
- archive bytes exist on disk that have no `archive_items` row — unindexed files
  are invisible to candidate selection;
- every candidate is excluded by the gates above (all within the recency floor,
  all pinned, all `SavedClips`/`SentryClips`, missing `started_at`);
- `statfs` reports a deficit larger than `anomaly_free_frac × total`, which
  refuses the whole pass on the assumption the reading is bad;
- the per-episode blast-radius budget latched the drain off.

Every one of these fails **closed** (keeps footage, frees nothing), which is the
right bias for a deletion path but means a full card is still reachable. Treat
"low free space **and** no eviction progress" as a condition worth surfacing,
not as an impossible state.

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
| `target_exit_frac` | `0.10` | **the real threshold** — both the entry gate and the drain stop |
| `target_free_frac` | `0.08` | *not* the trigger; feeds the anomaly guard and episode budget |
| `recency_floor_secs` | `3600` | never touch anything recorded in the last hour |
| `per_cycle_evict_bytes` | 8 GiB | per-pass byte cap |
| `per_cycle_evict_count` | 256 | per-pass item cap |
| `per_cycle_wall_ms` | 5000 | per-pass wall-clock cap |

**Read `target_free_frac` carefully — the name is misleading.** In
`drain_to_target` (`retentiond/src/serve.rs`) the early return is
`if free_before >= exit_free` and the loop break is `if current_free >=
exit_free`. Both use `target_exit_frac` (**10%**). So the governor deletes
whenever free space is below **10%**, and drains back up to 10% — there is no 8%
trigger and no 8→10 hysteresis band. `target_free_frac` (8%) is used only to
compute `bytes_to_free` for the anomaly guard and `cumulative_evict_budget`.
Corroborated by the 2026-07-15 run, which stopped at **10.01%**, not 8%.

The governor logs one line per pass including `free_before`, `free_after`,
`target_free`, `gap_to_target`, and a `stop=` reason.

## 7. Installs, upgrades, and reimages

`retentiond` is a normal app service (`TESLAUSB_APP_SERVICES` in
`setup-lib/common.sh`), listed last because it is an `indexd` IPC consumer. It is
therefore enabled, started, and restarted by the standard install/update path,
and the unit file is installed and upgraded by `install_unit_files`, so arming
survives reimage. This closes a real hazard: previously the armed configuration
existed only as a hand-installed drop-in on one SD card, and any reinstall
silently returned eviction to `Inert` on a card that was already 90% full.

- **Fresh install** — `retentiond` is enabled and started like any other app
  service, so the governor is live from first boot. Nothing is deleted until free
  space actually drops below **10%** (§6), so a new device with a mostly-empty
  card is unaffected for a long time. The recency floor and value tiers apply
  from the start.
- **Upgrade** — the new unit is installed and `retentiond` is restarted by
  `restart_app_services`, so the new command takes effect without manual action.
  (Before this change `retentiond` was in `TESLAUSB_STAGED_SERVICES`, which
  installs the unit file but never enables, starts, or restarts it — a unit-only
  edit would have shipped a file that no install ever ran.)
- **Existing devices carrying the legacy drop-in** — a hand-installed
  `/etc/systemd/system/retentiond.service.d/10-eviction.conf` still **overrides**
  the vendor unit, because that is how systemd drop-ins work. It is now redundant
  and should be removed so there is a single source of truth:

  ```sh
  sudo rm /etc/systemd/system/retentiond.service.d/10-eviction.conf
  sudo rmdir --ignore-fail-on-non-empty /etc/systemd/system/retentiond.service.d
  sudo systemctl daemon-reload && sudo systemctl restart retentiond
  systemctl show retentiond -p ExecStart --value   # confirm
  ```

Note the remaining asymmetry: neither the builder nor the installer descends into
`*.service.d/`, so a **local opt-out drop-in does not survive a reimage** — a
reimaged device comes back armed. That is the safe direction for an appliance
whose failure mode is a full card, but it means opting out is a per-device action
that must be reapplied after reinstall.

## 8. Operating it

```sh
# What is actually running?
systemctl show retentiond -p ExecStart --value

# Current governor state (root-only; /run/teslausb is mode 0750 —
# a plain `cat` fails with a *silent* empty result if stderr is discarded).
sudo cat /run/teslausb/retentiond.governor.json
sudo cat /run/teslausb/retentiond.health.json
```

**De-arm to `DryRun`** (keeps candidate selection and logging observable, deletes
nothing) — drop-in overriding `ExecStart` without `--allow-permanent-loss`:

```
[Service]
ExecStart=
ExecStart=/usr/local/bin/retentiond serve --archive-recent-only --enable-eviction \
  --archive-root /data/teslausb/archive --volume-image /data/teslausb/teslacam.img
```

**Disable entirely** — same shape but swap `--enable-eviction` for `--no-delete`
(they are mutually exclusive; passing both is a startup error). Doing this
accepts that the card will eventually fill and recording will stop.

Both are Tier-3 changes to a recording-adjacent daemon; use the hardware-test
skill, and re-verify with `systemctl show` afterwards.

## 9. ⚠ Interaction with Tesla dashcam encryption

If the car has **dashcam encryption** enabled, clips land under
`TeslaCam/EncryptedClips/RecentClips/`. `retentiond/src/volume_source.rs` matches
only the prefix `TeslaCam/RecentClips/`, so **archiving observes zero new clips**
while eviction stays armed.

The result is not a ring buffer. It is a **one-way shrinking archive**: old
footage is still deleted under space pressure, but no new footage arrives to
replace it. Space is reclaimed, so the appliance keeps working — but the archive
monotonically drains toward empty for as long as encryption stays on.

Check with `GET /api/recording/encryption`, which reports `encrypting`,
`latest_plain_at`, `latest_encrypted_at`, and `encrypted_clip_count`. If
`encrypting` is true, either turn encryption off in the car or accept that the
archive will shrink. Do not diagnose this as an eviction bug — eviction is
behaving exactly as configured; the ingest side is what stopped.