"""Probe which media-hub sub-pages should be visible.

v1 used :func:`services.partition_service.get_feature_availability` to
inject ``chimes_available`` / ``music_available`` / ``shows_available``
/ ``wraps_available`` / ``boombox_available`` /
``license_plates_available`` into every template via
``utils.get_base_context``. The B-1 equivalent has to skip the IMG-mount
gate (B-1 has no IMG files; see ``docs/00-PLAN.md`` invariant) and
instead probe the backing-mount directories that the Rust ``teslafat``
worker exposes.

The cascade logic in ``blueprints/media.py`` already does the same probe
for its own redirect decision; this module factors that probe out so the
context processor in ``app.py`` can publish the flags on every page —
otherwise the ``media_hub_nav.html`` pill bar shows only the current
page's pill instead of every available media sub-page (operator-flagged
during H5 hardware test).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_LIGHTSHOW_DIRNAME: Final[str] = "lightshow"
"""LightShow partition mount directory under ``backing_root``.

Mirrors ``blueprints/media.py`` / ``blueprints/lock_chimes.py`` /
``light_shows.py`` / ``wraps.py`` / ``license_plates.py``. Kept as a
module constant here so the context processor never has to import a
blueprint (which would break the layering rule).
"""


def _dir_exists(path: Path) -> bool:
    """Return ``True`` iff ``path`` is a directory we can stat."""
    try:
        return path.is_dir()
    except OSError as exc:
        logger.warning("media_availability: could not stat %s: %s", path, exc)
        return False


def probe_media_availability(cfg: WebConfig) -> dict[str, bool]:
    """Return v1-shaped ``*_available`` flags for every media sub-page.

    Mirrors v1's ``get_feature_availability`` keys so
    ``media_hub_nav.html`` and ``base.html`` can decide which pills /
    nav buttons to render. LightShow-partition pages (chimes, shows,
    wraps, plates) all live at the root of the MEDIA LUN
    (``cfg.paths.media_root``); music + boombox stay on the TeslaCam
    LUN for now (music browsing is a TeslaCam-side feature).
    """
    media_root = cfg.paths.media_root
    media_present = _dir_exists(media_root)
    music_drive_present = _dir_exists(media_root / cfg.music.folder)
    boombox_present = _dir_exists(media_root / cfg.music.folder / "Boombox")
    music_enabled = cfg.features.music_enabled
    boombox_enabled = cfg.features.boombox_enabled
    return {
        "chimes_available": media_present,
        "shows_available": media_present,
        "wraps_available": media_present,
        "license_plates_available": media_present,
        "music_available": music_drive_present and music_enabled,
        "boombox_available": boombox_present and boombox_enabled,
    }


__all__ = ("probe_media_availability",)
