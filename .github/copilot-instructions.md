# TeslaUSB AI Coding Guide

Focused tips to make safe changes quickly. This is a Raspberry Pi USB gadget project (dual-LUN mass storage) with strict mount/namespace rules and YAML-based configuration.

These devices run in a vehicle; power can drop at any time. Prioritize atomic writes, fsyncs, and recovery paths to avoid corruption.

## Configuration System
- **Single source of truth**: `config.yaml` at repository root contains ALL configuration (paths, credentials, network settings, limits).
- **Bash scripts**: Read YAML via `yq` using `scripts/config.sh` wrapper (auto-sources config.yaml).
  - **Optimized loading**: Single yq call with eval statement (properly quoted for security) - saves ~1.2s per invocation.
  - **Security**: All values double-quoted in eval to prevent command injection from special characters.
- **Python scripts**: Read YAML via `PyYAML` using `scripts/web/config.py` wrapper (auto-loads config.yaml).
- **Never hardcode values**: Always read from config via the wrappers. Both `config.sh` and `config.py` are thin wrappers around `config.yaml`.
- After editing `config.yaml`, restart affected services: `gadget_web.service` (web/Python changes), `wifi-monitor.service` (AP changes).

## Architecture & Modes
- Two disk images: `usb_cam.img` (part1 TeslaCam drive) and `usb_lightshow.img` (part2 LightShow/Chimes drive).
- Modes: **present** (USB gadget active, RO mounts at `/mnt/gadget/part*-ro`, Samba off) vs **edit** (gadget off, RW mounts at `/mnt/gadget/part*`, Samba on). `state.txt` holds the token; `mode_service.current_mode()` falls back to detection.
- **Boot-time fsck**: When `disk_images.boot_fsck_enabled: true` (default), `present_usb.sh` runs `fsck -p` on both drives before presenting to Tesla (~1 second total). Auto-repairs minor filesystem issues.
- Always resolve paths via `partition_service.get_mount_path/iter_all_partitions` instead of hardcoding.

## Template System
- Source templates live in `scripts/` and `templates/` with placeholders `__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__`, `__IMG_NAME__`, `__SECRET_KEY__`.
- After changing any template/script under those dirs, run `sudo ./setup_usb.sh` to substitute and deploy, then restart relevant services (e.g., `sudo systemctl restart gadget_web.service`). Never hardcode installed paths.

## Mount / Gadget Safety
- All mount/umount/mountpoint commands must be run in the PID 1 mount namespace: `sudo nsenter --mount=/proc/1/ns/mnt ...` (see `present_usb.sh`, `edit_usb.sh`).
- Switching to edit: unbind UDC first, remove gadget config, then unmount and detach loop devices; sync before and after.
- `partition_mount_service.quick_edit_part2` temporarily remounts part2 RW while in present mode; it uses `.quick_edit_part2.lock` (120s stale). Keep operations short and restore RO mount/LUN on all code paths.
- **Background subsystems must NEVER unmount/remount the USB gadget or change its LUN backing.** Tesla may be actively recording at any moment — any disruption (UDC unbind, LUN clear, RO→RW remount of part1) loses footage. The archive subsystem, indexer, file watcher, cloud sync, and live event sync are all read-only consumers. The only USB-disrupting operations are the user-initiated ones: `quick_edit_part2` (chime upload), mode switch, gadget rebind after lock chime change. Background workers yield to those via `task_coordinator`/mode-switch pause; they never *initiate* them.
- **VFS cache invalidation on the RO mount uses `echo 2 > /proc/sys/vm/drop_caches` (slabs only).** That clears the Pi's local dentry/inode cache so `readdir` sees files Tesla just wrote via the gadget; it does NOT touch the gadget binding, the loop device, or the image file. Never use `umount -l + remount` as a "cache refresh" — it would break the gadget's view.
- **Tesla writes via the gadget block layer; we read via VFS.** The two paths have no lock contention — Tesla and the Pi can read/write the same image file concurrently. The "fully written" detection (mtime + size stable for ≥ 5 s, or `IN_CLOSE_WRITE` if it fires) prevents copying a file Tesla is still writing.

## Loop Devices & USB Gadget LUNs
- **USB gadget serves image FILES directly**, not loop devices. The LUN backing file is the `.img` file path, not a `/dev/loopN` device.
- **Loop devices are for LOCAL mounting only** - they allow the Pi to mount and access the image file contents while the gadget serves the same file to the vehicle.
- **Multiple loop devices are normal**: The kernel may create 2-3 loop devices for the same image (one for local mount, others for internal gadget management). This is harmless.
- **Read-only loop devices cannot be mounted RW**: If a loop device is created with `-r` flag (read-only), you CANNOT mount it with `rw` options - the filesystem will still be read-only. Must detach and recreate without `-r`.
- **Cannot detach loop devices used by gadget**: If the gadget's LUN is backed by an image, any loop device for that image may be locked by the kernel. To safely edit, must temporarily clear the LUN backing file first.
- **quick_edit_part2 sequence**: Clear LUN1 backing → unmount RO → detach old loops → create RW loop → mount RW → do work → sync → unmount → detach → create RO loop → remount RO → restore LUN1 backing. Any shortcuts risk read-only filesystems or kernel locks.

