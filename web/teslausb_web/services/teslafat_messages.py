"""Wire types for the teslafat daemon IPC, mirroring Rust ``ipc::messages``.

These dataclasses are frozen + slotted (charter §"no stringly-typed
code") and serialise to / deserialise from the exact JSON shape
``serde`` emits for the Rust types in ``rust/crates/teslausb-core/src/
ipc/messages.rs``. ADR-0002 fixes the *vocabulary*; ADR-0014 fixes
the *framing*. This module bridges the Python typed surface to the
JSON wire form documented in those ADRs.

Wire-format contract (must stay byte-identical to the Rust serde
output):

* ``Envelope`` carries ``version`` (u8), ``id`` (u64), ``payload``.
* ``Request`` is ``#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]``
  so each variant serialises as ``{"type": "VARIANT", ...fields}``.
* ``Response`` is the same.
* ``DaemonState`` and ``ErrorCode`` are ``SCREAMING_SNAKE_CASE`` strings.
* ``RetentionAction`` is also tagged with ``type``.

Adding new request/response variants here MUST be accompanied by
the matching addition in ``messages.rs`` *and* the framing limits
in ADR-0014 must still be honoured (envelopes < 64 KiB).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Final

PROTOCOL_VERSION: Final[int] = 1
"""Wire protocol major version. Must equal Rust ``PROTOCOL_VERSION``."""


class IpcProtocolError(Exception):
    """Raised when a peer message violates the wire contract.

    Distinct from network errors (those surface as ``ConnectionError``
    / ``OSError`` from the transport layer). A ``IpcProtocolError``
    is unrecoverable — the message itself is malformed — and callers
    must not retry the same request without addressing the cause.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionUpdateExtend:
    """``RetentionAction::Extend`` payload."""

    until_unix_seconds: int


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionUpdate:
    """One entry in a ``Request::RetentionUpdate`` batch.

    ``action`` is either the string ``"HIDE"`` / ``"UNHIDE"`` or
    a :class:`RetentionUpdateExtend` carrying ``until_unix_seconds``.
    The serde-tagged-union form on the wire is reproduced exactly
    by :meth:`to_wire`.
    """

    clip_path: str
    action: str | RetentionUpdateExtend

    def to_wire(self) -> dict[str, object]:
        if isinstance(self.action, RetentionUpdateExtend):
            action_json: dict[str, object] = {
                "type": "EXTEND",
                "until_unix_seconds": self.action.until_unix_seconds,
            }
        else:
            action_json = {"type": self.action}
        return {"clip_path": self.clip_path, "action": action_json}


@dataclasses.dataclass(frozen=True, slots=True)
class StatusBody:
    lun_id: int
    state: str
    volume_label: str
    volume_size_bytes: int
    uptime_seconds: int

    @classmethod
    def from_wire(cls, body: dict[str, object]) -> StatusBody:
        try:
            return cls(
                lun_id=_coerce_int(body, "lun_id"),
                state=_coerce_str(body, "state"),
                volume_label=_coerce_str(body, "volume_label"),
                volume_size_bytes=_coerce_int(body, "volume_size_bytes"),
                uptime_seconds=_coerce_int(body, "uptime_seconds"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"malformed STATUS body: {exc}"
            raise IpcProtocolError(msg) from exc


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionFailure:
    clip_path: str
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionAck:
    applied: int
    failed: tuple[RetentionFailure, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class RetentionReloadAck:
    hide_after_seconds: int
    hidden: int
    shown: int


@dataclasses.dataclass(frozen=True, slots=True)
class ErrorBody:
    code: str
    message: str


def serialise_envelope(
    request_id: int,
    payload: dict[str, object],
) -> bytes:
    """Encode a request envelope as a single NDJSON line (with trailing ``\\n``).

    The byte layout matches ADR-0014: compact JSON, no internal
    raw newlines, single ``0x0a`` delimiter at the end.
    """
    envelope: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "payload": payload,
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def parse_envelope(line: bytes, *, expected_id: int) -> dict[str, object]:
    """Validate framing + envelope version + correlation id; return the payload.

    Raises :class:`IpcProtocolError` on any wire-contract violation
    (bad JSON, missing fields, wrong protocol version, mismatched
    correlation id, non-dict payload).
    """
    try:
        decoded = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"response was not valid UTF-8 JSON: {exc}"
        raise IpcProtocolError(msg) from exc

    if not isinstance(decoded, dict):
        msg = f"response envelope is not a JSON object: {type(decoded).__name__}"
        raise IpcProtocolError(msg)

    version = decoded.get("version")
    if version != PROTOCOL_VERSION:
        msg = f"unsupported IPC protocol version: got {version}, expected {PROTOCOL_VERSION}"
        raise IpcProtocolError(msg)

    response_id = decoded.get("id")
    if response_id != expected_id:
        msg = f"response id mismatch: got {response_id!r}, expected {expected_id}"
        raise IpcProtocolError(msg)

    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        msg = f"response payload is not a JSON object: {type(payload).__name__}"
        raise IpcProtocolError(msg)

    return payload


def _coerce_int(body: dict[str, object], key: str) -> int:
    value = body[key]
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{key} must be int; got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _coerce_str(body: dict[str, object], key: str) -> str:
    value = body[key]
    if not isinstance(value, str):
        msg = f"{key} must be str; got {type(value).__name__}"
        raise TypeError(msg)
    return value
