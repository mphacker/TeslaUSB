"""Unix-socket IPC client to the ``teslafat`` daemon.

Layer 2 service per ``docs/03-CODE-QUALITY-CHARTER.md`` §"The
Layering Rule" — no Flask imports, no Werkzeug. The transport is
``socket.AF_UNIX, SOCK_STREAM`` with NDJSON framing per ADR-0014.
The wire vocabulary is in
:mod:`teslausb_web.services.teslafat_messages` and mirrors the Rust
``teslausb_core::ipc::messages`` types.

## Concurrency

Each :class:`TeslaFatClient` instance is **single-threaded**: every
public method opens a fresh connection, sends one envelope, reads
one envelope, closes. Concurrent requests against the same daemon
are safe (the daemon's accept loop serialises clients) but each
caller MUST hold their own client. Flask blueprints instantiate
per-request clients, which keeps the lifecycle trivial.

## Retry policy

Network-level failures (``ConnectionRefusedError``,
``FileNotFoundError`` from a daemon restart, ``BlockingIOError``
from a kernel hiccup, ``TimeoutError``) are retried with bounded
exponential backoff up to :attr:`RetryPolicy.max_attempts`.
Protocol-level errors
(:class:`teslafat_messages.IpcProtocolError`) are NEVER retried —
the message itself is wrong and re-sending wouldn't help.
Daemon-level errors (:class:`IpcDaemonError`) are NEVER retried
either — the daemon explicitly told us "no".
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import socket
import time
from typing import TYPE_CHECKING, Final, Protocol

from teslausb_web.services.teslafat_messages import (
    ErrorBody,
    IpcProtocolError,
    RetentionAck,
    RetentionFailure,
    RetentionReloadAck,
    StatusBody,
    parse_envelope,
    serialise_envelope,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from teslausb_web.services.teslafat_messages import RetentionUpdate

logger = logging.getLogger(__name__)


_MAX_ENVELOPE_BYTES: Final[int] = 65_536
"""ADR-0014 framing cap. Wire MUST refuse oversize lines."""

_DEFAULT_CONNECT_TIMEOUT_S: Final[float] = 2.0
_DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 5.0
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_INITIAL_BACKOFF_S: Final[float] = 0.05
_DEFAULT_BACKOFF_MULTIPLIER: Final[float] = 2.0
_DEFAULT_BACKOFF_CEILING_S: Final[float] = 1.0
_RECV_CHUNK: Final[int] = 4096
_REQUEST_ID_MAX: Final[int] = 2**63 - 1  # u64 wire field; signed-int safe.


class IpcDaemonError(Exception):
    """Raised when the daemon returns ``Response::Error``.

    Distinct from :class:`teslafat_messages.IpcProtocolError`
    (malformed payload) and :class:`OSError` (network failure).
    Carries the typed :class:`ErrorBody` for structured handling.
    """

    def __init__(self, body: ErrorBody) -> None:
        super().__init__(f"daemon error {body.code}: {body.message}")
        self.body = body


@dataclasses.dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff for transport-level retries."""

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    initial_backoff_s: float = _DEFAULT_INITIAL_BACKOFF_S
    multiplier: float = _DEFAULT_BACKOFF_MULTIPLIER
    ceiling_s: float = _DEFAULT_BACKOFF_CEILING_S

    def backoff_for_attempt(self, attempt: int) -> float:
        """Compute the delay before the ``attempt``-th retry (0 = no wait)."""
        if attempt <= 0:
            return 0.0
        delay = self.initial_backoff_s * (self.multiplier ** (attempt - 1))
        return min(delay, self.ceiling_s)


class _SocketFactory(Protocol):
    """Test seam — open a connected SOCK_STREAM AF_UNIX socket to ``path``."""

    def __call__(self, path: str) -> socket.socket: ...


class _SleepFn(Protocol):
    def __call__(self, seconds: float, /) -> None: ...


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


