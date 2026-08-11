# B-1 parity gap report

## Scope and comparison basis

This report compares the legacy `main` application with the Rust/TypeScript
rewrite on `mhackermsft/b1-clean`. The branches have no usable merge base in
the current checkout, so this is a tree and behavior comparison rather than a
three-dot Git diff. The `main` reference was the checked-out `main` tree at
commit `75bfca0`.

The goal is not to copy the Flask implementation. `main` is the behavior and
UX reference; B-1 should provide the same useful capabilities through Rust
daemons, typed webd APIs, and the Preact SPA, while retaining B-1's stronger
handoff, bounded-I/O, and fail-closed safety properties.

## Already covered well

B-1 already has substantial parity in the core path:

- Map-centric trip browsing, routes, event markers, clip lookup, and timezone
  handling.
- Event playback with multiple camera angles, streaming, downloads, telemetry,
  SEI/HUD support, and car-visible clip deletion.
- Analytics and storage/device-health dashboards.
- Music, Boombox, Light Shows, Wraps, and License Plates media libraries,
  including uploads, downloads, removal, and bulk operations.
- Lock-chime library management, activation, WAV validation, scheduler,
  groups, random mode, rename, and bulk delete.
- Wi-Fi status, scanning, saved profiles, connect/forget/reorder, and AP
  status/configuration through `wifid`.
- Rust daemons for gadget handoff, scanning, indexing, uploads, retention,
  scheduling, and Wi-Fi, plus encrypted hardware-bound cloud credentials.
- Forward-only B-1 catalog migrations and release/install verification.

## Priority gaps

### P0 - required before claiming feature replacement

#### Cloud archive control plane

`main` exposes a working Cloud Archive page and APIs for sync status, Sync Now,
wake/stop, history, queue inspection/removal/clear, retries, cleanup,
connection testing, remote browsing, folder creation, remote path selection,
bandwidth testing, statistics reset, and sync policy. It supports Google Drive,
OneDrive, Dropbox, S3-style providers, and custom rclone backends.

B-1 currently exposes cloud credential CRUD in
`rust/crates/webd/src/cloud_creds.rs`, but the remaining Cloud screen is
explicitly inert in `spa/src/screens/CloudArchive.tsx`. Sync Now, counters,
sync settings, queue, and history are disabled or empty; S3, B2, Wasabi, and
custom rclone are disabled in the provider selector.

#### Cleanup and retention control

`main` has cleanup status, policy editing, preview, execute, report,
reconciliation, skipped-stationary tracking, and archive cleanup operations.
B-1 has a retention daemon but no equivalent operator-facing webd API or SPA
surface.

#### Archive and combined deletion

B-1 supports the car target through a gadgetd handoff, but
`DELETE /api/clips/:id?target=archive` and `target=both` return `501` in
`rust/crates/webd/src/route.rs`. The UI therefore cannot replace main's
archive/event deletion workflows.

### P1 - important operational parity

#### Wi-Fi onboarding mismatch

The B-1 `/captive-portal` screen is a static, read-only page that makes no API
calls (`spa/src/screens/CaptivePortal.tsx`). This is now inconsistent with the
live Wi-Fi APIs and controls already present in `MediaHub.tsx`,
`wifi.rs`, `wifi_ap.rs`, and `wifi_mutate.rs`.

The missing behavior includes live status, network scan, manual connection,
saved-network actions, AP controls, and captive-portal onboarding behavior.

#### Advanced settings

`main` exposes `/settings_advanced` for tunable archive, worker, network, and
performance settings. B-1 currently persists only a small validated set of
mapping/display preferences through `/api/settings`.

#### Failed-job administration

`main` supports failed-job counts, retry, and deletion across archive,
indexing, and cloud queues. B-1 has job status/streaming and a failed-jobs
screen, but no equivalent retry/delete control surface.

#### Gadget and mode controls

`main` supports present USB, edit USB, AP force/configuration, Wi-Fi
configuration, gadget recovery, recent-archive triggering, and mode-specific
configuration saves. B-1 has gadget status and safe handoff mutations, but
does not expose a complete replacement for these operator workflows.

#### Mapping administration

B-1 covers the main map and playback path, but the legacy mapping surface also
includes index status, trigger, rebuild, cancel, diagnose, driving statistics,
event charts, Sentry-event detail/clip lookup, playable-trip queries, and
all-route queries. Most of these have no corresponding typed B-1 client method
or webd route.

### P2 - maintenance and compatibility parity

#### Filesystem checks

`main` has FSCK start, status, cancel, history, and last-check routes. No B-1
webd/API/UI equivalent was found.

#### Captive-portal probe compatibility

`main` handles Apple, Android, Windows, and generic captive-portal probes plus
redirect behavior. B-1's SPA route is not a replacement for those HTTP probe
endpoints.

#### Main-to-B-1 migration

The B-1 installer preserves B-1 data roots and databases, but no complete
migration was found for the legacy `config.yaml`, cloud-sync state, cleanup
configuration, legacy mapping state, or credential formats. An upgrade from a
main installation therefore needs an explicit discovery, conversion, dry-run,
rollback, and post-migration verification path.

## Deliberate behavior changes that need explicit product approval

### Armed retention without durability proof

`docs/specs/retention-eviction.md` states that shipped B-1 retention is armed
and may permanently delete footage without cloud durability proof. This is
deliberate space protection, but differs materially from a cloud-aware
retention policy. The UI and migration documentation must make this visible;
it must not be described as cloud-gated.

### Safer explicit deletion target

B-1 requires an explicit `?target=car` rather than choosing a destructive
default. This is a good safety improvement, but parity is incomplete until
archive and combined targets are implemented through retentiond/indexd
contracts.

### Honest but incomplete cloud UI

The Cloud screen documents its inert sections honestly in source comments, but
an operator still sees a mostly finished page with disabled controls. Until the
control plane lands, the UI should make the unavailable status unmistakable.

## Recommended order

1. Freeze the parity matrix and compatibility contracts.
2. Complete cloud control APIs and the Cloud screen.
3. Add retention/cleanup controls and archive/both deletion.
4. Finish live Wi-Fi onboarding and captive-portal probes.
5. Port jobs administration, advanced settings, FSCK, gadget recovery, and
   mapping administration.
6. Add main-to-B-1 migration.
7. Run the full parity, reliability, migration, and release gates.

