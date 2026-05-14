# TeslaUSB Documentation

This directory holds the in-depth documentation for the TeslaUSB
project. It is organized around **what the device does** and the
**decisions** it makes — not around where the source files happen to
live. If you want a quick install or feature overview, the project
[`readme.md`](../readme.md) at the repo root is the right starting
point.

---

## Who is this for?

The docs are written for two audiences. Pick the column that matches
why you're here.

| Audience      | Goal                                                  | Start here                                           |
|---------------|-------------------------------------------------------|------------------------------------------------------|
| **Operator**  | Install, configure, monitor, recover a TeslaUSB device | [`operator/README.md`](operator/README.md)           |
| **Contributor** | Modify the code with full understanding of subsystem contracts | [`contributor/README.md`](contributor/README.md)     |

If you're not sure, you're probably an operator first — the operator
docs cross-link into the contributor docs when a deeper explanation
is helpful.

---

## Reading order — the 30-minute tour

If you have 30 minutes and want to understand the device, read these
five documents in order:

1. **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — what the major
   components are and how they hand off work to each other.
2. **[`VIDEO_LIFECYCLE.md`](VIDEO_LIFECYCLE.md)** — the flagship
   narrative. Follows a single dashcam clip from the moment Tesla
   writes it to the moment it's deleted, with every branch
   enumerated. **This is the most important doc in the repo.**
3. **[`GLOSSARY.md`](GLOSSARY.md)** — every recurring term with a
   one-paragraph explanation.
4. **[`UI_UX_DESIGN_SYSTEM.md`](UI_UX_DESIGN_SYSTEM.md)** — design
   tokens and rules for any UI work.
5. **[`contributor/REPO_LAYOUT.md`](contributor/REPO_LAYOUT.md)** —
   where things live in the source tree.

After that, navigate by interest from `operator/` or `contributor/`.

---

## Top-level documents

| Document                                          | What it covers                                                              |
|---------------------------------------------------|-----------------------------------------------------------------------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md)              | System overview, components, data flow, processes and threads              |
| [`VIDEO_LIFECYCLE.md`](VIDEO_LIFECYCLE.md)        | Full lifetime of a clip with every decision point — the **flagship doc**    |
| [`GLOSSARY.md`](GLOSSARY.md)                      | Definitions of every recurring TeslaUSB term                                |
| [`UI_UX_DESIGN_SYSTEM.md`](UI_UX_DESIGN_SYSTEM.md) | Design tokens, colors, typography, components, accessibility rules          |

---

## Operator docs (`operator/`)

Runbooks and reference for people deploying and managing devices.

| Document                                                    | When to read                                                  |
|-------------------------------------------------------------|---------------------------------------------------------------|
| [`operator/README.md`](operator/README.md)                  | Entry point — points you at the right runbook                  |

> Additional operator runbooks (installation walkthrough, configuration
> reference, services and timers, web interface tour, storage and
> retention, cloud providers setup, WiFi and AP, failed jobs and health,
> backup and recovery, upgrading, troubleshooting) are being filled in
> across the documentation waves. See **Documentation status** below.

---

## Contributor docs (`contributor/`)

Internals, contracts, and decision points for people modifying the
source.

| Document                                                                                  | When to read                                              |
|-------------------------------------------------------------------------------------------|-----------------------------------------------------------|
| [`contributor/README.md`](contributor/README.md)                                          | Entry point — points you at core / subsystems / flows      |
| [`contributor/REPO_LAYOUT.md`](contributor/REPO_LAYOUT.md)                                | Where everything lives in the source tree                  |
| [`contributor/DEV_WORKFLOW.md`](contributor/DEV_WORKFLOW.md)                              | Branching, testing, deploying, security review skill       |
| [`contributor/core/CONFIGURATION_SYSTEM.md`](contributor/core/CONFIGURATION_SYSTEM.md)    | `config.yaml` + Bash and Python wrappers + templating      |
| [`contributor/core/DATABASES.md`](contributor/core/DATABASES.md)                          | `geodata.db` and `cloud_sync.db` schemas, migrations       |

> Core internals (boot sequence, USB gadget and modes, mount safety,
> task coordinator, file safety), subsystems (every worker / service),
> end-to-end flows, and the reference catalog (IndexOutcome enum,
> archive priorities, event types, error catalog, API endpoints, etc.)
> are being filled in across the documentation waves below.

---

## Documentation status

Documentation is built in **waves**. Each wave is a coherent slice
landed as its own pull request. Status as of writing:

| Wave   | Theme                                                   | Status        |
|--------|---------------------------------------------------------|---------------|
| 1      | Foundation + flagship video lifecycle                   | **landed**    |
| 2      | Core internals (boot, gadget, mounts, coordinator)      | planned       |
| 3      | Video pipeline subsystems + flows + decision references | planned       |
| 4      | Cloud + sync (LES vs cloud_archive arbitration)         | planned       |
| 5      | Video playback / deletion / retention flows             | planned       |
| 6      | Networking + safety                                     | planned       |
| 7      | Mode + boot flows                                       | planned       |
| 8      | Asset-management subsystems + flows                     | planned       |
| 9      | Operator runbooks                                       | planned       |
| 10     | Reference completion + polish                           | planned       |

Until a planned wave lands, the most authoritative source for that
material is the source code itself (and, as a quick-reference index,
[`.github/copilot-instructions.md`](../.github/copilot-instructions.md),
which gives a dense overview of every subsystem).

---

## Conventions used throughout these docs

- **Mermaid diagrams.** Sequence flows, state machines, and decision
  trees use Mermaid blocks. GitHub renders them natively in the web UI;
  in plain-text viewers the source is still readable.
- **Code citations on every claim.** When a doc states "the worker
  does X", it cites the function and the source file (e.g.
  `mapping_service.index_single_file()`). Line numbers are deliberately
  avoided because they drift; function names and module paths stay
  stable.
- **Decision tables.** Every branch the code takes is rendered as a
  table or flowchart so a reader can answer "what happens if…?"
  without reading code.
- **No timelines or roadmap.** These docs describe **current**
  behavior. Plans for the future live in
  [GitHub Issues](https://github.com/mphacker/TeslaUSB/issues).
- **Stale-prevention footer.** Every doc ends with a `## Source files`
  list, so a reviewer touching one of those files knows exactly which
  docs to update.

---

## Source files

This document is purely navigational; it has no source-code dependency.
