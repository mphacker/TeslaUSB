# TeslaUSB AI Coding Guide

## Project Overview
TeslaUSB transforms a Raspberry Pi into a dual-mode USB gadget for Tesla vehicles. The Pi appears as a USB storage device to Tesla (for dashcam/sentry recording) while providing network access and a web interface for file management. This is a **hardware-interfacing project** with Linux USB gadget kernel modules, not a typical web app.

## Architecture: Dual-Mode System

### Present Mode (USB Gadget Active)
- **g_mass_storage** kernel module loaded, Pi appears as USB drive to Tesla
- Partitions mounted **read-only** at `/mnt/gadget/part1-ro` and `/mnt/gadget/part2-ro`
- Samba services stopped (no network writes while Tesla is writing)
- Web UI allows viewing/playback but no file modifications
- Auto-activated on boot via `present_usb_on_boot.service`

### Edit Mode (Network Access)
- USB gadget disconnected, partitions mounted **read-write** at `/mnt/gadget/part1` and `/mnt/gadget/part2`
- Samba shares active for Windows/network file access
- Web UI enables full CRUD operations (upload, delete, manage files)
- Mode switching handled by `present_usb.sh` and `edit_usb.sh` scripts

### Critical State Management
- `state.txt` tracks current mode ("present" or "edit")
- **All file operations** must check current mode and use correct mount paths:
  - Present mode: `/mnt/gadget/part1-ro`, `/mnt/gadget/part2-ro` (read-only)
  - Edit mode: `/mnt/gadget/part1`, `/mnt/gadget/part2` (read-write)
- Functions `get_mount_path()` and `iter_all_partitions()` in `web_control.py` handle this logic

## Template-Based Configuration System

### How It Works
1. Source files in `scripts/` and `templates/` contain placeholders like `__GADGET_DIR__`, `__TARGET_USER__`, `__MNT_DIR__`
2. `setup_usb.sh` uses `sed` to replace placeholders with actual configuration values
3. Configured files are copied to `GADGET_DIR` (default: `/home/pi/TeslaUSB/`)
4. Systemd services reference the configured scripts

### Key Placeholders
- `__GADGET_DIR__`: Installation directory (e.g., `/home/pi/TeslaUSB`)
- `__IMG_NAME__`: Disk image file (`usb_dual.img`)
- `__MNT_DIR__`: Mount directory (`/mnt/gadget`)
- `__TARGET_USER__`: Linux user running services (detected via `$SUDO_USER` or defaults to `pi`)
- `__SECRET_KEY__`: Flask secret for sessions

**Never hardcode paths** - always use placeholders in template files. When testing locally, run `setup_usb.sh` to generate configured scripts.

## Critical Developer Workflows

### Testing Mode Switching
```bash
# Switch to present mode (as root, required for kernel module operations)
sudo /home/pi/TeslaUSB/present_usb.sh

# Switch to edit mode
sudo /home/pi/TeslaUSB/edit_usb.sh

# Check current mode
cat /home/pi/TeslaUSB/state.txt
```

### Debugging Web Interface
```bash
# View Flask logs
sudo journalctl -u gadget_web.service -f

# Restart web service after code changes
sudo systemctl restart gadget_web.service

# Test manually without systemd
cd /home/pi/TeslaUSB
python3 web_control.py
```

### Filesystem Safety Rules
1. **Always sync before mode switches**: `sync; sleep 1` before unmounting
2. **Use fsck during transitions**: Both mode scripts run filesystem checks
3. **Lazy unmount as last resort**: `umount -lf` only after retries with `fuser -km`
4. **Check mount namespace**: Use `nsenter --mount=/proc/1/ns/mnt` for system-wide mount visibility (systemd mount isolation issue)

### Modifying Templates
```bash
# 1. Edit source template in scripts/ or templates/
nano scripts/web_control.py

# 2. Re-run setup to regenerate configured files
sudo ./setup_usb.sh

# 3. Restart relevant services
sudo systemctl restart gadget_web.service
```

## Project-Specific Conventions

