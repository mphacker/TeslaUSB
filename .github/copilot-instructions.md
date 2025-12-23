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
- Two disk images: `usb_cam.img` (part1 TeslaCam) and `usb_lightshow.img` (part2 LightShow/Chimes).
- Modes: **present** (USB gadget active, RO mounts at `/mnt/gadget/part*-ro`, Samba off) vs **edit** (gadget off, RW mounts at `/mnt/gadget/part*`, Samba on). `state.txt` holds the token; `mode_service.current_mode()` falls back to detection.
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
- Flask app under `scripts/web/`; blueprints in `scripts/web/blueprints/`; services in `scripts/web/services/` encapsulate logic (mount handling, chimes, thumbnails, Samba, mode).
- Mode-aware file ops: lock chimes/light shows/videos must go through services that choose RO/RW paths; avoid direct filesystem writes in view code.
- Samba cache: after edits in edit mode, call `close_samba_share()` and `restart_samba_services()` (see lock chime routes).
- **Web service runs on port 80** (not 5000) to enable captive portal functionality. The service runs as root (via systemd) to bind to privileged port 80.

## Thumbnail System
- **On-demand generation**: Thumbnails generated via PyAV when requested (80x45px, 1-3s generation time).
- **Queue-based loading**: Client-side queue with max 3 concurrent checks, 1-at-a-time generation to prevent memory exhaustion.
- **7-day browser cache**: `Cache-Control: public, max-age=604800, immutable` for generated thumbnails.
- **Placeholder handling**: `Cache-Control: no-store` prevents caching of transparent PNG placeholders.
- **Memory optimization**: Tuned for Pi Zero 2 W (512MB RAM) - small thumbnails, concurrency limits, request abort on navigation.

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
- Force mode persists across reboots via `OFFLINE_AP_FORCE_MODE` in `config.sh`.
- Runtime force mode stored in `/run/teslausb-ap/force.mode`; on boot, `wifi-monitor.sh` initializes runtime file from config.
- Web UI "Start AP Now" sets `force_on` (persistent); "Stop AP" sets `force-auto` (persistent, returns to auto behavior).
- AP runs concurrently with WiFi client on virtual interface `uap0`; WiFi client stays active on `wlan0`.
- `ap_control.sh set_force_mode()` writes both runtime file and persists to config.sh using `sed`.

## Captive Portal
- **DNS spoofing**: dnsmasq configured with `address=/#/<gateway-ip>` to redirect all DNS queries to the AP gateway.
- **Captive portal detection**: Flask blueprint (`scripts/web/blueprints/captive_portal.py`) intercepts OS-specific connectivity check URLs (Apple `/hotspot-detect.html`, Android `/generate_204`, Windows `/connecttest.txt`, etc.).
- **Splash screen**: Custom branded HTML template (`scripts/web/templates/captive_portal.html`) displays Tesla USB Gadget features with "Access Web Interface" button.
- **Port 80 requirement**: Web service must run on port 80 (standard HTTP) for automatic captive portal detection on all devices. No iptables redirects needed.
- **Automatic trigger**: When devices connect to TeslaUSB WiFi, they detect the captive portal and automatically open the splash screen without user typing any URL.

## Pitfalls to avoid
- Skipping `nsenter` for mounts (mounts vanish after subprocess exit).
- Unbinding/mount order wrong when leaving present mode (causes busy unmounts).
- Editing templates without rerunning `setup_usb.sh` (placeholders stay unexpanded).
- Long quick-edit operations holding the lock and leaving LUN unbound on failure; ensure cleanup paths restore RO mount and gadget backing.
- Modifying AP force mode without persisting to config.sh (state lost on reboot); always use `ap_control.sh` or `ap_service.ap_force()`.