## Web App Patterns
- Flask app under `scripts/web/`; blueprints in `scripts/web/blueprints/`; services in `scripts/web/services/` encapsulate logic (mount handling, chimes, indexing, Samba, mode).
- Mode-aware file ops: lock chimes/light shows/videos must go through services that choose RO/RW paths; avoid direct filesystem writes in view code.
- Samba cache: after edits in edit mode, call `close_samba_share()` and `restart_samba_services()` (see lock chime routes).
- **Web service runs on port 80** (not 5000) to enable captive portal functionality. The service runs as root (via systemd) to bind to privileged port 80.

## Feature Availability (Image-Gated UI)
- **Dynamic gating**: Nav items and routes are hidden/blocked when their required `.img` file doesn't exist. Uses per-request `os.path.isfile()` checks (negligible overhead, no restart needed).
- **Image path constants**: `IMG_CAM_PATH`, `IMG_LIGHTSHOW_PATH`, `IMG_MUSIC_PATH` in `config.py` — computed from `GADGET_DIR` + image name.
- **Availability function**: `partition_service.get_feature_availability()` returns a dict of boolean flags checked per request.
- **Feature-to-image mapping**:
  - `usb_cam.img` (part1) → `analytics_available`, `videos_available` (also gates cleanup sub-pages)
  - `usb_lightshow.img` (part2) → `chimes_available`, `shows_available`, `wraps_available`
  - `usb_music.img` (part3) → `music_available` (also requires `MUSIC_ENABLED`)
- **Template layer**: `get_base_context()` in `utils.py` merges availability flags into every template context. `base.html` wraps nav links in `{% if <flag> %}` guards (both desktop and mobile menus). Settings is always shown.
- **Route layer**: Each gated blueprint has a `@bp.before_request` hook that checks `os.path.isfile()` on the relevant image path. AJAX requests (`X-Requested-With: XMLHttpRequest`) get 503 JSON; normal requests redirect to Settings with a flash message.
- **Blueprints are always registered** — routes just redirect when images are missing. This keeps URL routing stable and avoids import-time checks.
- **Adding a new gated feature**: Add the image path check to `get_feature_availability()`, wrap the nav link in `base.html`, and add a `@bp.before_request` guard in the blueprint.
- **fsck.py is not gated** — it's API-only with no nav link and handles missing images internally.

## Memory Management (Pi Zero 2 W)
- **Desktop services disabled**: pipewire, wireplumber, colord masked (saves ~30MB RAM).
- **Persistent swap**: 1GB swap file at `/var/swap/fsck.swap` in /etc/fstab.
- **Setup optimization**: `optimize_memory_for_setup()` disables lightdm, enables swap before package install.
- **Watchdog**: Hardware watchdog configured (90s timeout, `max-load-1=24`, `max-load-5=16`). The 90s timeout (was 60s) gives headroom for transient SDIO bus contention — the Pi Zero 2 W shares one SDIO controller between the SD card and WiFi chip, and a heavy archive catch-up can briefly stall the watchdog daemon. If you change defaults, edit both `setup_usb.sh` and `readme.md`.
- **Kernel panic**: Auto-reboot after 10 seconds (sysctl kernel.panic=10).

## Lock Chimes & Light Shows
- Lock chime rules: WAV <1 MiB, 16-bit PCM, 44.1/48 kHz, mono/stereo. `lock_chime_service` validates, can reencode via ffmpeg, and replaces `LockChime.wav` with temp+fsync+MD5.
- Present-mode uploads and set-active use `quick_edit_part2` to minimize RW time; honor the lock and timeouts. Keep copies/renames atomic and verified.
- **Boot optimization**: `select_random_chime.py` detects boot RW mount at `/mnt/gadget/part2` and passes `skip_quick_edit=True` to `set_active_chime()` to avoid unnecessary mount/unmount cycles (reduces boot time by ~6s).
- **Tesla cache invalidation**: Tesla caches USB file contents and won't detect changes unless the USB device is re-enumerated. After replacing `LockChime.wav`, MUST unbind/rebind the USB gadget (see `partition_mount_service.rebind_usb_gadget()`). This simulates unplug/replug and forces Tesla to clear cache and re-scan the drive. The `set_active_chime()` function handles this automatically in present mode.

## Key Workflows
- Switch modes: `sudo /home/pi/TeslaUSB/present_usb.sh` or `edit_usb.sh`; check `state.txt`.
- Logs: `sudo journalctl -u gadget_web.service -f`; scheduler `chime_scheduler.service`; monitor quick-edit lock at `~/.quick_edit_part2.lock`.
- Manual web run: `cd /home/pi/TeslaUSB && python3 web_control.py` (use configured paths after setup).

## Services & Timers
- `gadget_web.service` (Flask UI), `present_usb_on_boot.service` (enable gadget on boot), `chime_scheduler.timer`, `wifi-monitor.service`, `watchdog.service` (hardware watchdog).