class TeslaFatClient:
    """Synchronous Unix-socket client for the ``teslafat`` daemon."""

    def __init__(  # noqa: PLR0913 — test seams (sleep, socket_factory, id_generator) are intentional.
        self,
        socket_path: str | Path,
        *,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
        retry_policy: RetryPolicy | None = None,
        sleep: _SleepFn = _default_sleep,
        socket_factory: _SocketFactory | None = None,
        id_generator: Iterator[int] | None = None,
    ) -> None:
        self._socket_path = str(socket_path)
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._socket_factory = socket_factory
        self._id_generator: Iterator[int] = id_generator or _default_id_generator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def status(self) -> StatusBody:
        """Send ``Request::Status`` and return the parsed body."""
        payload = self._request({"type": "STATUS"})
        self._raise_if_error(payload)
        self._require_type(payload, "STATUS")
        body = payload.get("body") if "body" in payload else payload
        # The Rust enum tags the variant with `type` and inlines the
        # body fields at the top level (``#[serde(tag = "type")]``
        # without ``content``). Be liberal about both layouts so
        # we cope with serde's per-variant convention without
        # speculative coupling.
        if not isinstance(body, dict):
            body = payload
        return StatusBody.from_wire(body)

    def invalidate_cache(self) -> None:
        """Send ``Request::InvalidateCache`` and confirm the ack."""
        payload = self._request({"type": "INVALIDATE_CACHE"})
        self._raise_if_error(payload)
        self._require_type(payload, "INVALIDATE_ACK")

    def reload_retention(self, hide_after_seconds: int) -> RetentionReloadAck:
        """Send ``Request::ReloadRetention`` with a new threshold."""
        if hide_after_seconds < 0:
            msg = f"hide_after_seconds must be non-negative; got {hide_after_seconds}"
            raise ValueError(msg)
        payload = self._request(
            {
                "type": "RELOAD_RETENTION",
                "hide_after_seconds": hide_after_seconds,
            },
        )
        self._raise_if_error(payload)
        self._require_type(payload, "RETENTION_RELOAD_ACK")
        return RetentionReloadAck(
            hide_after_seconds=_required_int(payload, "hide_after_seconds"),
            hidden=_required_int(payload, "hidden"),
            shown=_required_int(payload, "shown"),
        )

    def update_retention(self, updates: Sequence[RetentionUpdate]) -> RetentionAck:
        """Send ``Request::RetentionUpdate`` and return the ack."""
        if not updates:
            msg = "update_retention requires at least one update"
            raise ValueError(msg)
        payload = self._request(
            {
                "type": "RETENTION_UPDATE",
                "updates": [u.to_wire() for u in updates],
            },
        )
        self._raise_if_error(payload)
        self._require_type(payload, "RETENTION_ACK")
        return _parse_retention_ack(payload)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        """Connect, send one envelope, return the parsed response payload."""
        request_id = self._next_request_id()
        wire = serialise_envelope(request_id, payload)
        if len(wire) > _MAX_ENVELOPE_BYTES:
            msg = (
                f"outgoing envelope is {len(wire)} bytes, "
                f"exceeds ADR-0014 cap of {_MAX_ENVELOPE_BYTES}"
            )
            raise IpcProtocolError(msg)

        last_error: OSError | None = None
        for attempt in range(self._retry.max_attempts):
            if attempt > 0:
                delay = self._retry.backoff_for_attempt(attempt)
                logger.info(
                    "teslafat IPC retry %d/%d after %.3fs (last error: %s)",
                    attempt,
                    self._retry.max_attempts - 1,
                    delay,
                    last_error,
                )
                self._sleep(delay)
            try:
                response_bytes = self._roundtrip(wire)
            except (ConnectionError, TimeoutError, BlockingIOError, FileNotFoundError) as exc:
                last_error = exc
                continue
            return parse_envelope(response_bytes, expected_id=request_id)

        # All attempts failed with retryable network errors. The
        # last one carries the operator's diagnostic.
        if last_error is None:
            msg = "_request loop exited without sending — max_attempts <= 0?"
            raise IpcProtocolError(msg)
        raise last_error

    def _roundtrip(self, wire: bytes) -> bytes:
        """One connect+send+recv cycle. Caller owns retries."""
        sock = self._open_socket()
        try:
            sock.settimeout(self._request_timeout_s)
            sock.sendall(wire)
            return self._read_one_line(sock)
        finally:
            try:
                sock.close()
            except OSError:
                logger.debug("ignored close error on teslafat socket")

    def _open_socket(self) -> socket.socket:
        """Open and connect a SOCK_STREAM AF_UNIX socket."""
        if self._socket_factory is not None:
            return self._socket_factory(self._socket_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined] # AF_UNIX is Linux-only; Windows dev box doesn't expose the constant but production target is Linux.
        sock.settimeout(self._connect_timeout_s)
        sock.connect(self._socket_path)
        return sock

    @staticmethod
    def _read_one_line(sock: socket.socket) -> bytes:
        """Read until the first ``\\n`` or until the framing cap is hit."""
        buf = bytearray()
        while True:
            chunk = sock.recv(_RECV_CHUNK)
            if not chunk:
                msg = "teslafat closed connection before response newline"
                raise IpcProtocolError(msg)
            buf.extend(chunk)
            newline_at = buf.find(b"\n")
            if newline_at >= 0:
                return bytes(buf[:newline_at])
            if len(buf) > _MAX_ENVELOPE_BYTES:
                msg = (
                    f"teslafat response exceeded ADR-0014 cap of "
                    f"{_MAX_ENVELOPE_BYTES} bytes without a newline"
                )
                raise IpcProtocolError(msg)

    def _next_request_id(self) -> int:
        next_id = next(self._id_generator)
        if next_id < 0 or next_id > _REQUEST_ID_MAX:
            msg = f"request id out of u63 range: {next_id}"
            raise IpcProtocolError(msg)
        return next_id

    @staticmethod
    def _raise_if_error(payload: dict[str, object]) -> None:
        if payload.get("type") != "ERROR":
            return
        code = payload.get("code")
        message = payload.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            msg = f"malformed ERROR response: code={code!r} message={message!r}"
            raise IpcProtocolError(msg)
        raise IpcDaemonError(ErrorBody(code=code, message=message))

    @staticmethod
    def _require_type(payload: dict[str, object], expected: str) -> None:
        actual = payload.get("type")
        if actual != expected:
            msg = f"unexpected response type {actual!r}, expected {expected!r}"
            raise IpcProtocolError(msg)


