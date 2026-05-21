"""Deterministic clip-value + recommendation classifiers.

Pure functions — no DB access, no side effects. Ported from v1
``_classify_clip_value`` / ``_classify_recommendation`` but
narrowed to the buckets B-1 actually surfaces (the ``archive``
subsystem is dropped in B-1 because the cleanup pipeline is
fire-and-forget; see docs/00-PLAN.md).
"""

from __future__ import annotations

import re
from typing import Final

from teslausb_web.services.jobs_service._models import (
    Recommendation,
    SubsystemKey,
    ValueTier,
)

_VALUE_TIERS: Final[dict[str, ValueTier]] = {
    "event": ValueTier(
        tier="event",
        label="Event clip",
        description=(
            "Tesla recorded this because something happened to the car "
            "(impact, alarm, or manual save). Usually irreplaceable."
        ),
    ),
    "recent": ValueTier(
        tier="recent",
        label="Rolling buffer",
        description=(
            "RecentClips — Tesla writes these continuously while the car "
            "is powered (driving OR parked in Sentry standby) and rotates "
            "them out automatically. Losing one row is usually fine."
        ),
    ),
    "archived": ValueTier(
        tier="archived",
        label="Already on SD card",
        description=(
            "This clip is in ArchivedClips, so the source file is already "
            "preserved on the Pi even if the queue row is dropped."
        ),
    ),
    "cloud": ValueTier(
        tier="cloud",
        label="Cloud upload",
        description=(
            "The file itself is still on the SD card; only the cloud "
            "upload failed. Re-uploading later is always safe."
        ),
    ),
    "index": ValueTier(
        tier="index",
        label="Map / trip data",
        description=(
            "Just the trip-DB index row for this clip. The video file is "
            "untouched; deleting the row only loses the map waypoint, "
            "not the footage."
        ),
    ),
    "unknown": ValueTier(
        tier="unknown",
        label="Background task",
        description=(
            "The subsystem did not provide enough information to classify the underlying data."
        ),
    ),
}


def classify_clip_value(subsystem: SubsystemKey, identifier: str) -> ValueTier:
    """Return the operator-facing tier for a failed-job row.

    Pure / deterministic — looks only at the subsystem and identifier.
    Never raises: an unrecognised input falls through to ``unknown``.
    """
    ident = (identifier or "").lower()
    if "/sentryclips/" in ident or "/savedclips/" in ident:
        return _VALUE_TIERS["event"]
    if "/recentclips/" in ident:
        return _VALUE_TIERS["recent"]
    if "/archivedclips/" in ident:
        return _VALUE_TIERS["archived"]
    if subsystem is SubsystemKey.INDEXER:
        return _VALUE_TIERS["index"]
    # SubsystemKey.CLOUD_SYNC is the only remaining enum value; the
    # ``unknown`` tier is reachable only via reflection (not via the
    # enum API), so we keep it as a defensive fallback that mypy
    # statically rules out.
    if subsystem is SubsystemKey.CLOUD_SYNC:  # pragma: no branch
        return _VALUE_TIERS["cloud"]
    return _VALUE_TIERS["unknown"]  # type: ignore[unreachable]


# Recommendation buckets B-1 surfaces. Each entry is
# (regex, action, reason). First match wins.
_RULES: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    (
        re.compile(
            r"(?i)\b(?:no such file|file (?:not found|missing)|"
            r"enoent|source[_ ]?gone|does not exist)\b"
        ),
        "delete",
        "The source file is gone. Retrying will fail the same way.",
    ),
    (
        re.compile(
            r"(?i)\b(?:moov atom|invalid (?:data|argument)|"
            r"corrupt|truncat\w*|parse[_ ]?error|unsupported "
            r"(?:codec|format)|not a valid )\b"
        ),
        "delete",
        (
            "The file is on disk but cannot be parsed. The clip itself is "
            "corrupt — retrying will hit the same error."
        ),
    ),
    (
        re.compile(
            r"(?i)\b(?:i/?o ?error|input/output error|stale file "
            r"handle|device or resource busy|read[_ ]?error|"
            r"write[_ ]?error)\b"
        ),
        "retry",
        ("Transient I/O glitch. Often fixed by waiting for the SDIO bus to settle, then retrying."),
    ),
    (
        re.compile(
            r"(?i)\b(?:connection (?:refused|reset|timed? ?out|aborted)|"
            r"network (?:is )?(?:unreachable|down)|"
            r"temporary (?:failure|name resolution)|"
            r"no route to host|enotconn|name or service|"
            r"tls handshake|ssl handshake|x509|getaddrinfo|"
            r"dial tcp)\b"
        ),
        "retry",
        ("Network failure. Retry once WiFi is healthy. The file itself is fine."),
    ),
    (
        re.compile(
            r"(?i)\b(?:401|403|access denied|forbidden|invalid "
            r"(?:credential|token|api[_ ]?key|signature)|quota "
            r"exceeded|out of space|over capacity|insufficient "
            r"storage|payment required|429|rate ?limit)\b"
        ),
        "retry",
        (
            "Cloud-side auth / quota / rate-limit error. Fix the root "
            "cause (rotate creds, free space, wait for quota window) "
            "then retry."
        ),
    ),
    (
        re.compile(
            r"(?i)\b(?:permission denied|operation not permitted|"
            r"eacces|read[-_ ]?only file ?system|erofs)\b"
        ),
        "retry",
        "Permission / read-only-mount error. Fix permissions then retry.",
    ),
    (
        re.compile(
            r"(?i)\b(?:lock (?:contention|timeout|busy)|could not "
            r"acquire|coordinator (?:busy|timeout))\b"
        ),
        "retry",
        (
            "Another subsystem held the lock during the previous attempt. "
            "Retrying once the lock is free almost always succeeds."
        ),
    ),
)

_STUCK_ATTEMPTS_THRESHOLD: Final[int] = 5


def classify_recommendation(last_error: str | None, attempts: int = 0) -> Recommendation:
    """Return a Retry/Delete recommendation for a failed-job row.

    Pure / deterministic. Looks only at the redacted ``last_error``
    string and the attempt count. The operator can override — this
    just nudges the obvious cases toward the right button.
    """
    err = (last_error or "").strip()
    if not err:
        return Recommendation(
            action="either",
            reason=(
                "No error message recorded. Retry once to see a fresh "
                "failure, or delete if the source is no longer needed."
            ),
        )
    for pat, action, reason in _RULES:
        if pat.search(err):
            return Recommendation(action=action, reason=reason)
    if attempts >= _STUCK_ATTEMPTS_THRESHOLD:
        return Recommendation(
            action="delete",
            reason=(
                f"Retried {attempts} times without matching any known "
                "recoverable pattern. Likely stuck — delete and let the "
                "watcher re-enqueue if the source is still valid."
            ),
        )
    return Recommendation(
        action="either",
        reason=(
            "Error pattern not recognised. Retry once; if the same error "
            "returns, delete is safe (the source file stays on disk)."
        ),
    )
