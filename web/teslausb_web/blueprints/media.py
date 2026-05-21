"""Media-hub landing blueprint — replaces the Phase 5.4 scaffold.

This blueprint owns one route — ``GET /media/`` — that redirects to
the first available media sub-page. It is the target of the **Media**
button in ``base.html``; the actual media pages (lock chimes, light
shows, wraps, music, boombox, license plates) live in their own
blueprints (Phase 5.8 / 5.9 / 5.10 / 5.11 / 5.12 / 5.16a).

## Cascade order — WHY LightShow → Music → Boombox

The order mirrors v1 (``scripts/web/blueprints/media.py``) so operator
muscle-memory carries over:

1. **LightShow drive present** → ``lock_chimes.lock_chimes``. The
   LightShow partition hosts lock chimes, light shows, wraps, and
   license-plate art; chimes is the v1-historical landing page for
   that whole sub-area.
2. **Music drive present + ``music_enabled``** → ``music.music_home``.
3. **Music drive present + ``boombox_enabled`` (music disabled)** →
   ``boombox.boombox_home``. Boombox lives on the music partition so
   the partition gate is the same; the feature flag distinguishes the
   two consumers.
4. **Fallback** → ``lock_chimes.lock_chimes``. The lock-chimes page
   renders a "no LightShow drive" empty state when the partition is
   missing, so it doubles as the universal "no media drives mounted"
   landing page. This matches v1 behaviour.

## B-1 adaptation — NO IMG files

v1 gated each branch on ``os.path.isfile(IMG_*_PATH)`` because the
drives were loopback-mounted IMG files. B-1 has no IMG layer
(``docs/00-PLAN.md`` invariant): the LightShow and Music partitions
are real directories under ``cfg.paths.backing_root`` exposed by the
Rust ``teslafat`` worker. We probe their on-disk presence via
:func:`teslausb_web.services.media_availability.probe_media_availability`
which the app-wide context processor also consumes — same
operator-observable semantics, no IMG assumption, single source of
truth for the pill-bar visibility flags.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, cast

from flask import Blueprint, current_app, redirect, url_for

from teslausb_web.services.media_availability import probe_media_availability

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

media_bp = Blueprint("media", __name__, url_prefix="/media")


@dataclasses.dataclass(frozen=True, slots=True)
class _MediaAvailability:
    """Snapshot of which media drives + features are currently usable."""

    lightshow_present: bool
    music_drive_present: bool
    music_enabled: bool
    boombox_enabled: bool


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _probe_availability(cfg: WebConfig) -> _MediaAvailability:
    """Snapshot drive presence + feature flags for the cascade.

    Defers the directory probes to
    :func:`probe_media_availability` so the cascade and the pill-bar
    context processor agree byte-for-byte.
    """
    flags = probe_media_availability(cfg)
    return _MediaAvailability(
        lightshow_present=flags["chimes_available"],
        music_drive_present=flags["music_available"] or flags["boombox_available"],
        music_enabled=cfg.features.music_enabled,
        boombox_enabled=cfg.features.boombox_enabled,
    )


def _pick_target(availability: _MediaAvailability) -> str:
    """Return the endpoint name for the highest-priority media page.

    See module docstring for WHY the cascade is ordered the way it
    is. Returning the endpoint as a string (rather than calling
    ``url_for`` here) keeps this function pure and trivially
    unit-testable.
    """
    if availability.lightshow_present:
        return "lock_chimes.lock_chimes"
    if availability.music_drive_present:
        if availability.music_enabled:
            return "music.music_home"
        if availability.boombox_enabled:
            return "boombox.boombox_home"
    return "lock_chimes.lock_chimes"


@media_bp.route("/", endpoint="media_home")
def media_home() -> ResponseReturnValue:
    """Redirect to the first available media sub-page.

    Always returns a 302 — the chosen destination depends on
    runtime drive state and feature flags, so a permanent redirect
    would be wrong. The cascade order (LightShow → Music →
    Boombox → fallback) is documented at module level.
    """
    availability = _probe_availability(_cfg())
    endpoint = _pick_target(availability)
    logger.info(
        "media: redirecting to %s (lightshow=%s, music_drive=%s, "
        "music_enabled=%s, boombox_enabled=%s)",
        endpoint,
        availability.lightshow_present,
        availability.music_drive_present,
        availability.music_enabled,
        availability.boombox_enabled,
    )
    return redirect(url_for(endpoint))