def _default_id_generator() -> Iterator[int]:
    """Yield fresh u63 request ids.

    Uses ``secrets.randbits(63)`` so a long-running worker doesn't
    wrap into a duplicate id. Values are non-monotonic — each id
    is opaque per ADR-0002, used only as a correlation handle.
    """
    while True:
        yield secrets.randbits(63)


def _parse_retention_ack(payload: dict[str, object]) -> RetentionAck:
    applied_raw = payload.get("applied")
    if not isinstance(applied_raw, int) or isinstance(applied_raw, bool):
        msg = f"RETENTION_ACK.applied must be int; got {type(applied_raw).__name__}"
        raise IpcProtocolError(msg)
    failed_raw = payload.get("failed", [])
    if not isinstance(failed_raw, list):
        msg = f"RETENTION_ACK.failed must be list; got {type(failed_raw).__name__}"
        raise IpcProtocolError(msg)
    failures: list[RetentionFailure] = []
    for entry in failed_raw:
        if not isinstance(entry, dict):
            msg = "RETENTION_ACK.failed entries must be dicts"
            raise IpcProtocolError(msg)
        clip = entry.get("clip_path")
        reason = entry.get("reason")
        if not isinstance(clip, str) or not isinstance(reason, str):
            msg = f"malformed RETENTION_ACK.failed entry: {entry!r}"
            raise IpcProtocolError(msg)
        failures.append(RetentionFailure(clip_path=clip, reason=reason))
    return RetentionAck(applied=applied_raw, failed=tuple(failures))


def _required_int(payload: dict[str, object], key: str) -> int:
    if key not in payload:
        msg = f"missing required int field: {key}"
        raise IpcProtocolError(msg)
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{key} must be int; got {type(value).__name__}"
        raise IpcProtocolError(msg)
    return value


__all__ = (
    "IpcDaemonError",
    "IpcProtocolError",
    "RetentionAck",
    "RetentionFailure",
    "RetentionReloadAck",
    "RetryPolicy",
    "StatusBody",
    "TeslaFatClient",
)
