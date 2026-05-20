"""Wire-format tests for ``teslafat_messages``.

Validates the byte layout against ADR-0014 (framing) and ADR-0002
(vocabulary). Round-trip and rejection cases are both covered.
"""

from __future__ import annotations

import json

import pytest
from teslausb_web.services.teslafat_messages import (
    PROTOCOL_VERSION,
    IpcProtocolError,
    RetentionUpdate,
    RetentionUpdateExtend,
    StatusBody,
    parse_envelope,
    serialise_envelope,
)


def test_serialise_envelope_is_compact_ndjson() -> None:
    wire = serialise_envelope(42, {"type": "STATUS"})
    assert wire.endswith(b"\n")
    assert wire.count(b"\n") == 1
    assert b" " not in wire
    body = json.loads(wire.rstrip(b"\n"))
    assert body == {"version": PROTOCOL_VERSION, "id": 42, "payload": {"type": "STATUS"}}


def test_parse_envelope_round_trip() -> None:
    wire = serialise_envelope(7, {"type": "INVALIDATE_ACK"})
    payload = parse_envelope(wire.rstrip(b"\n"), expected_id=7)
    assert payload == {"type": "INVALIDATE_ACK"}


def test_parse_envelope_rejects_version_mismatch() -> None:
    raw = json.dumps({"version": 99, "id": 1, "payload": {}}).encode()
    with pytest.raises(IpcProtocolError, match="unsupported IPC protocol version"):
        parse_envelope(raw, expected_id=1)


def test_parse_envelope_rejects_id_mismatch() -> None:
    wire = serialise_envelope(7, {"type": "INVALIDATE_ACK"}).rstrip(b"\n")
    with pytest.raises(IpcProtocolError, match="response id mismatch"):
        parse_envelope(wire, expected_id=8)


def test_parse_envelope_rejects_non_object_payload() -> None:
    raw = json.dumps({"version": PROTOCOL_VERSION, "id": 1, "payload": []}).encode()
    with pytest.raises(IpcProtocolError, match="payload is not a JSON object"):
        parse_envelope(raw, expected_id=1)


def test_parse_envelope_rejects_invalid_json() -> None:
    with pytest.raises(IpcProtocolError, match="valid UTF-8 JSON"):
        parse_envelope(b"{not json", expected_id=1)


def test_parse_envelope_rejects_non_dict_root() -> None:
    raw = json.dumps([1, 2, 3]).encode()
    with pytest.raises(IpcProtocolError, match="envelope is not a JSON object"):
        parse_envelope(raw, expected_id=1)


def test_status_body_from_wire_round_trip() -> None:
    body = {
        "lun_id": 1,
        "state": "SERVING",
        "volume_label": "TESLACAM",
        "volume_size_bytes": 64_000_000_000,
        "uptime_seconds": 1234,
    }
    parsed = StatusBody.from_wire(body)
    assert parsed.lun_id == 1
    assert parsed.state == "SERVING"
    assert parsed.volume_label == "TESLACAM"
    assert parsed.volume_size_bytes == 64_000_000_000
    assert parsed.uptime_seconds == 1234


def test_status_body_from_wire_rejects_wrong_types() -> None:
    body = {
        "lun_id": "not an int",
        "state": "SERVING",
        "volume_label": "TESLACAM",
        "volume_size_bytes": 64_000_000_000,
        "uptime_seconds": 1234,
    }
    with pytest.raises(IpcProtocolError, match="malformed STATUS body"):
        StatusBody.from_wire(body)


def test_status_body_from_wire_rejects_missing_field() -> None:
    body = {"lun_id": 1, "state": "SERVING"}
    with pytest.raises(IpcProtocolError, match="malformed STATUS body"):
        StatusBody.from_wire(body)


def test_retention_update_hide_to_wire() -> None:
    upd = RetentionUpdate(clip_path="2024-01-01_00-00-00-front.mp4", action="HIDE")
    assert upd.to_wire() == {
        "clip_path": "2024-01-01_00-00-00-front.mp4",
        "action": {"type": "HIDE"},
    }


def test_retention_update_extend_to_wire() -> None:
    upd = RetentionUpdate(
        clip_path="clip.mp4",
        action=RetentionUpdateExtend(until_unix_seconds=1_700_000_000),
    )
    assert upd.to_wire() == {
        "clip_path": "clip.mp4",
        "action": {"type": "EXTEND", "until_unix_seconds": 1_700_000_000},
    }
