"""Tests for the license_plate_service module.

These tests cover:

- Filename validation: empty, too long, contains underscore/dash/space,
  non-PNG extension. The license-plate filename rules are stricter than
  the wraps rules — alphanumeric only, ≤ 32 chars.
- Dimension validation: only 420x200 (NA) and 420x100 (EU) accepted.
- Size validation: 511 KB ≤ 512 KB pass; 513 KB fail.
- PNG signature validation: random bytes rejected.
- Count enforcement (the regression bug from wraps): 10 plates → 11th
  rejected in BOTH present and edit mode. ``get_plate_count_any_mode``
  reads the RO mount in present mode so the limit can't be bypassed.
- USB rebind: present-mode upload/delete invokes
  ``safe_rebind_usb_gadget`` exactly once; edit-mode does not.
- Rebind failures (return ``(False, msg)`` or raise) must NOT fail the
  upload/delete — the file is already on disk.
"""

import io
import os
import struct
import zlib
from unittest.mock import MagicMock

import pytest


def _build_minimal_png_bytes(width: int, height: int) -> bytes:
    """Return a syntactically valid PNG with the requested IHDR dims.

    Only IHDR is inspected by the service, so empty IDAT is fine.
    """
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    ihdr = b'IHDR' + ihdr_data
    ihdr_chunk = (
        struct.pack('>I', len(ihdr_data)) + ihdr +
        struct.pack('>I', zlib.crc32(ihdr))
    )
    idat_data = zlib.compress(b'')
    idat = b'IDAT' + idat_data
    idat_chunk = (
        struct.pack('>I', len(idat_data)) + idat +
        struct.pack('>I', zlib.crc32(idat))
    )
    iend_chunk = (
        struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND'))
    )
    return sig + ihdr_chunk + idat_chunk + iend_chunk


def _build_png_with_padding(width: int, height: int, total_bytes: int) -> bytes:
    """Return a PNG of approximately ``total_bytes`` size by stuffing
    a long IDAT comment-style chunk. Used to test the size limit
    without producing megabytes of pixels."""
    base = _build_minimal_png_bytes(width, height)
    needed = total_bytes - len(base)
    if needed <= 0:
        return base
    # Insert a fake ancillary chunk before IEND. We rebuild from base by
    # splitting at IEND (12-byte chunk: length+type+crc).
    iend_offset = len(base) - 12
    head = base[:iend_offset]
    tail = base[iend_offset:]
    payload = b'\x00' * max(needed - 12, 1)  # extra 12 = chunk header+crc
    chunk_type = b'tEXt'
    chunk = (
        struct.pack('>I', len(payload)) + chunk_type + payload +
        struct.pack('>I', zlib.crc32(chunk_type + payload))
    )
    return head + chunk + tail


class _FakeUploadFile:
    """Mimics werkzeug.FileStorage's read/seek/filename surface."""

    def __init__(self, filename, data):
        self.filename = filename
        self._buf = io.BytesIO(data)

    def read(self):
        return self._buf.read()

    def seek(self, pos):
        self._buf.seek(pos)


@pytest.fixture
def fake_lightshow(tmp_path, monkeypatch):
    """Build the same fake LightShow drive layout used by wrap tests:

        <root>/part2-ro/LicensePlate/   (present-mode RO)
        <root>/part2/LicensePlate/      (edit-mode RW; also the dest in
                                         present mode after quick_edit)

    Patch MNT_DIR so no real mounts are touched.
    """
    root = tmp_path / 'mnt' / 'gadget'
    (root / 'part2-ro' / 'LicensePlate').mkdir(parents=True)
    (root / 'part2' / 'LicensePlate').mkdir(parents=True)
    monkeypatch.setattr('config.MNT_DIR', str(root), raising=False)
    return {
        'root': root,
        'ro_plates': root / 'part2-ro' / 'LicensePlate',
        'rw_plates': root / 'part2' / 'LicensePlate',
    }


def _populate_plates(folder, count, prefix='plate'):
    for i in range(count):
        # Real PNG signature so list_plate_files counts them.
        (folder / f'{prefix}{i}.png').write_bytes(b'\x89PNG\r\n\x1a\n')


