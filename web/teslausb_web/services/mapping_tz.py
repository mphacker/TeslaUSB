"""Timezone-aware day bucketing for the mapping service.

Every timestamp in the worker DB is a true UTC epoch. A "day" on the
map is a *presentation* concern: the operator wants drives and events
bucketed by their own local calendar day, not by UTC. A drive at
20:10 EDT on June 1 is 00:10 UTC on June 2 — bucketing by UTC files it
under the wrong day.

This module owns the only timezone math in the mapping stack so the
day boundary is defined in exactly one place. The display zone is an
IANA name resolved by the blueprint (saved preference > browser tz >
UTC); these helpers take that already-validated name and never raise
for an unknown zone — they fall back to UTC so a malformed value can
never break a query.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_TZ_NAME: Final[str] = "UTC"
_DATE_LENGTH: Final[int] = 10  # "YYYY-MM-DD"


@lru_cache(maxsize=512)
def _zone(tz_name: str) -> ZoneInfo:
    """Return the zone for ``tz_name``, or UTC when it cannot be loaded.

    Cached because the same handful of zones are resolved on every
    request; ``ZoneInfo`` itself caches internally but the validation
    branch is skipped on a hit.
    """
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(UTC_TZ_NAME)


def normalize_tz(tz_name: str | None) -> str:
    """Return a valid IANA zone name, or ``"UTC"`` when absent/invalid.

    Used at the request boundary to canonicalise the resolved display
    zone so cache keys and SQL bounds share one stable string.
    """
    if not tz_name:
        return UTC_TZ_NAME
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC_TZ_NAME
    return tz_name


def day_bounds_utc(date_str: str, tz_name: str) -> tuple[int, int]:
    """Return ``[start, end)`` UTC epoch seconds for a local calendar day.

    DST-safe: the span is the wall-clock interval from local midnight to
    the next local midnight, each resolved to its own UTC instant. On a
    spring-forward day that span is 23 h, on a fall-back day 25 h — it is
    never assumed to be 86 400 s.
    """
    zone = _zone(tz_name)
    local_day = date.fromisoformat(date_str[:_DATE_LENGTH])
    start_local = datetime(local_day.year, local_day.month, local_day.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return int(start_local.timestamp()), int(end_local.timestamp())


def local_date_of(epoch: float, tz_name: str) -> str:
    """Return the ``YYYY-MM-DD`` local calendar day for a UTC epoch."""
    return datetime.fromtimestamp(epoch, tz=_zone(tz_name)).date().isoformat()


def local_date_of_iso(iso_timestamp: str, tz_name: str) -> str:
    """Return the local calendar day for an ISO-8601 UTC timestamp.

    The mapping snapshot/derivation paths carry timestamps as ISO
    strings (``epoch_to_iso`` output, always ``+00:00``); this converts
    one to the operator's local day without the caller juggling epochs.
    """
    return datetime.fromisoformat(iso_timestamp).astimezone(_zone(tz_name)).date().isoformat()


__all__ = (
    "UTC_TZ_NAME",
    "day_bounds_utc",
    "local_date_of",
    "local_date_of_iso",
    "normalize_tz",
)
