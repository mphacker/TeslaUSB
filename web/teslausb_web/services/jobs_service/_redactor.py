"""Path / credential redaction for failed-job error messages.

A LAN/AP guest viewing the Failed Jobs page does not need to see
absolute mount paths, the operator's Pi username, or cloud bucket
names — that information stays in the on-device DB for journalctl
triage. Ported from v1's ``_redact_last_error`` (bp-jobs.py).
"""

from __future__ import annotations

import re
from typing import Final

# Strip absolute local paths the user did not pick:
#   /mnt/..., /home/<user>/..., /var/..., /run/..., /tmp/...
# AND any rclone-style "remote:bucket/..." reference that could
# disclose the user's cloud provider / bucket name.
# AND any S3 virtual-host endpoint that names the bucket+region.
_REDACT_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"/(?:mnt|home|var|run|tmp)/[^\s'\"\)]+"), "<path>"),
    (re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{0,30}:[A-Za-z0-9._/-]+"), "<remote>"),
    (
        re.compile(r"\b[A-Za-z0-9-]+\.s3[.-][^\s'\"\)]+\.amazonaws\.com\b"),
        "<s3-host>",
    ),
)
_REDACT_MAX_LEN: Final[int] = 600
_ELLIPSIS: Final[str] = " …"


def redact_last_error(msg: str | None) -> str:
    """Sanitize a ``last_error`` string for HTTP response.

    Returns an empty string for ``None`` / empty input. Caps the
    output at ``_REDACT_MAX_LEN`` so a runaway rclone stack trace
    cannot blow up the JSON payload.
    """
    if not msg:
        return ""
    text = msg
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    if len(text) > _REDACT_MAX_LEN:
        text = text[:_REDACT_MAX_LEN].rstrip() + _ELLIPSIS
    return text
