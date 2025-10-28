# Tesla USB Gadget Setup

A comprehensive Raspberry Pi setup script that creates a USB mass storage gadget with dual partitions for Tesla dashcam and sentry mode recording.

## Overview

This script (`setup-usb.sh`) transforms your Raspberry Pi into a USB storage device that appears as two separate drives when connected to a Tesla vehicle. It provides both manual control scripts and a web interface for managing the USB gadget functionality.

## Features

- **Dual Partition USB Gadget**: Creates two separate FAT32 partitions (configurable sizes)
- **Samba Network Sharing**: Access files remotely via network shares with authentication
- **Web Control Interface**: Browser-based control panel for switching between modes
- **Live Mode Indicator**: Web UI shows whether the gadget is in USB or Edit mode
- **Auto-Boot Presentation**: Automatically presents the USB gadget when Pi boots
- **Manual Control Scripts**: Command-line scripts for switching between present/edit modes
- **Robust Error Handling**: Comprehensive error checking and cleanup for reliability
- **Dynamic User Support**: Works with any user account (detects SUDO_USER automatically)

## Requirements

- Raspberry Pi with USB OTG capability (Pi Zero, Pi 4, etc.)
- Raspberry Pi OS (tested on recent versions)
- Root/sudo access for installation
- Internet connection for package installation

## Quick Start

1. **Clone and run the setup script:**
   ```bash
   git clone <repository-url>
   cd TeslaUSB
   sudo ./setup-usb.sh
   ```

2. **The script will:**
   - Install required packages (parted, dosfstools, python3-flask, samba)
   - Configure USB gadget kernel module
   - Create a dual-partition disk image
   - Set up Samba shares for network access
   - Create control scripts and web interface
   - Configure systemd services for auto-start

3. **Access the web interface:**
   - Open `http://<pi-ip-address>:5000` in your browser
   - Use the buttons to switch between "Present USB" and "Edit USB" modes

4. **To remove everything later:**
   ```bash
   # Navigate to the gadget directory and run cleanup
   cd /home/pi/TeslaUSB  # or your configured GADGET_DIR
   sudo ./cleanup.sh
   ```

## Configuration

Edit the configuration section at the top of `setup-usb.sh`:

```bash
# ================= Configuration =================
GADGET_DIR_DEFAULT="/home/pi/TeslaUSB"  # Installation directory
IMG_NAME="usb_dual.img"                    # Disk image filename
PART1_SIZE="16G"                           # First partition size
PART2_SIZE="16G"                           # Second partition size
LABEL1="TeslaCam"                           # First partition label
LABEL2="LightShow"                           # Second partition label
MNT_DIR="/mnt/gadget"                      # Mount point directory
CONFIG_FILE="/boot/firmware/config.txt"   # Pi config file location
WEB_PORT=5000                              # Web interface port
SAMBA_PASS="tesla"                         # Samba password
```

## Usage Modes

### Present USB Mode
When in this mode:
- Pi appears as a USB storage device to connected host (Tesla)
- Samba shares are stopped
- Partitions are unmounted for exclusive USB access
- Tesla can record dashcam/sentry footage directly

**Activate via:**
- Web interface: Click "Present USB Gadget"
- Command line: `sudo /home/pi/TeslaUSB/present_usb.sh`
- Auto-activated on boot by default

### Edit USB Mode  
When in this mode:
- USB gadget is disconnected
- Partitions are mounted locally on Pi
- Samba shares are active for network access
- You can manage files via network or direct Pi access

**Activate via:**
- Web interface: Click "Edit USB (mount + Samba)"
- Command line: `sudo /home/pi/TeslaUSB/edit_usb.sh`

## Network Access

When in Edit USB mode, access files via Samba shares:

- **Share 1**: `\\<pi-ip-address>\gadget_part1`
- **Share 2**: `\\<pi-ip-address>\gadget_part2`
- **Username**: Your Pi username (or SUDO_USER if run with sudo)
- **Password**: Value set in `SAMBA_PASS` (default: "tesla")

## Project Structure

```
TeslaUSB/
├── setup-usb.sh              # Main setup script
├── cleanup.sh                # Cleanup script  
├── scripts/                  # Source script templates
│   ├── present_usb.sh           # USB gadget presentation script
│   ├── edit_usb.sh              # Edit mode script  
│   └── web_control.py           # Flask web interface
├── templates/                # Systemd service templates
│   ├── gadget_web.service       # Web interface service
│   └── present_usb_on_boot.service # Auto-present service
├── README.md                 # This documentation
└── README_scripts.md         # Script template documentation
```

## Generated Files

The setup script copies and configures template files to the gadget directory:

| File | Source Template | Purpose |
|------|-----------------|---------|
| `usb_dual.img` | *Generated* | Disk image with two FAT32 partitions |
| `present_usb.sh` | `scripts/present_usb.sh` | Script to activate USB gadget mode |
| `edit_usb.sh` | `scripts/edit_usb.sh` | Script to activate edit/mount mode |
| `web_control.py` | `scripts/web_control.py` | Flask web interface application |
| `state.txt` | *Generated* | Stores the last-known USB gadget mode |
| `cleanup.sh` | *Repository file* | Script to safely remove all setup artifacts |

**Note**: Scripts are now maintained as individual template files in the repository, making them easier to update and version control. See `README_scripts.md` for details.

## Customizing Scripts

To modify script behavior:

1. **Edit source files**: Modify files in `scripts/` or `templates/` directories
2. **Re-run setup**: Execute `sudo ./setup-usb.sh` to apply changes
3. **Manual updates**: For testing, you can edit the generated files directly in the gadget directory

