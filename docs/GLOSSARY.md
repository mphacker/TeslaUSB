# Glossary

Every recurring term used in the TeslaUSB codebase, docs, and UI,
with the precise meaning the project gives it. If a term is used in
the docs and isn't defined here, that's a bug — please file an issue
or open a PR.

Terms are grouped by topic. Within a group, alphabetical order.

---

## Hardware and OS

### Pi Zero 2 W
The Raspberry Pi model TeslaUSB is built and tested on. 4 ARM cores,
512 MB RAM, single SDIO bus shared between the SD card and the
on-board Broadcom WiFi chip. The 512 MB ceiling and the shared SDIO
bus drive most of the codebase's "be gentle" engineering — load-pause
guards, chunked copies, swap configuration, and the watchdog priority
drop-in.

### SDIO bus
The single hardware bus the Pi Zero 2 W uses for both SD-card I/O
and WiFi. Heavy SD-card reads (catch-up archive runs) and heavy WiFi
writes (rclone uploads, AP teardown/restart) compete for the bus.
Saturating it can starve the userspace `watchdog` daemon and trigger
a hardware reset.

### Hardware watchdog
The Broadcom-side hardware timer that resets the Pi if the userspace
`watchdog` daemon stops pinging it. Configured with a 90-second
timeout (raised from 60 s to give SDIO contention some headroom).

### UDC (USB Device Controller)
The kernel interface that binds or unbinds the USB gadget from the
host (Tesla). Writing the UDC name to
`/sys/kernel/config/usb_gadget/g1/UDC` activates the gadget; writing
empty unbinds it. **Unbind is destructive — Tesla loses the drive
instantly.**

---

## USB gadget and storage

### Gadget
The USB device personality the Pi presents to Tesla. TeslaUSB's
gadget exposes 2 or 3 LUNs depending on `disk_images.music_enabled`.

### LUN (Logical Unit Number)
One "drive" inside the USB gadget. TeslaUSB always exposes:

- **LUN 0** — TeslaCam drive (exFAT, dashcam recordings)
- **LUN 1** — LightShow drive (FAT32, lock chimes / wraps / shows)
- **LUN 2** *(optional)* — Music drive (FAT32)

Each LUN is backed by an `.img` file in `installation.mount_dir`
(default `/mnt/gadget`).

### `.img` file
A raw filesystem image stored on the SD card and exposed to Tesla
via the USB gadget. **`*.img` files are protected** — `is_protected_file()`
in `partition_service` refuses every delete request that targets one.

### Loop device (`/dev/loopN`)
A kernel construct that lets the Pi mount an `.img` file locally so
the web UI can read or write its contents. **The gadget itself does
not use loop devices** — it serves the `.img` file directly. Loop
devices are only used for local mount access. Multiple loop devices
on the same image are normal and harmless.

### Mount namespace (`nsenter`)
Mounts performed inside a subprocess vanish when that subprocess
exits, because each subprocess runs in its own mount namespace.
Every mount/umount/mountpoint command in TeslaUSB is therefore
prefixed with `sudo nsenter --mount=/proc/1/ns/mnt …` so the mount
lands in PID 1's namespace and survives.

### Quick edit (`quick_edit_part2`)
A coordinated short-lived RW window on LUN 1, used while still in
present mode. The flow: clear LUN 1's backing file → unmount RO → detach
read-only loops → create RW loop → mount RW → do work → sync → unmount
→ reattach RO loop → restore LUN 1's backing file. Guarded by
`~/.quick_edit_part2.lock` (120 s stale timeout). Used for chime
upload, light-show upload, license plate upload, etc.

---

## Modes

### Present mode
The "default" mode: USB gadget is bound to Tesla, partitions mount
**read-only** at `/mnt/gadget/part*-ro`, Samba is off. State token in
`state.txt`. Tesla is actively recording.

### Edit mode
Maintenance mode: USB gadget is unbound, partitions mount
**read-write** at `/mnt/gadget/part*`, Samba is on. Used for bulk
manual edits via SMB. The UI does not expose "Edit Mode" / "Present
Mode" terminology — instead it shows a status dot:
"Network Sharing Active" (amber) vs normal (green).

---

## Files Tesla writes

### TeslaCam
The top-level folder Tesla creates on LUN 0. Contains:

- `RecentClips/` — rolling 1-hour buffer (continuous recording)
- `SentryClips/<event-dir>/` — one folder per Sentry-triggered event
- `SavedClips/<event-dir>/` — one folder per user-triggered save
- `boot/` — Tesla boot diagnostics