## Offline Access Point
- Three force modes: `auto` (default, AP starts when WiFi fails), `force_on` (AP always on), `force_off` (AP blocked, never starts).
- Force mode configured in `config.yaml` under `offline_ap.force_mode`.
- Runtime force mode stored in `/run/teslausb-ap/force.mode`; on boot, `wifi-monitor.sh` initializes runtime file from config.yaml.
- Web UI "Start AP Now" sets `force_on`; "Stop AP" sets `auto` (returns to auto behavior).
- **Note**: Runtime changes to force mode only persist until reboot. To make permanent changes, edit `config.yaml`.
- AP runs concurrently with WiFi client on virtual interface `uap0`; WiFi client stays active on `wlan0`.

## WiFi Roaming
- **Mesh/Extender support**: Configured to automatically switch between access points with the same SSID for optimal signal strength.
- **NetworkManager configuration**: `/etc/NetworkManager/conf.d/wifi-roaming.conf` disables power save (wifi.powersave=2), enables MAC randomization, and performs frequent connectivity checks (every 60 seconds).
- **Power save disabled**: Keeping WiFi power save off (`wifi.powersave=2`) is THE MOST CRITICAL setting for responsive roaming and fast scanning. This is more important than any other roaming parameter.
- **wpa_supplicant management**: NetworkManager manages wpa_supplicant automatically via D-Bus (`-u -s` flags). It does NOT use `/etc/wpa_supplicant/wpa_supplicant.conf` files.
- **Background scanning**: NetworkManager uses hardcoded bgscan parameters (`simple:30:-65:300` - scans every 30s when signal < -65 dBm). These cannot be overridden via configuration files.
- **Setup**: Automatically configured by `setup_usb.sh` during installation - creates NetworkManager roaming config only.
- **No BSSID lock**: Connections must not have BSSID locked to allow roaming between access points.
- **Automatic switching**: Device scans for better access points when signal is weak; wpa_supplicant handles the actual roaming decision based on signal strength, link quality, and auth compatibility.
- **Signal threshold**: -65 dBm threshold triggers aggressive scanning to find stronger access points (NetworkManager default).
- **Connectivity checks**: NetworkManager checks connection health every 60 seconds to detect issues early and potentially trigger reconnection.

## Captive Portal
- **DNS spoofing**: dnsmasq configured with `address=/#/<gateway-ip>` to redirect all DNS queries to the AP gateway.
- **Captive portal detection**: Flask blueprint (`scripts/web/blueprints/captive_portal.py`) intercepts OS-specific connectivity check URLs (Apple `/hotspot-detect.html`, Android `/generate_204`, Windows `/connecttest.txt`, etc.).
- **Splash screen**: Custom branded HTML template (`scripts/web/templates/captive_portal.html`) displays Tesla USB Gadget features with "Access Web Interface" button.
- **Port 80 requirement**: Web service must run on port 80 (standard HTTP) for automatic captive portal detection on all devices. No iptables redirects needed.
- **Automatic trigger**: When devices connect to TeslaUSB WiFi, they detect the captive portal and automatically open the splash screen without user typing any URL.

## UI/UX Design System

All frontend changes **must** follow the design system documented in [`docs/UI_UX_DESIGN_SYSTEM.md`](../docs/UI_UX_DESIGN_SYSTEM.md). Key rules:

- **Progressive disclosure**: Simple by default (Layer 1–2), advanced features behind deliberate actions (Layer 3). Casual users see a clean interface; power users access everything within 2 taps.
- **No emoji icons**: Use Lucide SVG icons (`map-pin`, `video`, `bell`, `music`, etc.). Emojis render inconsistently and are not accessible.
- **Color tokens only**: Never hardcode hex values — use CSS custom properties (`--bg-primary`, `--accent-success`, etc.) so dark/light mode works automatically.
- **Dark and light mode**: Both must work. Test both before merging. Use `[data-theme="dark"]` CSS selectors. Map/video overlays always use dark background.
- **Touch targets**: Minimum 44×44px for all interactive elements.
- **Mobile-first**: Bottom tab bar (<1024px), left sidebar rail (≥1024px). Tables convert to card lists on mobile. Test at 375px and 1024px+.
- **No "Edit Mode" / "Present Mode" in UI**: These are internal implementation details. The user-facing concept is "Network File Sharing" (Samba). All write operations (upload, delete, set active) auto-switch via `quick_edit` transparently. Show a status dot (green = normal, amber = sharing active), not a mode toggle.
- **Performance**: Bundle fonts locally (Inter WOFF2), no external CDN calls, no JS frameworks, inline critical CSS. This runs on a Pi Zero 2 W with 512MB RAM — every byte matters.
- **Accessibility**: WCAG AA contrast, visible focus rings, `aria-label` on icon buttons, `prefers-reduced-motion` respected, semantic HTML.

See the full design system for color palettes, typography scale, spacing tokens, component specs, responsive breakpoints, and the pre-merge checklist.

## Boot Priority
- **USB gadget presentation is the #1 priority at boot.** Tesla must see the USB drive within ~3 seconds. All other tasks (cleanup, chime selection, indexing, cloud sync) are deferred to background services that run AFTER the gadget is bound.
- `present_usb_on_boot.service` calls `present_usb.sh` directly — no cleanup wrapper.
- `teslausb-deferred-tasks.service` handles post-boot cleanup (via quick_edit) and random chime selection. It does **not** drive indexing or cloud sync — those run inside `gadget_web.service` (indexing worker thread + cloud archive worker) which starts in parallel.
- Never add blocking work before the UDC bind in `present_usb.sh`. Even RO local mounts happen AFTER the gadget is presented to Tesla.

