# Current plan

## Status

- Read-only cloud status/control-plane parity is in place and validated: `/api/cloud`, credentials status, queue/history surfaces, and the Cloud Archive SPA flow pass both desktop/mobile UATs.
- The durable delete ownership boundary is in place: indexd persists archive-delete requests and retentiond executes the crash-safe delete under a single generation token.
- The public archive delete route now derives the archive target fence from the catalog, computes the request hash server-side, persists the request through indexd, and returns replay/conflict-safe durable job responses. `target=both` remains explicitly refused until composite semantics are defined.
- The jobs capability contract advertises the durable mutation model with same-origin enforcement and required metadata (`requestId`, `idempotencyKey`, `requestHash`) before a public delete can proceed.
- The destructive delete confirmation flow in the SPA is validated and working across desktop/mobile UATs.
- The durable archive-delete path and SPA status polling are validated in the canonical Debian/WSLC environment; live-device validation remains gated behind the hardware-test safety wrapper.
- Fresh AArch64 `indexd`, `retentiond`, and `webd` plus the current SPA were deployed through the authorized `setup.sh deploy-app` path on `cybertruckusb.local`; SSH, Wi-Fi, all B-1 services, system state, and HTTP serving remained healthy.
- The isolated archive-delete validation completed successfully for fixture clip `5245`: durable job `m-1787064148609600874` reached `done`, the selected archive directory was removed, unrelated archive footage remained, and map-relevant catalog data was preserved. A stale retentiond unit in the staged artifact was corrected before the successful run.
- A matched `scannerd`/`indexd` deployment removed the scanner protocol-v1/v3 mismatch and reconnect loop.
- WSLC was recovered by restarting its WSL/container-manager services; the diagnostic AArch64 build and guarded deployment then completed successfully.
- Recovery diagnostics identified the remaining transient as `clear archive delete generation 276: Resource temporarily unavailable`; retentiond retried, completed recovery of all 512 rows, and then ran cleanly through multiple scanner/governor cycles.
- A fresh post-deployment device-log review found no failed units, service restart loops, kernel/OOM/storage faults, scanner protocol errors, or webd errors. Retention recovery completed after one transient resource-contention retry and remained `AlreadyHealthy` through the review window.
- The Wi-Fi profile-noise issue is fixed in source and in the last matched deployment; no new `teslausb-ap` errors appeared while the real STA link stayed connected.
- `/api/index/lifecycle` was incorrectly `stale` because its missing-provenance query counted archive-only front angles. The three reported keys were confirmed as `view_kind = 'archive'`; lifecycle now scopes missing-provenance checks to live `ro_usb` angles, exposes a bounded key list, and reports `healthy` on hardware.
- Noninteractive sudo was restored on the test device with a validated `/etc/sudoers.d/teslausb-b1` rule and a timestamped rollback backup, allowing guarded deployment to proceed.
- Schema v13 freshness tracking was rebuilt, manifest-verified, and deployed as a matched AArch64 set. The device migrated successfully; `/api/index/lifecycle` now reports `schema_version: 13`, `lifecycle_state: "healthy"`, `front_parse_missing_count: 0`, and a current `last_derived_at` that advances across no-change scan passes.
- Post-deployment SSH, Wi-Fi, systemd, all six app services, deployed hashes, and retention recovery were healthy. One known transient retention SQLite contention message recurred during restart, then subsequent governor cycles completed as `AlreadyHealthy`; the dead-man timer was cancelled only after the settle checks passed.
- The captive-portal parity slice is now wired to the existing typed Wi-Fi APIs for scan, manual/saved-network connection, forget, priority, and safe AP controls. Mutations require explicit operator confirmation; AP mutations now enforce the same-origin contract; `force_off` is intentionally unavailable. Focused desktop UAT (18 tests including failed-job coverage) and webd AP-origin tests pass.

## Next step

Continue parity work now that Wi-Fi onboarding source parity and lifecycle diagnostics are clean; live onboarding mutation UAT remains the gate before hardware use.

Why this is next:

1. The durable queueing, owner handoff, and SPA status loop are now wired.
   - webd persists a fenced archive request through indexd, retentiond claims and executes it, and status is available through an indexd-backed polling route.

2. Device deployment validation passed without an unguarded public mutation.
   - `target=both` stays fail-closed, and archive deletion still requires same-origin evidence, durable metadata, server-derived fencing, idempotency, and restart-safe ownership.

3. Retention recovery now completes before destructive work and remains clean across multiple cycles.
   - The transient SQLite/resource contention is retried without leaving recovery stuck; no protocol mismatch or recovery EOF loop remained after deployment.

4. The next parity value is onboarding and migration polish.
   - Wi-Fi profile selection and index freshness are now hardware-validated; the remaining parity work is captive-portal/onboarding behavior and read-only migration discovery/dry-run conversion.

## Implementation slice

- Run any further B-1 validation through the hardware-test safety wrapper; do not issue direct SSH or mutation commands outside that wrapper.
- Wire the live Wi‑Fi onboarding/captive portal flow into the existing status and scan APIs without exposing unsafe mutation behavior prematurely.
- Continue the read-only migration discovery and dry-run convert flow, keeping any apply/rollback work explicitly out of the parity release path.

## Current honest status

We are not at full legacy-main parity yet. We are at safe read-only parity plus a durable, archive-only mutation path. B-1 deployment and isolated archive-delete validation passed; the next milestone is onboarding/migration polish. `target=both` remains intentionally unavailable.