### RecentClips
Tesla's rolling buffer. Written continuously while the car is
powered (driving **or** Sentry standby) and rotated automatically
every ~60 minutes. **Most RecentClips on a parked car are not
"driving footage" — they're idle dashcam.** UI labels them
**"Rolling buffer"**, not "Driving footage", to keep that honest.

### SentryClips / SavedClips
Per-event folders, each containing 6 camera files (`front`, `back`,
`left_repeater`, `right_repeater`, `left_pillar`, `right_pillar`)
plus an `event.json` describing what triggered the recording.

### `event.json`
Tesla writes this file **last** in an event folder, after the 6
camera mp4s. So `event.json` arriving via inotify means the event
recording is fully written and safe to upload. This is why the
file_watcher's event.json callback path **does not apply the
60-second age gate** that mp4 callbacks use.

### ArchivedClips
The Pi-managed archive on the SD card, not the USB gadget. The
archive subsystem copies `RecentClips` (and event clips) here before
Tesla rotates them out. Path: `~/ArchivedClips/<YYYY-MM-DD>/…`.
Source of truth for the indexer and cloud sync — they never index
or upload directly from the live RO mount.

---

## Background workers

### File watcher
`file_watcher_service.py`. Single process using `inotify` (Linux
kernel filesystem-event API) plus a 5-minute polling fallback.
Routes new mp4s on the RO mount to the **archive** producer, new
mp4s in `ArchivedClips` to the **indexing** queue, and `event.json`
arrivals to **Live Event Sync**.

### Archive producer / queue / worker / watchdog
Four cooperating components in `archive_*.py`:

- **Producer** scans Tesla's folders and enqueues new mp4s
- **Queue** is a SQLite table (`archive_queue` in `cloud_sync.db`)
- **Worker** is one thread that copies one file at a time from RO mount
  to `ArchivedClips`, then enqueues the destination for indexing
- **Watchdog** classifies severity, triggers retention prune, and
  surfaces health to the `/api/archive/status` endpoint

### Indexing queue / worker
`indexing_queue` table in `geodata.db` plus a single
`indexing_worker.py` thread. Parses each video's H.264 SEI metadata
to extract GPS waypoints, telemetry, and detect events. Outcomes are
typed via the `IndexOutcome` enum.

### Cloud archive
`cloud_archive_service.py`. Bulk catch-up cloud uploader. Uses
`rclone` under `nice -n 19` and `ionice -c 3`. Priority order:
events first, then geolocated, then non-event clips (opt-in).

### Live Event Sync (LES)
`live_event_sync_service.py`. Opt-in real-time uploader that fires
on `event.json` arrivals. Has its own queue (`live_event_queue` in
`cloud_sync.db`), its own worker thread, and a strict 25 MB
steady-state RSS budget. **Disabled by default.**

### Task coordinator
`task_coordinator.py`. The single mutual-exclusion point for the
heavy background workers (`indexer`, `archive`, `cloud_sync`,
`live_event_sync`, `retention`). Provides fairness so a tight cyclic
worker (the indexer) cannot starve a less-frequent priority task
(archive). `WATCHDOG_NEAR_MISS_THRESHOLD_SECONDS = 60.0` seconds —
any worker holding the lock longer logs a WARNING.

---

## Indexing and mapping

### Canonical key
`mapping_service.canonical_key(path)`. Resolves both the SD-card
view (`~/ArchivedClips/.../front.mp4`) and the USB-RO view
(`/mnt/gadget/part1-ro/TeslaCam/RecentClips/front.mp4`) of the same
file to the **same string**, so the indexing queue can dedupe them.

### IndexOutcome
The enum in `mapping_service` that names every possible result of
parsing a single video. Values include `INDEXED`,
`ALREADY_INDEXED`, `DUPLICATE_UPGRADED`, `NO_GPS_RECORDED`,
`NOT_FRONT_CAMERA`, `TOO_NEW`, `FILE_MISSING`, `PARSE_ERROR`,
`DB_BUSY`. Each is either **terminal** (deletes the queue row) or
**retry** (with exponential backoff).

### `mvhd` time
The MP4 `mvhd` (Movie Header) atom carries the GPS-derived UTC
start-of-recording time. **Authoritative** for absolute time —
unaffected by Tesla's onboard-clock drift. The indexer's
`_resolve_recording_time()` reads `mvhd` first and only falls back
to the filename when the atom is unreadable. A divergence of ≥ 5
minutes between `mvhd` and the filename logs a WARNING.

