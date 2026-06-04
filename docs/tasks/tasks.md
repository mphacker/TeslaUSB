# Task Breakdown — TeslaUSB B-1 reset

> Companion to [`plan.md`](./plan.md). Each task has acceptance criteria,
> verification, dependencies, likely files, and a size. **Sizes:** XS=1 file,
> S=1–2, M=3–5, L=5–8, XL=break further before starting.
> **Device tasks run only via the `hardware-test` skill** (dead-man reboot timer,
> SSH/WiFi/boot protected, backups before mutate). **UI tasks require Playwright**
> verification. **Build tasks gated by a spike must not start until that spike is
> PASS with captured parameters.**

Legend: 🖥️ = touches live hardware · 🎭 = requires Playwright · 🔒 = gated by a spike.

> **Pre-`setup.sh` deployment:** Phases 3–6 verify services on the device before
> the installer exists (7.1). For that window, on-device deploy is the
> **rails-protected manual deploy of the `hardware-test` skill** (sanctioned by
> [`SPEC.md` §5](../specs/SPEC.md)); these are throwaway/validation deploys.
> `setup.sh` (7.1) is the durable product installer and the **only** supported
> end-user/migration path ([`setup.md`](../specs/setup.md)). See [`plan.md` §6](./plan.md).

---

## Phase 0 — Clean slate (no hardware)

### Task 0.1: Create the clean branch carrying the KEEP-list
**Description:** Establish a fresh branch whose working tree contains only the
KEEP-list (decision P1): `docs/`, `.github/`, `rust/` (workspace + `teslausb-core`
only), and root hygiene files. Legacy stays recoverable on `main`/`b1-userspace-rust`.

**Acceptance criteria:**
- [ ] New branch (proposed `b1-clean`) exists; working tree has no `web/`,
      `teslafat/`, `rust/crates/teslafat`, `rust/crates/teslausb-worker`, `config/`
      (nginx/gunicorn), v1 `setup*`/`setup-lib/`/`uninstall*`/`uninstall-lib/`, or
      old `scripts/`.
- [ ] `docs/` (specs + tasks) and `.github/` (skills + copilot-instructions) carried intact.
- [ ] Legacy confirmed retrievable from `main` (`git show main:web/...` works).

**Verification:**
- [ ] In **source/runtime paths only** (`rust/`, `spa/`, build scripts; **exclude
      `docs/` and `.github/skills/`**, which intentionally name the forbidden terms):
      `git ls-files -- rust spa | Select-String 'teslafat|teslausb-worker|nginx|gunicorn'` → empty;
      no `web/` tree present.
- [ ] Branch resolves the open "orphan vs single-commit" question (plan §9) with the operator.

**Dependencies:** None. **Files:** branch/tree only. **Size:** S.

### Task 0.2: Curate the Rust workspace (drop teslafat + worker)
**Description:** Reduce `rust/` to the `teslausb-core` crate. Rewrite the workspace
`Cargo.toml` (remove `teslafat`/`teslausb-worker` members; delete the NBD-era
header comment; fix the lint block that references the deleted
`docs/03-CODE-QUALITY-CHARTER.md` → point at `SPEC.md` §7). **Prune the
teslafat-only / NBD-backing modules** from `teslausb-core` so the clean branch
carries **no legacy runtime code**: the **synthesizer/backing** path
(`fs/exfat/synth.rs`, `fs/backing_tree.rs`, `fs/cluster_layout.rs`,
`fs/cluster_map.rs`, `fs/data_cluster_source.rs`, `fs/exfat/lazy_load.rs`, the
`backend.rs` `BlockBackend`/NBD trait, the `ipc` envelope) is **synth-only and
removed**; the **raw-read/parse** path that `scannerd` reuses
(`fs/exfat/parse.rs`, `boot_sector`, `directory`, `dir_decode`, `geometry`,
`mbr`, the entire `sei/` tree) is **KEPT** ([`SPEC.md` §4 reuse-vs-new](../specs/SPEC.md)).
If a clean reader/synth split is non-trivial, this becomes its own card before 0.2 closes.

**Acceptance criteria:**
- [ ] Workspace members = `crates/teslausb-core` only.
- [ ] No comment/string references a deleted charter/NBD/teslafat doc.
- [ ] Synth/backing/NBD-IPC modules removed; the raw-read + SEI modules retained and compiling.
- [ ] `teslausb-core` compiles and tests pass with no teslafat/worker/synth dependency.

**Verification:**
- [ ] `cargo build --workspace` ✅ · `cargo test -p teslausb-core` ✅
- [ ] `cargo clippy --workspace --all-targets -- -D warnings` ✅ · `cargo fmt --all --check` ✅

**Dependencies:** 0.1. **Files:** `rust/Cargo.toml`, remove two crate dirs. **Size:** S.