### Bash Scripts (`present_usb.sh`, `edit_usb.sh`)
- **Always use `set -euo pipefail`** for error safety
- **Unmount with retry logic**: 3 attempts with `fuser -km` before lazy unmount
- **Ephemeral loop devices for fsck**: Create temporary loop, run fsck, detach immediately
- **Update state file last**: Write to `state.txt` only after successful mode switch
- **Stop background services first**: Kill `thumbnail_generator.service` before mount operations

### Python Web App (`web_control.py`)
- **Mode-aware file access**: Always call `get_mount_path(partition)` or `iter_all_partitions()` instead of hardcoding paths
- **Samba cache busting**: After file uploads/deletes, call `close_samba_share()` and `restart_samba_services()` to force Samba to see changes
- **Read-only mode detection**: Check `current_mode()` before enabling delete/upload buttons in UI
- **MD5 verification**: After setting active lock chime, verify file integrity with hash check
- **Thumbnail caching**: Store in persistent `GADGET_DIR/thumbnails`, not `/tmp` (survives reboots)

### Systemd Services
- **gadget_web.service**: Flask app runs as `TARGET_USER` (not root), `MountFlags=shared` ensures mounts visible in service namespace
- **present_usb_on_boot.service**: Oneshot service, runs as root, ensures USB gadget active after reboot
- **thumbnail_generator.timer**: Runs every 15 minutes, uses `Nice=19` and `IOSchedulingClass=idle` for low priority

## Integration Points

### USB Gadget Kernel Module
- Module: `g_mass_storage` (legacy) via `libcomposite`
- Configuration: `/boot/firmware/config.txt` must have `dtoverlay=dwc2` under `[all]` section
- Loading: `modprobe g_mass_storage file=/path/to/img removable=1 ro=0 stall=0`
- **Critical**: Must unbind UDC before rmmod to prevent hangs

### Samba Network Shares
- Config: `/etc/samba/smb.conf` with `gadget_part1` and `gadget_part2` shares
- Authentication: User must exist in both Linux and Samba password database (`smbpasswd -a`)
- Force reload: `smbcontrol all reload-config` + `smbcontrol all close-share <name>` after file changes

### FFmpeg Thumbnail Generation
- Background process scans for `*.mp4` in `TeslaCam/` folders
- Extracts frame at 1 second: `ffmpeg -ss 1 -i video.mp4 -vframes 1 -s 320x180 thumbnail.jpg`
- Hash-based naming: `{md5(path_mtime_size)}.jpg` for cache consistency
- Orphan cleanup: Removes thumbnails for deleted videos

## Common Pitfalls

1. **Mount namespace isolation**: If mounts aren't visible in web service, check `MountFlags=shared` in service file
2. **Stale Samba cache**: Always call `close_samba_share()` after file operations, or Windows won't see new files
3. **Partition format for large drives**: Use exFAT for partitions >32GB, FAT32 for smaller (see `setup_usb.sh` logic)
4. **Sudoers configuration required**: Scripts need passwordless sudo for `modprobe`, `mount`, `systemctl` operations
5. **Loop device cleanup**: Always detach with `losetup -d` in cleanup trap to prevent resource leaks

## Key Files Reference
- `setup_usb.sh`: Main installer, creates image, configures services (run this first)
- `scripts/present_usb.sh`: Template for USB gadget mode script
- `scripts/edit_usb.sh`: Template for network edit mode script  
- `scripts/web_control.py`: Flask app with video browser, lock chime, and light show management
- `scripts/generate_thumbnails.py`: Background thumbnail generator with orphan cleanup
- `cleanup.sh`: Complete removal script (stops services, unmounts, removes configs)
- `templates/*.service`: Systemd service templates with placeholder substitution

## Testing Checklist
- [ ] Mode switch works in both directions without errors
- [ ] Files uploaded in Edit mode appear in Samba shares immediately
- [ ] Videos play in Present mode with read-only access
- [ ] Thumbnails generate and persist across reboots
- [ ] Web UI shows correct mode indicator and enables/disables buttons appropriately
- [ ] Tesla recognizes Pi as USB drive after switching to Present mode