## Video Panel (Map-Integrated)
- **There is no standalone Videos page.** All video browsing happens in the map page (`mapping.html`) via a slide-out side panel with three tabs: "Events", "Trips", and "All Clips".
- **Events tab**: Sentry/dashcam events sorted most-recent-first with type icons. Sentry entries play directly from the list (no map route exists for stationary events).
- **Trips tab**: Browse trips with per-clip Play / Download ZIP / Delete actions.
- **All Clips tab**: Unified list of every clip across sources. If geolocation data exists, the user plays from the map route; otherwise the list entry exposes a play button.
- **All sources included**: TeslaCam USB folders (SentryClips, SavedClips, RecentClips) AND ArchivedClips on the SD card.
- **No thumbnails**: Thumbnail generation code has been removed. Video entries show metadata (date, event type, duration, cameras) but no preview images.
- **Overlay player** (`openVideoOverlay()` in `mapping.html`):
  - Camera switcher uses directional Lucide SVG icons (no emoji) and equal-width buttons; full labels including "L Pillar"/"R Pillar" must fit at the default 480px overlay width — keep `gap` and per-button padding tight if adding cameras.
  - Native `<video>` fullscreen + PiP controls are hidden (`controlslist="nofullscreen" disablepictureinpicture`); all fullscreen flows go through the nav-row buttons so the affordances stay consistent.
  - Two fullscreen affordances are intentional and distinct:
    - **Fullscreen** (`icon-maximize`, four-corner) calls `requestFullscreen()` on the `.video-overlay-stage` wrapper (NOT the bare `<video>`) so the `.overlay-hud` telemetry overlay rides into the OS fullscreen layer with the video.
    - **Maximize** (`icon-maximize-2`, diagonal arrows) toggles a `.maximized` class on `.video-overlay` to fill the browser viewport with HUD/header/cam-switcher/nav still visible.
  - **iOS Safari limitation**: `webkitEnterFullscreen` only works on `<video>` elements and the iOS native player cannot have HTML overlaid, so the HUD is not visible in OS fullscreen on iPhone/iPad. Use Maximize instead.
- **Disambiguation popup**: When the user clicks the map at a location with multiple overlapping clips (e.g., a road driven multiple times in one day or across days), `mapping.html` opens a chooser listing each clip with its trip date/time so the right clip can be selected before the overlay player is launched.

## Cloud Sync Architecture
TeslaUSB has **two separate cloud upload subsystems** that share one rclone provider config but otherwise operate independently. **Never merge them or have one call into the other's worker** — the priority/coordination contract below is what guarantees real-time event uploads aren't blocked by bulk catch-up sync.

### `cloud_archive_service` — bulk catch-up sync
- **Queue-based continuous sync**: A persistent sync queue (SQLite, `cloud_sync.db`) is populated by the inotify file watcher, WiFi connect handler, and manual "Archive to Cloud" actions. A sync worker thread processes items one at a time.
- **Priority order (default)**: (1) Oldest videos with Tesla events (from event.json), (2) Oldest trip videos with geolocation (from geodata.db), (3) Non-event/non-geo videos (opt-in, disabled by default via `cloud_archive.sync_non_event_videos: false`).
- **Detection**: event.json for folder-level classification + SEI telemetry for fine-grained event detection.
- **Power-loss safe**: File marked as `synced` only after rclone confirms upload + DB commit + fsync. Partially uploaded files detected on restart and re-queued.
- **Low impact**: `nice -n 19` + `ionice -c3` on rclone, bandwidth limit (`max_upload_mbps`), one file at a time, inter-file sleep. Web UI must remain responsive during sync.
- **Keeps going**: Sync worker idles only when queue is empty AND inotify reports no new files. On WiFi reconnect, immediately re-checks queue.
- **Yields to LES**: between every file, `_run_sync` checks `live_event_queue` for pending rows; if found it releases the `task_coordinator` lock, sleeps briefly, then re-acquires. `trigger_auto_sync()` skips entirely when LES has pending work. **Both checks are O(1) indexed `SELECT 1`** — sub-millisecond, no measurable impact.

