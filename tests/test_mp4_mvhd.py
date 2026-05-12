"""Unit tests for ``sei_parser.extract_mvhd_creation_time`` and
``mapping_service._resolve_recording_time``.

These tests build the MP4 atoms by hand rather than depending on any
real Tesla file. The atoms below are the minimum shape the helper
needs to walk:

    [ftyp][moov[mvhd]][mdat]

The helper only reads the ``mvhd`` payload, so the other boxes can be
empty placeholders.
"""

from __future__ import annotations

import os
import struct
import tempfile
from datetime import datetime, timezone
from unittest import mock

import pytest

from services import mapping_service, sei_parser

# Reference moment used across tests: 2026-05-10 15:57:41 UTC. Picked
# to match the real Tesla file we verified the mvhd against during
# investigation, so the numbers in the test would also pass against a
# real Tesla MP4 if you swap the synthetic file for one.
_REF_DT = datetime(2026, 5, 10, 15, 57, 41, tzinfo=timezone.utc)
_REF_MP4 = int(_REF_DT.timestamp()) + sei_parser._MP4_EPOCH_OFFSET


def _box(name: bytes, payload: bytes) -> bytes:
    """Build an MP4 box: 4-byte big-endian total size, 4-byte name, payload."""
    assert len(name) == 4
    return struct.pack(">I", 8 + len(payload)) + name + payload


def _mvhd_v0(creation_time: int) -> bytes:
    """Build a minimal mvhd v0 payload.

    Layout: 1 byte version + 3 bytes flags + 4 bytes creation_time +
    4 bytes modification_time + 4 bytes timescale + 4 bytes duration
    + 4 bytes rate + 2 bytes volume + 10 bytes reserved +
    36 bytes matrix + 24 bytes pre_defined + 4 bytes next_track_id.

    The helper only reads version + creation_time so the rest can be
    zeros, but we include the full structure so the atom is
    spec-compliant.
    """
    return (
        b"\x00"                       # version
        + b"\x00\x00\x00"             # flags
        + struct.pack(">I", creation_time)
        + struct.pack(">I", creation_time)  # modification_time
        + struct.pack(">I", 1000)            # timescale
        + struct.pack(">I", 60000)           # duration
        + struct.pack(">I", 0x00010000)      # rate
        + struct.pack(">H", 0x0100)          # volume
        + b"\x00" * 10                       # reserved
        + b"\x00" * 36                       # matrix
        + b"\x00" * 24                       # pre_defined
        + struct.pack(">I", 1)               # next_track_id
    )


def _mvhd_v1(creation_time: int) -> bytes:
    """Build a minimal mvhd v1 payload (64-bit times)."""
    return (
        b"\x01"                       # version
        + b"\x00\x00\x00"             # flags
        + struct.pack(">Q", creation_time)
        + struct.pack(">Q", creation_time)  # modification_time
        + struct.pack(">I", 1000)            # timescale
        + struct.pack(">Q", 60000)           # duration
        + struct.pack(">I", 0x00010000)      # rate
        + struct.pack(">H", 0x0100)          # volume
        + b"\x00" * 10                       # reserved
        + b"\x00" * 36                       # matrix
        + b"\x00" * 24                       # pre_defined
        + struct.pack(">I", 1)               # next_track_id
    )


def _make_mp4(payload_mvhd: bytes, *, with_moov: bool = True,
              extra_moov_boxes: bytes = b"") -> bytes:
    """Wrap a mvhd payload in a complete (minimal) MP4 file."""
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")
    if with_moov:
        moov_payload = _box(b"mvhd", payload_mvhd) + extra_moov_boxes
        moov = _box(b"moov", moov_payload)
    else:
        moov = b""
    mdat = _box(b"mdat", b"\x00" * 16)
    return ftyp + moov + mdat