### Task 0.3: Prune legacy hygiene (Python tooling, gitignore, pre-commit)
**Description:** Remove Python-era tooling that no longer applies: `.pre-commit-config.yaml`
(ruff/mypy/pytest) reworked or removed; trim `.gitignore` Python/Flask-only entries
to a Rust+SPA-relevant set (keep secret/data-file ignores). Remove stray `etc/sudoers.d`,
`tools/xbuild`, `deploy/` references that belong to the old stack (or migrate the
useful cross-build bits into the new build path as a noted follow-up).

**Acceptance criteria:**
- [ ] No Python linters/hooks referenced anywhere in the branch.
- [ ] `.gitignore` keeps all secret/runtime-data ignores; drops dead Python-cache entries.
- [ ] Any retained tooling (e.g. cross-build) is explicitly justified in the commit body.

**Verification:**
- [ ] `Select-String -Path .pre-commit-config.yaml,.gitignore 'ruff|mypy|pytest|flask|nginx'` → only intentional matches.
- [ ] Pre-commit (if kept) runs clean; otherwise removed cleanly.

**Dependencies:** 0.1. **Files:** `.pre-commit-config.yaml`, `.gitignore`, `etc/`, `tools/`. **Size:** S.

### Task 0.4: Capture the UI parity baseline 🎭
**Description:** Run the **old Flask app** (from `main`/legacy checkout, locally)
and capture a parity baseline with Playwright: full-page screenshots of every
screen at 375px and ≥1280px, the DOM, and a network/perf snapshot. Store under a
session artifact / `docs/tasks/parity-baseline/` (reference only, not runtime).
This is the SPA's visual target (decision P5) and must be done **before** the
device's old web app is decommissioned.

**Acceptance criteria:**
- [ ] Screenshots + DOM + perf snapshot for **every screen in
      [`spa.md` §3](../specs/spa.md)** (16): home/media hub, trip map, event
      player, video overlay HUD, **analytics**, boombox, music, light shows, lock
      chimes, license plates, wraps, cloud archive, storage settings, **storage
      health**, **failed jobs**, **captive portal**.
- [ ] Both viewports (375px + ≥1280px) captured; perf/console snapshots saved.
- [ ] Artifacts stored and referenced from `tasks.md`/the SPA tasks (5.2, 5.3).

**Verification:**
- [ ] Old app served locally; Playwright run completes; artifacts present and legible.

**Dependencies:** 0.1 (legacy retrievable). **Files:** artifacts only. **Size:** M.

### ✅ Checkpoint: Phase 0 — see [`plan.md` §5](./plan.md).

---

## Phase 1 — Device prep (M1–M2) 🖥️

