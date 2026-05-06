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
- **Watchdog**: Hardware watchdog configured (15s timeout, monitors load/memory).
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
- **There is no standalone Videos page.** All video browsing happens in the map page (`mapping.html`) via a slide-out side panel with two tabs: "Recent Events" and "All Clips".
- **Recent Events tab**: Shows trips with events and sentry clips, sorted most-recent-first. Trip entries have NO play button (user plays from the map route). Sentry entries HAVE a play button (no map route for stationary events).
- **All Clips tab**: Unified list of every trip, sentry event, and clip. If geolocation data exists, show on map and play from there (no play button in list). If no geolocation, show play button in the list entry.
- **All sources included**: TeslaCam USB folders (SentryClips, SavedClips, RecentClips) AND ArchivedClips on the SD card.
- **No thumbnails**: Thumbnail generation code has been removed. Video entries show metadata (date, event type, duration, cameras) but no preview images.

## Cloud Sync Architecture
- **Queue-based continuous sync**: A persistent sync queue (SQLite) is populated by the inotify file watcher, WiFi connect handler, and manual "Archive to Cloud" actions. A sync worker thread processes items one at a time.
- **Priority order (default)**: (1) Oldest videos with Tesla events (from event.json), (2) Oldest trip videos with geolocation (from geodata.db), (3) Non-event/non-geo videos (opt-in, disabled by default via `cloud_archive.sync_non_event_videos: false`).
- **Detection**: event.json for folder-level classification + SEI telemetry for fine-grained event detection.
- **Power-loss safe**: File marked as `synced` only after rclone confirms upload + DB commit + fsync. Partially uploaded files detected on restart and re-queued.
- **Low impact**: `nice -n 19` + `ionice -c3` on rclone, bandwidth limit (`max_upload_mbps`), one file at a time, inter-file sleep. Web UI must remain responsive during sync.
- **Keeps going**: Sync worker idles only when queue is empty AND inotify reports no new files. On WiFi reconnect, immediately re-checks queue.

## Video Indexing
- **Single SQLite-backed queue + one worker.** All video indexing flows through `indexing_queue` (in `geodata.db`, schema v6). One low-priority background thread (`indexing_worker.py`, started inside `gadget_web.service`) drains the queue one file at a time. **Never re-introduce parallel triggers** — the old design had 6 redundant paths (every page load, every mode switch, every WiFi connect, etc.) that caused the constantly-flashing "Indexing…" banner.
- **Producers** (the only legal ways to add work to the queue):
  - **Boot catch-up scan** — `mapping_service.boot_catchup_scan()` runs once at `gadget_web` start; cheap directory walk + batch INSERT, no parsing.
  - **inotify file watcher** — `file_watcher_service.py` callback calls `enqueue_many_for_indexing()` on `IN_CREATE` / `IN_MOVED_TO`.
  - **Archive run** — `video_archive_service` enqueues each newly archived clip with a short defer.
  - **Manual reindex** — `POST /api/index/trigger` (single file) and `POST /api/index/rebuild` (full rebuild).
- **Dedup is the queue's job**, not the producers'. `mapping_service.canonical_key(path)` resolves both the SD-card and USB-RO views of the same file to the same key. `enqueue_*` is idempotent — duplicate enqueues are no-ops.
- **Outcomes are structured.** The worker calls `mapping_service.index_single_file(file_path, db_path, teslacam_root)` which returns `IndexResult(outcome=IndexOutcome.X, ...)`. `IndexOutcome` enumerates every terminal/retry state — `INDEXED`, `ALREADY_INDEXED`, `DUPLICATE_UPGRADED`, `NO_GPS_RECORDED`, `NOT_FRONT_CAMERA`, `TOO_NEW`, `FILE_MISSING`, `PARSE_ERROR`, `DB_BUSY`. Add new branches there, not via stringly-typed return values. `_TERMINAL_OUTCOMES` lists which delete the queue row vs. which retry with backoff.
- **Mutual exclusion.** Worker calls `task_coordinator.acquire_task('indexer')` before each item to coordinate with the `archive` and `cloud_sync` tasks. If the lock is busy it sleeps briefly and retries.
- **Banner truth.** `/api/index/status` reports `active_file` (file currently being parsed), `queue_depth`, `claimed_count`, `dead_letter_count`, `paused`, `last_outcome`, `worker_running`. The UI shows the banner **only** when `active_file != null`. Don't add UI heuristics that flash the banner based on queue depth alone.
- **Pause for mode switches.** `pause_worker()` / `resume_worker()` bracket any RW remount or quick_edit; the worker drops its current claim cleanly so RO/RW transitions never race the parser.
- **Power-loss safe.** Claims auto-expire (claimed_at older than the stale threshold get re-claimed by the next worker startup). Permanent failures move to dead-letter (`dead_letter_count`); they don't keep retrying forever.
- **Daily stale scan** (`start_daily_stale_scan()`) sweeps `indexed_files` rows whose source file no longer exists; cheap (one `os.path.isfile` per row) and runs ~daily with jitter.

## File Watcher (inotify)
- `file_watcher_service.py` monitors USB RO mount + ArchivedClips using `watchdog` library.
- On new file: enqueues into `indexing_queue` (via `enqueue_many_for_indexing`) and notifies the cloud sync producer. Both consumers dedup by canonical key.
- Falls back to 5-minute polling if inotify unavailable (mount changes, etc.).
- Must be memory-efficient: watch directory-level events only (IN_CREATE, IN_MOVED_TO).

## Safety & Stability
- **SSH is sacred**: sshd has a systemd drop-in preventing it from being stopped or masked. Safe-mode boot detection skips TeslaUSB services after 3+ reboots in 10 minutes.
- **IMG files are never deleted**: `is_protected_file()` guard in all code paths that delete files. `*.img` files in GADGET_DIR are always refused deletion.
- **RecentClips preservation**: Archive timer runs every 5 minutes regardless of WiFi state, copying clips to SD card before Tesla's circular buffer overwrites them.
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
- Marking a file as `synced` in the cloud database before rclone confirms the upload completed.
- Re-introducing redundant indexing triggers (every page load, every mode switch, every WiFi connect, full filesystem walks). The indexing worker is the SINGLE consumer of `indexing_queue`; producers only enqueue. Adding parallel "trigger_auto_index"-style code paths is what caused the constantly-flashing banner the redesign removed.
- Flashing the indexing banner based on queue depth instead of `active_file`. The user only wants to know when a file is *actively being parsed*, not when items are queued.

## AI & Testing Workspace Rules
- **Never install packages, dependencies, or node_modules inside the git repo.** Test tooling (Playwright, npm packages, debug scripts) must live **outside** the repository — use `../playwright-test/` or another folder above the repo root.
- **Never create temporary test scripts, debug files, or scratch files inside the repo.** If you need a test script, put it in the parent directory or a temp folder.
- **The git working tree must stay clean.** After any task, `git status` should show only intentional changes. No untracked test artifacts, no `package.json`, no `node_modules/`, no screenshot dumps.
- **Playwright MCP artifacts** (`.playwright-mcp/`) are already gitignored but should also be cleaned up at end of session.
- **Python `__pycache__/`** directories are gitignored — never commit `.pyc` files.
