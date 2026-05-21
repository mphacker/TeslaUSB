"""Media-hub landing blueprint — replaces the Phase 5.4 scaffold.

This blueprint owns one route — ``GET /media/`` — that redirects to
the first available media sub-page. It is the target of the **Media**
button in ``base.html``; the actual media pages (lock chimes, light
shows, wraps, music, boombox, license plates) live in their own
blueprints (Phase 5.8 / 5.9 / 5.10 / 5.11 / 5.12 / 5.16a).

## Landing target — ALWAYS lock chimes

Operator directive (2026-05-21): ``/media/`` must always redirect to
``/lock_chimes/`` because chimes is the first pill in the media
sub-nav. The cascade probe is retained so the decision is logged for
diagnostics, but the chosen target no longer depends on drive state.

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
    """Return the endpoint name for the media landing page.

    Operator directive (2026-05-21): always land on lock chimes because
    it is the first pill in the media sub-nav, so muscle memory matches
    the nav order. The ``_MediaAvailability`` snapshot is retained for
    logging/diagnostics so the cascade decision is still observable.
    """
    del availability  # observability only; kept in caller's log line
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
