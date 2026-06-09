# Implementation Plan — TeslaUSB B-1 reset (kernel LUN + full-Rust rebuild)

> Status: Draft for review. Derived from [`docs/specs/`](../specs/README.md).
> Companion to [`tasks.md`](./tasks.md) (the detailed task cards) and
> [`README.md`](./README.md) (how to use this set).
> Methodology is **hardware-first**: prove the risky thing on the real Pi with a
> small throwaway spike **before** any long buildout
> ([`hardware-first-development.md`](../specs/hardware-first-development.md)).

---

## 1. Overview

We are resetting the B-1 architecture: removing the userspace "pretend drive"
(teslafat over NBD) and the Python/Flask web app, and rebuilding on a
**kernel-backed mass-storage gadget** (`usb_f_mass_storage`, `file=disk.img`) with
a **full-Rust service layer** and a **static SPA** that achieves *visual parity*
with today's UI. The single non-negotiable invariant governs everything:

> **The car must ALWAYS be able to write TeslaCam when powered on.**
> A Pi crash/reboot must look like a *clean unplug*, never an I/O error
> ([`SPEC.md` §2](../specs/SPEC.md)).

This plan sequences the work as: **clean slate → device prep → hardware spikes
(gates) → build services bottom-up → parity SPA → storage/uploads/wifi →
installer + migration + hardening.** Each build phase is **gated** by the spike(s)
that de-risk it; a spike **FAIL** re-orders the roadmap rather than sinking weeks
of work.

## 2. Operator decisions captured for this plan

| # | Decision | Consequence |
|---|----------|-------------|
| P1 | **Clean branch carrying the KEEP-list** (not docs-only, not in-place delete). Carry `docs/`, `.github/`, `rust/` with **only `teslausb-core`**, and root hygiene files. | Legacy (`web/` Flask, `teslafat`, `teslausb-worker`, v1 `setup*`/`uninstall*`, `config/` nginx/gunicorn, old `scripts/`) stays recoverable on `main`/`b1-userspace-rust`; the new branch starts clean. |
| P2 | **Dev Pi reachable over SSH; recording down/unsure.** | Phase 1 begins with assessment + (operator) VBUS recovery per [`migration.md` §4](../specs/migration.md). |
| P3 | **In-place clean (O1 / migration M1–M2)** on the dev Pi — keep the existing OS, strip old B-1 software. | Rehearses the real in-car conversion path; no reflash. |
| P4 | **Keep `teslausb-core`** (SEI parser + raw exFAT/MP4 parsing) per [`SPEC.md` §4](../specs/SPEC.md) "Reuse vs. new". | Its teslafat-specific synth modules are pruned in a later task, not deleted wholesale up front. |
| P5 | **Capture a UI parity baseline** from the running old Flask app before it is decommissioned. | The SPA's parity target is preserved as screenshots/DOM artifacts even after the legacy app leaves the working tree. |

## 3. Architecture decisions (from the specs — not re-litigated here)

- **S1 + A + O1:** image-file LUN (MBR + p1 TeslaCam exFAT + p2 media exFAT) +
  full-Rust app + existing OS cleaned in place ([`SPEC.md` §3](../specs/SPEC.md)).
- **`gadgetd` is the only CRITICAL service**; everything else is disposable and
  memory-capped. OOM order: `uploadd → wifid → webd → scannerd → retentiond →
  indexd → NEVER gadgetd` ([`SPEC.md` §7](../specs/SPEC.md)).
- **No transcoding on the Pi**; SEI parsed once at index time; HUD rendered
  client-side. **No Python/Flask, nginx, or NBD** in the runtime
  ([`SPEC.md` §10](../specs/SPEC.md)).
- **`gadgetd` is the sole owner of `disk.img`/the write path.** `setup.sh` never
  partitions/formats it ([`setup.md` §2](../specs/setup.md)).

## 4. Dependency graph (build order follows it bottom-up)

```
docs/specs (done) ── teslausb-core (KEEP, curate)
                          │
   ┌──────────────────────┼───────────────────────────────┐
   │                      │                                │
HARDWARE SPIKES (gates, Phase 2) ── fold params into specs │
   │  LUN acceptance ─┬─ Eject/rebind ─┬─ Boot time         │
   │                  │                │                    │
   │  Parse stability ┤  SEI/HUD       │  WiFi TX cap       │
   │  microSD cont.   │  disk.img size │                    │
   ▼                  ▼                ▼                    ▼
 gadgetd (CRITICAL) ── scannerd ── indexd ── webd ── SPA (parity)
   │                      │          │        │
   │                      └── retentiond (storage governor) ── uploadd
   │                                                              │
   └── wifid                                                      │
                                                                  ▼
                                  setup.sh + manifest/artifacts ── migration M4/M5
```

