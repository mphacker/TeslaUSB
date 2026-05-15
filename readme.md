# TeslaUSB

Transform your Raspberry Pi into a smart USB drive for Tesla dashcam recordings with a map-centric dashboard, GPS trip visualization, telemetry-rich video playback, and automated maintenance.

> **🚨 IMPORTANT - CHANGES FOR EXISTING USERS 🚨**
>
> Significant changes have been made to the application. All configuration is now centralized in a single `config.yaml` file. Please read the [Configuration](#configuration) section for details on updating your setup.  You may want to restart from a clean Raspberry PI OS image and follow the [Installation](#installation) steps again to ensure everything is set up correctly. If you do not want to do that, ensure that the config.yaml file is created and updated with your desired settings and then run the `setup_usb.sh` script again to apply the new configuration structure.
>
> **The web interface now runs on PORT 80 (standard HTTP) instead of port 5000.**
>
> - **Old URL**: `http://<pi-ip>:5000` ❌
> - **New URL**: `http://<pi-ip>` ✅ (no port needed!)
>
> This change enables the **captive portal feature** - when you connect to the TeslaUSB WiFi network, your device will automatically open the web interface without typing any URL.

## Overview

TeslaUSB creates a multi-drive USB gadget that appears as **two or three separate USB drives** to your Tesla:

- **TeslaCam Drive**: Large exFAT drive for dashcam and sentry recordings
- **LightShow Drive**: Smaller FAT32 drive for lock chimes, custom wrap images, and light shows with read-only optimization
- **Music Drive** *(optional)*: FAT32 drive for Tesla music playback (enabled via `music_enabled: true` in config)

**Key Benefits:**
- **Map-centric dashboard** with GPS trip routes, event markers, and floating trip cards as the landing page
- **Telemetry HUD** overlay during video playback — speed, gear, steering, pedals, blinkers, Autopilot status
- Remote access to dashcam footage without physically removing storage
- Web interface for browsing videos, managing chimes, music, boombox sounds, light shows, wraps, license plates, and monitoring storage (with dark/light mode)
- Automatic cleanup policies to manage disk space
- Scheduled chime changes for holidays, events, or automatic rotation
- Offline access point for in-car web access when WiFi is unavailable

<p align="center">
  <img src="docs/screenshots/map-event-popup.png" alt="TeslaUSB Map Dashboard — trip route with event popup" width="800">
  <br>
  <em>Map dashboard showing a trip route with event markers and detail popup</em>
</p>

> **⚠️ Personal Project Notice**
>
> This is a personal project built for my own use. You are welcome to fork the code and make your own changes or updates. Please be aware:
> - The Git repository may update frequently with new features and changes
> - Bugs may be introduced into the main branch without extensive testing
> - Bug fixes will be worked on as time permits, but **no timelines or guarantees** are provided
> - You have access to the source code - if something breaks, you can attempt to fix it yourself
> - This project is provided as-is with no warranty or support obligations

## Features

### Core Functionality
- **Multi-Drive USB Gadget**: Two or three independent filesystems (TeslaCam + LightShow + optional Music) with optimized performance
- **Two Operating Modes**:
  - **Present Mode**: Active USB gadget for Tesla recording (shown as "Connected to Tesla" in the UI)
  - **Edit Mode**: Network access via Samba shares for file management (shown as "Network Sharing Active" in the UI)
- **Web Interface**: Browser-based control panel accessible at `http://<pi-ip>` (port 80) with five main sections — Map (landing page), Analytics, Media, Cloud, and Settings — plus a sidebar rail on desktop and bottom tabs on mobile
- **Captive Portal**: Automatic splash screen when connecting to TeslaUSB WiFi network
- **Design System**: Dark/light mode with CSS design tokens, Inter variable font (bundled offline), Lucide SVG icon sprite, and glassmorphic overlay HUD

### Video Management
- **Map-integrated video browser**: Slide-out panel on the Map page with three tabs — Events (default), Trips, and All Clips — no separate video pages
- **Unified overlay player**: Map-launched video playback with camera angle switching (Front, Back, Left, Right, L Pillar, R Pillar) using directional Lucide SVG icons, plus two distinct fullscreen modes — **Fullscreen** (OS-level, hides browser chrome) and **Maximize** (fills the browser viewport). Both keep the telemetry HUD visible.
- **Disambiguation popup**: Tapping the map at a location with multiple overlapping clips (e.g., a road driven multiple times) opens a chooser listing each clip with its trip date/time so the right one can be selected.
- **Telemetry HUD**: Glassmorphic overlay showing real-time steering wheel angle, brake/gas pedal positions, speed, gear (P/R/N/D), turn signals, and Autopilot status — powered by pre-indexed server-side waypoint data (instant, no full video download needed)
- **Auto-indexing**: A single low-priority background worker drains a SQLite-backed `indexing_queue` one file at a time. Producers: boot catch-up scan, real-time inotify on new files, the post-WiFi archive run, and manual reindex from the UI. The "Indexing…" banner only appears while a specific file is actively being parsed. Sentry events placed on map using inferred location from nearest trip
- **RecentClips Archive**: Automatically copies RecentClips to the Pi's SD card every 2 minutes before Tesla's 1-hour circular buffer deletes them — zero USB disruption, videos preserved for 30 days
- **Skeuomorphic event markers**: Balloon-pin map markers — brake pedal, gas pedal, steering wheel, speedometer, eye (sentry) — always visible on the map
- **Trip navigation**: Floating trip card with prev/next navigation; FSD overlay toggle
- Download all camera views for an event as a zip file
- Delete entire events (Edit mode only)
- Cascade database cleanup when videos are deleted

### Lock Chime Management
- Upload WAV or MP3 files (automatically converted to Tesla-compatible format)
- Organized chime library with preview and download
- Volume normalization presets (Broadcast, Streaming, Loud, Maximum)
- **Chime Groups**: Organize chimes by theme (Holidays, Funny, Seasonal, etc.)
- **Random Selection on Boot**: Automatically pick a different chime from your selected group each time the device boots
- Scheduled chime changes:
  - Weekly schedules (specific days/times)
  - Date-based schedules
  - Holiday schedules (Christmas, Easter, Thanksgiving, etc.)
  - Recurring rotation (every 15min to 12 hours, or on boot)

### Light Show Management
- Upload FSEQ and MP3/WAV files
- Grouped display (pairs sequence + audio files)
- Preview MP3/WAV tracks in browser
- Delete complete light show sets

### Custom Wrap Management
- Upload PNG files for Tesla's Paint Shop 3D vehicle visualization
- Thumbnail previews of all uploaded wraps
- Automatic validation (512-1024px dimensions, max 1MB, PNG only)
- Supports up to 10 custom wraps at a time
- Drag-and-drop upload with progress indicator

### License Plate Management
- Upload custom license-plate background images for Tesla's Paint Shop visualization (LightShow drive, `/LicensePlate` folder)
- **Smart auto-cropping**: Drop in any image format (PNG, JPEG, WEBP, GIF, BMP) — the server crops/resizes to one of Tesla's two allowed dimensions: **420×200** (North America) or **420×100** (Europe)
- Output is always optimized PNG, capped at **512 KB**
- Up to **10 plates** at a time, alphanumeric filenames (32 characters max — Tesla's plate parser rejects anything else)
- Drag-and-drop multi-file upload, previews, individual download/delete
- Read-only at runtime (Tesla reads from the LightShow LUN); writes use the `quick_edit_part2` mechanism

### Boombox Sound Management
- Manage sounds Tesla plays through the external pedestrian-warning speaker (Music drive, `/Boombox` folder — requires `music_enabled: true`)
- **Tesla constraints enforced in the UI**: MP3 or WAV only, **1 MiB** maximum (≤ 5 seconds recommended), **64-character** filename limit (letters, numbers, spaces, underscores, dashes, dots), **5 sounds** max — Tesla loads the first 5 alphabetically
- In-browser preview, drag-and-drop upload, individual delete
- Prominent **NHTSA safety notice**: Boombox sounds only play while the vehicle is in Park (Feb 2022 NHTSA ruling), and the vehicle must have an external pedestrian-warning speaker — built September 2019 or later for Model 3/Y/S/X, or any Cybertruck

### Automatic Maintenance
- **Storage Cleanup**: Age, size, or count-based policies per folder
- **Boot Cleanup**: Deferred post-boot cleanup runs after USB gadget is presented to Tesla
- **RecentClips Archive**: Automatic background archival with 3-tier retention (free space floor, size cap, age limit)
- **Chime Scheduler**: Checks every 60 seconds for scheduled changes
- **Hardware Watchdog**: Automatic system recovery on hangs or crashes
- **Task Coordinator**: Exclusive lock prevents geo-indexer, video archiver, and cloud sync from running simultaneously (critical for Pi Zero 2 W's 512MB RAM)

### Network Features
- **Samba Shares**: Windows/Mac/Linux file access in Edit mode
- **Offline Access Point**: Automatic fallback AP when WiFi unavailable (in-car web access)
- **WiFi Roaming**: Automatic switching between access points with the same SSID for optimal signal strength (mesh networks and WiFi extenders)

### Cloud Archive
- **Queue-based continuous sync**: Automatically uploads dashcam events to cloud storage via rclone
- **Wide provider support**: OAuth providers (Google Drive, OneDrive, Dropbox); S3-compatible (Amazon S3, Backblaze B2, Wasabi, MinIO); and **NAS / custom rclone** backends (SFTP, WebDAV, SMB/CIFS, FTP, Azure Blob, OpenStack Swift) — issue #165
- **Priority ordering**: Events with Tesla event.json uploaded first, then geolocated trips, then remaining clips
- **Power-loss safe**: Files marked as synced only after rclone confirms upload; partial uploads detected and re-queued on restart
- **Low impact**: Runs with `nice`/`ionice` throttling, configurable bandwidth limits, one file at a time — web UI stays responsive
- **Web UI**: Configure cloud provider, browse remote folders, monitor sync queue and history, trigger manual uploads, bandwidth testing

## Requirements

> **Note**: This project has only been tested on **Raspberry Pi Zero 2 W**. Other OTG-capable models should work but are untested.

- **Tesla Software**: Version **2025.44.25.1 or later** (2025 Holiday Update) required for event thumbnails, SEI telemetry data, and multi-camera event structure
- **Raspberry Pi Zero 2 W** (tested and recommended) - Small form factor, low power, powered directly from Tesla USB port
- Other Raspberry Pi models with USB OTG capability should work (Pi 4, Pi 5, Compute Modules) - **untested**
- 128GB+ microSD card (for OS, dashcam storage, light shows, and music)
- Raspberry Pi OS (64-bit) Desktop - Debian "Trixie"
- Internet connection for initial setup

### Raspberry Pi OTG Compatibility

For USB gadget projects like TeslaUSB, the **Raspberry Pi Zero family and Compute Modules are the best choice**. Raspberry Pi 4 and 5 offer OTG support, but their higher power requirements can be an issue. **Raspberry Pi A, B, 2B, 3B, and 3B+ do NOT support OTG** (host mode only).

For detailed information, see the official Raspberry Pi whitepaper: [Using OTG mode on Raspberry Pi SBCs](https://pip.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-009276-WP/Using-OTG-mode-on-Raspberry-Pi-SBCs.pdf)

| Model | OTG Support | Notes |
|-------|-------------|-------|
| Raspberry Pi Zero / Zero W / Zero 2 W | ✅ Yes | Fully supported on USB data port |
| Raspberry Pi 4 | ⚠️ Yes* | USB-C port in device mode |
| Raspberry Pi 5 | ⚠️ Yes* | USB-C port in device mode |
| Raspberry Pi A/B/2B/3B/3B+ | ❌ No | Only host mode - **not compatible** |
| Raspberry Pi Compute Module 1-3 | ✅ Yes | Exposed on OTG pins |
| Raspberry Pi Compute Module 4 | ✅ Yes | micro-USB on CM4 IO board |

*\* Raspberry Pi 4 and 5 draw power from the host via USB cable, so there may be limitations on available current due to their higher power requirements.*

**Note for Pi Zero 2 W users**: Setup automatically optimizes memory by disabling unnecessary desktop services and enabling 1GB swap. This ensures stable operation on the 512MB RAM platform.

**⚠️ Note for Raspberry Pi 4/5 users**: USB OTG/gadget mode is **only available on the USB-C port**, which is also the power input. This creates a challenge: you cannot simultaneously power the Pi from a standard USB charger and present as a USB device to Tesla. Options include:
- USB-C power + data splitter adapters (search "USB-C OTG with PD charging")
- Powering the Pi via GPIO pins from a separate car charger (advanced)
- Using a larger SD card instead of external USB storage to avoid power budget issues

## Installation

### 1. Prepare Raspberry Pi

1. Flash **Raspberry Pi OS (64-bit) Desktop** using [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Configure OS customization settings:
   - Set hostname (e.g., `cybertruckusb`)
   - Enable SSH with password authentication
   - Set username/password (default: `pi`)
   - Configure WiFi credentials
   - Set timezone and keyboard layout
3. Insert microSD into Pi and boot (wait 2-3 minutes)
4. Verify SSH access: `ssh pi@cybertruckusb.local`

### 2. Install TeslaUSB

```bash
git clone https://github.com/mphacker/TeslaUSB.git
cd TeslaUSB
chmod +x setup_usb.sh
sudo ./setup_usb.sh
```

The setup script will:
- Install required packages (parted, dosfstools, python3-flask, python3-av, samba, hostapd, dnsmasq, ffmpeg)
- Optimize memory for low-RAM systems (disable desktop services, enable swap)
- Configure USB gadget kernel modules and hardware watchdog
- Detect and disable conflicting `rpi-usb-gadget` service (Pi OS Trixie default)
- Create disk images (TeslaCam + LightShow + optional Music) with interactive image dashboard
- Set up Samba shares and web interface
- Configure systemd services with auto-restart on failure
- Create `/Chimes` library and migrate existing lock chimes

### 3. Access Web Interface

Open `http://<pi-ip-address>` or `http://<hostname>.local` in your browser (port 80 - no port number needed).

Alternatively, connect to the TeslaUSB WiFi network and the captive portal will automatically open.

### 4. Connect to Tesla

Connect the Pi to your Tesla's USB port:
- **Pi Zero 2 W**: Use USB port labeled "USB" (not "PWR")
- **Pi 4/5**: Use USB-C port

Tesla will detect the USB drives automatically (two drives, or three if Music is enabled).

### Power & Sleep Behavior

The TeslaUSB device only runs when the car is awake. When your Tesla enters sleep mode, USB ports are powered off and the Raspberry Pi shuts down.

**To keep your vehicle awake for extended management sessions:**
1. Turn on climate control
2. Enable "Dog Mode" or "Camp Mode" from the climate screen
3. Connect to the TeslaUSB web interface and manage your lock chimes, light shows, or videos
4. When finished, disable Dog/Camp Mode and turn off climate control
5. The vehicle will return to sleep, powering off the USB ports

**Note:** For quick operations like viewing videos or changing a lock chime, the car typically stays awake long enough without needing Dog Mode. Use Dog/Camp Mode only for longer management sessions.

## Usage

### Operating Modes

The web interface abstracts the underlying modes behind user-friendly labels:

**"Connected to Tesla"** (Present USB Mode — default on boot):
- Pi appears as USB drives to Tesla
- Drives mounted read-only locally at `/mnt/gadget/part1-ro`, `/mnt/gadget/part2-ro`, `/mnt/gadget/part3-ro` (if Music enabled)
- Web interface: View/play only (no editing) — some operations (chime changes, music uploads) use temporary quick-edit for seamless access
- Samba shares disabled

**"Network Sharing Active"** (Edit USB Mode):
- USB gadget disconnected
- Drives mounted read-write at `/mnt/gadget/part1`, `/mnt/gadget/part2`, `/mnt/gadget/part3` (if Music enabled)
- Web interface: Full file management (upload, delete, organize)
- Samba shares active for network access

**Switch modes** via the device status card on the Settings page ("Enable Network Sharing" / "Reconnect to Tesla" buttons) or command line:
```bash
sudo ~/TeslaUSB/scripts/present_usb.sh  # Reconnect to Tesla
sudo ~/TeslaUSB/scripts/edit_usb.sh     # Enable Network Sharing
```

### Network Access

**Samba Shares** (Edit mode only):
- `\\<pi-ip-address>\gadget_part1` - TeslaCam drive
- `\\<pi-ip-address>\gadget_part2` - LightShow drive
- `\\<pi-ip-address>\gadget_part3` - Music drive (when `music_enabled: true`)
- Default credentials: username = `pi`, password = `tesla`

**Offline Access Point with Captive Portal**:
When WiFi is unavailable, the Pi automatically creates a fallback access point:
- SSID: `TeslaUSB` (configurable in `config.yaml`)
- Password: `teslausb1234` (change this!)
- IP: `192.168.4.1`
- **Captive Portal**: Automatically opens web interface when you connect (no URL needed!)
- Manual access: `http://192.168.4.1` or `http://teslausb` (port 80)
- Control from web UI: Force start/stop AP or leave in auto mode
  - **Start AP Now**: Forces AP on until reboot or manually stopped
  - **Stop AP**: Returns to auto mode (AP only starts if WiFi fails)
- Change credentials in `config.yaml` before first use
- **Note**: After clicking "Start AP Now" or "Stop AP" buttons, the status may not update immediately. Wait 10-20 seconds and refresh the page to see the current state.

### Web Features

The web interface uses a five-tab navigation — sidebar rail on desktop and bottom tabs on mobile.

**Map Tab** *(landing page at `/`)*:
- GPS trip routes rendered on an interactive map with floating trip card and prev/next navigation
- Skeuomorphic balloon-pin event markers (brake pedal, gas pedal, steering wheel, speedometer, eye for sentry) — always visible
- Video browser slide-out panel with three sub-tabs:
  - **Events**: Chronological view of driving events and sentry detections with event type icons
  - **Trips**: Browse trips with clip cards (Play / Download ZIP / Delete)
  - **All Clips**: Unified list of all video clips across all sources
- Unified overlay player with camera angle switching using directional Lucide SVG icons (Front, Back, Left, Right, L Pillar, R Pillar)
- Two fullscreen modes — **Fullscreen** (OS-level, hides browser chrome) and **Maximize** (fills browser viewport); both keep the telemetry HUD visible
- Disambiguation popup when a map location has multiple overlapping clips (lists each clip with trip date/time so the right one can be selected)
- Telemetry HUD overlay showing speed, gear, steering wheel, brake/gas pedals, blinkers, and Autopilot status (uses pre-indexed server-side waypoint data — instant, no full download)
- FSD overlay toggle
- **Shareable URLs**: The selected day and active sub-view are encoded in the URL — bookmarks, browser back/forward, and reload all return you to exactly the same state
- Auto-indexing of dashcam SEI telemetry via a queue-backed background worker (boot catch-up + inotify + post-WiFi archive run); banner shows only during real parse activity and is positioned so it never covers the date/filter controls

**Analytics Tab**:
- Storage metrics with drive usage gauges and folder-by-folder breakdown (including Music drive when enabled)
- Driving statistics and event analytics (Chart.js)
- Video count and size statistics

**Media Tab** *(hub with sub-tabs)*:
- **Lock Chimes**: Upload WAV/MP3 files (auto-converted to Tesla format), preview with in-browser player, set active chime, built-in audio editor with waveform visualization, schedule automatic changes (weekly, date, holiday, recurring)
- **Music** *(requires `music_enabled: true` and Music disk image)*: Browse folders with breadcrumb navigation, in-browser playback (MP3, FLAC, WAV, AAC, M4A), drag-and-drop uploads with chunked transfer, create/move/delete files and folders, usage gauge
- **Boombox** *(requires `music_enabled: true`)*: Manage the up to 5 sounds Tesla plays through the external pedestrian-warning speaker, with an NHTSA safety notice ("plays only in Park") prominently displayed. MP3/WAV, 1 MiB max
- **Light Shows**: Upload and manage FSEQ + MP3/WAV files, grouped display, preview audio in browser, delete complete sets
- **Wraps**: Upload PNG files for Tesla Paint Shop wraps (512–1024px, max 1MB, up to 10), thumbnail gallery, download or delete
- **License Plates**: Upload custom plate-background images — server auto-crops to Tesla's 420×200 (NA) or 420×100 (EU) format, outputs optimized PNG ≤ 512 KB, up to 10 plates, alphanumeric filenames

Each Media sub-tab is **automatically hidden** when the disk image it depends on is missing (Boombox/Music require `usb_music.img`; Chimes/Shows/Wraps/Plates require `usb_lightshow.img`).

**Cloud Tab** *(conditional — shown when cloud archive is configured)*:
- Configure cloud storage provider (Google Drive, S3, Dropbox, etc.) via rclone
- Browse remote folders and set upload destination
- Monitor sync queue, upload progress, and transfer history
- Manual "Archive to Cloud" for individual events from the Map page
- Bandwidth testing and configurable upload speed limits
- Start/stop sync on demand

**Settings Tab** *(at `/settings/`)*:
- **Device status card**: Shows "Connected to Tesla" or "Network Sharing Active" with mode-switch buttons ("Enable Network Sharing" / "Reconnect to Tesla")
- **WiFi configuration**: View and update network settings
- **Access Point controls**: Force start/stop AP or leave in auto mode
- **Auto-Cleanup settings**: Configure age, size, or count-based policies per folder; link to cleanup config page; preview and execute cleanup operations
- **Filesystem Health Check**: Quick Check (read-only, any mode) and Check & Repair (edit mode only) for all drives
- **System info**: Hostname, IP address, uptime, memory usage, disk image status, version

## Configuration

All configuration is centralized in a single **`config.yaml`** file - edit this file **before** running setup.

Both bash scripts and the Python web application read from this YAML file, ensuring consistency across the entire system.

### Configuration File: `config.yaml`

```yaml
# TeslaUSB Configuration File
#
# All paths, settings, and credentials are defined here.
# Both bash scripts and Python web application use this file.

# ============================================================================
# Installation & Paths
# ============================================================================
installation:
  target_user: pi                    # Linux user running services
  mount_dir: /mnt/gadget             # Mount directory for USB drives

# ============================================================================
# Disk Images
# ============================================================================
disk_images:
  cam_name: usb_cam.img              # TeslaCam disk image filename
  lightshow_name: usb_lightshow.img  # LightShow disk image filename
  cam_label: TeslaCam                # Filesystem label for TeslaCam drive
  lightshow_label: Lightshow         # Filesystem label for LightShow drive
  music_name: usb_music.img          # Music disk image filename (optional)
  music_label: Music                 # Filesystem label for Music drive
  music_enabled: true                # Create and present Music partition (LUN2)
  music_fs: fat32                    # Filesystem for Music image (fat32 recommended)
  boot_fsck_enabled: true            # Auto-repair filesystems on boot (recommended)

# ============================================================================
# Setup Configuration (used only by setup_usb.sh)
# ============================================================================
# Leave empty ("") for interactive prompts during setup
setup:
  part1_size: ""                     # TeslaCam drive size (e.g., "50G")
  part2_size: ""                     # LightShow drive size (e.g., "10G")
  part3_size: ""                     # Music drive size (e.g., "32G")
  reserve_size: ""                   # Free space headroom (default: 5G)
  archive_reserve_size: "50G"        # Space reserved for RecentClips archive

# ============================================================================
# Network & Security
# ============================================================================
network:
  samba_password: tesla              # Samba password (CHANGE THIS!)
  web_port: 80                       # Web port (80 required for captive portal)

# ============================================================================
# Offline Access Point Configuration
# ============================================================================
offline_ap:
  enabled: true                      # Enable/disable fallback AP
  ssid: TeslaUSB                     # AP network name (CHANGE THIS!)
  passphrase: teslausb1234           # WPA2 passphrase 8-63 chars (CHANGE THIS!)
  channel: 6                         # 2.4GHz channel (1-11)
  force_mode: auto                   # auto, force_on, or force_off

# ============================================================================
# Web Application Configuration
# ============================================================================
web:
  secret_key: CHANGE-THIS-TO-A-RANDOM-SECRET-KEY-ON-FIRST-INSTALL
  max_lock_chime_size: 1048576       # 1 MiB
  max_lock_chime_duration: 10.0      # 10 seconds
  max_upload_size_mb: 2048           # Max upload size for music/lightshow (MiB)
  max_upload_chunk_mb: 16            # Chunk size for streaming uploads (MiB)

# ============================================================================
# RecentClips Archive
# ============================================================================
archive:
  enabled: true                      # Enable RecentClips archival to SD card
  interval_minutes: 2                # How often to check for new clips
  retention_days: 30                 # Delete archived clips older than this
  min_free_space_gb: 10              # Stop archiving if SD card < this free
  max_size_gb: 50                    # Cap on total archive folder size
```

**Important settings to change before first use:**
- `network.samba_password` - Default is `tesla` (change this!)
- `offline_ap.ssid` - Default is `TeslaUSB` (customize for your vehicle)
- `offline_ap.passphrase` - Default is `teslausb1234` (change this!)
- `web.secret_key` - Auto-generated on first run, but can be set manually

**Optional settings:**
- `disk_images.music_enabled` - Create and present an optional Music drive as a third USB LUN (default: `true`)
- `disk_images.boot_fsck_enabled` - Auto-repair filesystems on boot (default: `true`, recommended)

**Note:** The installation directory (`GADGET_DIR`) is automatically derived from the script location — no path configuration is needed. Scripts and the web app detect their own location at runtime.

**After making changes:** Restart affected services
```bash
sudo systemctl restart gadget_web.service    # For web application changes
sudo systemctl restart wifi-monitor.service  # For offline AP changes
```

**How it works:**
- Bash scripts use `yq` to read YAML values
- Python web app uses `PyYAML` to load configuration
- Single source of truth for all settings
- Comments and structure make configuration clear

## Maintenance

### Upgrade to Latest Version

> **⚠️ BACK UP YOUR DISK IMAGES BEFORE UPGRADING**
>
> Your USB drive images (`usb_cam.img`, `usb_lightshow.img`, `usb_music.img`) contain all your dashcam videos, lock chimes, light shows, music, and wraps. These files can be **hundreds of gigabytes** and are not recoverable if lost.
>
> Before running any upgrade or `git pull`:
> 1. **Switch to Edit Mode** (or use "Enable Network Sharing" in Settings)
> 2. **Copy the `.img` files** to a safe location (external drive, NAS, or computer):
>    ```bash
>    scp pi@<hostname>.local:~/TeslaUSB/*.img /path/to/backup/
>    ```
> 3. Then proceed with the upgrade
>
> The `.img` files are listed in `.gitignore` and should not be affected by git operations, but backing up protects against accidental deletion, SD card corruption, or any unexpected issues during upgrade.

```bash
cd ~/TeslaUSB
./upgrade.sh
```

Upgrades to the latest version from GitHub. Supports both git-cloned installs (`git pull`) and manual installs (tarball download with automatic backup/restore on error). After updating code, prompts to re-run `setup_usb.sh`. Disk images and configuration are preserved.

### Uninstall

```bash
cd ~/TeslaUSB
sudo ./cleanup.sh
```

Removes all files, services, and system configuration.

## Systemd Services

| Service/Timer | Purpose |
|---------------|---------|
| `gadget_web.service` | Web interface (port 80) with captive portal |
| `present_usb_on_boot.service` | Auto-present USB gadget on boot (cleanup deferred) |
| `teslausb-deferred-tasks.service` | Post-boot tasks: cleanup, random chime selection |
| `chime_scheduler.timer` | Check scheduled chime changes every 60 seconds |
| `wifi-monitor.service` | Manage offline access point |
| `watchdog.service` | Hardware watchdog for system reliability |

**Common Commands:**
```bash
# Check service status
sudo systemctl status gadget_web.service

# View logs
sudo journalctl -u gadget_web.service -f

# Restart web interface
sudo systemctl restart gadget_web.service

# Disable auto-present on boot
sudo systemctl disable present_usb_on_boot.service
```

### Hardware Watchdog Configuration

The hardware watchdog automatically reboots the Pi if the system becomes unresponsive. The default configuration is intentionally simple and reliable:

```bash
watchdog-device = /dev/watchdog
watchdog-timeout = 90
max-load-1 = 24
realtime = yes
priority = 1
```

**⚠️ Warning: Aggressive watchdog settings can cause boot loops!**

The following options should be **avoided** on Raspberry Pi Zero 2 W (512MB RAM):

| Setting | Problem |
|---------|---------|
| `min-memory = 50000` | Pi Zero 2 W often has <50MB free during normal operation, triggering unnecessary reboots |
| `repair-binary = /usr/lib/watchdog/repair` | This file doesn't exist on Raspberry Pi OS |
| `interval` (low values) | Can cause timing issues with the kernel watchdog |

**If your device keeps rebooting in a loop**, the watchdog configuration may be too aggressive. To fix:

1. Pull the SD card and mount it on another computer
2. Edit `cmdline.txt` on the boot partition and add: `systemd.mask=watchdog.service`
3. Boot the Pi and SSH in
4. Fix `/etc/watchdog.conf` to use the simple configuration above
5. Remove the mask from `cmdline.txt` and reboot

**Watchdog timeout**: Set to 90 seconds to accommodate (a) large disk images (400GB+) which take longer to configure at boot and (b) transient SDIO bus contention on the Pi Zero 2 W during heavy archive catch-up. Smaller images work fine with 15 seconds, but 90 seconds is safe for all configurations and prevents spurious reboots when the watchdog daemon is briefly stalled by the shared SDIO controller (SD card + WiFi chip).

## Troubleshooting

### Common Issues

**Web interface not accessible:**
```bash
# Check service status and logs
sudo systemctl status gadget_web.service
sudo journalctl -u gadget_web.service -f
```

**Videos not showing:**
- Verify correct mode (Present or Edit, not Unknown)
- Check TeslaCam folder exists on drive 1
- Confirm drive is properly mounted

**Samba shares appear empty:**
```bash
# Force Samba refresh
sudo smbcontrol all close-share gadget_part1
sudo smbcontrol all close-share gadget_part2
sudo smbcontrol all close-share gadget_part3  # if music enabled
sudo systemctl restart smbd nmbd
```

**Tesla not recognizing new lock chime:**
Try these steps in order:
1. Power cycle Tesla (close doors, walk away 5+ minutes, wake up)
2. Switch USB modes (Edit → wait 10s → Present)
3. Physical reconnect (unplug Pi, wait 10s, plug back in)
4. Tesla reboot (hold both scroll wheels until screen goes black)

**Operation in Progress banner stuck:**
```bash
# Check and remove stale lock file if older than 120 seconds
ls -lh ~/TeslaUSB/.quick_edit_part2.lock
rm ~/TeslaUSB/.quick_edit_part2.lock
```

**iOS file upload not working:**
- Use **Safari** on iOS (third-party browsers have restricted file access)
- Desktop browsers work normally regardless of choice

### Logs

```bash
# Web interface logs
sudo journalctl -u gadget_web.service -f

# Chime scheduler logs
sudo journalctl -u chime_scheduler.service -f

# System USB logs
sudo dmesg | grep -i "mass_storage\|gadget"
```

## Technical Details

**Multi-Drive Architecture:**
- Two or three separate disk images:
  - `usb_cam.img` (exFAT) — TeslaCam recordings
  - `usb_lightshow.img` (FAT32) — LightShow, Chimes, and Wraps
  - `usb_music.img` (FAT32, optional) — Music playback
- Sparse files (only use disk space as data is written)
- Presented as multi-LUN USB gadget to Tesla

**USB Gadget Implementation:**
- Linux `g_mass_storage` kernel module via `libcomposite`
- LUN 0: Read-write (ro=0) for TeslaCam recordings
- LUN 1: Read-only (ro=1) for LightShow/Chimes
- LUN 2: Read-only (ro=1) for Music (optional, when `music_enabled: true`)

**Concurrency Protection:**
- `.quick_edit_part2.lock` file prevents race conditions during temporary RW mounts
- Shared lock for quick-edit operations on both part2 (LightShow) and part3 (Music)
- 10-second timeout, 120-second stale lock detection
- **Task coordinator**: Global exclusive lock prevents geo-indexer, video archiver, and cloud sync from running simultaneously (30-minute stale lock auto-clear)
- All services and scripts respect lock state

**Performance Optimizations:**
- **Boot time**: ~14 seconds on Pi Zero 2 W (detects existing RW mount at boot to skip unnecessary remount operations)
- **Configuration loading**: Single YAML parse with secure eval (properly quoted values prevent command injection)
- **Web UI responsiveness**: Settings page loads in ~0.4s (optimized from 133s through batched configuration reads)
- **Memory efficiency**: Desktop services disabled, 1GB swap enabled for stable operation on 512MB RAM

