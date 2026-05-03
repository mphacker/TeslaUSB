# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **⚠️ Living document rule**: Any architectural decision, layout choice, or configuration choice made during development or setup sessions **must be recorded here** before the session ends. This file is the single source of truth for why things are the way they are.

## Project Overview

TeslaUSB transforms a Raspberry Pi into a smart USB drive for Tesla dashcam recordings. It presents two or three USB mass storage LUNs to the vehicle (TeslaCam, LightShow, optional Music) while running a Flask web dashboard with GPS mapping, telemetry HUD, cloud sync, and media management.

Target hardware: **Raspberry Pi Zero 2 W** (512MB RAM). Every design decision — memory, CPU, no JS frameworks, no CDN calls — must account for this constraint.

## Common Commands

### Development (on the Pi)
```bash
# Run web app manually (after setup)
cd /home/pi/TeslaUSB && python3 web_control.py

# View web service logs
sudo journalctl -u gadget_web.service -f

# Restart web service after Python changes
sudo systemctl restart gadget_web.service

# Restart AP/WiFi service after config changes
sudo systemctl restart wifi-monitor.service

# Switch operating modes
sudo ~/TeslaUSB/present_usb.sh   # Connect to Tesla (Present mode)
sudo ~/TeslaUSB/edit_usb.sh      # Enable network sharing (Edit mode)
```

### Tests
```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_mapping_service.py

# Run a single test
pytest tests/test_sei_parser.py::test_function_name
```

### Setup & Deployment
```bash
# Full install/re-deploy (after changing templates or scripts/)
sudo ./setup_usb.sh
```

## Architecture

### Configuration System
- **Single source of truth**: `config.yaml` at repo root — all paths, credentials, and settings live here.
- **Bash scripts** read config via `scripts/config.sh` (uses `yq` with a single optimized eval call).
- **Python** reads config via `scripts/web/config.py` (uses PyYAML).
- Never hardcode paths or values; always use these wrappers.
- After editing `config.yaml`, restart `gadget_web.service` (web changes) or `wifi-monitor.service` (AP changes).

### Template System
- Source files in `scripts/` and `templates/` use placeholders: `__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__`, `__IMG_NAME__`, `__SECRET_KEY__`.
- Run `sudo ./setup_usb.sh` after changing any template to substitute and deploy. Never hardcode installed paths.

### Operating Modes
- **Present mode** (default on boot): USB gadget active, drives RO-mounted at `/mnt/gadget/part*-ro`, Samba off.
- **Edit mode**: Gadget off, drives RW-mounted at `/mnt/gadget/part*`, Samba on.
- Current mode stored in `state.txt`; `mode_service.current_mode()` falls back to detection.
- **Boot priority**: USB gadget presentation to Tesla is #1. Never add blocking work before UDC bind in `present_usb.sh`. All post-boot tasks (cleanup, chime selection, indexing, cloud sync) run via `teslausb-deferred-tasks.service`.

### Flask Web App (`scripts/web/`)
- Entry point: `web_control.py` — creates Flask app, registers all blueprints.
- **Blueprints** (`blueprints/`): one per feature area (mapping, videos, lock_chimes, light_shows, music, wraps, license_plates, media, analytics, cleanup, cloud_archive, api, fsck, captive_portal, mode_control).
- **Services** (`services/`): all business logic — mount handling, file ops, chimes, thumbnails, Samba, mode detection, geo-indexing, cloud sync, etc.
- **Helpers** (`helpers/`): config_updater, refresh_cloud_token, safe_mode.
- Blueprints are always registered — routes redirect when their required `.img` file is missing (never unregistered at import time).

### Feature Gating (Image-Gated UI)
Nav items and routes are hidden/blocked when the required disk image doesn't exist:
- `usb_cam.img` → analytics, videos, cleanup sub-pages
- `usb_lightshow.img` → chimes, light shows, wraps, license_plates
- `usb_music.img` + `MUSIC_ENABLED` → music

`partition_service.get_feature_availability()` returns a dict of booleans; `utils.get_base_context()` merges them into every template context; `base.html` wraps nav links in `{% if <flag> %}` guards. Each gated blueprint has a `@bp.before_request` hook — AJAX requests get 503 JSON, normal requests redirect to Settings with a flash.

