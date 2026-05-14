# Contributor Documentation

Welcome — this section is for **contributors**, the people who modify
the TeslaUSB source code. Read this first; it points you at the
right deeper docs.

If you're here to **operate** a device (deploy, configure, monitor,
recover), you want [`../operator/README.md`](../operator/README.md)
instead.

---

## Before you write a single line of code

Read these in order, even if you've contributed before:

1. **[`../README.md`](../README.md)** — the docs hub, including the
   conventions used in every document (Mermaid diagrams, code
   citations, decision tables, source-file footers).
2. **[`../ARCHITECTURE.md`](../ARCHITECTURE.md)** — what the major
   components are and how they interact.
3. **[`../VIDEO_LIFECYCLE.md`](../VIDEO_LIFECYCLE.md)** — the flagship
   narrative. Even if you're touching something unrelated to video,
   the lifecycle doc shows the **decision-density and citation
   style** every contributor doc aims for.
4. **[`REPO_LAYOUT.md`](REPO_LAYOUT.md)** — where things live in
   the source tree, plus naming conventions.
5. **[`DEV_WORKFLOW.md`](DEV_WORKFLOW.md)** — branching, testing,
   deploying, and the security-review skill.
6. **[`../../.github/copilot-instructions.md`](../../.github/copilot-instructions.md)**
   — the dense, opinionated rule-set every PR is reviewed against.
   The contributor docs in this folder are the **narrative**
   counterpart to that file. The two should never disagree; if they
   do, the source code is the tiebreaker.

---

## Folder layout

```
docs/contributor/
├── README.md            ← you are here
├── REPO_LAYOUT.md       ← source-tree map
├── DEV_WORKFLOW.md      ← test/branch/PR/deploy/security-review
│
├── core/                ← cross-cutting infrastructure
│   ├── CONFIGURATION_SYSTEM.md
│   └── DATABASES.md
│
├── subsystems/          ← one doc per worker / service (Waves 3-8)
├── flows/               ← end-to-end traces with sequence diagrams (Waves 3-8)
└── reference/           ← lookup tables and API surface (Waves 3-10)
```

`subsystems/`, `flows/`, and `reference/` are populated across
documentation waves — see the
[status table](../README.md#documentation-status). The two `core/`
docs that exist now (Configuration system, Databases) are the ones
every other doc cites.

---

## Where to look first by topic

These cross-references will be filled out wave-by-wave. Until then,
this table is also a map of where each topic **will** be documented.

| Topic                          | Doc to read (planned wave)                                           |
|--------------------------------|----------------------------------------------------------------------|
| Boot sequence                  | `core/BOOT_SEQUENCE.md` *(Wave 2)*                                   |
| USB gadget, modes, LUNs        | `core/USB_GADGET_AND_MODES.md` *(Wave 2)*                            |
| Mount safety, `nsenter`        | `core/MOUNT_SAFETY.md` *(Wave 2)*                                    |
| Configuration system           | [`core/CONFIGURATION_SYSTEM.md`](core/CONFIGURATION_SYSTEM.md)        |
| Task coordinator               | `core/TASK_COORDINATOR.md` *(Wave 2)*                                |
| Databases                      | [`core/DATABASES.md`](core/DATABASES.md)                              |
| File safety, atomic writes     | `core/FILE_SAFETY.md` *(Wave 2)*                                     |
| File watcher (`inotify`)       | `subsystems/FILE_WATCHER.md` *(Wave 3)*                              |
| Archive subsystem              | `subsystems/VIDEO_ARCHIVE.md` *(Wave 3)*                             |
| Indexing subsystem             | `subsystems/VIDEO_INDEXING.md` *(Wave 3)*                            |
| Mapping and trips              | `subsystems/MAPPING_AND_TRIPS.md` *(Wave 3)*                         |
| Event detection thresholds     | `subsystems/EVENT_DETECTION.md` *(Wave 3)*                           |
| Cloud archive                  | `subsystems/CLOUD_ARCHIVE.md` *(Wave 4)*                             |
| Live Event Sync (LES)          | `subsystems/LIVE_EVENT_SYNC.md` *(Wave 4)*                           |
| Failed Jobs internals          | `subsystems/FAILED_JOBS_INTERNALS.md` *(Wave 4)*                     |
| WiFi / AP internals            | `subsystems/WIFI_AND_AP_INTERNALS.md` *(Wave 6)*                     |
| Captive portal                 | `subsystems/CAPTIVE_PORTAL.md` *(Wave 6)*                            |
| Memory and watchdog            | `subsystems/MEMORY_AND_WATCHDOG.md` *(Wave 6)*                       |
| Lock chimes                    | `subsystems/LOCK_CHIMES.md` *(Wave 8)*                               |
| Light shows and wraps          | `subsystems/LIGHT_SHOWS_AND_WRAPS.md` *(Wave 8)*                     |
| Telemetry / SEI parsing        | `subsystems/TELEMETRY_AND_SEI.md` *(Wave 3)*                         |

---

## Writing new documentation

If you're contributing a new doc (or modifying one), follow the
**per-doc template** the existing docs use — at minimum:

1. **One-sentence summary** at the top.
2. **At a glance** — 30-second overview.
3. **Sequence diagram** in Mermaid.
4. **Step-by-step walkthrough** with code citations (function +
   module, no line numbers).
5. **Decision points** as a table or `flowchart` Mermaid block.
6. **Configuration** — which `config.yaml` keys affect this and how.
7. **Failure modes & recovery** — power loss, lock contention,
   missing files.
8. **Source files** footer — list every module the doc covers.

Every claim in the doc should either be obviously true from the
prose or cite a function. **Don't speculate** — if you're not sure
how the code branches, read the code or grep for the constant
before writing.

---

## A few non-obvious rules

These come from `.github/copilot-instructions.md` and are worth
internalizing before you make your first change:

1. **Background subsystems must NEVER unmount or rebind the USB
   gadget.** Tesla may be recording. The only USB-disrupting
   operations are user-initiated (`quick_edit_part2`, mode switch,
   gadget rebind after lock chime change).
2. **Loop devices serve local mounts only.** The gadget binds the
   `.img` file directly. Unmounting/detaching loop devices does not
   affect Tesla's view of the drive.
3. **Trips are sacred.** `purge_deleted_videos` only deletes the
   `indexed_files` row and NULLs the `video_path` on waypoints and
   detected events. It never deletes trips, waypoints, or events.
   That rule prevented (and prevents) a class of data-loss
   regressions.
4. **Tesla's onboard clock can drift hours.** The MP4 `mvhd` atom
   is the authoritative absolute time. Use `_resolve_recording_time`,
   never the filename, for anything time-sensitive.
5. **The `task_coordinator` lock must never be held across sleeps.**
   Cyclic workers do `acquire → work → release → sleep`, never
   `acquire → work → sleep → release`. That rule prevented production
   data loss in PR #78.
6. **The Pi Zero 2 W has 512 MB of RAM and a single SDIO bus.**
   Every new dependency, thread, and in-memory cache needs a
   justification. Saturating SDIO can starve the watchdog daemon
   and trigger a hardware reset.
7. **Never install dependencies, scratch files, or test artifacts
   inside the git working tree.** Test tooling (Playwright, npm
   packages, debug scripts) lives **outside** the repo.

---

## Source files

This document is purely navigational; it has no source-code
dependency.
