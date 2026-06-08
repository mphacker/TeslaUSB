# SPEC — Hardware-first development (de-risk on real hardware before buildout)

> Parent: [`SPEC.md`](./SPEC.md) · Type: methodology / process (not a service)
> Tooling: the **`hardware-test` skill** against the live
> target. Companion to [`SPEC.md` §9](./SPEC.md) (the prototype-first unknowns)
> and [`migration.md`](./migration.md) (the in-place M-series rollout).

This document defines **how we work**, not what we build: prove the risky thing
on the actual device with a small throwaway spike **before** committing to a long
buildout on top of it. It exists because several unknowns in this stack can
**invalidate large amounts of work** if an assumption is wrong (the car latches,
the SDIO bus deadlocks, the raw parser races the car's writes), and the
[#1 invariant](./SPEC.md) makes a late hardware surprise expensive — possibly
recording-down.

## 1. The principle

- **Spike before buildout.** When a decision rests on real-hardware behavior we
  can't prove on paper, build the **smallest experiment** that answers the
  question on the **live device first**. Fail fast, cheaply, early.
- **A FAIL is a win.** Discovering an assumption is wrong during a half-day spike
  is a *success* — the same discovery after a multi-week buildout is a disaster.
- **Working-on-hardware over comprehensive-on-paper.** Specs are necessary but
  not sufficient; the car and the Pi are the source of truth for anything they
  touch. Stay agile: short loops, respond to hardware reality immediately, re-rank
  the plan as findings land.
- **The invariant still binds during spikes.** Never knowingly leave the car
  unable to record; recording-affecting probes are gated behind operator
  confirmation and a known-good restore.

## 2. When a spike is required (vs. just build it)

Spike on hardware **before** production buildout when **any** of these hold:

- It depends on **undocumented Tesla / firmware** behavior ([`tesla-usb-contract.md`](./tesla-usb-contract.md)).
- It depends on **kernel gadget / USB / UDC** behavior under the live car.
- It depends on **BCM43436 / SDIO**, **microSD I/O contention**, or **boot timing**.
- It asserts a **timing/latency budget** no datasheet guarantees.
- Getting it wrong forces a **large rewrite** or risks the **write path**.

If **none** apply — pure app logic, SPA, SQL, parsing against recorded fixtures —
build it normally with unit/integration tests. **No spike needed.** Don't spike
what a fixture can prove on a laptop.

## 3. The spike loop (small, time-boxed, throwaway)

1. **Frame** — one question, a single **pass/fail predicate**, and the *smallest*
   experiment that answers it. Write it down before touching the device.
2. **Build the probe** — a shell script, a tiny throwaway Rust binary, manual
   `configfs`, etc. This is **not production code**; correctness-of-question beats
   code quality. It will be discarded (or kept only as a cheap regression fixture).
3. **Run on the live target** — **only** via the `hardware-test` skill, with its
   rails: dead-man reboot timer, SSH/WiFi/boot protected, backups before mutate,
   idempotent ops. Reconcile a **GPT-5.5 second opinion before any risky live
   step** (`.github/copilot-instructions.md`); recording-critical → explicit
   operator confirmation.
4. **Observe objective signals** — UDC `state=configured`, `diskstats` write
   counters climbing, `dmesg`/SDIO state, measured timings — not vibes.
5. **Decide** — PASS / FAIL / INCONCLUSIVE (§4).
6. **Fold back** — record the outcome **and the proven parameters** in the owning
   spec + the session log immediately; mark the [`SPEC.md` §9](./SPEC.md) unknown
   resolved. Then discard the probe.

**Time-box every spike** (target ≤ a half-day of device time). Overrunning the box
is itself a signal — stop, re-frame, or escalate; do not grind.

## 4. Decision outcomes

| Outcome | Meaning | Action |
|---------|---------|--------|
| **PASS** | The behavior/budget holds | Capture the **proven values** (TX cap Mbps, boot seconds, tolerated dropout window, disk.img size…) as **facts** in the spec; unblock the dependent buildout. |
| **FAIL** | The assumption is wrong | **Do not build on it.** Pivot to the documented alternative, or escalate to the operator for an architecture decision (e.g. a different LUN/partition layout). Cheap now, catastrophic later. |
| **INCONCLUSIVE** | The probe didn't decide it | Add instrumentation / refine the predicate and re-run. **Never** downgrade "inconclusive" to "probably fine." |

## 5. The de-risking backlog (ordered, gated)

The [`SPEC.md` §9](./SPEC.md) unknowns are the initial spike backlog, ordered by
**blast radius** and by **how much downstream work each unblocks**. Each spike is
named for the **risk it retires**, not a phase number. Before any spike runs, the
`hardware-test` rails must be green (target reachable; SSH/WiFi/boot verified;
backups taken).

| Spike | Question → pass predicate | Gates (don't build until PASS) | §9 |
|-------|---------------------------|--------------------------------|----|
| **LUN acceptance** | Car accepts **one image-file LUN, MBR + 2 exFAT partitions**; records to p1 **and** reads a chime/lightshow from p2 | the **entire S1 architecture**, `gadgetd`, `tesla-usb-contract` | #1 |
| **Eject / rebind** | **Clean eject/rebind**: soft-eject is benign (no latch), re-present resumes recording ~2 s; **measure** max mid-write dropout tolerated | eject-handoff, `gadgetd` mutator, **all** Pi-side writes | #2 |
| **Boot time** | **Cold boot-to-gadget-ready < 8–10 s** | boot ordering, `migration` M3 | #6 |
| **Parse stability** | **Raw exFAT/MP4 stability gating while the car writes** — never a false "stable" | `scannerd`, `indexd`, `retentiond` archiving | #3 |
| **SEI / HUD** | **H.264 SEI** present + HUD sync + browser playback (desktop+mobile) | `indexd` trip/event derivation, SPA HUD | #7 |
| **WiFi TX cap** | **BCM43436 TX cap** that avoids the SDIO deadlock + `rmmod/modprobe brcmfmac` recovery | `wifid`, `uploadd` throttle | #4 |
| **microSD contention** | **microSD latency** under simultaneous car-write + Pi index/copy — car writes **never** starved (ionice/IOWeight) | `scannerd`/`retentiond`/`uploadd` concurrency, governor cadence | #5 |
| **disk.img sizing** | **`disk.img` sizing + whole-card budget closure**; fully `fallocate`; reserves hold | `gadgetd` provisioning, [`storage.md` §2](./storage.md) budgets | #8 |

**Order / gating:** `LUN acceptance → {Eject/rebind, Boot time} → {Parse stability,
SEI/HUD} → {WiFi TX cap, microSD contention} → disk.img sizing`. **LUN acceptance
is make-or-break**: if the car won't accept the single-image 2-partition LUN, the
whole S1 architecture is reconsidered before anything else is built. A **FAIL**
anywhere re-orders the roadmap.

## 5.1 Spike status (live results)

> Append-only log of spike outcomes. The newest hardware truth wins (§6).

### LUN acceptance (#1) — **MECHANISM PASS** · car-acceptance **OPEN** (2026-06-08)

Proven on the bench (Pi Zero 2W, kernel `6.12.47+rpt-rpi-v8`, USB-OTG to the
**Windows dev PC** standing in for the car): a kernel `usb_f_mass_storage`
function backed by a **plain `file=disk.img`** enumerates on a real USB host
("Linux File-Stor Gadget"), the host mounts the exFAT, and a **bidirectional
read/write round-trip is sha256-verified** (Pi-written 8 MB read identically by
the host; host-written 4 MB read back identically from the backing image after
unbind). `udc state=configured`; SSH/WiFi/boot unaffected (USB-OTG path is
independent of the WiFi/SSH rails). This **retires the architecture pivot's core
technical risk** — the kernel-gadget + plain-image-file approach works on this
exact Pi/kernel — and **unblocks `gadgetd`** buildout of the bring-up/teardown
mechanism, built to the real MBR+2-exFAT layout.

**Still OPEN — car-only, cannot be proven against a PC host** (keep the gate
open; confirm against a car before declaring §9 #1 resolved):
1. Tesla acceptance of **MBR + 2 exFAT partitions** and reading media from **p2**
   (a Windows host mounts only the first partition of a removable disk, so the
   two-partition read is fundamentally car-only).
2. Tesla **records continuously to p1** over the kernel LUN.
3. **Clean eject/rebind, no latch, never EIO** on Pi crash/reboot (#2) — the
   #1-invariant property; bench crash-consistency is being de-risked separately
   (observe the host's error mode under abrupt Pi reboot mid-write), but final
   car tolerance is car-measured.

## 6. Agile cadence (responding fast)

- **Living backlog.** This table + `SPEC.md` §9 are re-ranked as findings land;
  the newest hardware truth wins.
- **Small reversible increments.** Each spike is independently runnable and
  reversible under the `hardware-test` rails — prefer many small steps over a
  big-bang deploy.
- **Mid-build surprise = a new spike.** When a hardware issue appears while
  building, **stop and spike it** (frame → probe → decide); never push through on
  assumption.
- **Findings are never lost.** Record outcome + proven parameters in the spec and
  the session log the moment a spike resolves.
- **"Safe to build big" is a gate, not a vibe:** the gating spike(s) for a piece
  of work are **PASS with captured parameters**; otherwise that work stays a spike.

## 7. Boundaries

**ALWAYS** spike a risky hardware unknown before its buildout; time-box it; run it
**only** through the `hardware-test` skill with the dead-man/SSH/WiFi/boot/backup
rails; reconcile a GPT-5.5 opinion before risky live steps; fold every finding
back into the specs; treat a FAIL as a cheap win.
**ASK FIRST** before any spike that could affect SSH/WiFi/boot/**recording**, and
before building a long buildout on an unproven assumption.
**NEVER** build a long buildout on an unproven hardware assumption; never run a
probe outside the `hardware-test` rails; never let a spike compromise the car's
write path; never discard a hardware finding without recording it; never promote a
throwaway probe to production without going through the normal standards
([`SPEC.md` §7](./SPEC.md)).