> **Maintenance-window framing (resolves the "don't remove a safety net before its
> replacement is proven" tension):** the dev Pi is treated as a maintenance-window
> device — recording is down/unsure (P2) and it is **not** protecting a live car —
> so M2 (1.3) may decommission the old write path before the new LUN exists. On a
> **real in-car** device this is stricter: keep the old path until M3 proves the new
> one ([`migration.md` §3](../specs/migration.md)). Confirm with the operator that
> the car is disconnected / recording is not required during this window.

### Task 1.1: Establish rails + assess device (+ recover recording) 🖥️
**Description:** Bring up the hardware-test rails and assess the dev Pi: SSH
liveness, dead-man reboot timer, WiFi/boot verification, software inventory, and
**recording state** (UDC `state`, write counters). If recording is down (P2),
perform the operator **VBUS power-cycle** recovery and confirm
([`migration.md` §4](../specs/migration.md)). **GPT-5.5 second opinion before any
live action.**

**Acceptance criteria:**
- [ ] Rails green: target reachable, dead-man armed, SSH/WiFi/boot confirmed.
- [ ] Recording state documented; if recovered, UDC `state=configured` + write counters climbing.
- [ ] Full before-state inventory captured (services, units, mounts, disk usage).

**Verification:**
- [ ] hardware-test skill session log shows rails + signals; GPT-5.5 reconciliation recorded.

**Dependencies:** None (can run parallel to Phase 0). **Files:** session log. **Size:** M.

### Task 1.2: M1 — Back up clips + configs, verify checksums 🖥️
**Description:** Copy existing clips + configs **off** the Pi; verify checksums.
Nothing is removed until backups are confirmed ([`migration.md` M1](../specs/migration.md)).

**Acceptance criteria:**
- [ ] Clips + configs copied off-device; checksums verified.
- [ ] Backup location + manifest recorded.

**Verification:** checksum compare passes; restore spot-check of one file.
**Dependencies:** 1.1. **Files:** session log/backup manifest. **Size:** S.

### Task 1.3: M2 — Inventory + stop + remove old B-1 software 🖥️
**Description:** List and **stop** all old B-1 services (teslafat, NBD, Flask web
app, old watchdogs); remove their systemd units and files; sweep leftover
temp/junk/orphaned files; capture a before/after inventory
([`migration.md` M2](../specs/migration.md)). Idempotent, reversible, rails-protected.

**Acceptance criteria:**
- [ ] All old services stopped + units removed; leftover/temp files swept.
- [ ] Before/after inventory captured; device still reachable (SSH/WiFi/boot intact).
- [ ] On the **dev Pi** the old teslafat/NBD write path is **explicitly not a
      protected safety net** (maintenance window, P2) — removing it is intended.
      The "never remove a safety net before its replacement is proven" rule
      ([`migration.md` §3](../specs/migration.md)) is **deferred to the real in-car
      migration**, where M2 keeps the old path until M3 proves the new LUN.

**Verification:** `systemctl` shows old units gone; `df`/inventory diff captured; device reboots clean and stays reachable.
**Dependencies:** 1.2; **0.4 must be done first** (parity captured before web app removed); operator confirmation of the maintenance window (car disconnected / recording not required). **Size:** M.

### ✅ Checkpoint: Phase 1 (device clean) — see [`plan.md` §5](./plan.md).

---

## Phase 2 — Hardware-first spikes (the gates) 🖥️🔒

> Each spike = smallest throwaway probe answering one pass/fail predicate, via the
> hardware-test skill, **GPT-5.5 reconciled before risky steps**. Outcome
> PASS/FAIL/INCONCLUSIVE; **fold proven parameters back into the owning spec**.
> Order + gating per [`hardware-first-development.md` §5](../specs/hardware-first-development.md).
> A FAIL re-orders the roadmap.

### Task 2.1: Spike — LUN acceptance (#1) — MAKE OR BREAK 🖥️🔒
**Predicate:** Car accepts **one image-file LUN, MBR + 2 exFAT partitions**;
records to p1 **and** reads a chime/lightshow from p2.
**Gates:** the entire S1 architecture, `gadgetd`, `tesla-usb-contract`.
**Acceptance:** PASS with captured params (incl. whether a top-level `TeslaCam/`
marker is required, [`setup.md` §8](../specs/setup.md)); on FAIL, **escalate to
operator for an architecture decision before any build.**
**Verification:** UDC `state=configured`; write counters on p1; chime/lightshow
read by car from p2. **Dependencies:** Phase 1. **Size:** M (spike).

### Task 2.2: Spike — Eject / rebind (#2) 🖥️🔒
**Predicate:** Soft-eject is benign (no latch); re-present resumes recording ~2 s;
**measure** max tolerated mid-write dropout window.
**Gates:** eject-handoff, `gadgetd` mutator, all Pi-side writes.
**Spike safety (mandatory — this probe can latch the port):** define the
**sacrificial scope** up front, a **max-attempts** bound, explicit **abort/stop
criteria**, and the **VBUS power-cycle recovery** procedure before starting; run
recording-affecting steps only behind operator confirmation + a known-good restore
([`hardware-first-development.md` §1, §7](../specs/hardware-first-development.md)).
**Verification:** repeated eject/rebind cycles; no latch; dropout tolerance recorded.
**Dependencies:** 2.1 PASS. **Size:** M (spike). *(Wave 2.)*

### Task 2.3: Spike — Boot time (#6) 🖥️🔒
**Predicate:** Cold **boot-to-gadget-ready < 8–10 s**.
**Gates:** boot ordering, migration M3.
**Verification:** measured cold-boot timing to UDC ready, repeated.
**Dependencies:** 2.1 PASS. **Size:** S (spike). *(Wave 2.)*

### ✅ Checkpoint: foundation gates (2.1–2.3) before `gadgetd` buildout.

### Task 2.4: Spike — Parse stability (#3) 🖥️🔒
**Predicate:** Raw exFAT/MP4 stability gating **while the car writes** — never a
false "stable". **Gates:** `scannerd`, `indexd`, `retentiond` archiving.
**Verification:** concurrent car-write + raw scan; zero false-stable over the run.
**Dependencies:** 2.2, 2.3 PASS (wave 3). **Size:** M (spike).

### Task 2.5: Spike — SEI / HUD (#7) 🖥️🔒
**Predicate:** **H.264 SEI** (`user_data_unregistered`, `payload_type=5`) present
and matches `teslausb-core/src/sei` across the target build/HW range; HUD sync +
browser playback on desktop + mobile. **Gates:** `indexd` derivation, SPA HUD.
**Verification:** SEI decoded from real clips; HUD overlay tracks video; plays in
target browsers. **Dependencies:** 2.2, 2.3 PASS (wave 3). **Size:** M (spike).

### Task 2.6: Spike — WiFi TX cap (#4) 🖥️🔒
**Predicate:** BCM43436 **TX cap** that avoids the SDIO deadlock; `rmmod/modprobe
brcmfmac` recovery reliable. **Gates:** `wifid`, `uploadd` throttle.
**Verification:** sustained TX at the cap without deadlock; recovery succeeds repeatedly.
**Dependencies:** 2.4, 2.5 PASS (wave 4). **Size:** M (spike).

### Task 2.7: Spike — microSD contention (#5) 🖥️🔒
**Predicate:** microSD latency under **simultaneous car-write + Pi index/copy** —
car writes **never** starved (ionice/IOWeight). **Gates:** scanner/retention/upload
concurrency, governor cadence, SD-swap opt-in. **Verification:** car write
throughput holds under Pi I/O load; tuning params recorded.
**Dependencies:** 2.4, 2.5 PASS (wave 4). **Size:** M (spike).

### Task 2.8: Spike — disk.img sizing + budget closure (#8) 🖥️🔒
**Predicate:** Pick absolute `disk.img` size + p1/p2 split; fully `fallocate`;
verify `card_total ≥ disk.img + OS + archive budget + reserves` with healthy
headroom ([`storage.md` §2](../specs/storage.md)). **Gates:** `gadgetd`
provisioning. **Verification:** budget math closes on the real card; reserves hold.
**Dependencies:** 2.6, 2.7 PASS (wave 5 — **last spike**, per hardware-first §5). **Size:** S (spike).

### ✅ Checkpoint: Phase 2 (all gates) — see [`plan.md` §5](./plan.md).

---

## Phase 3 — `gadgetd` (CRITICAL) 🖥️🔒

> Gated by 2.1, 2.2, 2.3, 2.8. This is migration **M3**. Spec:
> [`gadgetd.md`](../specs/gadgetd.md). Provisioning comes **before** LUN bring-up
> (you cannot bind a LUN to an image that doesn't exist yet). Break further before
> starting if any slice exceeds one session.
>
> **Pre-setup deploy guardrail (applies to all on-device runs in Phases 3–6,
> before `setup.sh` exists):** any service deployed by hand to the Pi for testing
> **must** still run under a canonical systemd unit carrying its production
> `MemoryMax`/`OOMScoreAdjust` and `LoadCredential` secret handling — never as a
> bare `cargo run` or a loose binary. Running pre-setup services without these
> risks an OOM that violates the #1 invariant (`gadgetd` killed) or leaks secrets.
> Maintain a temporary `deploy/units/` set from Phase 3 onward and converge it into
> `setup.sh` at 7.1 ([`SPEC.md` §5 memory order](../specs/SPEC.md),
> [`setup.md`](../specs/setup.md)).

### Task 3.1: `gadgetd` image provisioning (create the verified `disk.img`) 🖥️🔒
**Description:** First-run provisioning owned by `gadgetd`: fixed-size `fallocate`
to the **proven** size/split (from 2.8), MBR, p1 TeslaCam + p2 media exFAT, volume
labels, and the **TeslaCam bootstrap rule** (seed only an empty top-level
`TeslaCam/` **iff** the LUN spike proved it's needed; never the car's subfolders).
Destructive bootstrap is reachable only via explicit authorization
(`setup.sh --bootstrap-image` later, or the hardware-test rails now). **Gated by
2.8, 2.1.**
**Acceptance:** produces the proven layout/size; idempotent; **refuses to clobber
an existing image** without explicit authorization.
**Verification:** image inspected (partition table, labels, fully allocated); the
car later accepts it (3.2). **Dependencies:** 2.8, 2.1. **Files:**
`rust/crates/gadgetd/*`. **Size:** M.

### Task 3.2: `gadgetd` LUN bring-up + status/heartbeat 🖥️🔒
**Description:** Configure/maintain `usb_f_mass_storage` via configfs/libcomposite
**from the provisioned `disk.img`** (3.1); systemd unit with
`OOMScoreAdjust=-1000`; bring the LUN up at boot within the proven boot budget;
prove the car records. **Expose a write-heartbeat / USB-idle status** for
consumers (the `wifid` reboot gate and the `retentiond` handoff quiet-period gate
both depend on this — [`wifid.md` §4](../specs/wifid.md), [`retentiond.md` §5](../specs/retentiond.md)).
**Gated by 2.1, 2.3.**
**Acceptance:** car records to p1; `gadgetd` is the only write-path owner; status
API reports write-activity/idle.
**Verification (full invariant set per [`gadgetd.md` §6](../specs/gadgetd.md)):**
on-device — UDC configured, write counters climbing; **a `gadgetd` restart, a Pi
reboot, AND an OOM-kill of every other service each present to the car as a clean
unplug/replug, never EIO, recording resuming ~2 s**; **car reads a
chime/lightshow from p2**; status API reflects live writes. **Dependencies:** 3.1,
2.1, 2.3. **Files:** `rust/crates/gadgetd/*`, `deploy/` unit. **Size:** L.

### Task 3.3: `gadgetd` eject-handoff mutator 🖥️🔒
**Description:** Car-state-aware eject-handoff: soft-eject → mount RW locally →
mutate → fsync → re-present; **refuse during an active Sentry/honk save** and until
the LUN is write-idle for the defined quiet period (using 3.2's status); **re-validate
the target against the verified manifest after remount** before deleting; never
mount the Tesla FS RW while the car owns it. **Gated by 2.2.**
**Acceptance:** handoff completes within the proven dropout tolerance; refuses
mid-save/near-save; manifest re-validated; never leaves the LUN in EIO.
**Verification:** **invariant tests** — simulated active save ⇒ refuse; handoff
under load ⇒ clean re-present; manifest-changed ⇒ refuse; on-device cycle.
**On real-car calibration (mandatory):** the heartbeat/quiet-period thresholds and
the "active save" detection must be **calibrated and confirmed against a real
Sentry/honk save on the live car** — a simulated save alone does not prove the
refusal gate ([`gadgetd.md` §6](../specs/gadgetd.md), [`retentiond.md` §3.2](../specs/retentiond.md)).
**Dependencies:** 3.2, 2.2. **Files:** `rust/crates/gadgetd/*`. **Size:** L.

### ✅ Checkpoint: Phase 3 / migration M3 — see [`plan.md` §5](./plan.md).

---

## Phase 4 — Read/index pipeline 🔒

### Task 4.1: `scannerd` — raw exFAT/MP4/SEI parser 🔒
**Description:** Read-only raw parser (`pread`, never mount) reusing
`teslausb-core` parse + SEI; emit stable-clip + SEI records with the proven
stability gating; capped, best-effort keyframe thumbnails (no separate service).
**Gated by 2.4, 2.5.** Spec: [`scannerd.md`](../specs/scannerd.md). (Note: the
teslafat-only synth modules were already pruned from `teslausb-core` in 0.2;
`scannerd` reuses only the retained raw-read + SEI path.)
**Acceptance:** emits records only for clips proven stable; never mounts; memory-bounded.
**Verification:** `cargo test -p scannerd` with byte-level fixtures; on-device run
against live recordings shows no false-stable. **Dependencies:** 0.2, 2.4, 2.5.
**Files:** `rust/crates/scannerd/*`. **Size:** L.

### Task 4.2: `indexd` — derive trips/events/clips into SQLite (WAL) 🔒
**Description:** Consume scanner output; derive trips/events/clips into SQLite WAL
on Pi-side ext4; **sole SQLite writer**. Spec: [`indexd.md`](../specs/indexd.md).
**Owns the cross-service schema** every later phase depends on — define and
**version it here, before 5.1/6.1/6.3**: `clips`/`angles`/`trips`/`events`,
`archive_items` (incl. **`delete_state`** LIVE/DELETE_CLAIMED/DELETING/DELETED/
QUARANTINED, **durability**, **pin**, value signals, archived-at/grace), and the
**`leases`** table (kind upload/playback, holder, **TTL/`expires_at`**) so the
webd playback lease (5.1b), uploadd upload lease (6.3), and retentiond governor
(6.1e) all bind to one consistent contract ([`indexd.md` §2,§4](../specs/indexd.md),
[`storage.md` §4.1/§5](../specs/storage.md)). Also performs WAL
checkpoint/truncate on `retentiond`'s request.
**Acceptance criteria:**
- [ ] Schema **versioned**; `leases` + `archive_items.delete_state` present and documented as the shared contract.
- [ ] Idempotent re-index; WAL on ext4 (never the Tesla volume); is the only writer.
**Verification:** `cargo test -p indexd`; rebuild-from-scratch reproduces the same
catalog; concurrent-reader safety. **Dependencies:** 4.1. **Files:**
`rust/crates/indexd/*`, schema. **Size:** L.

### ✅ Checkpoint: Phase 4 — clips/trips/events indexed from live recordings.

---

## Phase 5 — Web + parity SPA 🎭

> `webd` API contract is fixed **first** (contract-first), then SPA screens fan out.
> Specs: [`webd.md`](../specs/webd.md), [`spa.md`](../specs/spa.md). Every screen
> task is a vertical slice verified by Playwright against the Phase 0 baseline.

### Task 5.1: `webd` — axum REST + SSE + static host + handoff
**Description:** axum REST + SSE API over the `indexd` SQLite catalog; serve the
hashed SPA bundle; drive `gadgetd` eject-handoff mutations. Per
[`webd.md`](../specs/webd.md) this is **larger than one session — execute as
slices**:
- **5.1a Read API** — overview, days/trips/events, clip lists/metadata, analytics,
  settings (read-only from SQLite).
- **5.1b Streaming/export + leases** — HTTP **range-request** video streaming
  (`_range.py` ref), zip/download **export** (`_zip.py` ref), and the
  **playback lease (TTL + heartbeat)** so `retentiond`'s governor can't evict a
  file mid-read ([`webd.md` §2.3](../specs/webd.md), [`storage.md` §4.1/§5](../specs/storage.md)).
- **5.1c Mutations** — delete clip + install/remove media; **input validation**
  (path traversal, file-type, size) **before** the `gadgetd` handoff; progress via SSE.
- **5.1d Status/jobs/health** — index/handoff/upload progress + **jobs SSE**;
  `GET /api/storage` + `/api/storage/health` (governor tier, per-FS free
  bytes+inodes, pinned/leased/reclaimable) + `GET /api/system/health`.
- **5.1e Captive portal** — `GET /portal` AP-mode onboarding entry ([`wifid.md` §5](../specs/wifid.md)).
- Cross-cutting: bind LAN/AP only, preserve the **no-app-login trusted-LAN** model
  (do not add/remove auth), secrets via `LoadCredential` (never in the bundle/logs/Tesla volume).
**Acceptance:** documented, versioned API; mutations go through the handoff and are
validated; **leases honored** by the governor; secrets `0600` root-owned.
**Verification:** `cargo test -p webd` (range logic, handoff/forward contract,
lease lifecycle); mutation refused during simulated save. **Dependencies:** 4.2, 3.3. **Files:** `rust/crates/webd/*`. **Size:** XL — **do
not start as one task; execute slices 5.1a–e as first-class cards.**

### Task 5.2: SPA scaffold + API client + first parity screen (media hub) 🎭
**Description:** Pick the small framework (Preact/Svelte/Solid); scaffold the hashed
static bundle; API client; build the **media hub** to parity. **Acceptance:** bundle
served by `webd`; media hub matches the baseline. **Verification:** Playwright UAT —
TTFB/DCL/FCP/interactive<2s (on-device profile), zero console/pageerror, 375px +
≥1280px screenshots vs baseline, **wiring proof** the changed JS loaded.
**Dependencies:** 5.1, 0.4. **Files:** `spa/*`. **Size:** L.

### Task 5.3: Remaining parity screens (one vertical slice each) 🎭
**Description:** Reproduce **every remaining screen in [`spa.md` §3](../specs/spa.md)**
to parity, **one task per screen** when executed: trip map (Leaflet +
MarkerCluster, event bubbles, clustering, speed-unit toggle), event player + the
**client-side telemetry HUD** (video overlay, Canvas over native `<video>`,
`dashcam-mp4` approach), **analytics** (Chart.js), boombox, music, light shows,
lock chimes (+scheduler), license plates, wraps, cloud archive,
storage settings, **storage health**, **failed jobs**, **captive portal**
(first-run WiFi onboarding, served over AP via `webd /portal` + `wifid`).
**Acceptance (per screen):** functional + visual parity vs. the 0.4 baseline;
Playwright UAT green. **Verification:** durable Playwright spec per screen with
perf/console/screenshot/wiring assertions. **Dependencies:** 5.2 + the relevant
`webd` slice (5.1a–e); captive portal also needs 6.2 (`wifid` AP). **Files:**
`spa/*`. **Size:** M each (break out per screen at execution).

> 5.3 dependency note: the **captive portal** screen needs **6.2** (`wifid` AP +
> `webd /portal`), not 6.3.

### ✅ Checkpoint: Phase 5 (parity) — see [`plan.md` §5](./plan.md).

---

## Phase 6 — Storage governor, uploads, wifi 🔒

### Task 6.1: `retentiond` + storage governor 🔒
**Description:** Archiving + retention + the continuous space governor. Per
[`retentiond.md`](../specs/retentiond.md) + [`storage.md`](../specs/storage.md)
this is **far larger than one task — execute as slices** (each its own card):
- **6.1a Per-folder archiving + manifest verification** — Saved/Sentry copy+verify
  against a *stable directory manifest* before any car-side delete; variable camera set.
- **6.1b RecentClips** — empirical rotation-window estimate + "keeping up?" /
  "unobserved-gap" health; bounded rolling mirror, oldest/event-near first, grace
  window + pins; **never** car-side delete RecentClips.
- **6.1c TeslaTrackMode** — archive+verify like Sentry, priority between Sentry and Recent.
- **6.1d Space governor** — reserve tiers (OS/`gadgetd`/SQLite **sacrosanct**),
  value-scoring eviction least-valuable-safe first, hysteresis, free-space/inode
  watch on **both** filesystems, reacts independently of the copy pipeline.
- **6.1e Crash-safe deletion + single-deleter + leases** — `retentiond` is the
  **sole** deleter of Pi-side archive files; honors **playback/upload leases**
  (5.1b/6.3) and grace/pins; crash-safe protocol; car-side deletes via the 3.3
  handoff (idle/quiet + manifest re-validate); **fail-closed** on ext4 exhaustion
  with no durable candidate (never delete undurable Saved/Sentry).
- **6.1f Storage status/health API** to `webd` (governor tier, per-FS free,
  archive breakdown, pinned/leased/reclaimable, last eviction).
**Gated by 2.7.** **Governor-defaults calibration gate (mandatory):** the tier
thresholds, cadence, and eviction/delete behavior must **not ship on guessed
defaults** — they are gated on the hardware measurements
[`storage.md` §7](../specs/storage.md) enumerates (`statfs` cost, recursive-delete
latency, SD `fsync` latency, WAL growth/checkpoint under load, rclone staging,
deleted-open-file behavior, governor reaction under a synthetic Sentry flood, UI
responsiveness during Critical/Emergency). Capture these (extend the 2.7 spike or
add a dedicated governor-calibration spike) **before 6.1d locks its defaults**.
**Acceptance:** §3.5 backpressure order honored (car writes
always win); reserves never starved; undurable user footage never auto-evicted.
**Verification:** `cargo test -p retentiond` (per-folder policy over synthetic
timelines, eviction ordering, reserve math, crash-safety, delete-vs-lease races,
fail-closed); on-device soak under fill pressure. **Dependencies:** 4.1, 4.2, 3.3,
2.7. **Files:** `rust/crates/retentiond/*`. **Size:** XL — **execute slices 6.1a–f
as first-class cards; do not attempt as one task.**

### Task 6.2: `wifid` — STA/AP state machine + SDIO watchdog 🔒
**Description:** STA/AP state machine (never both at once; WPA2 AP only); **TX rate
cap** (proven value, **coordinated with `uploadd`**); SDIO chip-reset watchdog
(`rmmod/modprobe brcmfmac`, **not** a Pi reboot); reboot path **gated on
USB-idle** via `gadgetd`'s write-heartbeat (3.2); WPA2 AP onboarding (`hostapd` +
`dnsmasq`) feeding the captive portal. **Gated by 2.6.** Spec:
[`wifid.md`](../specs/wifid.md). **Acceptance:** recovers the SDIO bus without
reboot; **never reboots while the car is writing** (verified against the gadgetd
heartbeat); AP onboarding uses WPA2 only. **Verification:** `cargo test -p wifid`
(STA↔AP mutual exclusion, throttle, recovery chooses chip-reset, reboot gated on
USB-idle); on-device deadlock-recovery + AP join. **Dependencies:** 2.6, 3.2.
**Files:** `rust/crates/wifid/*`. **Size:** L.

### Task 6.3: `uploadd` — durable resumable throttled cloud upload 🔒
**Description:** Durable, resumable, prioritized, **throttled** upload from the
Pi-side archive. **Decide the uploader backend first** — rclone (provider breadth)
vs. a small Rust uploader (footprint), left open by
[`uploadd.md` §2.2](../specs/uploadd.md). Respects the `wifid` TX cap (shared
throttle contract); holds an **upload lease** while transferring; marks items
`UPLOADED_VERIFIED` (durability signal for `retentiond`); **never deletes**
Pi-side files (single-deleter = `retentiond`). **Gated by 2.6.**
**Acceptance:** resumes across restarts without duplication; respects the proven
TX cap; sheds first under OOM; sources only from the archive. **Verification:**
`cargo test -p uploadd` (queue resume/idempotency, integrity, throttle vs. mocked
`wifid`); interrupted upload resumes on-device. **Dependencies:** 6.1, 6.2, 2.6.
**Files:** `rust/crates/uploadd/*`. **Size:** M.

### ✅ Checkpoint: Phase 6 — card never fills; uploads throttled; wifi stable.

---

## Phase 7 — Installer + migration + hardening 🖥️🎭

### Task 7.1: Build `setup.sh` + `setup-lib/` (new arch)
**Description:** Implement the installer per [`setup.md`](../specs/setup.md): thin
orchestrator sourcing numbered idempotent step files (preflight → packages →
users → data-roots → config → binaries → spa → units → [image+gadget+boot via
`gadgetd`] → network → memory → hardening → activate); modes install/deploy-app/
update/repair/rollback; convergent idempotency; `.b1-backup` sidecars; **no
build-on-Pi**; **NOT** nginx/python/nbd. Plus `uninstall.sh` safe-by-default.
**Acceptance:** `--dry-run` mutates nothing; `deploy-app` non-destructive; secrets
via `LoadCredential`. **Verification:** dry-run on a clean supported Pi prints every
action; shellcheck clean. **Dependencies:** Phases 3–6 (services exist). **Files:**
`setup.sh`, `setup-lib/*`, `uninstall.sh`. **Size:** XL — **execute as numbered
step-file slices; do not attempt as one task.**

### Task 7.2: Release/artifact pipeline + manifest
**Description:** Host cross-compile aarch64 release binaries + hashed SPA bundle;
assemble a release tarball + **manifest** (version, git commit, triple, per-binary
sha256, SPA hash, unit-set version, config-schema version). **Acceptance:** `setup.sh`
verifies every hash against the manifest and refuses mismatch unless
`--allow-unverified`. **Verification:** build artifacts on host; manifest verify
passes; tampered artifact refused. **Dependencies:** Phases 3–6. **Files:** CI/build
scripts, `deploy/`. **Size:** M.

### Task 7.3: Migration M4 — deploy full stack on the dev Pi 🖥️🎭
**Description:** Run `setup.sh deploy-app` on the dev Pi to deploy all 7 services +
SPA to the **Pi-side ext4 runtime paths** (binaries, units, SPA bundle) — the
`gadgetd`-provisioned `disk.img`/LUN from M3 is **not** written into by app
deploy; only the car and `gadgetd` touch the LUN. Migrate existing clips into the
new archive layout; verify the UI end-to-end with Playwright.
([`migration.md` M4](../specs/migration.md)). **Acceptance:** all services healthy;
`webd` reachable; media migrated; LUN untouched by deploy; Playwright suite green
on-device. **Verification:** hardware-test session; Playwright run against
`cybertruckusb.local`. **Dependencies:** Phases 3–6 (all services exist), 7.1, 7.2.
**Size:** M.

### Task 7.4: Migration M5 — harden + soak (execute as slices) 🖥️
**Description:** Final hardening + soak per [`migration.md` M5](../specs/migration.md).
**Larger than one task — execute as ordered slices, each its own card and each
proven reversible without losing SSH/WiFi/boot:**
- **7.4a RO-root + overlay/tmpfs** — make the root read-only with a writable
  overlay/tmpfs for runtime state; prove a clean reboot and rollback.
- **7.4b Hardware watchdog** — arm `/dev/watchdog`; prove it recovers a hung
  system and never fires during normal operation.
- **7.4c Memory governance** — apply cgroup `MemoryMax` per service +
  `gadgetd OOMScoreAdjust=-1000` with the canonical OOM order
  (uploadd→wifid→webd→scannerd→retentiond→indexd→never gadgetd); prove the order
  under induced pressure.
- **7.4d Soak** — extended on-device soak under realistic record+index+upload+fill
  load; capture the log.
- **7.4e Decommission old paths** — remove the last legacy code paths only after
  the soak passes.
**Acceptance:** hardening active; soak passes; SSH/WiFi/boot never lost; every step
demonstrated reversible. **Verification:** on-device soak log; watchdog/RO-root
verified; reboot-recovery clean; OOM order demonstrated. **Dependencies:** 7.3.
**Size:** L (slices 7.4a–e).

### Task 7.5: Installer-mode validation (fresh / update / rollback / uninstall) 🖥️
**Description:** Prove the installer modes [`setup.md` §12](../specs/setup.md)
acceptance demands but that 7.1/7.3 don't exercise: a **fresh
`install --bootstrap-image`** (on a **spare card / clean Pi** — destructive, never
the live dev Pi without the rails), `update` convergence (no destruction of
`disk.img`/config/secrets/archive/index), `repair`, `rollback` to the previous
release, and **`uninstall` refusal while the gadget is bound** + safe-default keeps
the LUN alive. Can run in parallel after 7.2.
**Acceptance:** every mode behaves per `setup.md` §3/§12; manifest mismatch refused;
secrets `0600` via `LoadCredential`; SSH/WiFi/boot survive on supported HW.
**Verification:** fresh install brings 7 services healthy + car records; `update`
preserves data; tampered artifact refused; `uninstall` refuses while bound.
**Dependencies:** 7.2 (artifacts), 7.1. **Size:** M.

### ✅ Checkpoint: Phase 7 (complete) — see [`plan.md` §5](./plan.md).

---

## Dependency summary

```
0.1 → 0.2, 0.3, 0.4
1.1 → 1.2 → 1.3   (1.3 also needs 0.4 + operator maintenance-window OK)
1.3 → 2.1 (make-or-break)
2.1 → {2.2, 2.3} → {2.4, 2.5} → {2.6, 2.7} → 2.8      (hardware-first §5 wave order)
{2.8, 2.1} → 3.1 → 3.2 (+2.1,2.3) → 3.3 (+2.2)         (provision before LUN bring-up)
{2.4, 2.5} + 0.2 → 4.1 → 4.2
4.2 (+3.3) → 5.1(a–e) → 5.2 → 5.3      (5.3 captive portal also needs 6.2)
{4.1, 4.2, 3.3} + 2.7 → 6.1(a–f)
{2.6, 3.2} → 6.2 (wifid)              (reboot gate needs gadgetd heartbeat 3.2)
{6.1, 6.2, 2.6} → 6.3 (uploadd)       (shared TX-cap throttle with wifid)
Phases 3–6 → 7.1, 7.2 → 7.3 → 7.4;  7.2 → 7.5 (installer-mode validation, spare card)
```

> Cross-cutting **lease** contract: `webd` holds playback leases (5.1b), `uploadd`
> holds upload leases (6.3); `retentiond`'s governor (6.1e) must honor both before
> evicting — keep the lease store/shape consistent across these three.