### Trip
A contiguous drive, defined by waypoints separated by less than
`mapping.trip_gap_minutes` (default 5). Trips are **sacred**: they
are real records of where the user drove, and the only thing that
may delete them is an explicit user "Delete Trip" action. Losing
the underlying clip does not unhappen the drive.

### Waypoint
A single GPS point with telemetry (speed, heading, gear, autopilot
state, steering, pedals, blinkers) extracted from one frame of one
clip.

### Detected event
A waypoint that crossed an event threshold — harsh brake, emergency
brake, hard accel, sharp turn, speeding, FSD engage, FSD disengage —
recorded in the `detected_events` table. Thresholds are configurable
under `mapping.event_detection`.

### RDP simplification
Ramer–Douglas–Peucker polyline simplification, used by
`mapping_queries._simplify_polyline_rdp()` to thin out the route
polylines drawn on the map. Default `epsilon_m = 8.0`. Run once per
trip; trips are split at gaps before simplification so the gap line
stays straight rather than getting "shortcut" through.

---

## Cloud and networking

### `rclone`
Third-party multi-cloud sync tool TeslaUSB invokes for every cloud
upload. Run with `nice -n 19`, `ionice -c 3`, and a `--bwlimit`
matching `cloud_archive.max_upload_mbps`. **Only one rclone
subprocess runs at a time**, ever, across both `cloud_archive` and
LES — coordinated via `task_coordinator`.

### NM dispatcher (`refresh_cloud_token.py`)
The single WiFi-connect entry point. NetworkManager runs
`/etc/NetworkManager/dispatcher.d/99-teslausb-cloud-refresh` on
every link-up event, which runs this script. The script: refresh
RO mount → wake LES → wait for archive (5 min cap) → wait for
indexing (2 min cap) → wait for LES drain (10 min cap) →
trigger cloud sync.

### AP / STA / `uap0`
- **STA** = WiFi client mode (the Pi connects to a home network).
- **AP** = WiFi access point mode (the Pi broadcasts its own
  network for in-car use when STA is unavailable).
- **`uap0`** is the virtual AP interface; STA stays on `wlan0`.
  TeslaUSB runs **concurrent AP+STA** (not exclusive switching)
  so STA reconnect attempts don't take the AP down.

### Captive portal
The OS feature that pops a splash screen automatically when a device
joins a WiFi network. TeslaUSB triggers it via dnsmasq DNS spoofing
(`address=/#/<gateway-ip>`) plus a Flask blueprint
(`captive_portal.py`) that intercepts every OS's connectivity-check
URL on port 80. The web service must run on port 80 for this to
work.

### AP force mode
One of `auto`, `force_on`, `force_off`. Persisted in
`config.yaml`'s `offline_ap.force_mode`; runtime changes are
written to `/run/teslausb-ap/force.mode` and lost on reboot.

---

## UI and operations

### Quick-edit lock
The file `~/.quick_edit_part2.lock` whose presence indicates an
in-flight `quick_edit_part2` operation. 120-second stale timeout —
any process holding the lock for longer is assumed dead and the
lock is forcibly broken.

### `state.txt`
A one-line file containing the mode token (`present` or `edit`).
`mode_service.current_mode()` reads this and falls back to live
detection if the file is absent or stale.

### Failed jobs
The `/jobs` page surfaces every queue row across `archive_queue`,
`indexing_queue`, `cloud_synced_files` (with status `failed` or
`dead_letter`), and `live_event_queue`. Each row is enriched with a
**clip value** badge ("Event clip" / "Rolling buffer" / "Already on
SD card" / etc.) and a **recommendation chip** ("Retry" / "Delete" /
"Either is safe") classified from the error message.

### System Health
The `/api/system/health` endpoint and the matching UI card. Reports
disk free, RAM, queue depths, worker liveness, retention summary,
and the archive watchdog snapshot.

### Severity vs actionable
The archive watchdog's status payload carries both. **Severity** is
about diagnostics: `ok` / `warning` / `error` / `critical`.
**Actionable** is about whether the user can fix it: `true` only
when the worker is **not** running with pending work, or disk free
is below the critical threshold. The in-page banner only fires
when both `severity ∈ {error, critical}` AND `actionable == true`,
so users are never alerted about something they cannot act on.

---

## Source files

This glossary is a navigational reference; it has no single source
of truth. Each defined term cites the function or module that
implements it. When you change one of those source files in a way
that affects the term's definition, please update this glossary.
