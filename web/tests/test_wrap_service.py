"""Tests for ``teslausb_web.services.wrap_service``."""

from __future__ import annotations

import struct
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from teslausb_web.config import PathsSection, WebConfig, WrapsSection
from teslausb_web.services.wrap_service import (
    WrapError,
    WrapFileError,
    WrapInfo,
    WrapService,
    make_wrap_service,
)
from werkzeug.datastructures import FileStorage


@pytest.fixture
def wraps_folder(tmp_path: Path) -> Path:
    path = tmp_path / "lightshow" / "Wraps"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def service(wraps_folder: Path) -> WrapService:
    return WrapService(
        wraps_folder=wraps_folder,
        max_size=1 * 1024 * 1024,
        min_dimension=512,
        max_dimension=1024,
        max_filename_length=30,
        max_upload_count=10,
        allowed_extensions=(".png",),
    )


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _png_bytes(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x00\x00\x00" * width)
    raw = row * height
    idat = zlib.compress(raw)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", ihdr),
            _chunk(b"IDAT", idat),
            _chunk(b"IEND", b""),
        )
    )


def test_list_wraps_returns_empty_tuple_when_folder_missing(tmp_path: Path) -> None:
    service = WrapService(
        wraps_folder=tmp_path / "missing" / "Wraps",
        max_size=1 * 1024 * 1024,
        min_dimension=512,
        max_dimension=1024,
        max_filename_length=30,
        max_upload_count=10,
        allowed_extensions=(".png",),
    )
    assert service.list_wraps() == ()


def test_list_wraps_returns_sorted_pngs(service: WrapService, wraps_folder: Path) -> None:
    _write(wraps_folder / "bravo.png", _png_bytes(512, 512))
    _write(wraps_folder / "Alpha.png", _png_bytes(1024, 1024))
    _write(wraps_folder / "skip.txt", b"x")

    wraps = service.list_wraps()

    assert [entry.filename for entry in wraps] == ["Alpha.png", "bravo.png"]
    assert all(isinstance(entry, WrapInfo) for entry in wraps)
    assert wraps[0].modified_at.tzinfo is not None
    assert wraps[0].width == 1024
    assert wraps[1].height == 512


def test_list_wraps_keeps_invalid_png_with_missing_dimensions(
    service: WrapService, wraps_folder: Path
) -> None:
    _write(wraps_folder / "bad.png", b"not-a-png")

    wraps = service.list_wraps()

    assert len(wraps) == 1
    assert wraps[0].width is None
    assert wraps[0].height is None


def test_list_wraps_raises_typed_error_when_scanning_fails(service: WrapService) -> None:
    with (
        patch("pathlib.Path.iterdir", side_effect=OSError("boom")),
        pytest.raises(WrapFileError, match="Failed to list wrap files"),
    ):
        service.list_wraps()


def test_get_wrap_count_counts_only_pngs(service: WrapService, wraps_folder: Path) -> None:
    _write(wraps_folder / "one.png", _png_bytes(512, 512))
    _write(wraps_folder / "two.png", _png_bytes(512, 512))
    _write(wraps_folder / "note.txt", b"x")
    assert service.get_wrap_count() == 2


def test_validate_png_accepts_minimum_square(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "min.png", _png_bytes(512, 512)))
    assert result.success is True
    assert result.width == 512
    assert result.height == 512


def test_validate_png_accepts_maximum_square(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "max.png", _png_bytes(1024, 1024)))
    assert result.success is True
    assert result.width == 1024
    assert result.height == 1024


def test_validate_png_rejects_non_png(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "bad.png", b"not-a-png"))
    assert result.success is False
    assert "Could not read image dimensions" in result.message


def test_validate_png_rejects_non_square(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "rect.png", _png_bytes(512, 768)))
    assert result.success is False
    assert "square" in result.message


def test_validate_png_rejects_too_small(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "small.png", _png_bytes(511, 511)))
    assert result.success is False
    assert "at least 512x512" in result.message


def test_validate_png_rejects_too_large(service: WrapService, tmp_path: Path) -> None:
    result = service.validate_png(_write(tmp_path / "large.png", _png_bytes(1025, 1025)))
    assert result.success is False
    assert "must not exceed 1024x1024" in result.message


def test_validate_png_rejects_oversize_file(service: WrapService, tmp_path: Path) -> None:
    payload = _png_bytes(512, 512) + (b"x" * (1 * 1024 * 1024))
    result = service.validate_png(_write(tmp_path / "oversize.png", payload))
    assert result.success is False
    assert "1 MB or less" in result.message


def test_validate_png_raises_typed_error_for_missing_file(
    service: WrapService,
    tmp_path: Path,
) -> None:
    with pytest.raises(WrapFileError, match="Failed to stat wrap file"):
        service.validate_png(tmp_path / "missing.png")


def test_upload_files_saves_single_wrap(service: WrapService, wraps_folder: Path) -> None:
    result = service.upload_files([_upload("solid.png", _png_bytes(512, 512))])

    assert result.success is True
    assert result.file_count == 1
    assert (wraps_folder / "solid.png").exists()


def test_upload_files_saves_multiple_wraps(service: WrapService, wraps_folder: Path) -> None:
    result = service.upload_files(
        [
            _upload("one.png", _png_bytes(512, 512)),
            _upload("two.png", _png_bytes(1024, 1024)),
        ]
    )

    assert result.success is True
    assert result.file_count == 2
    assert (wraps_folder / "one.png").exists()
    assert (wraps_folder / "two.png").exists()