### `live_event_sync_service` (LES) — real-time per-event uploader
- **Trigger**: Tesla's `event.json` arrival in `SentryClips/` or `SavedClips/`. Detected by the same `file_watcher_service` inotify (no second watcher) via `register_event_json_callback`.
- **Queue**: dedicated `live_event_queue` table in the same `cloud_sync.db`. Schema: `id, event_dir, event_json_path, event_timestamp, event_reason, upload_scope, status, enqueued_at, uploaded_at, attempts, last_error, next_retry_at`. **Sharing the DB but not the table** keeps backups simple and never crosses queues.
- **Worker**: ONE dedicated thread, idle-on-event semantics. Blocks on `threading.Event.wait()` when queue is empty (< 0.1% CPU). Wakes on (a) enqueue callback or (b) NM dispatcher's `/api/live_events/wake`.
- **File selection**: `event_minute` (default — 6 cameras × 1 minute centered on `event.json.timestamp`) or `event_folder` (everything in the event dir). Configured per-instance via `live_event_sync.upload_scope`.
- **Priority over cloud_archive**: When LES has work AND WiFi is up, cloud_archive yields between files (see above). The NM WiFi-connect dispatcher path wakes LES BEFORE triggering cloud sync, and waits up to 10 minutes for the LES queue to drain before kicking cloud sync.
- **WiFi-aware**: when WiFi is down, queue grows but worker idles. On reconnect, drains immediately. Persistent across reboots — `uploading` rows reset to `pending` on startup recovery.
- **Failure handling**: per-row `attempts` + `next_retry_at` with backoff `[30s, 120s, 300s, 900s, 3600s]`. After `retry_max_attempts` (default 5), row is `failed`; `POST /api/live_events/retry/<id>` resets it.
- **Resource ceilings**: ≤ 25 MB RSS steady, ≤ 60 MB peak during upload. No heavy imports (`sqlite3`, `os`, `subprocess`, `threading`, `json`, `re`, `time`, `urllib.request` only — NO `cv2`/`av`/`PIL`/`numpy`/`requests`). One rclone subprocess at a time, never stacked with cloud_archive (coordinated via `task_coordinator` keys `'cloud_sync'` and `'live_event_sync'`).
- **Opt-in**: `live_event_sync.enabled: false` by default. When disabled, the watcher callback is never registered, the worker never starts, and the API returns `{"enabled": false}`.

### Coordination contract (don't break this)
- **`task_coordinator` is the single mutual-exclusion point.** Adding parallel rclone subprocesses or a second cloud worker that doesn't go through `acquire_task` will overlap uploads and starve the gadget endpoint.
- **LES never touches `cloud_synced_files`**, and cloud_archive never touches `live_event_queue`. Each owns its table.
- **The shared rclone helpers** (`upload_path_via_rclone`, `write_rclone_conf`, `remove_rclone_conf`, `load_provider_creds`, `is_wifi_connected`, `RCLONE_MEM_FLAGS`) live in `cloud_archive_service` and are imported by LES. **Don't duplicate them** — fix-once vs. fix-twice.
- **The NM dispatcher (`refresh_cloud_token.py`) is the single WiFi-connect entry point.** It refreshes the RO mount → wakes LES → waits for archive timer → waits for indexing queue → waits for LES drain (capped) → triggers cloud_archive. Adding a second WiFi-up trigger would race the queues.

## Video Indexing
- **Single SQLite-backed queue + one worker.** All video indexing flows through `indexing_queue` (in `geodata.db`, schema v6). One low-priority background thread (`indexing_worker.py`, started inside `gadget_web.service`) drains the queue one file at a time. **Never re-introduce parallel triggers** — the old design had 6 redundant paths (every page load, every mode switch, every WiFi connect, etc.) that caused the constantly-flashing "Indexing…" banner.
- **Producers** (the only legal ways to add work to the queue):
  - **Boot catch-up scan** — `mapping_service.boot_catchup_scan()` runs once at `gadget_web` start; cheap directory walk + batch INSERT, no parsing.
  - **inotify file watcher** — `file_watcher_service.py` callback calls `enqueue_many_for_indexing()` on `IN_CREATE` / `IN_MOVED_TO`.
  - **Archive run** — `video_archive_service` enqueues each newly archived clip with a short defer.
  - **Manual reindex** — `POST /api/index/trigger` (single file) and `POST /api/index/rebuild` (full rebuild).
- **Dedup is the queue's job**, not the producers'. `mapping_service.canonical_key(path)` resolves both the SD-card and USB-RO views of the same file to the same key. `enqueue_*` is idempotent — duplicate enqueues are no-ops.
- **Outcomes are structured.** The worker calls `mapping_service.index_single_file(file_path, db_path, teslacam_root)` which returns `IndexResult(outcome=IndexOutcome.X, ...)`. `IndexOutcome` enumerates every terminal/retry state — `INDEXED`, `ALREADY_INDEXED`, `DUPLICATE_UPGRADED`, `NO_GPS_RECORDED`, `NOT_FRONT_CAMERA`, `TOO_NEW`, `FILE_MISSING`, `PARSE_ERROR`, `DB_BUSY`. Add new branches there, not via stringly-typed return values. `_TERMINAL_OUTCOMES` lists which delete the queue row vs. which retry with backoff.
- **Mutual exclusion via `task_coordinator` fairness model.** Worker calls `acquire_task('indexer', yield_to_waiters=True)` so its tight cycle does not starve less frequent priority tasks (archive). The worker releases the lock BEFORE all sleeps (idle, inter-file, backoff) — never holds it across `_stop_event.wait()`. Periodic priority tasks like archive use `acquire_task('archive', wait_seconds=60.0)` to block-wait. Archive duplicate-trigger guard is `_archive_pending` (set BEFORE `acquire_task`, cleared in outer `finally`). See `task_coordinator.py` docstring for the full fairness contract.
- **Banner truth.** `/api/index/status` reports `active_file` (file currently being parsed), `queue_depth`, `claimed_count`, `dead_letter_count`, `paused`, `last_outcome`, `worker_running`. The UI shows the banner **only** when `active_file != null`. Don't add UI heuristics that flash the banner based on queue depth alone.
- **Pause for mode switches.** `pause_worker()` / `resume_worker()` bracket any RW remount or quick_edit; the worker drops its current claim cleanly so RO/RW transitions never race the parser.
- **Power-loss safe.** Claims auto-expire (claimed_at older than the stale threshold get re-claimed by the next worker startup). Permanent failures move to dead-letter (`dead_letter_count`); they don't keep retrying forever.
- **Daily stale scan** (`start_daily_stale_scan()`) sweeps `indexed_files` rows whose source file no longer exists; cheap (one `os.path.isfile` per row) and runs ~daily with jitter. **It MUST never delete trips, waypoints, or detected_events.** When `purge_deleted_videos` finds an orphaned `indexed_files` row, it deletes only that row and NULLs `video_path` on the related waypoints/events — the GPS history and event detections are real records of the user's drive that survive video loss. Cascade-deleting trips because a clip was rotated out caused the May 7 McDonalds-trip data loss; the rule is now: trips are sacred, only an explicit user "Delete Trip" action may remove them.

