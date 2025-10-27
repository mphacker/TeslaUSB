# Script Templates and Sources

This directory contains the template files and source scripts used by the Tesla USB Gadget setup.

## Directory Structure

```
├── scripts/           # Source script files with placeholders
│   ├── present_usb.sh    # USB gadget presentation script
│   ├── edit_usb.sh       # Edit mode (mount + Samba) script
│   └── web_control.py    # Flask web interface
├── templates/         # Systemd service templates
│   ├── gadget_web.service           # Web interface service
│   └── present_usb_on_boot.service  # Auto-present service
└── README.md         # This file
```

## How It Works

1. **Template Processing**: The `setup-usb.sh` script processes these template files and replaces placeholders with actual configuration values.

2. **Placeholders**: Template files contain placeholders like `__GADGET_DIR__`, `__TARGET_USER__`, etc. that get replaced during setup.

3. **Output**: Configured files are copied to the gadget directory (e.g., `/home/mhacker/gadget/`) and made executable.

## Benefits of This Approach

- **Maintainability**: Each script is in its own file, making them easier to edit and version control
- **Reusability**: Templates can be used across different installations with different configurations  
- **Debugging**: Individual scripts can be tested and modified without regenerating from a monolithic setup script
- **Version Control**: Scripts are proper source files that can be tracked in git with meaningful diffs

## Modifying Scripts

To modify the behavior:

1. **Edit the source files** in `scripts/` or `templates/` directories
2. **Re-run setup** to apply changes: `sudo ./setup-usb.sh`
3. **Or manually copy** and configure individual files as needed

## Placeholders Used

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `__GADGET_DIR__` | Installation directory | `/home/mhacker/gadget` |
| `__IMG_NAME__` | Disk image filename | `usb_dual.img` |
| `__MNT_DIR__` | Mount point directory | `/mnt/gadget` |
| `__TARGET_USER__` | Target username | `mhacker` |
| `__WEB_PORT__` | Web interface port | `5000` |
| `__SECRET_KEY__` | Flask secret key | `random_hex_string` |

The `present_usb.sh` and `edit_usb.sh` templates update a shared state file (`state.txt`) inside the gadget directory so the web interface can display the current mode.