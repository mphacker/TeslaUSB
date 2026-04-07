#!/usr/bin/env python3
"""Cloud token refresh + auto-sync on WiFi connect.

Called by the NetworkManager dispatcher (99-teslausb-cloud-refresh)
whenever wlan0 comes up.  Refreshes the OAuth token, then triggers
an automatic cloud sync of event clips if sync is enabled.

Runs as a low-priority background task.
"""

import os
import sys

WEB_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, WEB_DIR)

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cloud-sync] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from config import CLOUD_PROVIDER_CREDS_PATH, CLOUD_ARCHIVE_ENABLED

    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        logger.info("No cloud credentials — skipping")
        return 0

    if not CLOUD_ARCHIVE_ENABLED:
        logger.info("Cloud archive disabled — skipping")
        return 0

    # Trigger auto-sync (checks sync_enabled, WiFi, not-already-running)
    try:
        from config import CLOUD_ARCHIVE_DB_PATH
        from services.video_service import get_teslacam_path
        from services.cloud_archive_service import trigger_auto_sync

        teslacam = get_teslacam_path()
        if teslacam:
            logger.info("WiFi connected — triggering cloud sync")
            trigger_auto_sync(teslacam, CLOUD_ARCHIVE_DB_PATH)
        else:
            logger.info("TeslaCam path not available — skipping sync")
    except Exception as e:
        logger.warning("Auto-sync trigger failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
