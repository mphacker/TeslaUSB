# TeslaUSB — Requirements Baseline (derived from v1)

> **Purpose.** This document captures *what the legacy TeslaUSB v1 solution does*
> from the perspective of the three audiences that interact with it:
>
> 1. the **Tesla vehicle** that sees the device as USB storage,
> 2. the **end user on the local network via SMB** (Windows Explorer / macOS Finder), and
> 3. the **end user in the web UI**.
>
> It is the **requirements baseline** for the Rust re-implementation (B-1): the goal
> is to reproduce every v1 end-user capability and the look-and-feel, but in Rust,
> with lower CPU / I/O / memory and zero loss of dashcam clips. v1 is **reference
> only** — no v1 Python code is reused.
>
> **Source of truth.** Behavior was extracted from the v1 Flask app
> (`web/teslausb_web/`) and setup scripts as they existed at git revision
> `7d90026~1` (the commit immediately before the Python web app was removed),
> plus the Samba services and the USB-gadget change-propagation scripts.
> Where a specific number/route is taken verbatim from source it is stated as
> fact; where a detail is inferred it is marked *(verify)*.

---

## 1. How the Tesla vehicle sees the device

- The Raspberry Pi presents itself to the car as a **USB Mass Storage device**
  (USB gadget). The car treats it exactly like a plugged-in USB flash drive — no
  driver, app, or account on the car side.
- The device exposes **two logical drives (LUNs)**, each backed by its own
  fixed-size disk image on the Pi:
  - **TeslaCam drive** — the dashcam / Sentry recording target. **The car WRITES
    to this drive** continuously while driving, parked in Sentry, etc.
  - **Media drive** — chimes, music, boombox, light shows, wraps, and license
    plates. **The car only READS from this drive.**
- The car expects **Tesla's standard folder names**. On the **TeslaCam drive**:
  - `TeslaCam/RecentClips/` — rolling buffer of recent footage (continuously
    overwritten by the car).
  - `TeslaCam/SavedClips/` — clips the driver explicitly saved (honk / tap).
  - `TeslaCam/SentryClips/` — Sentry-triggered event clips.
  - `TeslaCam/TeslaTrackMode/` — Track Mode telemetry/recordings *(verify; present
    in v1 folder lists)*.
  - Within `SavedClips`/`SentryClips`, the car creates **one subfolder per event**
    (named by timestamp, e.g. `2024-01-15_14-22-15`) containing per-camera MP4s
    and an `event.json` (reason, city/GPS, timestamp) and `thumb.png`.
    `RecentClips` is **flat** (timestamped clip files directly, no event folders).
  - Per-clip files are named `<timestamp>-<camera>.mp4`, where camera ∈
    `front`, `back`, `left_repeater`, `right_repeater`, `left_pillar`,
    `right_pillar`.
- On the **Media drive** (everything lives at the drive root) the car reads:
  - `LockChime.wav` — the **active custom lock chime** (played on lock/unlock).
  - `Boombox/` — external-speaker (Boombox) sounds.
  - `Music/` — music library for the car's media player (supports nested folders).
  - `LightShow/` — custom light-show files (`.fseq` + paired audio).
  - `Chimes/` — the **library** of available lock chimes (not read directly by the
    car; the active one is copied to `LockChime.wav`).
  - `Wraps/` — custom vehicle-wrap images *(v1 stored these under a `lightshow/`
    parent on the media drive; the B-1 requirement is to place `Wraps/` at the
    media-drive root — see §10)*.
  - `LicensePlate/` — custom license-plate images *(same placement note as Wraps)*.
- **Advertised capacity** of each drive is configurable (TeslaCam default 64 GB,
  Media default 32 GB; range 4–2048 GB). The backing image is fully pre-allocated
  so the car's writes never depend on the Pi's free SD-card space.

### 1.1 How the car notices changes the user makes

The car aggressively caches USB contents. After the web UI (or SMB) changes files,
v1 forces the car to re-read using one of two mechanisms:

