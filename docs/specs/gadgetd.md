# SPEC — `gadgetd` (CRITICAL: the invariant guardian)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: **CRITICAL** · Language: Rust
> This service is the single most important component. It owns the car-facing
> write path. The #1 invariant (`SPEC.md` §2) is its only real requirement;
> everything else is secondary.

## 1. Objective

Present a Tesla-compatible USB mass-storage drive to the car using the **kernel**
`usb_f_mass_storage` function (configfs/libcomposite), backed by an **image
file** on the Pi's ext4 data area, such that **no userspace code is ever in the
car's write path**. Own the disk image and its partition layout, bring the gadget
up early at boot, and perform safe **eject-handoff** mutations on behalf of other
services.

## 2. Responsibilities

1. **Provision the backing image** (once / on first run): create `disk.img` of
   the configured size in the ext4 data area; lay out **MBR + 2 partitions**
   (p1 TeslaCam exFAT, p2 media exFAT); format both exFAT.
2. **Bring up the gadget** via configfs/libcomposite: one `usb_f_mass_storage`
   function with the image file as the LUN backing; bind to the UDC. Target
   **boot-to-gadget-ready < 8–10 s** (bring the gadget up before mounting heavier
   `/data` consumers if needed).
3. **Stay alive and minimal.** Single critical service: `OOMScoreAdjust=-1000`,
   tiny `MemoryMax`, no allocation in steady state. It must survive while
   everything else is killed.
4. **Eject-handoff mutator (the only write authority for Pi-side changes):**
   expose a local IPC for `webd`/`retentiond` to request a mutation. On request:
   verify the car is not in an active Sentry/honk save → **soft-eject** the LUN
   (forced_eject / UDC handling) → mount the target partition RW locally → apply
   the mutation (delete clip / install chime / lightshow / etc.) → `fsync` →
   unmount → **re-present** the LUN. Window target **~5 s**. The car tolerates a
   brief USB disappearance (it buffers recent recording in RAM and treats a clean
   eject/replug as benign), but that tolerance is **seconds-scale and not
   published** — it is **prototype-unknown #2** (`SPEC.md` §9), to be **measured**,
   not assumed. Do **not** conflate it with the `RecentClips` rotation window
   (a 1–24 h on-disk vehicle setting, [`tesla-usb-contract.md` §5](./tesla-usb-contract.md)).
5. **Report state** (read-only) for the UI: gadget bound/unbound, UDC state,
   handoff in progress, last handoff result, write-activity heartbeat.

## 3. Non-responsibilities (explicit)

- Does **not** parse video, index, or read the filesystem for content (that is
  `scannerd`). It only mounts a partition RW transiently during a handoff.
- Does **not** decide *what* to delete/install — callers pass a validated
  operation; `gadgetd` only executes it safely.
- Does **not** manage WiFi, cloud, retention policy, or the web UI.

## 4. Interfaces

- **To the kernel:** configfs paths under `/sys/kernel/config/usb_gadget/...`;
  the LUN `file=`, `removable`, `ro`, `nofua`/`cdrom` flags as required for Tesla
  acceptance; UDC bind/unbind.
- **Local IPC (handoff API):** a Unix domain socket (or equivalent) with a
  minimal, typed request/response:
  - `request_mutation(partition, op, payload) -> handoff_id`
  - `handoff_status(handoff_id) -> {queued|ejecting|mounted|applying|repesenting|done|refused|failed, detail}`
  - `gadget_status() -> {udc_state, bound, handoff_active, last_result, write_heartbeat}`
  - Mutations are **serialized** (never two concurrent handoffs).
- **Refusal contract:** if the car appears to be mid-save, or the write heartbeat
  is active beyond a safety threshold, the request is **refused** (not queued
  indefinitely) so callers can retry later. Never force-eject during a save.

## 5. Disk image layout

```
disk.img
 ├─ MBR
 ├─ p1: TeslaCam  (exFAT)   # car writes dashcam/Sentry here
 └─ p2: media     (exFAT)   # car reads chimes/lightshow/boombox/music/etc. here
```

