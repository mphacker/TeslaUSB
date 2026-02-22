#!/bin/bash
set -euo pipefail

# TeslaUSB Upgrade Script
# This script pulls the latest code from GitHub and runs setup
# Supports both git-cloned installations and manual installations

REPO_URL="https://github.com/mphacker/TeslaUSB"
ARCHIVE_BASE_URL="https://github.com/mphacker/TeslaUSB/archive/refs/heads"
# Auto-derive install directory from this script's location (run-in-place)
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
    TEMP_DIR=$(mktemp -d)
    ARCHIVE_FILE="$TEMP_DIR/repo.tar.gz"
    EXTRACT_DIR="$TEMP_DIR/src"
    mkdir -p "$EXTRACT_DIR"

    ARCHIVE_DOWNLOAD_URL="${ARCHIVE_BASE_URL}/${BRANCH}.tar.gz"
    echo "Downloading archive: $ARCHIVE_DOWNLOAD_URL"
    curl -fsSL "$ARCHIVE_DOWNLOAD_URL" -o "$ARCHIVE_FILE" || { echo "Failed to download repository archive"; exit 1; }

    echo "Extracting archive..."
    tar -xzf "$ARCHIVE_FILE" -C "$EXTRACT_DIR" --strip-components=1 || { echo "Failed to extract repository archive"; exit 1; }

    echo "Copying files to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    cp -a "$EXTRACT_DIR/." "$INSTALL_DIR/" || { echo "Failed to copy files to install directory"; exit 1; }

    # Restore state file if it was backed up
    if [ -f "$BACKUP_DIR/state.txt" ]; then
        cp "$BACKUP_DIR/state.txt" "$INSTALL_DIR/"
    fi

    # Clean up temp directory
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
            sudo ./scripts/edit_usb.sh
        fi
    fi
else
    echo ""
    echo "Skipping setup. You can run it manually later with:"
    echo "  cd $INSTALL_DIR && sudo ./setup_usb.sh"
fi

echo ""
echo "Upgrade process finished!"
