# TeslaUSB B-1 — Plan & Task Breakdown

This folder is the **execution plan** for the B-1 reset, derived from the
specifications in [`../specs/`](../specs/README.md). The specs say *what* we are
building and *why*; this folder says *in what order*, *gated by what*, and *how we
know each step is done*.

## Documents

| File | Purpose |
|------|---------|
| [`plan.md`](./plan.md) | Strategy: overview, captured operator decisions, dependency graph, phases, checkpoints, risks, parallelization, open questions. |
| [`tasks.md`](./tasks.md) | The detailed task cards — acceptance criteria, verification, dependencies, likely files, and size per task. |

## How to use this

1. **Read [`plan.md`](./plan.md) first** for the phase structure and the gating
   model, then work the cards in [`tasks.md`](./tasks.md) in dependency order.
2. **Hardware-first is binding.** Build phases (3+) do **not** start until their
   gating spike (Phase 2) is **PASS with captured parameters**. A spike **FAIL is
   a win** — pivot or escalate before sinking buildout
   ([`../specs/hardware-first-development.md`](../specs/hardware-first-development.md)).
3. **Device work runs only via the `hardware-test` skill** (dead-man reboot timer,
   SSH/WiFi/boot protected, backups before mutate). **Reconcile a GPT-5.5 second
   opinion before any risky live step.**
4. **UI work requires Playwright** end-to-end verification (perf + console +
   screenshot + wiring) — it is not done without it
   ([`../specs/spa.md` §5](../specs/spa.md)).
5. **Review against the charter** (`SPEC.md` §7–§10) via the `charter-review`
   skill before merge.

## Phase map (at a glance)

| Phase | Theme | Hardware | Key gate |
|-------|-------|----------|----------|
| 0 | Clean slate (branch + workspace + parity baseline) | no | — |
| 1 | Device prep — assess, M1 backup, M2 clean | yes | — |
| 2 | Hardware-first spikes (the gates) | yes | **LUN acceptance = make-or-break** |
| 3 | `gadgetd` (CRITICAL) / migration M3 | yes | LUN, eject/rebind, boot, disk.img |
| 4 | Read/index (`scannerd`, `indexd`) | partial | parse-stability, SEI/HUD |
| 5 | Web + parity SPA (`webd`, screens) | Playwright | Phase 4 |
| 6 | Storage governor, uploads, wifi | partial | microSD contention, WiFi TX cap |
| 7 | Installer + migration M4/M5 + hardening | yes | Phases 3–6 |

## Status tracking

These documents are the **source of truth** for scope and ordering. Per-session
execution status can be tracked in the session todo store; keep it in sync with
the task IDs here (`0.1`, `2.1`, `3.3`, …). Update the spec files (not just these
tasks) whenever a hardware spike folds a proven parameter back in.
