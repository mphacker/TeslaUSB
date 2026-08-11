# B-1 parity contract index

This index ties the parity roadmap to the code and contract surfaces that must
change together. It is intentionally organized by ownership rather than by
legacy Flask blueprint names.

| Workstream | Contract/spec | Rust owner | Webd/API owner | SPA owner | Primary tests |
|---|---|---|---|---|---|
| Contract freeze | `b1-parity-gap-report.md`, `b1-parity-matrix.md` | all owners | all routes | all affected screens | matrix review |
| Cloud control | `specs/contracts/webd-cloud-api.md`, `specs/uploadd.md` | `uploadd`, `indexd/src/db/cloud.rs` | `webd/src/route.rs` and cloud module | `screens/CloudArchive.tsx`, `api/client.ts`, `api/types.ts` | cloud bridge, Rust IPC, `cloud-archive.spec.ts` |
| Cloud credentials | `specs/contracts/cloud-provider-creds.md` | `teslausb-creds`, `uploadd` | `webd/src/cloud_creds.rs` | Cloud provider section | credential unit tests and API UAT |
| Retention/control | `specs/retention-eviction.md` | `retentiond`, `indexd` delete/read modules | planned retention module in `webd` | planned retention/settings sections | retention unit, IPC, deletion UAT |
| Gadget deletion | gadgetd handoff contract in `gadgetd/src/handoff.rs` | `gadgetd`, `retentiond` | `webd/src/route.rs` mutation handlers | `Media.tsx`, `EventPlayer.tsx` | handoff, failure injection, clip-delete UAT |
| Wi-Fi onboarding | wifid operational contract | `wifid` | `webd/src/wifi.rs`, `wifi_ap.rs`, `wifi_mutate.rs` | `screens/CaptivePortal.tsx`, `MediaHub.tsx` | Wi-Fi unit/IPC and captive UAT |
| Captive probes | new webd compatibility contract | `wifid` status integration if needed | planned probe routes | none | HTTP probe integration tests |
| Advanced settings | new versioned B-1 settings contract | owning daemon per setting | planned settings module | planned advanced settings screen | validation, persistence, restart tests |
| Jobs administration | `specs/contracts/durable-mutation-jobs.md`, existing jobs/job events stream contract | owning daemon queues (`gadgetd`, `indexd`, `uploadd`, `retentiond`) | `webd/src/jobs.rs`, `webd/src/route.rs` (`/api/jobs/capabilities`, `durable_mutation_routes_enabled=false`, legacy mutation disclosure), mutation envelope/validation helpers, planned mutations | `screens/FailedJobs.tsx` | state-transition + validation unit tests, capabilities route test, job lifecycle and failed-jobs UAT |
| Gadget/mode operations | gadgetd IPC contract | `gadgetd` | gadget/mode routes | settings/mode controls | gadget IPC and operation-banner UAT |
| Mapping administration | indexd schema/query contract | `indexd/src/db`, scan/index jobs | `webd/src/query.rs`, `route.rs` | `TripMap.tsx`, Analytics/event screens | index lifecycle, query bounds, mapping UAT |
| FSCK | new maintenance job contract | new Rust owner after design decision | planned `/api/fsck/*` | planned settings/maintenance screen | cancellation/exclusion integration |
| Migration | new setup migration contract | new Rust migration utility or crate | optional migration status API | migration is operator CLI/setup work | fixture dry-run/apply/rollback |
| Release validation | release manifest/setup contracts | all shipped daemons | webd static bundle | SPA build/UAT | release tests, setup-lib tests, full UAT |

## Contract change rules

1. A new webd mutation requires a typed request/response DTO, same-origin or
   operator authorization, bounded input, explicit error mapping, and a
   corresponding API-client method.
2. A cross-daemon mutation requires an IPC contract update, durable terminal
   status, restart behavior, and at least one failure-injection test.
3. A destructive operation requires a server-side plan/validation step and
   must not rely on client confirmation as its safety boundary.
4. A UI change requires the affected Playwright spec, console/network checks,
   and the full two-viewport UAT gate before its workstream is marked complete.
5. A migration change requires a fixture, dry-run output, idempotence proof,
   verification report, and rollback/recovery test.

## Current implementation order

1. Finish this index and the capability matrix.
2. Extend the cloud contracts and implement the cloud control plane.
3. Define retention/delete contracts before exposing archive mutation buttons.
4. Wire the already-existing Wi-Fi APIs into captive onboarding.
5. Add jobs, settings, gadget/mode, mapping administration, and FSCK in
   dependency order.
6. Implement migration only after destination contracts are stable.
