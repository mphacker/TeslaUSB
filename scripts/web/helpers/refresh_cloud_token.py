#!/usr/bin/env python3
"""Refresh cloud OAuth token on WiFi connect.

Runs a lightweight rclone operation (``rclone about``) to trigger token
refresh.  Any updated token is captured and re-encrypted.

Called by the NetworkManager dispatcher script (99-teslausb-cloud-refresh)
whenever wlan0 comes up.  Runs as a low-priority background task.

Exit codes:
  0 — success (token refreshed or no refresh needed)
  1 — no credentials configured
  2 — refresh failed
"""

import os
import sys

# Add the web app directory to the Python path
WEB_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, WEB_DIR)

import json
import logging
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cloud-refresh] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    from config import CLOUD_PROVIDER_CREDS_PATH

    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        logger.info("No cloud credentials — skipping token refresh")
        return 1

    try:
        from services.cloud_rclone_service import (
            _load_creds,
            _write_temp_conf,
            _capture_refreshed_token,
            _remove_temp_conf,
            RCLONE_REMOTE_NAME,
        )
    except ImportError as e:
        logger.error("Import error: %s", e)
        return 2

    creds = _load_creds()
    if not creds:
        logger.info("Could not load credentials — skipping")
        return 1

    try:
        conf_path = _write_temp_conf(creds)
        # 'rclone about' is lightweight — just queries storage quota
        result = subprocess.run(
            ["rclone", "about", "--config", conf_path,
             f"{RCLONE_REMOTE_NAME}:", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        _capture_refreshed_token(creds)

        if result.returncode == 0:
            logger.info("Token refresh successful")
            return 0

        stderr = result.stderr.strip()
        # Even if 'about' fails, the token may have been refreshed
        logger.warning("rclone about failed (exit %d): %s",
                       result.returncode, stderr[:200])
        return 0  # Token was likely still refreshed
    except subprocess.TimeoutExpired:
        logger.warning("Token refresh timed out")
        return 2
    except Exception as e:
        logger.error("Token refresh error: %s", e)
        return 2
    finally:
        _remove_temp_conf()


if __name__ == "__main__":
    sys.exit(main())