- **Soft SCSI medium-change** (`tesla_cache_invalidate.sh`, ~200 ms): clears and
  restores the LUN's backing file in configfs so the kernel signals "medium
  changed." The car re-reads **directory listings** — new music, new light shows,
  new boombox sounds, deletions — *without* a full re-plug. The TeslaCam drive is
  unaffected.
- **Full USB re-enumeration** (`tesla_gadget_rebind.sh`, ~2–4 s): unbinds and
  rebinds the gadget so the car sees a re-plug. This is the **only** mechanism
  that makes the car pick up a **changed `LockChime.wav`** (the car only re-reads
  the lock chime on a fresh enumeration). It briefly detaches the whole device
  (including TeslaCam), so it is used **only** for the deliberate act of changing
  the active lock chime, with a bounded health check to guarantee recording
  resumes.

> **Invariant.** Routine media changes must use the soft medium-change and must
> never stall or detach the TeslaCam drive. Only an explicit active-chime change
> may trigger a (brief, verified) full re-enumeration.

---

## 2. How the end user sees the device over the network (SMB)

When network sharing (Samba) is enabled, the Pi exposes the storage as standard
**SMB/CIFS network shares**, reachable from Windows Explorer / macOS Finder /
Linux at `\\teslausb\…` (or the device's hostname/IP).

- **Two shares** are published by default:
  - **`TeslaCam`** — comment "TeslaUSB Dashcam & Sentry footage". Points at the
    TeslaCam storage tree.
  - **`Media`** — comment "TeslaUSB Music, Boombox & Light Shows". Points at the
    media storage tree.
- Both shares are **browseable** and **read-write**, so the user can **drag files
  in and delete files** directly (e.g. drop a batch of MP3s into `Media\Music`, or
  copy dashcam events off `TeslaCam` for archiving).
- Shares are **authenticated** (guests are rejected — `map to guest = Bad User`).
  The user sets/changes the Samba password from the web UI; there is no anonymous
  access.
- **Expected outcome of an SMB write:** the file lands in the corresponding folder
  on the drive; the car picks it up on the next medium-change / re-read cycle (for
  the lock chime, a full re-enumeration is still required — SMB drops do not
  themselves trigger the chime re-enumeration).
- Samba can be **toggled on/off** in the web UI (Settings). When off, no shares are
  advertised and the top-bar status dot is hidden; when on, the dot is amber
  ("Network sharing active").

---

## 3. Web UI — global shell

The web UI is a single responsive app (mobile + desktop) reached at the device
hostname (e.g. `http://teslausb.local` or the device IP; during first-time setup
via the device's Wi-Fi access point — see §4.13).

- **Top bar:** brand/logo (links to the Map), a **system-health status dot**
  (polls `/api/system/health`; green/amber/red/grey by severity; click → Settings
  health card), the **Samba status dot** (shown only when sharing is on), and a
  **light/dark theme toggle** (persisted in the browser).
- **Primary navigation** (sidebar on desktop, bottom tabs on mobile), each item
  shown only when its data/feature is available:
  - **Map** — trip map + event/clip browser (the home screen).
  - **Analytics** — driving & storage analytics dashboard.
  - **Media** — hub linking to Chimes, Music, Boombox, Light Shows, Wraps,
    License Plates.
  - **Cloud** — cloud archive / off-device backup.
  - **Settings** — Wi-Fi, network sharing, storage, system health, etc.
- **Feedback model:** actions either return JSON (for in-page AJAX) or perform a
  redirect with a **flash banner** (success = green, error = red, info = blue,
  warning = yellow). Long-running/health views **poll** and update live.

---

## 4. Web UI — end-user capabilities by page

For each page below: what the user can see, **every action they can take**, and the
**expected outcome**.

### 4.1 Trip Map (home)

**Purpose:** browse where and when the car drove and recorded, on an interactive
map, and jump into the footage.

The user can:

- **View a day's trips on the map.** The map draws each trip's **route as a
  polyline colored by speed** (a speed legend can be toggled). Routes and GPS come
  from **telemetry (SEI) embedded in the dashcam MP4s** indexed by the device.
  - *Outcome:* the selected day's routes + event markers render; a day card shows
    date and summary stats (distance, duration, trip count, event count, avg/max
    speed).
- **Step between days** (previous / next day). *Outcome:* the map reloads that
  day's routes, events, and stats in a single fetch.
- **See event markers** (hard-braking, collision, Sentry motion, etc.) placed at
  their GPS location, styled by **severity** (critical / warning / caution / info).
  *Outcome:* clicking a marker surfaces the event and a way to open its footage.
- **Filter** trips/events by **date range**, **map bounding box** (pan/zoom),
  **event type**, **severity**, and minimum trip distance. *Outcome:* the visible
  set updates.
- **Open the side video/clip browser panel** and switch tabs between **Events**
  (Saved/Sentry events), **Trips** (driving sessions), and **All Clips**, and
  **change the source folder** (`RecentClips` / `SavedClips` / `SentryClips`).
  *Outcome:* the panel lists matching items; selecting one opens the event player.
- **Choose units & timezone** (mph/km-h, local/UTC) via preferences. *Outcome:*
  speeds and times re-render in the chosen units.

### 4.2 Event / Video Player

**Purpose:** watch a recorded event or clip across all camera angles, with a
telemetry HUD, and manage the clip.

The user can:

- **Play a clip.** Video **streams from the device** with HTTP range support
  (seek/scrub works). *Outcome:* the selected angle plays in the browser.
- **Switch camera angle** among the available angles for that clip — front, back,
  left/right repeater, left/right pillar. *Outcome:* the player swaps to that
  angle's MP4 (playback position preserved where possible).
- **Navigate clips within a multi-clip event** (previous / next). *Outcome:* loads
  the adjacent minute's clip and updates which angles are available.
- **Toggle the telemetry HUD overlay** (SEI). When on, the device parses the MP4's
  embedded telemetry and overlays **speed, gear, brake/throttle, steering,
  Autopilot/FSD state**, synced to playback. *Outcome:* HUD appears/updates per
  frame; "loading" shown while telemetry downloads.
- **Download** a single angle, or **download the whole event** as a ZIP.
  *Outcome:* browser downloads the file(s).
- **Archive the event to the cloud** (if a cloud backend is configured).
  *Outcome:* the event is enqueued for off-device upload (see §4.10).
- **Delete the event/clip** (with confirmation). *Outcome:* the event folder is
  removed from the TeslaCam tree (via a privileged, path-validated delete helper,
  since the car/daemon own those folders), the list refreshes, and the car re-reads
  on the next medium-change.

### 4.3 Analytics

**Purpose:** at-a-glance health and driving statistics.

The user can **view** (read-only dashboard):

- **Storage usage per partition** (TeslaCam, Media, SD card): total / used / free /
  percent.
- **Video statistics:** total clip files, multi-angle clip count, total size,
  oldest/newest dates, and a **per-folder breakdown** (SavedClips / SentryClips /
  RecentClips) with counts and sizes.
- **Storage health summary** with alerts and recommendations (e.g. "primary
  partition 95% full → consider deleting older RecentClips").
- **Recording-time estimate** ("hours remaining" before reuse), labeled by
  confidence (computed from indexed clip sizes, or a theoretical fallback).
- **Driving stats & charts:** total distance/time, trip/event counts, avg/max
  speed, **FSD-engagement %**, events per 100 miles, and **event severity / type
  timelines** and an **FSD timeline** (interactive charts).
- *Empty state:* if the index isn't ready, cards show "—" and the page explains it
  will recover once indexing is healthy.

### 4.4 Media hub

**Purpose:** landing page that links to each media library. *Outcome:* cards/links
navigate to Chimes, Music, Boombox, Light Shows, Wraps, and License Plates. Each
card is shown only if that feature/folder is available.

### 4.5 Lock Chimes

**Purpose:** manage the library of lock-chime sounds and choose which one the car
plays on lock/unlock. The active chime is the file `LockChime.wav` at the media
drive root; the library lives in `Chimes/`.

The user can:

- **See the active lock chime** at the top of the page — its **original name**
  (the library file it was copied from, not just "LockChime.wav"), with size /
  duration, and **play it** in the browser. *(In v1 this is a player for the real
  active sound — not a bare filename with a remove button.)*
- **Play any library chime** in the browser (HTML5 audio streamed from the device).
- **Upload** one or more chimes (`.wav`, also `.mp3` which is converted to WAV).
  Limits: **≤ 1 MB and ≤ 5 seconds** each *(verify exact transcode/normalize
  behavior)*. *Outcome:* the file is validated and added to `Chimes/`; it appears
  in the list; the car re-reads the directory on the next medium-change.
- **Delete** a library chime. *Outcome:* removed from `Chimes/`; list refreshes.
- **Rename** a chime *(v1 exposes a rename API)*. *Outcome:* file renamed in place.
- **Set a chime active.** *Outcome:* the chosen library file is **copied to
  `LockChime.wav`** at the media root, and a **full USB re-enumeration** is
  triggered so the car actually adopts the new chime (the one case that briefly
  re-plugs the device); the UI shows which library chime is active.
- **Organize chimes into groups** (create / edit / delete groups; groups persist
  in `chime_groups.json`). *Outcome:* groups can be targeted by scheduling/random.
- **Schedule chimes** (weekly by day+time, specific date, holiday, or recurring
  interval). Create / edit / enable-disable / delete schedules (persist in
  `chime_schedules.json`). *Outcome:* the targeted chime/group plays on the schedule
  (subject to the scheduling daemon).
- **Enable "random" mode** from a selected group (persists in
  `chime_random_config.json`). *Outcome:* the active chime is rotated from the
  group.

> Lock-chime library and the active `LockChime.wav` both live **in the media drive
> image** (not on the Pi's SD card).

### 4.6 Music

**Purpose:** manage the car's music library on the Media drive (`Music/`).

The user can:

- **Browse** the music library, including **nested folders**.
- **Play** a track in the browser (streamed from the device).
- **Upload** audio files (`.mp3`, `.flac`, `.wav`, `.aac`, `.m4a`); large files
  supported (**up to 2 GB each**, uploaded in 16 MB chunks). *Outcome:* the file is
  written into the chosen folder under `Music/`; appears in the list; car re-reads
  on medium-change.
- **Create folders** and **move** files between folders. *Outcome:* the tree is
  reorganized on disk.
- **Delete** files (and folders). *Outcome:* removed from `Music/`.
- **See storage usage** for the media drive (used / free / total).

### 4.7 Boombox

**Purpose:** manage the small set of external-speaker (Boombox) sounds (`Boombox/`).

The user can:

- **List / play** the current Boombox sounds.
- **Upload** sounds (`.mp3`, `.wav`), limited to **≤ 1 MB each** and **at most 5
  files total**. *Outcome:* validated and written to `Boombox/`; rejected with a
  clear message if over size or over the 5-file cap.
- **Delete** sounds (including bulk delete). *Outcome:* removed from `Boombox/`.

### 4.8 Light Shows

**Purpose:** manage custom light-show files on the Media drive (`LightShow/`).

The user can:

- **List** shows. v1 groups a show by its common name stem (the `.fseq` sequence
  plus its paired audio).
- **Play** the show's audio in the browser.
- **Upload** `.fseq` and audio (`.mp3`/`.wav`) files — single files up to **100 MB**,
  or a **ZIP up to 500 MB** that is auto-extracted and flattened. *Outcome:* files
  written into `LightShow/`; appear in the list; car re-reads on medium-change.
- **Set an active show** *(active selection persisted in `lightshow_active.json`)*.
- **Delete** files / shows (including bulk delete). *Outcome:* removed from
  `LightShow/`.

### 4.9 Wraps & License Plates (vehicle imagery)

**Wraps** — custom vehicle-wrap images the car can display.

The user can:

- **List wraps with image thumbnails** (the thumbnail is the **raw PNG served from
  the drive**; the browser scales it — no separate thumbnail file in v1).
- **Upload** wrap PNGs: **`.png` only**, **≤ 1 MB**, dimensions **64×64 to
  2048×2048**, filename ≤ 32 chars `[A-Za-z0-9_- and space]`, **up to ~10** wraps.
  *Outcome:* validated (PNG header + dimensions parsed without heavyweight image
  libs) and atomically published to the wraps folder; appears in the list.
- **Delete** wraps (including bulk delete). *Outcome:* removed from the wraps folder.

**License Plates** — this page has **two distinct functions** in v1:

1. **Custom plate images** the car can display:
   - **List with thumbnails**, **upload**, **delete** plate **PNGs**: **`.png`
     only**, **≤ 512 KB**, dimensions **exactly 420×75 (NA)** or **492×75 (EU)**,
     filename **≤ 12 alphanumeric chars** (no spaces/dashes/underscores), **up to 5**
     plates. *Outcome:* validated and atomically published to the license-plate
     folder; clear error if dimensions/size/name/count are wrong.
2. **Tracked-plate list (privacy / redaction):** a small database of plate strings
   the user wants the system to recognize.
   - **Add / edit / delete** tracked plates (plate text normalized to uppercase
     alphanumeric ≤ 16 chars; optional label ≤ 64 and notes ≤ 240; duplicates
     rejected). **Bulk delete** supported.
   - **Toggle plate redaction** on/off. *Outcome:* the redaction setting is saved
     and applied by downstream processing.

> Both wraps and plate images are **atomically published** (write-temp → fsync →
> rename, `chmod 0644`) so a successful upload can never leave a torn file the car
> might read.

### 4.10 Cloud Archive

**Purpose:** back recordings up to an off-device cloud destination (rclone-style
remotes), and watch the sync.

The user can:

- **Configure a cloud backend** (provider + credentials + remote path prefix).
  Supported providers are the rclone family (e.g. S3, B2, Google Drive, Dropbox,
  Crypt) *(verify exact provider list)*. *Outcome:* settings saved; the worker can
  upload.
- **Choose which folders sync** (`RecentClips` / `SavedClips` / `SentryClips`) and
  **priority ordering**; choose whether to **sync non-event media** and whether to
  **sync RecentClips that have telemetry**. *Outcome:* only selected folders are
  enqueued; priority folders upload first.
- **Set a bandwidth limit** (kbps; 0 = unlimited) and a **cloud free-space reserve**
  (pause uploads when the remote is nearly full). *Outcome:* the uploader throttles
  / pauses accordingly.
- **Set max retry attempts** before a file goes to the **dead-letter** (failed)
  queue, and options for **cloud auto-cleanup** and **keep clips until synced**.
- **Trigger an immediate sync** and **stop an in-progress sync**. *Outcome:* the
  queue drains now / cancels.
- **Watch progress live:** current sync status, the **queue** (pending / synced),
  and **recent history** (polled). *Outcome:* the UI updates without reloads.

### 4.11 Storage Settings

**Purpose:** size the two drives and tune cleanup/retention.

The user can:

- **Set the TeslaCam and Media drive sizes** (GB; 4–2048). *Outcome:* the advertised
  capacity changes; applying it resizes the backing image and briefly disconnects
  the gadget (~30–60 s) to re-advertise. A shrink **below current usage is rejected**
  with a clear message.
- **Set a safety buffer** (≥ 5 GB held back to protect the OS partition).
- **Tune cleanup:** target free space %, Sentry max-age (days; 0 = never auto-delete
  by age), and **preserve clips that have GPS/telemetry** (lower delete priority).
  *Outcome:* the retention worker frees space per these rules; car writes always win.

### 4.12 Storage Health

**Purpose:** monitor filesystem / SD-card health and recover.

The user can:

- **View health:** mount status, filesystem error counts, SMART/health indicators
  (severity ok / warn / critical / unknown), with alerts and recommendations.
- **Run an online (read-only) filesystem check.** *Outcome:* a background `e2fsck`
  runs; the page fast-polls and reports clean / errors-found / failed.
- **Arm or cancel an fsck on next boot.** *Outcome:* a repair is scheduled/unscheduled
  for the next reboot.
- **Reboot the device now.** *Outcome:* the device reboots (confirmation expected).

### 4.13 Wi-Fi / Captive Portal

**Purpose:** get the device onto the user's Wi-Fi, including a first-run captive
portal.

The user can:

- **First-run setup AP:** the device broadcasts a setup SSID (default
  `TeslaUSB-Setup`, passphrase auto-generated). Joining it and opening any site
  triggers the **captive portal** (Apple/Android/Windows/generic detection routes
  all redirect to the Wi-Fi setup page). *Outcome:* the setup UI appears
  automatically.
- **Scan for networks** and **see saved networks** with in-range/signal status.
- **Join a network** (select SSID + passphrase; open networks need none).
  *Outcome:* the device connects; on success the setup AP can stand down.
- **Disconnect** / **forget** a saved network. *Outcome:* connection dropped /
  profile deleted; the AP can re-enable for recovery.
- **Enable / disable the setup AP** manually. Disabling arms an **auto-restore
  timer** so the AP comes back if the device loses connectivity (so the user is
  never locked out).

### 4.14 Failed Jobs

**Purpose:** see and clear background jobs that failed permanently (dead-letter).

The user can:

- **View failed-job counts by subsystem** (indexer, cloud sync) and the failed rows
  (paginated, with reason).
- **Retry** selected failed jobs. *Outcome:* the row(s) reset for reprocessing.
- **Delete** selected failed jobs. *Outcome:* the row(s) are removed.

### 4.15 Settings (system)

The user can:

- **Toggle network sharing (Samba)** on/off (see §2). *Outcome:* shares
  appear/disappear; status dot reflects state.
- **Set/Change the Samba password** (8–63 chars). *Outcome:* SMB auth uses the new
  password.
- **Set map/display preferences** (speed units, timezone) and **network settings**.
- Access the **system-health** card (per-subsystem breakdown behind the top-bar dot).

---

## 5. Cross-cutting behavior (applies to all media actions)

- **All media files live inside the drive images** (TeslaCam image / Media image),
  **never shadow-copied onto the Pi's SD card**. The SD card holds the OS, the
  backing images, the index DB, and (for off-device upload) a Pi-side archive of
  clips.
- **Atomic writes:** uploads are written to a temp file, fsync'd, validated, then
  atomically renamed into place with car-readable permissions — a successful upload
  is never a torn file.
- **Filename safety:** path separators, `..`, and NUL are rejected; per-category
  character and length rules apply (see each section). Symlinks are rejected.
- **Validation & errors:** oversize → 413, bad input → 400, missing → 404, duplicate
  → 409, server/IO error → 500, not-yet-implemented → 503. Browser form posts get a
  flash + redirect; AJAX gets JSON `{success, message, …}`.
- **Change propagation to the car:** directory changes → soft SCSI medium-change;
  the active lock-chime change → full USB re-enumeration (§1.1). Neither must stall
  or risk the TeslaCam recording stream.

---

## 6. Requirements the B-1 (Rust) re-implementation must meet

These are the binding takeaways for the rewrite (see also
`docs/specs/usb-io-and-archiving-architecture.md` §0.1):

1. **TeslaCam drive is never disconnected** and the car can always write to it; no
   media action may eject, stall, or gate it.
2. **Recorded TeslaCam clips are readable** for the map/player — not only the
   off-device archive copies, but clips still on the live TeslaCam drive.
3. **Media upload/delete may momentarily take the Media drive offline only**, never
   impacting the TeslaCam drive.
4. **All media files live in the drive images** (including the lock-chime library),
   never on the SD card.
5. **Reproduce every end-user capability and the look-and-feel of v1** listed above,
   in Rust, more efficiently (lower CPU / I/O / memory) and with zero clip loss.

> **Open point — wraps/plates location.** v1's code placed `Wraps/` and
> `LicensePlate/` under a `lightshow/` parent on the media drive. The B-1 direction
> is that **`Wraps/` (and license-plate images) should sit at the media-drive root**
> alongside `Music/`, `Boombox/`, `LightShow/`, `Chimes/`, and `LockChime.wav`.
> Confirm the exact Tesla-required paths before finalizing the media layout.