## File Watcher (inotify)
- `file_watcher_service.py` monitors USB RO mount + ArchivedClips using `watchdog` library.
- On new file: enqueues into `indexing_queue` (via `enqueue_many_for_indexing`) and notifies the cloud sync producer. Both consumers dedup by canonical key.
- **Two callback types** (subscribed independently): `register_callback(cb)` for `.mp4` arrivals (consumed by the indexing producer) and `register_event_json_callback(cb)` for Tesla `event.json` arrivals (consumed by Live Event Sync). The mp4 callback uses a 60-second age gate; the event_json callback fires immediately because Tesla writes event.json atomically as the last file in the event dir.
- Falls back to 5-minute polling if inotify unavailable (mount changes, etc.). Both callback types fire from the polling path too.
- Must be memory-efficient: watch directory-level events only (IN_CREATE, IN_MOVED_TO, IN_CLOSE_WRITE for event.json).

## Live Event Sync (LES) — Real-Time Event Uploader
LES is a **separate, opt-in subsystem** that uploads Sentry and Saved events to the cloud the moment Tesla writes them. It runs **alongside** `cloud_archive_service`, not inside it. See **Cloud Sync Architecture** above for the full coordination contract; key points for working in this codebase:

- **Disabled by default** (`live_event_sync.enabled: false`). When disabled, the watcher callback is never registered, no thread starts, and the API returns `{"enabled": false}`. **Existing installs see zero behavior change.**
- **Service**: `scripts/web/services/live_event_sync_service.py`. Public API: `start()`, `stop()`, `wake()`, `enqueue_event_json(paths)`, `enqueue_event_dir(dir, event_json)`, `get_status()`, `list_queue(limit)`, `retry_failed(id_or_None)`. **Don't reach inside the module — use these functions.**
- **Blueprint**: `scripts/web/blueprints/live_events.py`, mounted at `/api/live_events/{status,queue,retry/<id>,retry_all,wake}`. All routes are JSON-only and image-gated on `IMG_CAM_PATH`.
- **NM dispatcher integration**: `helpers/refresh_cloud_token.py` calls `/api/live_events/wake` BEFORE waiting for the index drain or triggering cloud sync, then waits up to 10 min for `/api/live_events/status` to show `pending+uploading == 0` before kicking cloud_archive. Don't reorder these steps — the priority contract depends on them.
- **Queue table**: `live_event_queue` in `cloud_sync.db`. `_startup_recovery()` resets stale `uploading` rows back to `pending` on every start; `_prune_old_uploaded()` evicts rows older than 7 days to keep the table ≤ 200 KB.
- **File selection**: `select_event_files(event_dir, mode, timestamp)` — `event_minute` (default) matches the 6 cameras for the timestamp's `YYYY-MM-DD_HH-MM` prefix; `event_folder` selects everything in the dir. **Add new modes here, not in the worker.**
- **Daily data cap**: `live_event_sync.daily_data_cap_mb` (0 = unlimited). When exceeded, the worker idles until the local clock crosses midnight (cheap `_today_uploaded_bytes()` SUM over `uploaded_at` rows).
- **Webhook**: `live_event_sync.notify_webhook_url` — fired via `urllib.request` (no `requests` import) on successful upload. Don't add HTTP libraries here.
- **Resource budget is enforced by code review, not by tooling** — every new dependency, thread, or in-memory cache in this module needs a justification. The Pi Zero 2 W has no headroom for "just one more" library.


## Safety & Stability
- **SSH is sacred**: sshd has a systemd drop-in preventing it from being stopped or masked. Safe-mode boot detection skips TeslaUSB services after 3+ reboots in 10 minutes.
- **IMG files are never deleted**: `is_protected_file()` guard in all code paths that delete files. `*.img` files in GADGET_DIR are always refused deletion.
- **RecentClips preservation**: Archive timer runs every 2 minutes regardless of WiFi state, copying clips to SD card before Tesla's circular buffer overwrites them.
- **WiFi always reconnects**: wifi-monitor.sh uses adaptive check intervals (20s when searching, 60s when connected), always tries to rejoin configured SSID even when AP is active.

