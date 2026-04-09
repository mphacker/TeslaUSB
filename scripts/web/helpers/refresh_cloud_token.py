#!/usr/bin/env python3
"""WiFi connect handler — index videos + sync to cloud.

Called by the NetworkManager dispatcher (99-teslausb-cloud-refresh)
whenever wlan0 comes up.  This is the "car arrived home" trigger:

1. Refresh the RO mount (see Tesla's latest writes)
2. Index new videos (build trips, detect events)
3. Sync event clips to cloud (if configured)

The user parks at home, WiFi connects, and within ~60 seconds
everything is fresh — no manual action needed.
"""

import os
import sys

WEB_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, WEB_DIR)

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wifi-handler] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from services.video_service import get_teslacam_path

    teslacam = get_teslacam_path()
    if not teslacam:
        logger.info("TeslaCam path not available — skipping")
        return 0

    # Step 1: Refresh the RO mount so we see Tesla's latest files
    try:
        from services.mapping_service import _refresh_ro_mount
        _refresh_ro_mount(teslacam)
        logger.info("RO mount refreshed")
    except Exception as e:
        logger.warning("Mount refresh failed (non-fatal): %s", e)

    # Step 1.5: Archive RecentClips to SD card (before Tesla deletes them)
    try:
        from config import ARCHIVE_ENABLED
        if ARCHIVE_ENABLED:
            from services.video_archive_service import trigger_archive_now
            logger.info("Archiving RecentClips...")
            trigger_archive_now()
            # Don't wait for completion — it runs in background
            logger.info("RecentClips archive triggered")
    except Exception as e:
        logger.warning("RecentClips archive failed (non-fatal): %s", e)

    # Step 2: Index new videos (builds trips, detects events)
    try:
        from config import (
            MAPPING_ENABLED, MAPPING_DB_PATH,
            MAPPING_SAMPLE_RATE, MAPPING_EVENT_THRESHOLDS,
            MAPPING_TRIP_GAP_MINUTES,
        )
        if MAPPING_ENABLED:
            from services.mapping_service import trigger_auto_index
            logger.info("Indexing new videos...")
            trigger_auto_index(
                db_path=MAPPING_DB_PATH,
                teslacam_path=teslacam,
                sample_rate=MAPPING_SAMPLE_RATE,
                thresholds=MAPPING_EVENT_THRESHOLDS,
                trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
            )
            # Wait for indexing to complete before syncing
            # (so newly indexed events can be synced)
            import time
            from services.mapping_service import get_indexer_status
            for _ in range(120):  # max 10 minutes
                if not get_indexer_status().get('running'):
                    break
                time.sleep(5)
            logger.info("Video indexing complete")

            # Step 2.5: Index ArchivedClips on SD card (if enabled)
            try:
                from config import ARCHIVE_ENABLED, ARCHIVE_DIR, MAPPING_ARCHIVE_INDEXING
                if MAPPING_ARCHIVE_INDEXING and ARCHIVE_ENABLED and os.path.isdir(ARCHIVE_DIR):
                    logger.info("Indexing ArchivedClips at %s...", ARCHIVE_DIR)
                    trigger_auto_index(
                        db_path=MAPPING_DB_PATH,
                        teslacam_path=ARCHIVE_DIR,
                        sample_rate=MAPPING_SAMPLE_RATE,
                        thresholds=MAPPING_EVENT_THRESHOLDS,
                        trip_gap_minutes=MAPPING_TRIP_GAP_MINUTES,
                    )
                    for _ in range(120):
                        if not get_indexer_status().get('running'):
                            break
                        time.sleep(5)
                    logger.info("ArchivedClips indexing complete")
            except Exception as e:
                logger.warning("ArchivedClips indexing failed (non-fatal): %s", e)
    except Exception as e:
        logger.warning("Video indexing failed (non-fatal): %s", e)

    # Step 3: Sync event clips to cloud (if configured)
    # Trigger sync via the web API so the thread runs inside the
    # long-lived gadget_web process.  Direct imports would start a
    # daemon thread in THIS process which dies when the script exits.
    try:
        from config import CLOUD_ARCHIVE_ENABLED, CLOUD_PROVIDER_CREDS_PATH
        if CLOUD_ARCHIVE_ENABLED and os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
            import urllib.request
            logger.info("Triggering cloud sync via web API...")
            req = urllib.request.Request(
                "http://localhost/cloud/api/sync_now",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}",
            )
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                logger.info("Cloud sync triggered: %s", resp.read().decode()[:200])
            except Exception as e:
                logger.warning("Cloud sync API call failed: %s", e)
        else:
            logger.info("Cloud archive not configured — skipping sync")
    except Exception as e:
        logger.warning("Cloud sync trigger failed (non-fatal): %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