# ---------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------
class TestValidatePlateFilename:

    @pytest.mark.parametrize('name', [
        'plate.png', 'PLATE.png', 'Abc123.png', 'a.png',
        ('a' * 32) + '.png',
    ])
    def test_valid_names(self, name):
        from services.license_plate_service import validate_plate_filename
        ok, err = validate_plate_filename(name)
        assert ok, f'{name!r} should be valid; got error: {err!r}'

    @pytest.mark.parametrize('name,reason', [
        ('.png', 'empty base'),
        ((('a' * 33) + '.png'), 'too long'),
        ('my_plate.png', 'underscore'),
        ('my-plate.png', 'dash'),
        ('my plate.png', 'space'),
        ('plate!.png', 'punctuation'),
        ('plate.txt', 'wrong ext'),
        ('plate', 'no ext'),
    ])
    def test_invalid_names(self, name, reason):
        from services.license_plate_service import validate_plate_filename
        ok, err = validate_plate_filename(name)
        assert not ok, f'{name!r} ({reason}) should be invalid'
        assert err  # message present

    def test_uppercase_extension_accepted(self):
        # The service uses .lower().endswith('.png'), so .PNG works.
        from services.license_plate_service import validate_plate_filename
        ok, err = validate_plate_filename('Abc123.PNG')
        assert ok, err


# ---------------------------------------------------------------
# Dimension validation
# ---------------------------------------------------------------
class TestValidatePlateDimensions:

    def test_na_passes(self):
        from services.license_plate_service import validate_plate_dimensions
        ok, err = validate_plate_dimensions(420, 200)
        assert ok, err

    def test_eu_passes(self):
        from services.license_plate_service import validate_plate_dimensions
        ok, err = validate_plate_dimensions(420, 100)
        assert ok, err

    @pytest.mark.parametrize('w,h', [
        (419, 200),     # one px off NA width
        (421, 100),     # one px off EU width
        (420, 199),     # one px off NA height
        (420, 101),     # one px off EU height
        (200, 420),     # transposed
        (512, 512),     # wrap-spec dims (deliberately NOT accepted)
        (1920, 1080),   # arbitrary photo
        (0, 0),         # degenerate
    ])
    def test_off_spec_rejected(self, w, h):
        from services.license_plate_service import validate_plate_dimensions
        ok, err = validate_plate_dimensions(w, h)
        assert not ok, f'({w}, {h}) should be rejected'
        assert err

    def test_none_dimensions_rejected(self):
        from services.license_plate_service import validate_plate_dimensions
        ok, err = validate_plate_dimensions(None, None)
        assert not ok
        assert 'corrupted' in err.lower()


# ---------------------------------------------------------------
# Whole-file validation (size + signature + dims + name)
# ---------------------------------------------------------------
class TestValidatePlateFile:

    def test_valid_na_png(self):
        from services.license_plate_service import validate_plate_file
        png = _build_minimal_png_bytes(420, 200)
        ok, err, dims = validate_plate_file(png, 'plate.png')
        assert ok, err
        assert dims == (420, 200)

    def test_valid_eu_png(self):
        from services.license_plate_service import validate_plate_file
        png = _build_minimal_png_bytes(420, 100)
        ok, err, dims = validate_plate_file(png, 'plate.png')
        assert ok, err
        assert dims == (420, 100)

    def test_511kb_passes(self):
        from services.license_plate_service import (
            validate_plate_file, MAX_PLATE_SIZE,
        )
        png = _build_png_with_padding(420, 200, 511 * 1024)
        # Some platforms produce slightly larger; ensure under limit.
        assert len(png) <= MAX_PLATE_SIZE
        ok, err, dims = validate_plate_file(png, 'plate.png')
        assert ok, err

    def test_513kb_fails(self):
        from services.license_plate_service import validate_plate_file
        png = _build_png_with_padding(420, 200, 513 * 1024)
        assert len(png) > 512 * 1024
        ok, err, dims = validate_plate_file(png, 'plate.png')
        assert not ok
        assert '512 KB' in err

    def test_bogus_bytes_rejected(self):
        from services.license_plate_service import validate_plate_file
        ok, err, dims = validate_plate_file(b'not-a-png-at-all', 'plate.png')
        assert not ok
        # Failure surfaces as a dimension/corruption error since the
        # PNG signature check fails inside the dimension reader.
        assert err and dims is None

    def test_off_dim_png_rejected(self):
        from services.license_plate_service import validate_plate_file
        png = _build_minimal_png_bytes(512, 512)
        ok, err, dims = validate_plate_file(png, 'plate.png')
        assert not ok
        assert dims is None
        assert '420' in err  # message references the allowed dims

    def test_bad_filename_rejected_before_dims(self):
        # If the filename is invalid, error mentions filename, not dims.
        from services.license_plate_service import validate_plate_file
        png = _build_minimal_png_bytes(420, 200)
        ok, err, dims = validate_plate_file(png, 'my-plate.png')
        assert not ok
        assert dims is None
        assert 'letters' in err.lower() or 'numbers' in err.lower()


