#!/bin/bash
# Graceful reload of gadget_web service
# This script ensures the service reloads without disrupting the USB gadget state

set -euo pipefail

echo "Performing graceful reload of gadget_web service..."

# Check if service is running
if systemctl is-active --quiet gadget_web.service; then
    echo "Service is running, sending reload signal..."
    
    # Try to use reload first (if Flask supports it)
    # Otherwise fall back to restart with minimal downtime
    if systemctl reload gadget_web.service 2>/dev/null; then
        echo "✓ Service reloaded via reload signal"
    else
        echo "Reload not supported, using quick restart..."
        systemctl restart gadget_web.service
        
        # Wait for service to be fully up
        for i in {1..10}; do
            if systemctl is-active --quiet gadget_web.service; then
                echo "✓ Service restarted and active (attempt $i)"
                break
            fi
            sleep 0.5
        done
    fi
else
    echo "Service not running, starting it..."
    systemctl start gadget_web.service
fi

# Verify service is active
if systemctl is-active --quiet gadget_web.service; then
    echo "✓ gadget_web.service is active and running"
    exit 0
else
    echo "✗ Failed to start/reload service"
    systemctl status gadget_web.service --no-pager || true
    exit 1
fi