**Example**: To add custom logging to the present script:
```bash
# Edit the source template
nano scripts/present_usb.sh

# Re-run setup to apply changes
sudo ./setup-usb.sh
```

## Systemd Services

Two services are installed:

| Service | Purpose | Status |
|---------|---------|---------|
| `gadget_web.service` | Runs web interface on boot | Enabled |
| `present_usb_on_boot.service` | Auto-presents USB on boot | Enabled |

**Service management:**
```bash
# Check web interface status
sudo systemctl status gadget_web.service

# Disable auto-present on boot
sudo systemctl disable present_usb_on_boot.service

# Restart web interface
sudo systemctl restart gadget_web.service
```

## Cleanup and Removal

### Automatic Cleanup Script

The repository includes a comprehensive cleanup script (`cleanup.sh`) that safely removes all files and configurations created by the setup script.

**Usage:**
```bash
# Navigate to the gadget directory
cd /home/pi/TeslaUSB  # or your configured GADGET_DIR

# Run the cleanup script (requires sudo)
sudo ./cleanup.sh
```

**What the cleanup script removes:**
- **Systemd Services**: Stops and removes `gadget_web.service` and `present_usb_on_boot.service`
- **USB Gadget Module**: Safely removes `g_mass_storage` kernel module
- **Loop Devices**: Detaches any loop devices associated with the disk image
- **Mount Points**: Unmounts partitions and removes mount directories (`/mnt/gadget`)
- **Samba Configuration**: Removes gadget share sections from `/etc/samba/smb.conf`
- **Generated Files**: Removes all scripts, web interface, and disk image
- **System Configuration**: Reloads systemd and restarts Samba

**Safety Features:**
- **Confirmation Prompt**: Asks for confirmation before proceeding
- **Resource Cleanup**: Ensures proper cleanup of system resources before file removal
- **Error Resilience**: Continues cleanup even if individual steps fail
- **Backup Creation**: Creates backup of Samba configuration before modification
- **Root Requirement**: Ensures proper permissions for system-level cleanup

**Example Output:**
```bash
Tesla USB Gadget Cleanup Script
===============================
Gadget directory: /home/pi/TeslaUSB
Image file: /home/pi/TeslaUSB/usb_dual.img

This will remove all USB gadget configuration and files.
The following will be cleaned up:
  - Systemd services (gadget_web, present_usb_on_boot)
  - USB gadget module and loop devices
  - Samba share configuration
  - Mount directories (/mnt/gadget)
  - All files in /home/pi/TeslaUSB (except this script)
  - Disk image: /home/pi/TeslaUSB/usb_dual.img

Are you sure you want to proceed? (y/N): y

Starting cleanup process...
[... detailed cleanup steps ...]
Cleanup completed successfully!
```

**Note**: The cleanup script preserves itself, so you can run it multiple times if needed. You may delete it manually after cleanup is complete.

### Manual Package Removal

After running the cleanup script, you can optionally remove the packages that were installed:

```bash
# Remove installed packages (optional)
sudo apt remove --autoremove python3-flask samba samba-common-bin

# Note: parted, dosfstools, and util-linux are usually system packages
# and should not be removed unless you're certain they're not needed
```

## Troubleshooting

### Common Issues

**"unbound variable" errors:**
- Ensure the script has been updated with the latest variable escaping fixes
- Check that `set -euo pipefail` is compatible with your bash version

**Partition nodes not appearing:**
- The script waits up to 10 seconds for partition nodes to appear
- On slower systems, this may indicate hardware or kernel issues

**Web interface not accessible:**
- Check that port 5000 is open: `sudo netstat -tulpn | grep 5000`
- Verify service is running: `sudo systemctl status gadget_web.service`

**Samba access denied:**
- Verify username and password match the configuration
- Check that user exists in Samba: `sudo pdbedit -L`

### Log Files

Check systemd logs for issues:
```bash
# Web interface logs
sudo journalctl -u gadget_web.service -f

# Auto-present service logs  
sudo journalctl -u present_usb_on_boot.service

# General system logs
sudo dmesg | grep -i "mass_storage\|gadget"
```

### Manual Cleanup

**For automatic cleanup, use the provided cleanup script instead:**
```bash
cd /home/pi/TeslaUSB  # or your GADGET_DIR
sudo ./cleanup.sh
```

**If you need to manually clean up (emergency situations only):**
```bash
# Remove USB gadget module
sudo rmmod g_mass_storage

# Detach loop devices
sudo losetup -D

# Unmount partitions
sudo umount /mnt/gadget/part1 /mnt/gadget/part2

# Stop services
sudo systemctl stop gadget_web.service present_usb_on_boot.service
```

## Security Considerations

- **Samba Password**: The default password "tesla" should be changed for production use
- **Web Interface**: Runs on all interfaces (0.0.0.0) - consider firewall rules
- **File Permissions**: Uses umask 002 for group write access

## Technical Details

### Disk Image Structure
- **Total Size**: PART1_SIZE + PART2_SIZE + 2MB (for partition table)
- **Partition Table**: MBR/MSDOS style
- **File System**: FAT32 for both partitions
- **Sparse File**: Only uses space as needed

### USB Gadget Implementation
- Uses Linux `g_mass_storage` kernel module
- Configured as removable storage (removable=1)
- Write access enabled (ro=0)
- No command stalling (stall=0)

### Error Handling Features
- Comprehensive error checking for all operations
- Automatic cleanup of loop devices on failure
- Trap handlers for graceful script interruption
- Partition detection polling (avoids race conditions)

