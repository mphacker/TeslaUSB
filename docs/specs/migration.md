# SPEC — In-place migration & hardening (M1–M5)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: ops · Decision D2: **no reflash,
> no clean SD** — convert the existing device in place, reversibly.
> Execution: **only** via the hardware-test skill (dead-man reboot timer,
> SSH/WiFi/boot protected, backups before mutate, GPT-5.5 review before risky
> live steps).

## 1. Objective

Convert the existing B-1 Raspberry Pi to the reset architecture (S1 + A + O1)
**in place**, with every step reversible and the device's SSH/WiFi/boot kept
alive throughout. Prove the car records reliably on the new kernel LUN **before**
removing any safety nets.

## 2. Stages

### M1 — Back up (verify checksums)
Copy existing clips + configs **off** the Pi; verify checksums. Nothing is
removed until backups are confirmed.

### M2 — Inventory + clean the device
List and **stop** all old B-1 services (teslafat, NBD, the Python/Flask web app,
old watchdogs); remove their systemd units and files; sweep leftover
temp/junk/orphaned files. (This also satisfies the standing "review the device
for leftover/temp content" request.) Capture a before/after inventory.

### M3 — Stand up the kernel image-file LUN
Create `disk.img` in existing free space; lay out MBR + 2 exFAT partitions; bring
the gadget up via the new Rust `gadgetd`. **Prove the car records reliably** (UDC
`state=configured`; `diskstats`/write counters climbing; clean-unplug behavior on
restart) **before** removing the old safety nets. Gate: prototype unknowns #1, #2,
#6 from `SPEC.md` §9 must pass here.

### M4 — Deploy the Rust app + migrate media
Deploy `scannerd`, `indexd`, `webd`, `uploadd`, `retentiond`, `wifid` + the SPA
via **`setup.sh deploy-app`** (the **non-destructive** install mode — it touches
binaries, the SPA bundle, units, and config only, never `disk.img`/partitions/boot,
which M3 already provisioned and proved; see [`setup.md`](./setup.md)). Migrate
existing clips into the new image/archive layout. Verify the UI **end-to-end with
Playwright** (perf + console + screenshot + wiring) per
`.github/copilot-instructions.md`.

### M5 — Harden + soak
Enable **read-only root + overlay/tmpfs**, arm the **hardware watchdog**
(`/dev/watchdog`), apply cgroup `MemoryMax` caps and `gadgetd
OOMScoreAdjust=-1000` with the canonical OOM kill order (`uploadd → wifid → webd →
scannerd → retentiond → indexd → NEVER gadgetd`; see [`SPEC.md` §7](./SPEC.md)).
Soak-test, then decommission the old code paths.

## 3. Safety rules (every step)

- Reversible: keep the backed-up card state so any step can roll back **without
  losing recording**.
- SSH, WiFi, and boot must stay alive at every step (hardware-test rails).
- Backups **before** any mutating action; re-verify after.
- A **GPT-5.5 second opinion** is reconciled **before** any risky live step
  (per `.github/copilot-instructions.md`).
- Never remove a safety net until its replacement is proven on the device.

## 4. Carry-over incident (do not lose)

Recording may still be **down** from a prior session; recovery needs an operator
**VBUS power-cycle**. Verify recovery on the live device **only** via the
hardware-test skill (UDC `state=configured`; write counters incrementing).
Mandatory GPT-5.5 second opinion before any live action.

## 5. Acceptance criteria

- [ ] Backups verified before any removal (M1).
- [ ] All old B-1 software + leftover/temp files removed; inventory captured (M2).
- [ ] Car records reliably on the kernel LUN, clean-unplug on restart, **before**
      safety nets are removed (M3).
- [ ] Full Rust stack + SPA deployed; media migrated; UI verified by Playwright
      (M4).
- [ ] RO-root + overlay, hardware watchdog, memory caps + OOM order in place;
      soak passed (M5).
- [ ] Every step demonstrated reversible; SSH/WiFi/boot never lost.

## 6. Boundaries

**ALWAYS** back up first; keep steps reversible; protect SSH/WiFi/boot; prove the
new write path before removing the old; reconcile a GPT-5.5 opinion before risky
live steps; verify UI with Playwright.
**ASK FIRST** before any irreversible live operation, or before deviating from
the in-place (no-reflash) constraint.
**NEVER** reflash/repartition the live boot card (unless the operator explicitly
chooses to); never remove a safety net before its replacement is proven; never
run a live step without the hardware-test rails.
