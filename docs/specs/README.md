# TeslaUSB specs

Spec set for the **B-1 reset** architecture (kernel-backed LUN + full-Rust
rebuild, preserving the existing web app's look & feel). Background and the
4-model synthesis behind these decisions live in [`../plan.md`](../plan.md).

Start here: **[`SPEC.md`](./SPEC.md)** — the overarching system spec (objective,
the #1 invariant, the S1+A+O1 architecture, component map, and cross-cutting
standards). The component specs below are subordinate to it; `SPEC.md` §7–§8
carry the engineering and testing standards directly (these specs are
self-contained — there is no separate B-1 charter or ADR set anymore).

| Spec | What it covers |
|------|----------------|
| [`SPEC.md`](./SPEC.md) | System overview, invariant, architecture, standards, boundaries |
| [`tesla-usb-contract.md`](./tesla-usb-contract.md) | External Tesla USB interface: partitions, folders, case/naming, camera files, media features, rotation reality |
| [`gadgetd.md`](./gadgetd.md) | **CRITICAL** kernel mass-storage LUN + eject-handoff (the write path) |
| [`scannerd.md`](./scannerd.md) | R1 raw exFAT/MP4/SEI reader (never mounts) |
| [`indexd.md`](./indexd.md) | Trips/events/clips derivation → SQLite (WAL) |
| [`webd.md`](./webd.md) | axum REST/SSE API + static SPA host |
| [`spa.md`](./spa.md) | Parity UI (media hub, trip map, event player + HUD, media managers) |
| [`uploadd.md`](./uploadd.md) | Durable, throttled, prioritized cloud upload |
| [`retentiond.md`](./retentiond.md) | Per-folder archiving + retention (never lose Saved/Sentry; bounded RecentClips mirror) |
| [`storage.md`](./storage.md) | SD-card space governance: continuous low-space governor, reserve tiers, value-based eviction, crash-safe deletion |
| [`wifid.md`](./wifid.md) | STA/AP state machine + SDIO chip-reset watchdog |
| [`hardware-first-development.md`](./hardware-first-development.md) | Methodology: spike/PoC on real hardware before buildout; ordered/gated risk-named de-risking backlog; agile feedback loop |
| [`migration.md`](./migration.md) | In-place M1–M5 migration + hardening |
| [`setup.md`](./setup.md) | Idempotent `setup.sh` device install/configure (public clone-and-install + maintainer release) |
