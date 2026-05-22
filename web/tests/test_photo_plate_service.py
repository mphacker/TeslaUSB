# ruff: noqa: ARG002  # pytest fixtures; intentional in-test imports.
"""Tests for the photo-plate service (Tesla custom-background PNGs)."""

from __future__ import annotations

import io
import os
import struct
import zlib
from typing import TYPE_CHECKING, cast

import pytest
from teslausb_web.services.photo_plate_service import (
    PhotoPlateError,
    PhotoPlateService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from werkzeug.datastructures import FileStorage


def _png_bytes(width: int, height: int, *, payload_size: int = 0) -> bytes:
    """Build a minimally-valid PNG (signature + IHDR + IDAT + IEND)."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = (
        struct.pack(">I", len(ihdr_data))
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    )
    idat_payload = b"\x00" * max(1, payload_size)
    idat_compressed = zlib.compress(idat_payload)
    idat_chunk = (
        struct.pack(">I", len(idat_compressed))
        + b"IDAT"
        + idat_compressed
        + struct.pack(">I", zlib.crc32(b"IDAT" + idat_compressed) & 0xFFFFFFFF)
    )
    iend_chunk = (
        struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )
    return signature + ihdr_chunk + idat_chunk + iend_chunk


class _FakeUpload:
    """Minimal stand-in for werkzeug FileStorage."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.stream = io.BytesIO(data)


def _fake_uploads(*uploads: _FakeUpload) -> list[FileStorage]:
    return cast("list[FileStorage]", list(uploads))


@pytest.fixture
def plates_folder(tmp_path: Path) -> Path:
    return tmp_path / "LicensePlate"


@pytest.fixture
def service(plates_folder: Path) -> PhotoPlateService:
    return PhotoPlateService(plates_folder=plates_folder)


class TestUpload:
    def test_accepts_exact_420x75_png(
        self, service: PhotoPlateService, plates_folder: Path
    ) -> None:
        upload = _FakeUpload("MI.png", _png_bytes(420, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is True
        assert result.file_count == 1
        assert (plates_folder / "MI.png").is_file()

    def test_accepts_exact_492x75_png(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("Italy.png", _png_bytes(492, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is True

    def test_rejects_wrong_dimensions(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("Bad.png", _png_bytes(100, 100))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False
        assert "dimensions" in result.message.lower()

    def test_rejects_non_png(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("MI.jpg", b"not a png")
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False
        assert "png" in result.message.lower()

    def test_rejects_oversize_file(self, plates_folder: Path) -> None:
        smaller_limit = PhotoPlateService(plates_folder=plates_folder, max_file_size=256)
        random_data = os.urandom(2_000)
        payload = b"\x89PNG\r\n\x1a\n" + random_data
        upload = _FakeUpload("MI.png", payload)
        result = smaller_limit.upload_files(_fake_uploads(upload))
        assert result.success is False
        assert "size" in result.message.lower()

    def test_rejects_filename_too_long(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("thirteenchars.png", _png_bytes(420, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False
        assert "12" in result.message

    def test_rejects_filename_with_underscore(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("MI_plate.png", _png_bytes(420, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False
        assert "letters and digits" in result.message

    def test_rejects_filename_with_dash(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("MI-1.png", _png_bytes(420, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False

    def test_rejects_path_traversal(self, service: PhotoPlateService) -> None:
        upload = _FakeUpload("../evil.png", _png_bytes(420, 75))
        result = service.upload_files(_fake_uploads(upload))
        assert result.success is False

    def test_enforces_max_plate_count(self, plates_folder: Path) -> None:
        capped = PhotoPlateService(plates_folder=plates_folder, max_plate_count=2)
        for i, name in enumerate(("A", "B")):
            assert (
                capped.upload_files(
                    _fake_uploads(_FakeUpload(f"{name}.png", _png_bytes(420, 75)))
                ).success
                is True
            )
            assert capped.count_plates() == i + 1
        third = capped.upload_files(_fake_uploads(_FakeUpload("C.png", _png_bytes(420, 75))))
        assert third.success is False
        assert "maximum" in third.message.lower()

    def test_empty_upload_list_reports_no_files(self, service: PhotoPlateService) -> None:
        assert service.upload_files([]).success is False


class TestListAndDelete:
    def test_list_returns_empty_when_folder_missing(self, service: PhotoPlateService) -> None:
        assert service.list_plates() == ()

    def test_list_reports_compliant_metadata(
        self, service: PhotoPlateService, plates_folder: Path
    ) -> None:
        service.upload_files(_fake_uploads(_FakeUpload("MI.png", _png_bytes(420, 75))))
        plates = service.list_plates()
        assert len(plates) == 1
        plate = plates[0]
        assert plate.filename == "MI.png"
        assert plate.compliant is True
        assert plate.dimensions == "420x75"
        assert plate.partition_key == "LightShow"

    def test_delete_removes_file(self, service: PhotoPlateService, plates_folder: Path) -> None:
        service.upload_files(_fake_uploads(_FakeUpload("MI.png", _png_bytes(420, 75))))
        result = service.delete_plate("MI.png")
        assert result.success is True
        assert not (plates_folder / "MI.png").exists()

    def test_delete_missing_returns_not_found(self, service: PhotoPlateService) -> None:
        result = service.delete_plate("ghost.png")
        assert result.success is False

    def test_resolve_plate_raises_for_missing(self, service: PhotoPlateService) -> None:
        with pytest.raises(PhotoPlateError):
            service.resolve_plate("missing.png")

    def test_resolve_plate_returns_existing(
        self, service: PhotoPlateService, plates_folder: Path
    ) -> None:
        service.upload_files(_fake_uploads(_FakeUpload("MI.png", _png_bytes(420, 75))))
        path = service.resolve_plate("MI.png")
        assert path == plates_folder / "MI.png"