## Pitfalls to avoid
- Skipping `nsenter` for mounts (mounts vanish after subprocess exit).
- Unbinding/mount order wrong when leaving present mode (causes busy unmounts).
- Editing templates without rerunning `setup_usb.sh` (placeholders stay unexpanded).
- Long quick-edit operations holding the lock and leaving LUN unbound on failure; ensure cleanup paths restore RO mount and gadget backing.
- Modifying AP force mode without persisting to config.sh (state lost on reboot); always use `ap_control.sh` or `ap_service.ap_force()`.
- Adding a new blueprint route without a `before_request` image guard when the feature depends on a disk image (users hit crashes or empty pages).
- Using emoji icons in templates or UI elements (use Lucide SVG icons instead).
- Hardcoding color hex values instead of using CSS custom property tokens.
- Exposing "Edit Mode" / "Present Mode" terminology to users in the UI.
- Skipping mobile testing — all pages must work at 375px viewport width.
- **Installing dependencies or files into the git repo** that are not part of the application (see below).
- Adding blocking work before UDC bind in `present_usb.sh` (delays USB presentation to Tesla).
- Generating or referencing video thumbnails (thumbnail system has been removed).
- Creating a standalone Videos page (all video browsing is in the map page panel).
- Deleting or overwriting `*.img` files in GADGET_DIR from any code path.
- Running rclone without `nice`/`ionice` (starves the web server and gadget).
- **Removing or weakening the archive worker's load-pause / inter-file-sleep guards.** The Pi Zero 2 W's SDIO controller is shared between the SD card and the Broadcom WiFi chip; tight back-to-back archive copies (especially during catch-up of a 1000+ clip backlog) can saturate the bus, starve the userspace `watchdog` daemon, and trigger a hardware reset. The `archive_queue.inter_file_sleep_seconds` (default 1.0 s), `load_pause_threshold` (3.5), `load_pause_seconds` (30), and `boot_scan_defer_seconds` (30) defaults are all calibrated against this failure mode — don't lower them without re-validating on hardware. Symptom: watchdog reboot ~3 minutes into a service restart with `mmc1: Controller never released inhibit bit(s)` and `brcmfmac: brcmf_sdio_read_control: ... failed: -5` in the kernel log immediately before the reset.
- Marking a file as `synced` in the cloud database before rclone confirms the upload completed.
- Letting `cloud_archive_service` upload a Sentry/Saved event clip when LES has a pending row for that event (cloud_archive yields between files; don't remove or weaken the inter-file `live_event_queue` check).
- Calling LES from inside `cloud_archive_service` (or vice versa) — they're separate subsystems with separate enable flags. Cross-call only via the documented coordination points (`task_coordinator`, the inter-file LES-pending check, and the `/api/live_events/wake` endpoint).
- Importing `cv2`/`av`/`PIL`/`numpy`/`requests` inside `live_event_sync_service.py` — blows the 25 MB steady-state RSS budget on Pi Zero 2 W.
- Adding a second inotify watcher for LES (use `register_event_json_callback` on the existing one — zero additional file descriptors).
- Stacking rclone subprocesses (one at a time, ever, across both cloud_archive and LES — coordinated via `task_coordinator`).
- Re-introducing redundant indexing triggers (every page load, every mode switch, every WiFi connect, full filesystem walks). The indexing worker is the SINGLE consumer of `indexing_queue`; producers only enqueue. Adding parallel "trigger_auto_index"-style code paths is what caused the constantly-flashing banner the redesign removed.
- Flashing the indexing banner based on queue depth instead of `active_file`. The user only wants to know when a file is *actively being parsed*, not when items are queued.
- Calling `requestFullscreen()` on the bare `<video>` element in `mapping.html` — the `.overlay-hud` is a sibling DOM node, so it drops out of the OS fullscreen layer. Always fullscreen the `.video-overlay-stage` wrapper instead so the HUD rides along.
- Removing `controlslist="nofullscreen" disablepictureinpicture` from the overlay `<video>` — exposes the native fullscreen button (which fullscreens just the video and hides the HUD), which contradicts the dedicated nav-row Fullscreen button and the HUD-visible architecture.
- **Background subsystems unmounting or rebinding the USB gadget.** Archive, indexer, file watcher, cloud sync, LES — all read-only. Even a "harmless" `umount -l + remount` of `/mnt/gadget/part1-ro` to refresh VFS cache loses footage if Tesla is recording. Use `drop_caches=2` (slab only) for cache refresh; never remount.
- **Using `(julianday(a) - julianday(b)) * 86400` for second-gap math in SQLite.** Returns 300.0000223 for true 300 s gaps due to float precision and silently fails `<= 300` boundary checks. Always use `CAST(strftime('%s', x) AS INTEGER)` for exact integer-second arithmetic. Caused trip-merge fragmentation in PR #78.
- **Holding the `task_coordinator` lock across sleeps in cyclic workers.** A worker that does `acquire → work → sleep → release` blocks priority tasks during the sleep. Always `acquire → work → release → sleep`. The indexer was rewritten in PR #78 to enforce this — caused real production data loss before the fix.
- **Calling `_run_archive` (or any archive entry point) without going through `trigger_archive_now`.** The `_archive_pending` flag is set inside `trigger_archive_now`; bypassing it can spawn duplicate archive threads when `acquire_task('archive', wait_seconds=60.0)` is blocking.
- **Indexing files directly from the RO USB mount (`/mnt/gadget/part1-ro/TeslaCam/RecentClips/...`).** Tesla rotates RecentClips at the 1-hour mark. A file the indexer is mid-parse can disappear, causing `FILE_MISSING` errors and broken map entries. Index only from `~/ArchivedClips` on the SD card, after the archive subsystem has copied the file there.
- **Cascade-deleting trips/waypoints/events from `purge_deleted_videos`.** A trip is real driving that happened; losing the dashcam clip doesn't unhappen the drive. Stale-scan, watcher delete, and cleanup callers all go through `purge_deleted_videos`, which now ONLY deletes the orphan `indexed_files` row and NULLs `waypoints.video_path` / `detected_events.video_path`. Reintroducing a `DELETE FROM trips/waypoints/detected_events` in this code path would re-cause the May 7 trip-loss regression. Explicit user "Delete Trip" actions belong in a separate code path, not in filesystem reconciliation.
- **Re-running the stale-scan cadence aggressively without pinning the trip-preservation contract.** PR #80 moved the first stale-scan from "6 hours after boot" to "5–10 min after boot" so orphans get cleaned promptly. That schedule is fine — but only because `purge_deleted_videos` is now safe. If you ever shorten the cadence further or add a "scan on every event," double-check that the function still preserves trips/waypoints/events.
- **`git stash -u` during deploy can capture untracked production data.** If `geodata.db`, `cloud_sync.db`, `state.txt`, or any `*.bak.*` file isn't in `.gitignore`, a stash + later branch switch can lose it. The `.gitignore` was hardened in commit `e5fc297` to permanently block all runtime data files (`*.db*`, `state.txt`, `tesla_salt.bin`, `*.log`, `fsck_status.json`, `cleanup_config.json`, `config.yaml.bak.*`, `*.key`, `*.pem`). Don't ever commit these — `git stash -u` should be a no-op for them.
- **Running `setup_usb.sh` to deploy a single template change.** It has interactive prompts that hang on closed stdin and re-runs many steps unnecessarily. For one-file template updates, sed-substitute the placeholders manually (`__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__`) and `systemctl daemon-reload` — much faster and avoids prompt hangs.
- **Retrying WiFi STA reconnect on a fixed interval when the AP is up** (the May 11 2026 crash root cause). The Pi Zero 2 W's Broadcom chip lives on the same SDIO bus as the SD card; each STA-retry stops the AP (`brcmf_cfg80211_stop_ap`) which is a heavy SDIO write. Forensic logs showed seven `stop_ap failed -52 (ENETUNREACH)` clusters in 16 minutes leading up to the chip lockup — combined with sustained archive SD reads, the controller missed the 90-second hardware watchdog ping and reset. `wifi-monitor.sh` MUST use exponential backoff (2.5 → 5 → 15 → 30 min cap) on consecutive failures and reset to the floor on success. Never reintroduce a fixed-interval retry loop while the AP is active. **Symptom:** unclean-shutdown journal files (`system@*.journal~`) accumulating without OOM in `dmesg`, plus repeated `brcmf_cfg80211_stop_ap failed -52` in the kernel log.
- **Logging routine cyclic-worker activity at INFO.** `task_coordinator` previously emitted one acquire and one release line per worker per cycle (~2 lines/sec when the indexer queue was empty), bloating the journal, driving SD-card writes, and making `journalctl` unusably slow under load. Per-cycle acquire/release is DEBUG-only; emit one INFO summary per worker per minute (`acquires=N, longest_hold=Xs`). The same rule applies to any new cyclic worker — don't log per-tick events at INFO unless the human reading the journal could act on each line.
- **Loading entire MP4 files into a `bytes` buffer for parsing.** `sei_parser.extract_sei_messages` uses `mmap.mmap(..., access=ACCESS_READ)` on the file descriptor — slicing/indexing semantics are identical to bytes, so the existing helpers (`_find_box`, `_decode_sei_nal`, `_get_timescale_and_durations`) operate unchanged. The kernel pages 4 KB chunks in on demand and evicts under pressure, keeping resident memory bounded by the I/O pattern (~200 KB for a sequential walk) regardless of file size. Reverting to `data = f.read()` would re-introduce 30-80 MB RSS spikes per parse and was a documented OOM contributor. Both the file descriptor and the mapping MUST be released in a `finally` block to handle GeneratorExit (early generator abandon via `.close()` or GC).

## AI & Testing Workspace Rules
- **Never install packages, dependencies, or node_modules inside the git repo.** Test tooling (Playwright, npm packages, debug scripts) must live **outside** the repository — use `../playwright-test/` or another folder above the repo root.
- **Never create temporary test scripts, debug files, or scratch files inside the repo.** If you need a test script, put it in the parent directory or a temp folder.
- **The git working tree must stay clean.** After any task, `git status` should show only intentional changes. No untracked test artifacts, no `package.json`, no `node_modules/`, no screenshot dumps.
- **Playwright MCP artifacts** (`.playwright-mcp/`) are already gitignored but should also be cleaned up at end of session.
- **Python `__pycache__/`** directories are gitignored — never commit `.pyc` files.