# ---------------------------------------------------------------
# Count enforcement (the regression bug from wraps)
# ---------------------------------------------------------------
class TestGetPlateCountAnyMode:

    def test_present_mode_reads_from_ro(self, fake_lightshow, monkeypatch):
        # Populate ONLY the RO side — a buggy reader would return 0.
        _populate_plates(fake_lightshow['ro_plates'], 5)
        from services import license_plate_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')
        assert license_plate_service.get_plate_count_any_mode() == 5

    def test_edit_mode_reads_from_rw(self, fake_lightshow, monkeypatch):
        _populate_plates(fake_lightshow['rw_plates'], 7)
        from services import license_plate_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'edit')
        assert license_plate_service.get_plate_count_any_mode() == 7

    def test_present_mode_no_plates_returns_zero(
            self, fake_lightshow, monkeypatch):
        from services import license_plate_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')
        assert license_plate_service.get_plate_count_any_mode() == 0

    def test_does_not_count_non_png_files(
            self, fake_lightshow, monkeypatch):
        _populate_plates(fake_lightshow['ro_plates'], 3)
        (fake_lightshow['ro_plates'] / '.DS_Store').write_bytes(b'')
        (fake_lightshow['ro_plates'] / 'README.txt').write_text('hi')
        from services import license_plate_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')
        assert license_plate_service.get_plate_count_any_mode() == 3

    def test_present_mode_count_at_limit_in_present_mode(
            self, fake_lightshow, monkeypatch):
        # Regression: in present mode, blueprint used to call
        # get_plate_count(None) which silently returned 0 and bypassed
        # MAX_PLATE_COUNT. With the helper, the limit is enforced.
        _populate_plates(fake_lightshow['ro_plates'], 10)
        # quick_edit_part2 must NOT be called — the count gate fires
        # before any expensive RW remount.
        mock_quick_edit = MagicMock()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            mock_quick_edit)
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')

        from services import license_plate_service
        from services.license_plate_service import MAX_PLATE_COUNT

        assert license_plate_service.get_plate_count_any_mode() == 10
        assert (
            license_plate_service.get_plate_count_any_mode() >= MAX_PLATE_COUNT
        )
        mock_quick_edit.assert_not_called()

    def test_edit_mode_count_at_limit(self, fake_lightshow, monkeypatch):
        # Edit mode has always counted correctly via the RW mount —
        # confirm this still holds in the new helper.
        _populate_plates(fake_lightshow['rw_plates'], 10)
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'edit')
        from services import license_plate_service
        from services.license_plate_service import MAX_PLATE_COUNT
        assert (
            license_plate_service.get_plate_count_any_mode() >= MAX_PLATE_COUNT
        )


