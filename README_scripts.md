# Script Templates and Sources

This directory contains the template files and source scripts used by the Tesla USB Gadget setup.

## Directory Structure

```
├── scripts/           # Source script files with placeholders
│   ├── present_usb.sh    # USB gadget presentation script (with read-only mounts)
│   ├── edit_usb.sh       # Edit mode (mount + Samba) script
│   └── web_control.py    # Flask web interface with video browser
├── templates/         # Systemd service templates
│   ├── gadget_web.service           # Web interface service
│   └── present_usb_on_boot.service  # Auto-present service
└── README_scripts.md  # This file
```

## How It Works

1. **Template Processing**: The `setup_usb.sh` script processes these template files and replaces placeholders with actual configuration values.

2. **Placeholders**: Template files contain placeholders like `__GADGET_DIR__`, `__TARGET_USER__`, etc. that get replaced during setup.

3. **Output**: Configured files are copied to the gadget directory (e.g., `/home/pi/TeslaUSB/`) and made executable.

## Benefits of This Approach

- **Maintainability**: Each script is in its own file, making them easier to edit and version control
- **Reusability**: Templates can be used across different installations with different configurations  
- **Debugging**: Individual scripts can be tested and modified without regenerating from a monolithic setup script
- **Version Control**: Scripts are proper source files that can be tracked in git with meaningful diffs

## Modifying Scripts

To modify the behavior:

1. **Edit the source files** in `scripts/` or `templates/` directories
2. **Re-run setup** to apply changes: `sudo ./setup_usb.sh`
3. **Or manually copy** and configure individual files as needed

## Placeholders Used

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `__GADGET_DIR__` | Installation directory | `/home/pi/TeslaUSB` |
| `__IMG_CAM__` | TeslaCam disk image filename | `usb_cam.img` |
| `__IMG_LIGHTSHOW__` | Lightshow disk image filename | `usb_lightshow.img` |
| `__MNT_DIR__` | Mount point directory | `/mnt/gadget` |
| `__TARGET_USER__` | Target username | `mhacker` |
| `__WEB_PORT__` | Web interface port | `5000` |
| `__SECRET_KEY__` | Flask secret key | `random_hex_string` |

The `present_usb.sh` and `edit_usb.sh` templates update a shared state file (`state.txt`) inside the gadget directory so the web interface can display the current mode.

## Script Features

### present_usb.sh - Dual-LUN USB Gadget Mode
Switches the Raspberry Pi to USB gadget mode with optimized dual-LUN configuration:

**Dual-LUN Architecture:**
- **LUN 0 (TeslaCam)**: Read-write access (ro=0) for dashcam and sentry recordings
  - Uses `usb_cam.img` (large exFAT partition, 400GB+)
  - Mounted locally at `/mnt/gadget/part1-ro` (read-only)
- **LUN 1 (LightShow)**: Read-only access (ro=1) for 15-30% performance improvement
  - Uses `usb_lightshow.img` (smaller FAT32 partition, 20GB)
  - Mounted locally at `/mnt/gadget/part2-ro` (read-only)
  - Optimized for fast loading of lock chimes and light show files

**Configfs API Details:**
- Uses Linux USB Gadget configfs API at `/sys/kernel/config/usb_gadget/teslausb`
- **Critical Requirement**: LUN attributes (removable, ro, cdrom) MUST be set BEFORE the file attribute
- Once `file` is assigned, attributes become read-only and cannot be changed
- Cleanup must remove LUN subdirectories (`lun.0`, `lun.1`) before removing parent function

**Process:**
1. Stops thumbnail generator service
2. Unmounts read-write partitions from edit mode
3. Runs filesystem checks (fsck) on both images
4. Creates USB gadget configfs structure:
   - Creates gadget at `/sys/kernel/config/usb_gadget/teslausb`
   - Sets USB device descriptors (idVendor, idProduct, strings)
   - Creates configuration and function (mass_storage.usb0)
   - Creates two LUN directories (`lun.0` and `lun.1`)
   - Sets attributes for each LUN (removable, ro, cdrom) BEFORE file
   - Assigns disk images to LUN file attributes LAST
   - Links function to configuration
   - Binds to UDC (USB Device Controller)
5. Mounts both images locally in read-only mode for browsing
6. Updates state file to "present"

**Example Configfs Commands:**
```bash
# LUN 0 - Read-write TeslaCam
echo 1 > functions/mass_storage.usb0/lun.0/removable
echo 0 > functions/mass_storage.usb0/lun.0/ro        # Must be set before file!
echo 0 > functions/mass_storage.usb0/lun.0/cdrom
echo "$IMG_CAM" > functions/mass_storage.usb0/lun.0/file

# LUN 1 - Read-only Lightshow (optimized)
mkdir functions/mass_storage.usb0/lun.1
echo 1 > functions/mass_storage.usb0/lun.1/removable
echo 1 > functions/mass_storage.usb0/lun.1/ro        # Read-only optimization!
echo 0 > functions/mass_storage.usb0/lun.1/cdrom
echo "$IMG_LIGHTSHOW" > functions/mass_storage.usb0/lun.1/file
```

### edit_usb.sh - Network Edit Mode
Switches the system to edit mode with local RW mounts and Samba shares:

**Process:**
1. Stops thumbnail generator service
2. Tears down USB gadget using configfs API:
   - Unbinds from UDC (critical: must unbind before removing gadget)
   - Removes configuration link
   - Removes LUN subdirectories (`lun.0`, `lun.1`) - **Must be done before removing function!**
   - Removes function directory (`mass_storage.usb0`)
   - Removes configuration, strings, gadget directories
3. Unmounts read-only partitions from present mode
4. Runs filesystem checks (fsck) on both images
5. Mounts both images locally in read-write mode:
   - `/mnt/gadget/part1` - Read-write access to TeslaCam
   - `/mnt/gadget/part2` - Read-write access to LightShow
6. Starts Samba services for network file access
7. Updates state file to "edit"

**Dual-LUN Cleanup:**
The script includes special logic to remove LUN subdirectories:
```bash
# Remove LUNs from functions directory before removing function itself
sudo rmdir "$CONFIGFS_GADGET"/functions/mass_storage.usb0/lun.* 2>/dev/null || true
sudo rmdir "$CONFIGFS_GADGET"/functions/mass_storage.usb0 2>/dev/null || true
```

Without this cleanup, the `rmdir` of `functions/mass_storage.usb0` would fail because the directory contains `lun.0` and `lun.1` subdirectories.

### web_control.py
- Flask web application providing:
  - Mode switching interface (Present/Edit)
  - Real-time mode status display
  - **TeslaCam video browser** with folder navigation
  - **In-browser video playback**
  - **Video download functionality**
  - **Lock Chimes management** with upload, playback, set as active chime, and delete
  - **Light Shows management** with upload, playback, grouped file display (fseq+mp3), and delete
  - Network share information display
- Automatically detects current mode and adapts functionality
- Works with both read-only (present) and read-write (edit) mounts
- Cache-busting for audio playback ensures fresh content after file updates
- Loading indicators prevent multiple submissions during operations