Implementation order (each gated by its spike):
`gadgetd → scannerd → indexd → webd → SPA → retentiond → uploadd → wifid →
setup.sh → migration M4 → hardening M5`.

## 5. Phases & checkpoints

The full task cards (acceptance criteria, verification, files, size) live in
[`tasks.md`](./tasks.md). Phases:

| Phase | Theme | Hardware? | Gated by |
|-------|-------|-----------|----------|
| **0** | Clean slate (branch + workspace + parity baseline) | No | — |
| **1** | Device prep — assess, M1 backup, M2 clean | **Yes (rails)** | — |
| **2** | Hardware-first spikes (the gates) | **Yes (rails)** | Phase 1 clean |
| **3** | `gadgetd` (CRITICAL) | **Yes** | LUN acceptance, Eject/rebind, Boot time, disk.img sizing |
| **4** | Read/index pipeline (`scannerd`, `indexd`) | partial | Parse stability, SEI/HUD |
| **5** | Web + parity SPA (`webd`, SPA screens) | No (Playwright) | Phase 4 |
| **6** | Storage governor, uploads, wifi (`retentiond`, `uploadd`, `wifid`) | partial | microSD contention, WiFi TX cap |
| **7** | Installer + migration + hardening (`setup.sh`, M4, M5) | **Yes** | Phases 3–6 |

### Checkpoint: after Phase 0
- [ ] Clean branch builds: `cargo build --workspace` green with only `teslausb-core`.
- [ ] `cargo clippy --workspace -- -D warnings` and `cargo fmt --all --check` clean.
- [ ] No `teslafat`/`teslausb-worker`/`web/`/`nginx`/`python` in **runtime/source paths**
      (`rust/`, `spa/`, build scripts) — `docs/` and `.github/skills/` intentionally
      reference those terms as *forbidden/legacy* and are excluded from the check.
- [ ] UI parity baseline artifacts captured and stored.

### Checkpoint: after Phase 1 (device clean)
- [ ] Device reachable; SSH/WiFi/boot verified; dead-man rails green.
- [ ] Recording state known; if it was down, recovery verified (UDC `state=configured`, write counters climbing).
- [ ] M1 backups verified (checksums); M2 before/after inventory captured; old B-1 software gone.

### Checkpoint: after Phase 2 (gates)
- [ ] **LUN acceptance PASS** (make-or-break) with captured parameters, or architecture re-decided.
- [ ] Every §9 unknown is PASS-with-params or explicitly re-ordered on FAIL; findings folded into the owning spec.

### Checkpoint: after Phase 3 (`gadgetd` / migration M3)
- [ ] Car records reliably on the kernel LUN; service crash/restart presents as a **clean unplug** (invariant test + on-device proof).
- [ ] Eject-handoff refuses to mutate during a simulated active save.

### Checkpoint: after Phase 5 (parity)
- [ ] Every parity screen reproduced; durable Playwright suite green (perf, zero console/pageerror, 375px + ≥1280px, wiring proof).
- [ ] Visual parity confirmed against the Phase 0 baseline.

### Checkpoint: after Phase 7 (complete)
- [ ] `setup.sh install --dry-run` clean on a supported Pi; `install` brings all 7 services healthy and the **car records**.
- [ ] `update` converges without destroying `disk.img`/secrets/archive/index.
- [ ] M5 hardening in place (RO-root+overlay, watchdog, MemoryMax, OOM order); soak passed; old paths decommissioned.

## 6. Working method (binding for every phase)

- **Hardware-first:** never start a long buildout on an unproven hardware
  assumption; spike it first, time-boxed, via the **hardware-test skill** with
  dead-man/SSH/WiFi/boot/backup rails
  ([`hardware-first-development.md`](../specs/hardware-first-development.md)).
- **Fail fast:** a spike **FAIL is a win** — pivot or escalate before sinking
  buildout into a wrong assumption.
- **Second opinion:** reconcile a **GPT-5.5** opinion before any risky live step;
  run a **rubber-duck** critique on non-trivial plans/implementations
  (`.github/copilot-instructions.md`).
- **UI = Playwright:** no UI task is "done" without end-to-end Playwright
  verification (perf + console + screenshot + wiring) — [`spa.md` §5](../specs/spa.md).
- **Charter:** code is reviewed against `SPEC.md` §7–§10 via the **charter-review
  skill** before merge.
