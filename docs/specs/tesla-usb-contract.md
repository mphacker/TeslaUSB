# SPEC — Tesla USB drive contract (the external interface we MUST conform to)

> Parent: [`SPEC.md`](./SPEC.md) · Type: external interface contract (not a service)
> This is the authoritative description of what the Tesla vehicle expects on the
> USB device. `gadgetd` (disk image layout), `scannerd` (parsing), and
> `retentiond` (archiving) all conform to this. Scope: **recent Tesla software
> builds** (≈2023–2025, incl. Cybertruck / Model S/X with extra cameras).
> Behavior here is validated empirically as prototype-first unknown #1 in
> `SPEC.md` §9 — Tesla does not publish a formal spec, so this is "observed
> contract", confirmed on the live car before we depend on it.

## 1. Why this matters

The car is the **writer/owner** of the dashcam filesystem; we only emulate the
drive and read it conservatively. If our layout/names/format don't match what the
car expects, the car silently won't record (or won't read media features). The
#1 invariant (`SPEC.md` §2) means we must match this contract **exactly** and
never make the car see an error.

## 2. Device / partitioning

- **Single physical USB device** (we emulate one LUN backed by `disk.img`).
- **MBR partition table**, **2 primary partitions** (our chosen layout):
  - **p1 — dashcam**, contains the `TeslaCam/` folder tree. Filesystem **exFAT**.
  - **p2 — media**, contains the car's media-feature files/folders (chimes,
    boombox, light shows, music). Filesystem **exFAT**.
- **Partition-scanning behavior (observed):** the car locates the **dashcam**
  feature by finding the partition that contains a top-level `TeslaCam/` folder,
  and locates **media features** (LockChime/Boombox/LightShow) and **music** by
  scanning the *other* partition(s) on the same device. This is why p1 and p2
  must be on the **same physical device** — confirmed behavior (unknown #1).
- **Filesystem:** **exFAT** is the target for both partitions (large files,
  cross-platform, what current builds prefer). FAT32 is legacy-only; **we do not
  use FAT32** (and a single 2-partition exFAT image avoids the FAT32 32 GB cap).
  NTFS is **not** supported by the car. No encryption/BitLocker.
- **Free-space / sizing:** the car needs real free space on p1 to keep recording;
  p1 must be sized generously and never be allowed to read as full in a way that
  errors the car.

## 3. exFAT case & naming rules (critical)

- exFAT is **case-insensitive but case-preserving**. The car creates the dashcam
  folders itself with specific casing; our parser and any folders/files **we**
  create (media features) must use the **exact** casing below to be safe across
  firmware and across host tools.
- **Names are matched as the car expects them** — treat every name below as
  **case-sensitive and exact**. Do not rename, lowercase, or "normalize" them.
- Timestamp tokens use the literal format **`YYYY-MM-DD_HH-MM-SS`** (24-hour,
  hyphen/underscore separators, local vehicle time). Our parser keys off this
  exact pattern (`\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}`).

## 4. Dashcam tree (p1 — `TeslaCam/`)

The car creates and manages this; we **read** it, we do **not** create or rename
its folders.

```
TeslaCam/
├── RecentClips/        # ROTATING continuous buffer (see §5)
├── SavedClips/         # user-saved events (persistent)
├── SentryClips/        # Sentry-triggered events (persistent)
└── TeslaTrackMode/     # Track Mode telemetry (when used; persistent)
```

### 4.1 RecentClips — flat per-minute segment FILES
- Contains **files directly** (no per-event subfolders): one ~1-minute segment
  **per camera**, named **`YYYY-MM-DD_HH-MM-SS-<camera>.mp4`**.
- This is a **rotating buffer**: the car overwrites the oldest segments. The
  rotation window is a **vehicle setting, ~1 h up to 24 h** (see §5).

### 4.2 SavedClips / SentryClips — event FOLDERS
- Contains **timestamped subfolders**: **`YYYY-MM-DD_HH-MM-SS/`**, each holding
  the per-camera videos plus metadata:
  ```
  SavedClips/2025-06-04_14-17-20/
    front.mp4
    back.mp4
    left_repeater.mp4
    right_repeater.mp4
    event.json        # trigger reason, est. lat/lon, city, timestamp, camera
    thumb.png         # event thumbnail
  ```
- **NOT rotated** by the car. They persist until the volume fills (after which
  saving degrades) — so archiving them off also **protects the car volume**.

### 4.3 Camera files (firmware/model dependent)
- Baseline: `front.mp4`, `back.mp4`, `left_repeater.mp4`, `right_repeater.mp4`.
- Newer vehicles (Cybertruck, newer S/X) add cameras — e.g. pillar cameras and
  additional repeaters (`*_pillar*.mp4`, `*_repeater2*.mp4`, etc.).
- **Do not hard-code a fixed camera set.** Treat any `*.mp4` (and legacy `*.ts`)
  in a RecentClips segment group / event folder as a camera angle; group by the
  shared `YYYY-MM-DD_HH-MM-SS` timestamp into one logical **"clip"**.

## 5. RecentClips rotation — the timing reality (drives archiving)

- The naive "**~1 hour buffer**" assumption is **wrong** and must not be coded.
  The window is configurable **~1–24 h**, and the *effective* retention also
  varies with clip rate, camera count, and free space.
- The car setting is **not exposed** to the drive. Therefore the rotation window
  must be **measured empirically**, not read or assumed: track the
  newest/oldest complete segments and observe when previously-seen segments
  disappear; estimate effective retention as the age of the oldest still-visible
  segment, treated as **advisory, not guaranteed**.
- Consequence for design: the archiver must **race** the rotation, copying
  oldest / event-adjacent complete segments first. See
  [`retentiond.md`](./retentiond.md) for the full policy and the honest guarantee.

## 6. Media features (p2 — read by the car)

Observed locations for current builds (validate per unknown #1). These live on
the **media partition (p2)**; the car scans the non-dashcam partition for them.

| Feature | Path on p2 | Format / rules |
|---------|-----------|----------------|
| Custom lock chime | `LockChime.wav` (partition root) | WAV, 16/24-bit, 44.1/48 kHz, short (~a few seconds) |
| Boombox sounds | `Boombox/<name>.{mp3,wav}` | flat folder, **no subfolders**, simple names |
| Light shows | `LightShow/<name>.fseq` (+ optional audio) | **.fseq v2** (v1 not accepted), flat folder |
| Music | partition root / any layout | scanned from the non-dashcam partition |

We **own** p2 and create these via the `gadgetd` eject-handoff; use the exact
folder/file names above. (`webd`/`spa` manage the user-facing media library that
populates p2 — see those specs and the existing blueprints: `lock_chimes`,
`boombox`, `light_shows`, `music`.)

## 7. Boundaries (contract-specific)

**ALWAYS** match folder/file names and casing exactly; treat names as
case-sensitive; key timestamps off the exact `YYYY-MM-DD_HH-MM-SS` pattern;
treat RecentClips as flat rotating files and Saved/Sentry as persistent event
folders; keep p1+p2 on one physical device; use exFAT.
**ASK FIRST / PROVE FIRST** before depending on any partition-scan or media-path
behavior — validate on the live car (unknown #1); firmware can shift these.
**NEVER** rename/normalize the car's folders; never assume a fixed camera set;
never assume a fixed (e.g. 1 h) RecentClips window; never use FAT32/NTFS/
encryption; never let p1 read as full in a way that errors the car.