def _write_tmp_mp4(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# -- extract_mvhd_creation_time ---------------------------------------------


class TestExtractMvhdCreationTime:
    def test_returns_none_when_file_missing(self, tmp_path):
        assert sei_parser.extract_mvhd_creation_time(
            str(tmp_path / "no-such-file.mp4")
        ) is None

    def test_returns_none_when_file_too_small(self, tmp_path):
        path = _write_tmp_mp4(tmp_path, "tiny.mp4", b"\x00")
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_returns_none_when_no_moov_box(self, tmp_path):
        data = _make_mp4(b"", with_moov=False)
        path = _write_tmp_mp4(tmp_path, "no_moov.mp4", data)
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_returns_none_when_moov_lacks_mvhd(self, tmp_path):
        ftyp = _box(b"ftyp", b"isom" + b"\x00" * 12)
        moov = _box(b"moov", _box(b"trak", b""))  # moov but no mvhd
        path = _write_tmp_mp4(tmp_path, "no_mvhd.mp4", ftyp + moov)
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_returns_none_when_creation_time_is_zero(self, tmp_path):
        # 0 is the "uninitialised" sentinel some pre-2010 firmware
        # writes; we explicitly reject anything <= the MP4 epoch.
        path = _write_tmp_mp4(
            tmp_path, "zero.mp4", _make_mp4(_mvhd_v0(0)),
        )
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_returns_none_when_creation_time_below_epoch_offset(self, tmp_path):
        # Anything that would land before unix epoch 1970 is bogus.
        path = _write_tmp_mp4(
            tmp_path, "bogus.mp4",
            _make_mp4(_mvhd_v0(sei_parser._MP4_EPOCH_OFFSET - 1)),
        )
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_decodes_v0_creation_time(self, tmp_path):
        path = _write_tmp_mp4(
            tmp_path, "v0.mp4", _make_mp4(_mvhd_v0(_REF_MP4)),
        )
        result = sei_parser.extract_mvhd_creation_time(path)
        assert result is not None
        assert result == _REF_DT
        assert result.tzinfo is timezone.utc

    def test_decodes_v1_creation_time(self, tmp_path):
        path = _write_tmp_mp4(
            tmp_path, "v1.mp4", _make_mp4(_mvhd_v1(_REF_MP4)),
        )
        result = sei_parser.extract_mvhd_creation_time(path)
        assert result is not None
        assert result == _REF_DT
        assert result.tzinfo is timezone.utc

    def test_truncated_v0_payload_returns_none(self, tmp_path):
        # mvhd box exists but its payload is too short for a v0 header.
        ftyp = _box(b"ftyp", b"isom" + b"\x00" * 12)
        # Only version + flags, no creation_time bytes
        bad_mvhd = _box(b"mvhd", b"\x00\x00\x00\x00")
        moov = _box(b"moov", bad_mvhd)
        path = _write_tmp_mp4(tmp_path, "trunc.mp4", ftyp + moov)
        assert sei_parser.extract_mvhd_creation_time(path) is None

    def test_unknown_box_before_mvhd_is_skipped(self, tmp_path):
        # The walker must traverse moov children to find mvhd; a
        # leading unrelated box shouldn't trip it up.
        ftyp = _box(b"ftyp", b"isom" + b"\x00" * 12)
        moov = _box(
            b"moov",
            _box(b"udta", b"\x00" * 8) + _box(b"mvhd", _mvhd_v0(_REF_MP4)),
        )
        path = _write_tmp_mp4(tmp_path, "udta_first.mp4", ftyp + moov)
        result = sei_parser.extract_mvhd_creation_time(path)
        assert result == _REF_DT


# -- mapping_service._resolve_recording_time --------------------------------


class TestResolveRecordingTime:
    def _expected_local_iso(self, mvhd_dt: datetime) -> str:
        # The helper converts UTC -> system local -> naive ISO. Tests
        # mirror that conversion so they pass regardless of the host TZ.
        return datetime.fromtimestamp(mvhd_dt.timestamp()).isoformat()

    def test_uses_mvhd_when_available(self, tmp_path):
        path = _write_tmp_mp4(
            tmp_path, "2026-05-11_07-50-38-front.mp4",
            _make_mp4(_mvhd_v0(_REF_MP4)),
        )
        result = mapping_service._resolve_recording_time(path)
        assert result == self._expected_local_iso(_REF_DT)

    def test_falls_back_to_filename_when_no_mvhd(self, tmp_path):
        # File exists but has no moov atom. Helper must fall back to
        # the filename-derived value (legacy behaviour).
        path = _write_tmp_mp4(
            tmp_path, "2026-05-11_07-50-38-front.mp4",
            _make_mp4(b"", with_moov=False),
        )
        result = mapping_service._resolve_recording_time(path)
        assert result == "2026-05-11T07:50:38"

    def test_returns_none_when_both_sources_unusable(self, tmp_path):
        # File missing AND filename has no parseable timestamp prefix.
        result = mapping_service._resolve_recording_time(
            str(tmp_path / "garbage-name.mp4")
        )
        assert result is None

    def test_logs_warning_on_large_skew(self, tmp_path, caplog):
        # Filename says May 11 but mvhd says May 10. Helper should
        # log a WARNING flagging the clock-skew incident.
        path = _write_tmp_mp4(
            tmp_path, "2026-05-11_07-50-38-front.mp4",
            _make_mp4(_mvhd_v0(_REF_MP4)),
        )
        with caplog.at_level("WARNING", logger="services.mapping_service"):
            result = mapping_service._resolve_recording_time(path)
        assert result == self._expected_local_iso(_REF_DT)
        assert any("clock skew" in r.message.lower() for r in caplog.records)

    def test_no_warning_on_small_skew(self, tmp_path, caplog):
        # Filename minute matches mvhd minute (a few seconds drift is
        # normal — mvhd is exact start, filename is a rounded label).
        # The helper must not spam WARNING for the healthy case.
        small_dt = datetime(2026, 5, 10, 11, 57, 50, tzinfo=timezone.utc)
        small_mp4 = int(small_dt.timestamp()) + sei_parser._MP4_EPOCH_OFFSET
        # Build a filename whose timestamp minute matches the local
        # rendering of small_dt so the skew is a few seconds, not hours.
        fname_dt = datetime.fromtimestamp(small_dt.timestamp())
        fname = fname_dt.strftime("%Y-%m-%d_%H-%M-%S") + "-front.mp4"
        path = _write_tmp_mp4(
            tmp_path, fname, _make_mp4(_mvhd_v0(small_mp4)),
        )
        with caplog.at_level("WARNING", logger="services.mapping_service"):
            mapping_service._resolve_recording_time(path)
        assert not any(
            "clock skew" in r.message.lower() for r in caplog.records
        )

    def test_mvhd_exception_falls_back_to_filename(self, tmp_path):
        # If the mvhd helper raises (defence in depth — it shouldn't,
        # but we want the indexer to keep working anyway), we must
        # still return the filename-derived value.
        path = _write_tmp_mp4(
            tmp_path, "2026-05-11_07-50-38-front.mp4",
            _make_mp4(_mvhd_v0(_REF_MP4)),
        )
        with mock.patch.object(
            sei_parser, "extract_mvhd_creation_time",
            side_effect=RuntimeError("boom"),
        ):
            # Force the lazy getter to re-resolve so the patch takes effect.
            mapping_service._sei_parser = None
            result = mapping_service._resolve_recording_time(path)
        assert result == "2026-05-11T07:50:38"
