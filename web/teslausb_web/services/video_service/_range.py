"""HTTP ``Range`` request parser — pure, exhaustively tested.

Split out from the blueprint so the parser is unit-testable without
spinning up a Flask app. Only the single-range form
``bytes=start-end`` (and its open-ended / suffix variants) is
supported; ``multipart/byteranges`` responses are not implemented.

There is no Flask import in this module.
"""

from __future__ import annotations

from teslausb_web.services.video_service._models import RangeRequest


class RangeParseError(ValueError):
    """Raised on a malformed Range header (caller should return 416)."""


def parse_range(header: str | None, file_size: int) -> RangeRequest | None:
    """Parse a single ``bytes=`` range against a known file size.

    Returns ``None`` when no Range header is present (caller serves
    the full file). Raises :class:`RangeParseError` when the header
    is present but unparseable; v1 returned HTTP 416 for that case
    and this signature lets the blueprint do the same.

    Multi-range requests (``bytes=0-99,200-299``) are explicitly
    rejected — see RFC 7233 §3.1, the server is allowed to ignore
    them and v1 never supported them.
    """
    if header is None:
        return None
    header = header.strip()
    if not header:
        raise RangeParseError("empty Range header")
    if file_size < 0:
        raise RangeParseError("negative file size")

    rng = _validate_units_and_extract_range(header)
    start, end = _resolve_range_bounds(rng, file_size)

    if file_size == 0:
        raise RangeParseError("cannot range an empty file")
    if start < 0 or end < start or end >= file_size:
        raise RangeParseError(f"range out of bounds: {start}-{end}/{file_size}")

    return RangeRequest(start=start, end=end, full_size=file_size)


def _validate_units_and_extract_range(header: str) -> str:
    if "=" not in header:
        raise RangeParseError(f"missing '=' in Range: {header!r}")
    units, _, rng = header.partition("=")
    if units.strip() != "bytes":
        raise RangeParseError(f"unsupported unit: {units!r}")
    if "," in rng:
        raise RangeParseError("multi-range requests are not supported")
    if "-" not in rng:
        raise RangeParseError(f"missing '-' in range spec: {rng!r}")
    return rng


def _resolve_range_bounds(rng: str, file_size: int) -> tuple[int, int]:
    start_str, _, end_str = rng.partition("-")
    start_str = start_str.strip()
    end_str = end_str.strip()
    try:
        if start_str == "":
            if end_str == "":
                raise RangeParseError("empty suffix range")
            suffix = int(end_str)
            if suffix <= 0:
                raise RangeParseError(f"non-positive suffix: {suffix}")
            return max(file_size - suffix, 0), file_size - 1
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
    except ValueError as exc:
        raise RangeParseError(f"non-integer range bound: {rng!r}") from exc
    return start, end