**When adding a new gated feature**: add to `get_feature_availability()`, guard nav link in `base.html`, add `@bp.before_request` in the blueprint.

### Mount / Gadget Safety Rules
- All mount/umount commands must run in PID 1 mount namespace: `sudo nsenter --mount=/proc/1/ns/mnt ...`
- Resolve paths via `partition_service.get_mount_path()` / `iter_all_partitions()` — never hardcode.
- `partition_mount_service.quick_edit_part2` temporarily remounts part2 RW in present mode (uses `.quick_edit_part2.lock`, 120s stale). Always restore RO mount and LUN on all code paths including failures.
- **quick_edit_part2 sequence**: Clear LUN1 backing → unmount RO → detach loops → create RW loop → mount RW → do work → sync → unmount → detach → create RO loop → remount RO → restore LUN1 backing.
- USB gadget serves the `.img` file directly as the LUN backing file — not loop devices. Loop devices are for local mounting only.

### Lock Chimes
- Rules: WAV <1 MiB, 16-bit PCM, 44.1/48 kHz, mono/stereo. `lock_chime_service` validates, re-encodes via ffmpeg if needed.
- After replacing `LockChime.wav`, MUST rebind USB gadget (`partition_mount_service.rebind_usb_gadget()`) to force Tesla cache invalidation.
- Present-mode uploads use `quick_edit_part2`; keep operations short and atomic (temp + fsync + MD5).
- Boot optimization: `select_random_chime.py` passes `skip_quick_edit=True` when part2 is already RW-mounted at boot.

### Video & Mapping
- **No standalone Videos page.** All video browsing is in the map page (`mapping.html`) via a slide-out panel.
- GPS/telemetry indexed from dashcam SEI metadata by `mapping_service.py` into `geodata.db` (SQLite).
- Indexing triggered on startup, inotify file detection, and WiFi connect.
- **No thumbnail system** — it has been removed. Never add thumbnail generation.
- `file_watcher_service.py` uses `watchdog` (inotify) with 5-minute polling fallback.

### Cloud Sync
- Queue-based (SQLite), one file at a time, `nice -n 19` + `ionice -c3` on rclone.
- Priority: Tesla event.json folders → geolocated trips → other clips (opt-in).
- Mark file as `synced` only AFTER rclone confirms upload + DB commit + fsync.

### Task Coordinator
- `task_coordinator.py` enforces exclusive lock: geo-indexer, video archiver, and cloud sync never run simultaneously (critical for 512MB RAM).

### Offline Access Point & Captive Portal
- AP runs on virtual interface `uap0` concurrent with WiFi client on `wlan0`.
- Three force modes: `auto`, `force_on`, `force_off` — persisted in `config.yaml` and runtime file `/run/teslausb-ap/force.mode`.
- Captive portal: Flask blueprint intercepts OS connectivity check URLs; dnsmasq spoofs all DNS to gateway IP. Port 80 is required — no iptables redirects.

### Services & Timers (systemd)
- `gadget_web.service` — Flask web UI
- `present_usb_on_boot.service` — presents USB gadget at boot
- `teslausb-deferred-tasks.service` — post-boot: cleanup, chime selection, indexing, cloud sync
- `chime_scheduler.timer` — checks scheduled chime changes every 60s
- `wifi-monitor.service` — manages AP/STA roaming
- `watchdog.service` — hardware watchdog (15s timeout)

## UI/UX Rules

Full design system: [`docs/UI_UX_DESIGN_SYSTEM.md`](docs/UI_UX_DESIGN_SYSTEM.md)

- **No emoji icons** — use Lucide SVG icons exclusively.
- **CSS tokens only** — never hardcode hex values; use `--bg-primary`, `--accent-success`, etc.
- **Dark and light mode**: both must work; test both. Use `[data-theme="dark"]` selectors.
- **Mobile-first**: bottom tab bar (<1024px), sidebar rail (>=1024px). Test at 375px and 1024px+.
- **Touch targets**: minimum 44x44px.
- **No "Edit Mode" / "Present Mode" in UI** — user-facing concept is "Network File Sharing". Show a status dot.
- **No external CDN calls** — fonts, icons, and all assets must be bundled locally.
- **Progressive disclosure**: Layer 1 (glanceable), Layer 2 (one tap), Layer 3 (deliberate action).

