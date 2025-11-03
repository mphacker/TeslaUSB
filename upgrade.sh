#!/bin/bash
set -euo pipefail

# TeslaUSB Upgrade Script
# This script pulls the latest code from GitHub and runs setup
# Supports both git-cloned installations and manual installations

REPO_URL="https://github.com/mphacker/TeslaUSB"
RAW_URL="https://raw.githubusercontent.com/mphacker/TeslaUSB"
INSTALL_DIR="/home/pi/TeslaUSB"
BRANCH="main"
BACKUP_DIR=""

# Cleanup function for error handling
cleanup_on_error() {
    local exit_code=$?
    if [ $exit_code -ne 0 ] && [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
        echo ""
        echo "============================================"
        echo "ERROR: Upgrade failed with exit code $exit_code"
        echo "============================================"
        echo ""
        echo "Restoring from backup: $BACKUP_DIR"
        
        # Restore backed up files
        if [ -f "$BACKUP_DIR/state.txt" ]; then
            cp "$BACKUP_DIR/state.txt" "$INSTALL_DIR/" 2>/dev/null || true
        fi
        if [ -d "$BACKUP_DIR/thumbnails" ]; then
            rm -rf "$INSTALL_DIR/thumbnails"
            cp -r "$BACKUP_DIR/thumbnails" "$INSTALL_DIR/" 2>/dev/null || true
        fi
        
        echo "Backup restored."
        echo "Removing backup directory..."
        rm -rf "$BACKUP_DIR"
        echo "Backup directory removed."
        echo ""
        echo "System restored to previous state."
        exit $exit_code
    fi
}

# Set trap for error handling (only for non-git path)
trap cleanup_on_error EXIT

echo "==================================="
echo "TeslaUSB Upgrade Script"
echo "==================================="
echo ""

# Store current mode state if it exists
if [ -f "$INSTALL_DIR/state.txt" ]; then
    CURRENT_MODE=$(cat "$INSTALL_DIR/state.txt")
    echo "Current mode: $CURRENT_MODE"
else
    CURRENT_MODE="unknown"
fi
echo ""

# Check if this is a git repository
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Git repository detected - using git pull method"
    echo ""
    
    cd "$INSTALL_DIR"
    
    echo "Current directory: $(pwd)"
    echo "Current branch: $(git branch --show-current)"
    echo ""
    
    # Fetch latest changes
    echo "Fetching latest changes from GitHub..."
    git fetch origin
    
    # Reset any local changes to tracked files (including chmod changes)
    echo "Resetting local changes to tracked files..."
    git reset --hard origin/$BRANCH
    
    # Clean up any untracked files (optional - commented out for safety)
    # git clean -fd
    
else
    echo "No git repository detected - using direct download method"
    echo ""
    
    # Create backup directory with timestamp
    BACKUP_DIR="${INSTALL_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
    echo "Creating backup at: $BACKUP_DIR"
    
    # Backup important files
    mkdir -p "$BACKUP_DIR"
    [ -f "$INSTALL_DIR/state.txt" ] && cp "$INSTALL_DIR/state.txt" "$BACKUP_DIR/"
    [ -f "$INSTALL_DIR/usb_cam.img" ] && echo "Preserving usb_cam.img (not backed up due to size)"
    [ -f "$INSTALL_DIR/usb_lightshow.img" ] && echo "Preserving usb_lightshow.img (not backed up due to size)"
    [ -d "$INSTALL_DIR/thumbnails" ] && cp -r "$INSTALL_DIR/thumbnails" "$BACKUP_DIR/"
    
    echo ""
    echo "Downloading latest files from GitHub..."
    
    # Create temp directory for downloads
    TEMP_DIR=$(mktemp -d)
    cd "$TEMP_DIR"
    
    # Download main scripts and setup files
    echo "Downloading setup files..."
    curl -fsSL "${RAW_URL}/${BRANCH}/setup_usb.sh" -o setup_usb.sh || { echo "Failed to download setup_usb.sh"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/cleanup.sh" -o cleanup.sh || { echo "Failed to download cleanup.sh"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/upgrade.sh" -o upgrade.sh || { echo "Failed to download upgrade.sh"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/readme.md" -o readme.md 2>/dev/null || echo "  (readme.md not found, skipping)"
    curl -fsSL "${RAW_URL}/${BRANCH}/README_scripts.md" -o README_scripts.md 2>/dev/null || echo "  (README_scripts.md not found, skipping)"
    
    # Download scripts directory
    echo "Downloading scripts..."
    mkdir -p scripts
    curl -fsSL "${RAW_URL}/${BRANCH}/scripts/present_usb.sh" -o scripts/present_usb.sh || { echo "Failed to download present_usb.sh"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/scripts/edit_usb.sh" -o scripts/edit_usb.sh || { echo "Failed to download edit_usb.sh"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/scripts/web_control.py" -o scripts/web_control.py || { echo "Failed to download web_control.py"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/scripts/generate_thumbnails.py" -o scripts/generate_thumbnails.py || { echo "Failed to download generate_thumbnails.py"; exit 1; }
    
    # Download templates directory
    echo "Downloading templates..."
    mkdir -p templates
    curl -fsSL "${RAW_URL}/${BRANCH}/templates/gadget_web.service" -o templates/gadget_web.service || { echo "Failed to download gadget_web.service"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/templates/present_usb_on_boot.service" -o templates/present_usb_on_boot.service || { echo "Failed to download present_usb_on_boot.service"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/templates/thumbnail_generator.service" -o templates/thumbnail_generator.service || { echo "Failed to download thumbnail_generator.service"; exit 1; }
    curl -fsSL "${RAW_URL}/${BRANCH}/templates/thumbnail_generator.timer" -o templates/thumbnail_generator.timer || { echo "Failed to download thumbnail_generator.timer"; exit 1; }
    
    echo ""
    echo "Copying files to $INSTALL_DIR..."
    
    # Copy downloaded files to install directory
    cp -f setup_usb.sh "$INSTALL_DIR/"
    cp -f cleanup.sh "$INSTALL_DIR/"
    cp -f upgrade.sh "$INSTALL_DIR/"
    [ -f readme.md ] && cp -f readme.md "$INSTALL_DIR/"
    [ -f README_scripts.md ] && cp -f README_scripts.md "$INSTALL_DIR/"
    
    cp -rf scripts/* "$INSTALL_DIR/scripts/"
    cp -rf templates/* "$INSTALL_DIR/templates/"
    
    # Restore state file if it was backed up
    if [ -f "$BACKUP_DIR/state.txt" ]; then
        cp "$BACKUP_DIR/state.txt" "$INSTALL_DIR/"
    fi
    
    # Clean up temp directory
    cd "$INSTALL_DIR"
    rm -rf "$TEMP_DIR"
    
    echo ""
    echo "Files updated successfully!"
    
    # Delete backup if we got here successfully
    if [ -d "$BACKUP_DIR" ]; then
        echo "Removing backup (upgrade successful)..."
        rm -rf "$BACKUP_DIR"
        echo "Backup removed."
    fi
fi

# Disable error trap for git-based updates (they handle their own errors)
trap - EXIT

# Ensure scripts are executable
echo ""
echo "Setting execute permissions on scripts..."
chmod +x "$INSTALL_DIR/setup_usb.sh"
chmod +x "$INSTALL_DIR/cleanup.sh"
chmod +x "$INSTALL_DIR/upgrade.sh"

echo ""
echo "==================================="
echo "Code updated successfully!"
echo "==================================="
echo ""

# Ask user if they want to run setup
read -p "Do you want to run setup_usb.sh now? [y/n]: " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "Running setup_usb.sh..."
    sudo ./setup_usb.sh
    
    echo ""
    echo "==================================="
    echo "Upgrade complete!"
    echo "==================================="
    
    # Restore previous mode if it was in edit mode
    if [ "$CURRENT_MODE" = "edit" ]; then
        echo ""
        echo "Previous mode was 'edit'. You may want to switch back to edit mode."
        read -p "Switch to edit mode now? [y/n]: " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            sudo ./edit_usb.sh
        fi
    fi
else
    echo ""
    echo "Skipping setup. You can run it manually later with:"
    echo "  cd $INSTALL_DIR && sudo ./setup_usb.sh"
fi

echo ""
echo "Upgrade process finished!"