Both partitions are on the **same physical (emulated) device** — required: the
car reads media features only from a partition of the device it records to.

**Sizing (decided at provisioning, measured on HW — `SPEC.md` §6.1/§9 #8):** the
image is **fully `fallocate`d** to a fixed size so car writes never depend on
ext4 free space. **p1 (dashcam)** takes the large majority; **p2 (media)** is
small (chimes/boombox/lightshow/music are MB-scale). The absolute size and split
are chosen per card capacity so the whole-card budget closes
(`card_total ≥ disk.img + OS + archive budget + reserves`, [`storage.md` §2](./storage.md))
— **not** hard-coded in this spec.

## 6. Acceptance criteria

- [ ] Car records continuously to p1 via the kernel LUN; `diskstats` write
      counters climb; `gadgetd` uses no measurable CPU/alloc in steady state.
- [ ] A `gadgetd` restart, a Pi reboot, and an OOM-kill of every other service
      each present to the car as a **clean unplug/replug**, never EIO; recording
      resumes within ~2 s of re-present. **The car never latches.**
- [ ] Car reads chimes/lightshow/boombox/music from p2 successfully.
- [ ] Eject-handoff: a clip delete / chime install completes end-to-end in ~5 s
      and the car resumes recording afterward without a latch.
- [ ] Handoff is **refused** when a save is active; never mutates during a save.
- [ ] Cold boot-to-gadget-ready < 8–10 s.
- [ ] Two mutation requests never run concurrently.

## 7. Testing

- Unit tests for the configfs builder (paths/flags), the partition/format
  routine (against a temp image + loop on CI Linux), and the handoff state
  machine (including the refusal path and the "save active" guard).
- A fault-injection harness: kill/restart `gadgetd` mid-write to a loop-mounted
  consumer and assert clean-unplug semantics (no I/O error surfaced).
- Hardware acceptance (prototype-first unknowns #1, #2, #6 in `SPEC.md` §9) via
  the hardware-test skill, with a GPT-5.5 second opinion before live runs.

## 8. Boundaries

**ALWAYS** keep zero userspace in the write path; serialize handoffs; refuse
rather than force during saves; bring the gadget up first at boot; stay the only
critical, OOM-protected service.
**ASK FIRST** before any change that adds a code path, syscall, or latency
between the car and the image file.
**NEVER** put the LUN on dm-thin/CoW; never take an unbounded snapshot under the
live LUN; never mount the Tesla FS RW while the car owns it; never reboot the Pi
to "fix" the gadget while the car is writing.

## 9. Lock-chime car pickup — PROVEN working behavior (do NOT regress)

> **Status: HW-PROVEN & operator-confirmed on B-1 (`cybertruckusb.local`),
> 2026-06-24.** Setting a lock chime in the SPA reaches the parked car and the
> car plays the new chime on the next lock, with TeslaCam recording intact.
> Evidence: session `files/hw-results.md` (Option A manual reenum
> EngineRev→MarioFart; i2 auto-reenum EngineRev then PortalSentryMode, each
> `reason=chime_apply`, lun.0 untouched; operator heard the new chime).

### 9.1 The mechanism (what works, end to end)

1. SPA **"Set Active"** → `POST /api/chimes/library/{name}/activate` (webd;
   alias `/api/chime-scheduler/library/{filename}/activate`). webd installs the
   chosen file as **`LockChime.wav` on p2 (media)** via the proven gadgetd
   eject-handoff write path. The **scheduler/enforcement** activation
   (`install_library_chime_as_active`) reuses this *same* write path, so this all
   holds for scheduled/random chime changes too.
2. After a **successful** `InstallFile(LockChime.wav)` on **p2**, gadgetd writes a
   durable pending marker `chime-reenum.json` (sha256 token of the installed
   bytes) at `<queue_dir>/chime-reenum.json` (on device
   `/data/teslausb/gadgetd/chime-reenum.json`), persisted **before** the staged
   blob is reclaimed so it survives power loss.
3. gadgetd then **auto-fires a full USB re-enumeration** (`reenum::reenumerate`,
   `reason="chime_apply"`) — soft-connect **disconnect → hold `REENUM_HOLD`
   (700 ms) → connect**. Journal signature (on `gadgetd-control.service`):
   `gadgetd reenumerate: reason=chime_apply`. `dmesg` shows
   `bound driver configfs-gadget.teslausb` → `new device is high-speed` →
   `new address N`. On success the pending marker is marked satisfied.
4. The car re-reads **and re-decodes** `LockChime.wav` and plays the new chime on
   the next lock.

### 9.2 Why a full re-enumeration is required (the cache gotcha)

A soft SCSI **medium-change** on p2 (used for directory-listing refresh) makes the
car re-read the **file bytes** but it keeps its **decoded-audio cache** → it plays
the **OLD** chime. **Only a full USB re-enumeration** makes the car re-read *and*
re-decode. This is the single reason chimes use the re-enum path (§1.1 #2), not the
soft medium-change used for new/deleted media listings. Do not "optimize" a chime
change down to a medium-change — it will silently play the stale chime.

### 9.3 Invariants that must hold (regression fences)

- **Recording is never gated.** lun.0 (`teslacam.img`) is never ejected, swapped,
  or `ro`-flipped for a chime change. The re-enum is a whole-device soft-connect
  blip (~700 ms = one TeslaCam clip boundary), accepted only because changing the
  chime is a deliberate operator act. The file-storage kthread PID and
  `lun.0/file` are unchanged across the reenum.
- **Trigger scope is exactly one mutation.** Only an **install of `LockChime.wav`
  on p2** triggers the reenum — not deletes, not other files, not p1. Locked by
  `mutation_requires_chime_reenum_matches_lock_chime_install_only` (ipc.rs).
- **Recording-idle gated (`force=false`).** If the car is actively recording at the
  apply moment, the reenum **defers** (`Deferred{reason:"recording_active"}`) and
  retries from the durable marker; it never punches a hole in a save. Locked by the
  `is_recording_idle*` and deferral tests in `reenum.rs`.
- **Durable pending state.** `chime-reenum.json` is written before blob reclaim and
  survives reboot/power loss; a deferred reenum resumes after restart. Locked by
  `chime_reenum_state_pending_derivation` + `chime_reenum_state_persist_roundtrip`.
- **Restart-safe deploy.** Only `gadgetd-control.service` (the IPC `serve`) is
  restarted on deploy; the binder `gadgetd.service` (oneshot `up`,
  `RemainAfterExit`) is never touched. A healthy restart is a **no-op** (no blip):
  `startup_needs_connect` only reconnects when `rebound || udc_state=="not attached"`.

### 9.4 How to verify (no regression check)

- **Automated:** `cargo test -p gadgetd` (the named tests above lock the trigger
  scope, the recording-idle gate, and the durable pending state).
- **On hardware:** `POST /api/chimes/library/<Other>.wav/activate`, then confirm
  (a) `GET /api/chimes` `size_bytes` flips to the new file's size, (b)
  `journalctl -u gadgetd-control.service` shows `reason=chime_apply`, (c)
  `lun.0/file` + the file-storage kthread PID are unchanged, and (d) the operator
  hears the new chime on the next lock. Always arm the dead-man reboot first
  (see the hardware-test skill); use `force=false` unless the operator explicitly
  accepts a mid-recording clip boundary.

### 9.5 Known operational gotcha (not a bug)

The SPA's displayed "active chime" can be **stale** if a server-side change (a
scheduled/random pick, another browser tab, or an API call) flipped it after the
page loaded. "Set Active" on a chime the SPA *thinks* is already active no-ops and
never reaches webd — so the car keeps whatever was last installed. **Hard-refresh
the Lock Chimes page** (Ctrl+Shift+R) before relying on the displayed active chime.
(The HTTP passthrough of `chime_reenum_pending` to the SPA "Syncing chime to your
car" overlay — slice **i3** — is implemented but **not yet deployed**; it is UX
polish and is not required for the chime change to reach the car.)
