"""Tests for :class:`TeslaFatClient` against a scripted fake socket.

The fake socket lets us drive the client's send/recv path
deterministically — no real ``AF_UNIX`` socket required, so the
tests pass on Windows dev boxes per the cross-platform contract
in ADR-0014.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Final

import pytest
from teslausb_web.services.teslafat_client import (
    IpcDaemonError,
    IpcProtocolError,
    RetryPolicy,
    TeslaFatClient,
)
from teslausb_web.services.teslafat_messages import (
    PROTOCOL_VERSION,
    RetentionUpdate,
    RetentionUpdateExtend,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIXED_ID: Final[int] = 99


class _FakeSocket:
    """Records ``sendall`` bytes and replays a queued response stream."""

    def __init__(self, *, recv_chunks: list[bytes], raise_on_send: Exception | None = None) -> None:
        self.sent = bytearray()
        self._recv_chunks = list(recv_chunks)
        self._raise_on_send = raise_on_send
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def sendall(self, data: bytes) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.extend(data)

    def recv(self, _n: int) -> bytes:
        if not self._recv_chunks:
            return b""
        return self._recv_chunks.pop(0)

    def close(self) -> None:
        self.closed = True


def _make_response(payload: dict[str, object], *, response_id: int = _FIXED_ID) -> bytes:
    envelope = {"version": PROTOCOL_VERSION, "id": response_id, "payload": payload}
    return json.dumps(envelope, separators=(",", ":")).encode() + b"\n"


def _client_with_responses(
    responses: list[bytes],
    *,
    ids: Iterator[int] | None = None,
    socket_errors: list[Exception] | None = None,
) -> tuple[TeslaFatClient, list[_FakeSocket], list[float]]:
    sockets: list[_FakeSocket] = []
    errors = list(socket_errors or [])
    response_iter = iter(responses)

    def factory(_path: str) -> _FakeSocket:
        if errors:
            raise errors.pop(0)
        sock = _FakeSocket(recv_chunks=[next(response_iter)])
        sockets.append(sock)
        return sock

    sleeps: list[float] = []

    client = TeslaFatClient(
        "/run/teslafat.sock",
        retry_policy=RetryPolicy(max_attempts=3, initial_backoff_s=0.01, ceiling_s=0.01),
        sleep=sleeps.append,
        socket_factory=factory,  # type: ignore[arg-type] # fake socket duck-types the real one for the methods used.
        id_generator=ids if ids is not None else itertools.repeat(_FIXED_ID),
    )
    return client, sockets, sleeps


def test_status_happy_path() -> None:
    payload = {
        "type": "STATUS",
        "lun_id": 0,
        "state": "SERVING",
        "volume_label": "TESLACAM",
        "volume_size_bytes": 32_000_000_000,
        "uptime_seconds": 60,
    }
    client, sockets, _ = _client_with_responses([_make_response(payload)])
    body = client.status()
    assert body.lun_id == 0
    assert body.state == "SERVING"
    assert sockets[0].closed
    sent = json.loads(sockets[0].sent.rstrip(b"\n"))
    assert sent == {
        "version": PROTOCOL_VERSION,
        "id": _FIXED_ID,
        "payload": {"type": "STATUS"},
    }


def test_invalidate_cache_happy_path() -> None:
    client, sockets, _ = _client_with_responses([_make_response({"type": "INVALIDATE_ACK"})])
    client.invalidate_cache()
    assert sockets[0].closed


def test_reload_retention_returns_counts() -> None:
    payload = {
        "type": "RETENTION_RELOAD_ACK",
        "hide_after_seconds": 86_400,
        "hidden": 12,
        "shown": 3,
    }
    client, _, _ = _client_with_responses([_make_response(payload)])
    ack = client.reload_retention(86_400)
    assert ack.hide_after_seconds == 86_400
    assert ack.hidden == 12
    assert ack.shown == 3


def test_reload_retention_rejects_negative() -> None:
    client, _, _ = _client_with_responses([_make_response({"type": "RETENTION_RELOAD_ACK"})])
    with pytest.raises(ValueError, match="non-negative"):
        client.reload_retention(-1)


def test_update_retention_serialises_actions() -> None:
    payload = {"type": "RETENTION_ACK", "applied": 2, "failed": []}
    client, sockets, _ = _client_with_responses([_make_response(payload)])
    ack = client.update_retention(
        [
            RetentionUpdate(clip_path="a.mp4", action="HIDE"),
            RetentionUpdate(
                clip_path="b.mp4",
                action=RetentionUpdateExtend(until_unix_seconds=1_700_000_000),
            ),
        ],
    )
    assert ack.applied == 2
    assert ack.failed == ()
    sent = json.loads(sockets[0].sent.rstrip(b"\n"))
    assert sent["payload"]["updates"][0]["action"] == {"type": "HIDE"}
    assert sent["payload"]["updates"][1]["action"] == {
        "type": "EXTEND",
        "until_unix_seconds": 1_700_000_000,
    }


def test_update_retention_surfaces_failures() -> None:
    payload = {
        "type": "RETENTION_ACK",
        "applied": 1,
        "failed": [{"clip_path": "x.mp4", "reason": "missing"}],
    }
    client, _, _ = _client_with_responses([_make_response(payload)])
    ack = client.update_retention([RetentionUpdate(clip_path="x.mp4", action="HIDE")])
    assert ack.applied == 1
    assert len(ack.failed) == 1
    assert ack.failed[0].clip_path == "x.mp4"
    assert ack.failed[0].reason == "missing"


def test_update_retention_rejects_empty_batch() -> None:
    client, _, _ = _client_with_responses([])
    with pytest.raises(ValueError, match="at least one update"):
        client.update_retention([])


def test_daemon_error_is_typed() -> None:
    payload = {"type": "ERROR", "code": "INVALID_PAYLOAD", "message": "bad clip_path"}
    client, _, _ = _client_with_responses([_make_response(payload)])
    with pytest.raises(IpcDaemonError) as excinfo:
        client.invalidate_cache()
    assert excinfo.value.body.code == "INVALID_PAYLOAD"
    assert "bad clip_path" in str(excinfo.value)


def test_unexpected_response_type_is_protocol_error() -> None:
    payload = {"type": "INVALIDATE_ACK"}
    client, _, _ = _client_with_responses([_make_response(payload)])
    with pytest.raises(IpcProtocolError, match="unexpected response type"):
        client.status()


def test_retry_on_connection_refused_then_succeed() -> None:
    payload = {"type": "INVALIDATE_ACK"}
    client, _, sleeps = _client_with_responses(
        [_make_response(payload)],
        socket_errors=[ConnectionRefusedError()],
    )
    client.invalidate_cache()
    assert sleeps  # one backoff happened before the successful retry.


def test_no_retry_on_protocol_error() -> None:
    bad = b'{"version":1,"id":99,"payload":"not a dict"}\n'
    client, sockets, sleeps = _client_with_responses([bad])
    with pytest.raises(IpcProtocolError):
        client.invalidate_cache()
    assert len(sockets) == 1
    assert sleeps == []


def test_framing_cap_enforced_on_response() -> None:
    # 70 KiB payload without a newline → must exceed ADR-0014 cap.
    blob = b"x" * 70_000
    client, _, _ = _client_with_responses([blob])
    with pytest.raises(IpcProtocolError, match="ADR-0014 cap"):
        client.invalidate_cache()


def test_partial_reads_are_reassembled() -> None:
    payload = {"type": "INVALIDATE_ACK"}
    wire = _make_response(payload)
    sockets: list[_FakeSocket] = []

    def factory(_path: str) -> _FakeSocket:
        # Split mid-stream so the client must loop on recv.
        sock = _FakeSocket(recv_chunks=[wire[:5], wire[5:20], wire[20:]])
        sockets.append(sock)
        return sock

    client = TeslaFatClient(
        "/run/teslafat.sock",
        retry_policy=RetryPolicy(max_attempts=1),
        socket_factory=factory,  # type: ignore[arg-type] # fake duck-types real socket.
        id_generator=itertools.repeat(_FIXED_ID),
    )
    client.invalidate_cache()
    assert sockets[0].closed


def test_oversize_outgoing_envelope_rejected_before_send() -> None:
    client, sockets, _ = _client_with_responses([])
    huge = [RetentionUpdate(clip_path="a" * 70_000, action="HIDE")]
    with pytest.raises(IpcProtocolError, match="exceeds ADR-0014 cap"):
        client.update_retention(huge)
    assert sockets == []


def test_daemon_closed_connection_is_protocol_error() -> None:
    sockets: list[_FakeSocket] = []

    def factory(_path: str) -> _FakeSocket:
        sock = _FakeSocket(recv_chunks=[])  # immediate EOF
        sockets.append(sock)
        return sock

    client = TeslaFatClient(
        "/run/teslafat.sock",
        retry_policy=RetryPolicy(max_attempts=1),
        socket_factory=factory,  # type: ignore[arg-type] # fake duck-types real socket.
        id_generator=itertools.repeat(_FIXED_ID),
    )
    with pytest.raises(IpcProtocolError, match="closed connection"):
        client.invalidate_cache()
