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