## Pitfalls

- Skipping `nsenter` for mounts — mounts vanish after subprocess exit.
- Wrong unbind/mount order when leaving present mode — causes busy unmounts.
- Editing templates without rerunning `setup_usb.sh` — placeholders stay unexpanded.
- Long `quick_edit` operations leaving LUN unbound on failure — always restore on all code paths.
- Adding blocking work before UDC bind in `present_usb.sh` — delays Tesla USB detection.
- Adding a new blueprint route without a `before_request` image guard when the feature requires a disk image.
- Marking a cloud file as `synced` before rclone confirms the upload.
- Running rclone without `nice`/`ionice` — starves the web server.
- Generating video thumbnails (system removed — don't add it back).
- Deleting or overwriting `*.img` files — `is_protected_file()` guard must always be respected.

## Partition Layout & Content Decisions

These decisions were made during the initial setup session (2026-04-29) and must not be changed without updating this file.

### USB LUN layout

| LUN | Image | Format | What goes here |
|-----|-------|--------|----------------|
| 0 | `usb_cam.img` | exFAT | TeslaCam dashcam + Sentry recordings (Tesla writes here automatically) |
| 1 | `usb_lightshow.img` | FAT32 | **Light shows only** — `/LightShow/*.fseq` + paired audio. Also `/Chimes/`, `/Wrap/`, and `/LicensePlate/` managed by TeslaUSB web UI |
| 2 | `usb_music.img` | FAT32 | Music files + **`/Media/` folder** for Tesla Boombox and other Tesla-native media features |

### Content placement rules

- **Light shows** (`.fseq` + `.mp3`/`.wav` pairs) → LUN 1 `/LightShow/`
- **Lock chimes** → LUN 1 `/Chimes/` — managed by TeslaUSB web UI (upload, preview, scheduler)
- **Wraps** (PNG files for Tesla Paint Shop) → LUN 1 `/Wrap/` — managed by TeslaUSB web UI
- **License plates** (PNG images, 420x100px recommended) → LUN 1 `/LicensePlate/` — managed by TeslaUSB web UI (upload, download, delete; up to 10 images)
- **Boombox sounds** → LUN 2 `/Media/` — Tesla reads this folder natively; managed by the **Boombox blueprint** (`blueprints/boombox.py`, `services/boombox_service.py`). Upload/delete/preview via the web UI (Media Hub → Boombox tab). MP3 only, max 1 MiB per file, max 20 files. Uses `quick_edit_part3` in present mode.
- **Music** → LUN 2 root or subfolders — Tesla music player reads from here

### Why Boombox lives on LUN 2 (Music), not LUN 1 (LightShow)

Tesla reads Boombox sounds from a `/Media/` folder on any connected USB drive. Placing it on LUN 2 (Music) keeps LUN 1 (LightShow) clean — only light shows, chimes, and wraps that TeslaUSB actively manages. This avoids confusion between Tesla-native media features and TeslaUSB-managed features.

**Do not** put Boombox sounds, raw chime files, or wrap PNGs directly on the LightShow partition outside of the folders TeslaUSB manages (`/Chimes/`, `/Wrap/`, `/LightShow/`, `/LicensePlate/`).

### Partition sizes (initial setup)

| Partition | Size | Rationale |
|-----------|------|-----------|
| TeslaCam | 150 GB | Maximise dashcam + sentry storage |
| LightShow | 10 GB | 30+ shows, chimes, wraps, license plates — ample headroom |
| Music | 20 GB | Boombox sounds + room to grow a music library |
| SD reserve | 5 GB | Keep filesystem healthy |

## Workspace Rules

- **Never install packages or node_modules inside the git repo.** Test tooling (Playwright, npm packages) must live outside — use `../playwright-test/` or similar.
- **Never create temporary test/debug/scratch files inside the repo.** Put them in a parent directory.
- **The git working tree must stay clean.** After any task, `git status` should show only intentional changes.
- **Python `__pycache__/`** is gitignored — never commit `.pyc` files.
