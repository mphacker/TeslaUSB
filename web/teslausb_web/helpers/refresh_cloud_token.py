#!/usr/bin/env python3
"""Refresh stored cloud OAuth tokens for cron or systemd timers."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from teslausb_web.config import ConfigError, load_config
from teslausb_web.services.cloud_oauth_service import (
    OAuthError,
    TokenRefreshError,
    make_oauth_service,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to teslausb-web.toml")
    parser.add_argument(
        "--provider",
        choices=("dropbox", "google-drive", "onedrive"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh even if the token is not near expiry",
    )
    parser.add_argument(
        "--allow-defaults",
        action="store_true",
        help="Allow built-in defaults when the config file is absent",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [refresh-cloud-token] %(message)s",
    )
    try:
        config = load_config(args.config, allow_defaults=args.allow_defaults)
        service = make_oauth_service(config)
        result = service.refresh_if_needed(force=args.force, provider=args.provider)
    except (ConfigError, OAuthError, TokenRefreshError) as exc:
        logger.error("Cloud token refresh failed: %s", exc)
        return 1
    if result.credentials is None:
        logger.info(result.message)
        return 0
    logger.info(
        "%s for %s (expires_at=%s)",
        result.message,
        result.credentials.provider,
        result.credentials.expires_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