def test_upload_files_returns_false_when_no_candidates(service: WrapService) -> None:
    assert service.upload_files([]).success is False
    assert service.upload_files([_upload("", b"x")]).message == "No files selected"


def test_upload_files_collects_partial_failures(service: WrapService, wraps_folder: Path) -> None:
    result = service.upload_files(
        [
            _upload("ok.png", _png_bytes(512, 512)),
            _upload("bad chars!.png", _png_bytes(512, 512)),
        ]
    )

    assert result.success is True
    assert result.file_count == 1
    assert "Errors:" in result.message
    assert (wraps_folder / "ok.png").exists()


@pytest.mark.parametrize("name", ["cover.jpg", "bad chars!.png", "a" * 31 + ".png"])
def test_upload_files_rejects_invalid_names(service: WrapService, name: str) -> None:
    result = service.upload_files([_upload(name, _png_bytes(512, 512))])
    assert result.success is False


@pytest.mark.parametrize("name", ["../../evil.png", "folder\\evil.png"])
def test_upload_files_rejects_path_traversal(service: WrapService, name: str) -> None:
    result = service.upload_files([_upload(name, _png_bytes(512, 512))])
    assert result.success is False
    assert "Invalid filename" in result.message


def test_upload_files_rejects_non_square_png(service: WrapService, wraps_folder: Path) -> None:
    result = service.upload_files([_upload("rect.png", _png_bytes(512, 768))])
    assert result.success is False
    assert "square" in result.message
    assert not (wraps_folder / "rect.png").exists()


def test_upload_files_rejects_oversize_png(service: WrapService, wraps_folder: Path) -> None:
    payload = _png_bytes(512, 512) + (b"x" * (1 * 1024 * 1024))
    result = service.upload_files([_upload("big.png", payload)])
    assert result.success is False
    assert "1 MB or less" in result.message
    assert not (wraps_folder / "big.png").exists()


def test_upload_files_rejects_excess_count(service: WrapService) -> None:
    uploads = [_upload(f"wrap{i}.png", _png_bytes(512, 512)) for i in range(11)]
    result = service.upload_files(uploads)
    assert result.success is False
    assert "at most 10 wraps" in result.message


def test_upload_files_overwrites_duplicate_filename(
    service: WrapService,
    wraps_folder: Path,
) -> None:
    first = service.upload_files([_upload("dup.png", _png_bytes(512, 512))])
    second = service.upload_files([_upload("dup.png", _png_bytes(1024, 1024))])

    assert first.success is True
    assert second.success is True
    assert service.validate_png(wraps_folder / "dup.png").width == 1024


def test_upload_files_raises_typed_error_on_filesystem_failure(service: WrapService) -> None:
    with (
        patch("teslausb_web.services.wrap_service.os.replace", side_effect=OSError("boom")),
        pytest.raises(WrapFileError, match="Failed to write"),
    ):
        service.upload_files([_upload("wrap.png", _png_bytes(512, 512))])


def test_delete_wrap_removes_existing_file(service: WrapService, wraps_folder: Path) -> None:
    _write(wraps_folder / "delete.png", _png_bytes(512, 512))

    result = service.delete_wrap("delete.png")

    assert result.success is True
    assert result.deleted_count == 1
    assert not (wraps_folder / "delete.png").exists()


def test_delete_wrap_returns_false_for_missing_file(service: WrapService) -> None:
    result = service.delete_wrap("missing.png")
    assert result.success is False
    assert result.message == "File not found"


def test_delete_wrap_rejects_path_traversal(service: WrapService) -> None:
    with pytest.raises(WrapError, match="Invalid filename"):
        service.delete_wrap("../../evil.png")


def test_bulk_delete_deletes_multiple_files_and_reports_missing(
    service: WrapService,
    wraps_folder: Path,
) -> None:
    _write(wraps_folder / "one.png", _png_bytes(512, 512))
    _write(wraps_folder / "two.png", _png_bytes(512, 512))

    result = service.bulk_delete(["one.png", "missing.png", "two.png"])

    assert result.success is True
    assert result.deleted_count == 2
    assert "Errors:" in result.message
    assert not (wraps_folder / "one.png").exists()
    assert not (wraps_folder / "two.png").exists()


def test_bulk_delete_deduplicates_names(service: WrapService, wraps_folder: Path) -> None:
    _write(wraps_folder / "dup.png", _png_bytes(512, 512))

    result = service.bulk_delete(["dup.png", "dup.png"])

    assert result.success is True
    assert result.deleted_count == 1


def test_bulk_delete_rejects_path_traversal(service: WrapService) -> None:
    with pytest.raises(WrapError, match="Invalid filename"):
        service.bulk_delete(["good.png", "../../evil.png"])


def test_bulk_delete_returns_false_when_no_names(service: WrapService) -> None:
    result = service.bulk_delete([])
    assert result.success is False
    assert result.message == "No files selected"


def test_factory_uses_configured_paths() -> None:
    cfg = WebConfig(
        paths=PathsSection(backing_root=Path("/srv/teslausb"), state_dir=Path("/var/lib/teslausb")),
        wraps=WrapsSection(
            folder="CustomWraps",
            max_size=2048,
            min_dimension=600,
            max_dimension=900,
            max_filename_length=20,
            max_upload_count=4,
            allowed_extensions=(".png",),
        ),
    )

    service = make_wrap_service(cfg)

    assert service._wraps_folder == Path("/srv/teslausb") / "lightshow" / "CustomWraps"
    assert service._max_size == 2048
    assert service._min_dimension == 600
    assert service._max_dimension == 900
    assert service._max_filename_length == 20
    assert service._max_upload_count == 4