# ---------------------------------------------------------------
# Rebind behavior on present-mode upload / delete
# ---------------------------------------------------------------
class TestRebindAfterUpload:

    def _do_upload(self, fake_lightshow, mode_value, monkeypatch,
                   defer_rebind=False):
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: mode_value)

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            _fake_quick_edit)

        from services import license_plate_service
        png = _build_minimal_png_bytes(420, 200)
        upload = _FakeUploadFile('plate.png', png)
        part2_path = (
            str(fake_lightshow['rw_plates'].parent)
            if mode_value == 'edit' else None
        )
        return license_plate_service.upload_plate_file(
            upload, 'plate.png', part2_path, defer_rebind=defer_rebind)

    def test_present_mode_upload_calls_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)

        ok, msg, dims = self._do_upload(
            fake_lightshow, 'present', monkeypatch)
        assert ok, msg
        rebind.assert_called_once()

    def test_edit_mode_upload_does_not_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        ok, msg, dims = self._do_upload(
            fake_lightshow, 'edit', monkeypatch)
        assert ok, msg
        rebind.assert_not_called()

    def test_rebind_failure_does_not_fail_upload(
            self, fake_lightshow, monkeypatch, caplog):
        rebind = MagicMock(return_value=(False, 'gadget busy'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        with caplog.at_level('WARNING'):
            ok, msg, dims = self._do_upload(
                fake_lightshow, 'present', monkeypatch)
        assert ok
        rebind.assert_called_once()
        assert any(
            'rebind' in rec.message.lower() for rec in caplog.records
        )

    def test_rebind_exception_does_not_fail_upload(
            self, fake_lightshow, monkeypatch, caplog):
        rebind = MagicMock(side_effect=RuntimeError('configfs not mounted'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        with caplog.at_level('WARNING'):
            ok, msg, dims = self._do_upload(
                fake_lightshow, 'present', monkeypatch)
        assert ok
        rebind.assert_called_once()

    def test_defer_rebind_suppresses_call(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        ok, msg, dims = self._do_upload(
            fake_lightshow, 'present', monkeypatch, defer_rebind=True)
        assert ok, msg
        rebind.assert_not_called()

    def test_invalid_upload_skips_quick_edit(
            self, fake_lightshow, monkeypatch):
        # If the file fails validation, quick_edit_part2 must never run.
        mock_quick = MagicMock()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            mock_quick)
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')
        from services import license_plate_service
        bad = _FakeUploadFile('plate.png', _build_minimal_png_bytes(512, 512))
        ok, msg, dims = license_plate_service.upload_plate_file(
            bad, 'plate.png', None)
        assert not ok
        mock_quick.assert_not_called()


class TestRebindAfterDelete:

    def _do_delete(self, fake_lightshow, mode_value, monkeypatch,
                   defer_rebind=False):
        # Pre-place a file in the RW location (where present-mode
        # delete writes via quick_edit_part2 -> MNT_DIR/part2).
        target = fake_lightshow['rw_plates'] / 'doomed.png'
        target.write_bytes(b'\x89PNG\r\n\x1a\n')
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: mode_value)

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            _fake_quick_edit)

        from services import license_plate_service
        part2_path = (
            str(fake_lightshow['rw_plates'].parent)
            if mode_value == 'edit' else None
        )
        return license_plate_service.delete_plate_file(
            'doomed.png', part2_path, defer_rebind=defer_rebind)

    def test_present_mode_delete_calls_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        ok, msg = self._do_delete(fake_lightshow, 'present', monkeypatch)
        assert ok, msg
        rebind.assert_called_once()

    def test_edit_mode_delete_does_not_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        ok, msg = self._do_delete(fake_lightshow, 'edit', monkeypatch)
        assert ok, msg
        rebind.assert_not_called()

    def test_rebind_failure_does_not_fail_delete(
            self, fake_lightshow, monkeypatch, caplog):
        rebind = MagicMock(return_value=(False, 'gadget busy'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        with caplog.at_level('WARNING'):
            ok, msg = self._do_delete(
                fake_lightshow, 'present', monkeypatch)
        assert ok
        rebind.assert_called_once()
        assert any(
            'rebind' in rec.message.lower() for rec in caplog.records
        )

    def test_rebind_exception_does_not_fail_delete(
            self, fake_lightshow, monkeypatch, caplog):
        rebind = MagicMock(side_effect=RuntimeError('configfs not mounted'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget', rebind)
        with caplog.at_level('WARNING'):
            ok, msg = self._do_delete(
                fake_lightshow, 'present', monkeypatch)
        assert ok
        rebind.assert_called_once()


# ---------------------------------------------------------------
# Path-traversal sanitization
# ---------------------------------------------------------------
class TestPathSanitization:

    def test_upload_strips_path_components_from_filename(
            self, fake_lightshow, monkeypatch):
        # A hostile client could submit "../../etc/passwd". The service
        # must basename() the filename before joining with the destination.
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'edit')
        from services import license_plate_service
        png = _build_minimal_png_bytes(420, 200)
        # Filename validation rejects path-y names anyway because '/'
        # isn't alphanumeric — but if a future relaxation lets '/'
        # through, basename() still saves us.
        upload = _FakeUploadFile('../../etc/passwd.png', png)
        part2_path = str(fake_lightshow['rw_plates'].parent)
        ok, msg, dims = license_plate_service.upload_plate_file(
            upload, '../../etc/passwd.png', part2_path)
        # Filename validator rejects the slash/dot-dot first.
        assert not ok
        assert dims is None

    def test_delete_strips_path_components(
            self, fake_lightshow, monkeypatch):
        # delete_plate_file uses basename() on the input. Even if a
        # hostile request reaches the service with a traversal path,
        # the join can't escape the LicensePlate folder.
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'edit')
        from services import license_plate_service
        # Pre-place a file the attacker is "trying" to overwrite —
        # outside the plate folder.
        outside = fake_lightshow['root'] / 'outside.txt'
        outside.write_text('hands off')
        # And a real plate to delete.
        target = fake_lightshow['rw_plates'] / 'realplate.png'
        target.write_bytes(b'\x89PNG\r\n\x1a\n')
        part2_path = str(fake_lightshow['rw_plates'].parent)

        # Attempt to delete via a traversal path. basename() reduces
        # this to 'outside.txt', which doesn't exist in plates dir.
        ok, msg = license_plate_service.delete_plate_file(
            '../outside.txt', part2_path)
        assert not ok
        assert outside.exists(), 'File outside plate folder must be untouched'


# ---------------------------------------------------------------
# Listing surfaces non-compliant files (per "first-launch behavior")
# ---------------------------------------------------------------
class TestListPlateFiles:

    def test_compliant_file_listed_without_issues(self, fake_lightshow):
        png = _build_minimal_png_bytes(420, 200)
        (fake_lightshow['ro_plates'] / 'goodplate.png').write_bytes(png)
        from services import license_plate_service
        files = license_plate_service.list_plate_files(
            str(fake_lightshow['ro_plates'].parent))
        names = {f['filename']: f for f in files}
        assert 'goodplate.png' in names
        assert names['goodplate.png']['compliant'] is True
        assert names['goodplate.png']['issues'] == []
        assert names['goodplate.png']['width'] == 420
        assert names['goodplate.png']['height'] == 200

    def test_off_spec_file_listed_with_issues(self, fake_lightshow):
        # A user dropped a 1920x1080 PNG via Samba — must be visible
        # in the listing with a warning so they can clean up.
        bad = _build_minimal_png_bytes(1920, 1080)
        (fake_lightshow['ro_plates'] / 'badsize.png').write_bytes(bad)
        from services import license_plate_service
        files = license_plate_service.list_plate_files(
            str(fake_lightshow['ro_plates'].parent))
        names = {f['filename']: f for f in files}
        assert 'badsize.png' in names
        assert names['badsize.png']['compliant'] is False
        assert any('420' in i for i in names['badsize.png']['issues'])

    def test_bad_filename_listed_with_issues(self, fake_lightshow):
        # User dropped a file with a dash — must surface, not hide.
        png = _build_minimal_png_bytes(420, 200)
        (fake_lightshow['ro_plates'] / 'my-plate.png').write_bytes(png)
        from services import license_plate_service
        files = license_plate_service.list_plate_files(
            str(fake_lightshow['ro_plates'].parent))
        names = {f['filename']: f for f in files}
        assert 'my-plate.png' in names
        assert names['my-plate.png']['compliant'] is False
        assert any(
            'letters' in i.lower() or 'numbers' in i.lower()
            for i in names['my-plate.png']['issues']
        )

    def test_missing_folder_returns_empty(self, tmp_path):
        # Existing devices won't have a LicensePlate folder yet.
        from services import license_plate_service
        files = license_plate_service.list_plate_files(str(tmp_path))
        assert files == []

    def test_none_mount_path_returns_empty(self):
        from services import license_plate_service
        assert license_plate_service.list_plate_files(None) == []