- **Deploy mechanism (resolves an apparent contradiction).** [`setup.md`](../specs/setup.md)
  says `setup.sh` is the **single install mechanism** — that governs the *product*
  install (end users + the operator migration M4). But Phases 3–6 must verify real
  services **on the device before `setup.sh` exists** (7.1). For that pre-`setup.sh`
  window, on-device deployment is the **rails-protected manual deploy of the
  `hardware-test` skill**, which [`SPEC.md` §5](../specs/SPEC.md) explicitly
  sanctions ("deployment to the live device is **only** via the hardware-test
  skill"). These dev deploys are throwaway/validation; `setup.sh` (7.1) then
  codifies the durable, idempotent installer that supersedes them. No hand-editing
  the device by any other path.

## 7. Risks & mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| **LUN-acceptance FAIL** — car rejects single-image 2-partition LUN | **Critical** (sinks S1) | First spike, make-or-break; escalate to operator for a layout/architecture decision before any build. |
| Recording currently down on dev Pi | High | Phase 1 starts with assessment + operator VBUS recovery (P2, migration §4). The dev Pi is treated as a **maintenance-window** device — recording down/unsure, not protecting a live car — so M2 may decommission the old write path before the new LUN exists (1.3). On a **real in-car** device this ordering is stricter: keep the old path until M3 proves the new one. |
| Losing the visual-parity reference when legacy leaves the tree | Medium | P5: capture Playwright baseline of the old app first; legacy stays recoverable on `main`. |
| `teslausb-core` carries teslafat-only synth code | Low/Medium | Keep the crate; prune the synth/backing/NBD modules as an explicit **Phase 0 (task 0.2)** step — list KEEP vs DROP modules, not a rushed delete. |
| SDIO deadlock / boot-time / microSD contention surprises | High | Dedicated gated spikes (#4, #6, #5) before `wifid`/`uploadd`/concurrency buildout. |
| Building on the Pi (too slow / OOM) | Medium | Host cross-compile + manifest/artifact verification; `setup.sh` never builds on-device ([`setup.md` §5](../specs/setup.md)). |
| Clean branch loses git provenance | Low | Branch from current history (not an empty orphan unless chosen); `main` retains the full old stack. |

## 8. Parallelization opportunities

- **Safe to parallelize:** Phase 0 workspace curation vs. parity-baseline capture;
  within Phase 5, independent SPA screens once `webd` endpoints exist; durable
  Playwright tests for already-built screens.
- **Must be sequential:** all device-mutating steps (one hardware-test session at a
  time); the spike order in Phase 2; `gadgetd` before anything that mounts/handoffs.
- **Needs coordination (contract-first):** `webd` REST/SSE API shape must be fixed
  before SPA screens fan out; `indexd` SQLite schema before `webd`/`retentiond`
  read it.

## 9. Open questions (resolve before / during the gated phases)

- **✅ Resolved — `wifid` Pi-reboot path vs `SPEC.md` §2:** previously a spec-vs-spec
  conflict ([`SPEC.md` §2](../specs/SPEC.md) "nothing else may ever reboot" vs
  [`wifid.md` §4](../specs/wifid.md) USB-idle-gated last-resort reboot). **Operator
  decision: amend `SPEC.md` §2** to record the USB-idle-gated `wifid` reboot as the
  single sanctioned non-`gadgetd` reboot (chip-reset tried first; reboot only while
  the write path is idle per `gadgetd`'s heartbeat). `SPEC.md` invariant 4 and the
  NEVER list now carry this exception; task 6.2 implements it as written.
- **Branch mechanics:** orphan branch (no history) vs. new branch off
  `b1-userspace-rust` with a single clean-slate commit? (Plan assumes the latter
  for provenance; confirm at T0.1.) New branch name? (proposed: `b1-clean`.)
- **`disk.img` absolute size + p1/p2 split:** decided at the disk.img-sizing spike
  on the real card, not here ([`SPEC.md` §9 #8](../specs/SPEC.md)).
- **Cloud uploader backend:** **rclone** (provider breadth) vs. a **small Rust
  uploader** (footprint) — left **open** by [`uploadd.md` §2.2](../specs/uploadd.md);
  decide at the start of Phase 6 (task 6.3-upload), don't pre-bake it.
- **Spike ordering:** Phase 2 tasks follow the
  [`hardware-first-development.md` §5](../specs/hardware-first-development.md) wave
  order exactly; within a wave, independent spikes may be re-ranked as findings land
  (hardware-first §6), but the **make-or-break LUN gate is absolute**.
- **TeslaCam bootstrap marker:** whether the car needs a seeded empty top-level
  `TeslaCam/` — resolved by the LUN-acceptance spike ([`setup.md` §8](../specs/setup.md)).
- **Network manager choice:** NetworkManager vs systemd-networkd — pick one in
  Phase 6/7 and stay consistent ([`setup.md` §7](../specs/setup.md)).
- **SPA framework:** Preact/Svelte/Solid — pick at T5.2 within the parity
  constraints ([`SPEC.md` §7](../specs/SPEC.md)).

## 10. Out of scope (for this plan)

- Reflashing/repartitioning the live boot card (S2) — off the table unless the
  operator explicitly chooses it ([`SPEC.md` §10 ASK FIRST](../specs/SPEC.md)).
- Any redesign of the UI's visual language — parity is the goal, not a new look
  ([`spa.md` §7](../specs/spa.md)).
- Re-introducing Python/Flask, nginx, or NBD in any form.

---

## 11. Website completion backlog — enumerated parity gap (2026-06-09)

> Added after an audit showed the SPA ships **4 of 16 parity screens** and `webd`
> is a **read-only catalog** (no mutation/config/status surface). `tasks.md` Task
> 5.1 (slices a–e) and Task 5.3 (per-screen) named this work in prose but never
> enumerated it, so it was invisible in tracking. IDs below match the session
> tracker. Every UI card carries the mandatory Playwright UAT gate (spa.md §5/§6).

### 11.1 `webd` backend (contract-first; screens depend on these)
| ID | Slice | Status |
|----|-------|--------|
| (done) | 5.1a Read API (days/trips/events/clips/analytics/settings) | DONE |
| (done) | 5.1b stream + zip export | DONE (leases pending) |
| be-leases | 5.1b playback lease (TTL+heartbeat) so governor can't evict mid-read | dep: svc-retentiond |
| be-status-health | 5.1d /api/system/health, /api/storage, /api/storage/health | ready |
| be-jobs-sse | 5.1d /api/jobs SSE (index/handoff/upload/job status) | ready |
| be-mutations-core | 5.1c delete clip → validate → gadgetd handoff → SSE progress | ready |
| be-media-install | 5.1c install/remove media (toybox write path) | ready |
| be-toybox-endpoints | boombox/music/lightshows/chimes/plates/wraps GET+POST | dep: be-media-install |
| be-cloud-config | /api/cloud/* → uploadd | dep: svc-uploadd-decide |
| be-retention-config | retention policy → retentiond | ready |
| be-wifi-config | wifi/AP config → wifid | ready |
| be-captive-portal | 5.1e /portal AP onboarding entry | dep: be-wifi-config |

### 11.2 SPA screens (4 built: trip map, event player+HUD, analytics, settings shell)
| ID | Screen | Depends on |
|----|--------|-----------|
| fe-settings-live | Settings dashboard → live data (kills "Status Unknown / —") | be-status-health |
| fe-media-hub | Home / media hub landing (today a ComingSoon stub) | — |
| fe-clip-delete | clip-delete + handoff progress in player | be-mutations-core, be-jobs-sse |
| fe-boombox / fe-music / fe-lightshows / fe-chimes / fe-plates / fe-wraps | six toybox managers | be-toybox-endpoints |
| fe-cloud | Cloud archive (today a ComingSoon stub) | be-cloud-config, svc-uploadd |
| fe-storage-settings | Storage settings | be-status-health, be-retention-config |
| fe-storage-health | Storage health widgets | be-status-health |
| fe-failed-jobs | Failed jobs | be-jobs-sse |
| fe-captive-portal | First-run WiFi onboarding (over AP) | be-captive-portal, svc-wifid-ap |
| fe-durable-suite | Checked-in Playwright suite (CI gate, spa.md §6) | — |

### 11.3 Supporting services / hardware these depend on
| ID | Work |
|----|------|
| svc-retentiond | enable+validate retentiond (inactive today; card-fill protection) |
| svc-uploadd-decide | OPEN: rclone vs native-Rust cloud backend (gates cloud) |
| svc-uploadd | implement+enable uploadd |
| svc-wifid-hw | real wifid hardware layer (stubs today); seed Trez STA + boot backstop FIRST |
| svc-wifid-ap | AP mode + connectivity backstop (lockout risk — SSH must survive) |
| svc-gadgetd-handoff-uat | first live UI→webd→gadgetd write-path exercise |
| ops-wifi-watchdog | decommission legacy wifi-watchdog.timer (still armed — safety) |
| hw-2-1-lun | make-or-break LUN-acceptance spike (operator-at-vehicle) |
| hrd-m5-hardening | non-root User=, CAP_NET_BIND_SERVICE (webd :80), MemoryMax, watchdog |
| hrd-installer-finalize | Phase 7 installer/verifier/first-boot |
| ops-push-port80 | commit+push :80 change + 2 held commits (awaiting go) |
