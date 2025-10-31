#!/bin/bash
set -euo pipefail

# TeslaUSB Upgrade Script
# This script pulls the latest code from GitHub and runs setup

REPO_URL="https://github.com/mphacker/TeslaUSB.git"
INSTALL_DIR="/home/pi/TeslaUSB"
BRANCH="main"

echo "==================================="
echo "TeslaUSB Upgrade Script"
echo "==================================="
echo ""

# Check if we're in the right directory
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "Error: $INSTALL_DIR is not a git repository"
    echo "This script must be run from a git-cloned installation"
    exit 1
fi

cd "$INSTALL_DIR"

echo "Current directory: $(pwd)"
echo "Current branch: $(git branch --show-current)"
echo ""

# Store current mode state if it exists
if [ -f "state.txt" ]; then
    CURRENT_MODE=$(cat state.txt)
    echo "Current mode: $CURRENT_MODE"
else
    CURRENT_MODE="unknown"
fi
echo ""

# Fetch latest changes
echo "Fetching latest changes from GitHub..."
git fetch origin

# Reset any local changes to tracked files (including chmod changes)
echo "Resetting local changes to tracked files..."
git reset --hard origin/$BRANCH

# Clean up any untracked files (optional - commented out for safety)
# git clean -fd

# Ensure scripts are executable
echo "Setting execute permissions on scripts..."
chmod +x setup_usb.sh
chmod +x cleanup.sh
chmod +x upgrade.sh

echo ""
echo "==================================="
echo "Code updated successfully!"
echo "==================================="
echo ""

# Ask user if they want to run setup
read -p "Do you want to run setup_usb.sh now? (y/n): " -n 1 -r
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
        read -p "Switch to edit mode now? (y/n): " -n 1 -r
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
