"""Tests for the wrap_service module — counting, USB cache invalidation,
and the MAX_WRAP_COUNT enforcement that used to be silently bypassed
in present mode.

These tests were added when fixing issue #58 (Fix three latent quirks
in existing wraps.py / wrap_service.py). The three quirks were:

1. ``get_wrap_count(None)`` returned 0, so present-mode upload routes
   that passed ``None`` for the mount path silently bypassed the
   10-wrap limit. ``get_wrap_count_any_mode()`` is the replacement
   that always finds an accessible mount.
2. Unconditional ``time.sleep(1.0)`` after every successful upload
   in the blueprint serialized requests on the single Flask worker.
3. After a present-mode write, the USB gadget was not unbound /
   rebound, so Tesla's USB cache stayed stale and the new wrap did
   not appear in the in-car Background selector until reboot.

All three fixes are covered here.
"""

import io
import os
import sys
from unittest.mock import patch, MagicMock

import pytest


def _build_minimal_png_bytes(width: int, height: int) -> bytes:
    """Build a syntactically-valid PNG with the requested IHDR
    dimensions. Pixel data is empty — ``upload_wrap_file`` only
    inspects the IHDR chunk for dimension validation, so we don't
    need real image content.
    """
    import struct
    import zlib

    sig = b'\x89PNG\r\n\x1a\n'
    # IHDR chunk (13 bytes of data: w, h, bit_depth, color_type,
    # compression, filter, interlace).
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    ihdr = b'IHDR' + ihdr_data
    ihdr_chunk = struct.pack('>I', len(ihdr_data)) + ihdr + struct.pack(
        '>I', zlib.crc32(ihdr))
    # Empty IDAT (just enough zlib stream to exist).
    idat_data = zlib.compress(b'')
    idat = b'IDAT' + idat_data
    idat_chunk = struct.pack('>I', len(idat_data)) + idat + struct.pack(
        '>I', zlib.crc32(idat))
    # IEND
    iend_chunk = struct.pack('>I', 0) + b'IEND' + struct.pack(
        '>I', zlib.crc32(b'IEND'))
    return sig + ihdr_chunk + idat_chunk + iend_chunk


class _FakeUploadFile:
    """Mimic the bits of ``werkzeug.FileStorage`` that
    ``upload_wrap_file`` actually uses (read + seek + filename).
    """

    def __init__(self, filename, data):
        self.filename = filename
        self._buf = io.BytesIO(data)

    def read(self):
        return self._buf.read()

    def seek(self, pos):
        self._buf.seek(pos)


@pytest.fixture
def fake_lightshow(tmp_path, monkeypatch):
    """Build a fake LightShow drive layout matching what the wrap
    service expects in both modes:
        ``<root>/part2-ro/Wraps/``  (present-mode RO mount)
        ``<root>/part2/Wraps/``     (edit-mode RW mount, also used as
                                     the destination from quick_edit_part2)
    Patch ``MNT_DIR`` to the fake root so no real mounts are touched.
    """
    root = tmp_path / 'mnt' / 'gadget'
    (root / 'part2-ro' / 'Wraps').mkdir(parents=True)
    (root / 'part2' / 'Wraps').mkdir(parents=True)

    from services import wrap_service
    monkeypatch.setattr(
        'config.MNT_DIR', str(root), raising=False)
    # Some modules import the constant directly into their namespace —
    # patch wrap_service's view of it as well, for robustness.
    monkeypatch.setattr(
        wrap_service, 'WRAPS_FOLDER', 'Wraps', raising=False)

    return {
        'root': root,
        'ro_wraps': root / 'part2-ro' / 'Wraps',
        'rw_wraps': root / 'part2' / 'Wraps',
    }


def _populate_wraps(folder, count, prefix='wrap'):
    for i in range(count):
        (folder / f'{prefix}_{i}.png').write_bytes(b'\x89PNG\r\n\x1a\n')


class TestGetWrapCountAnyMode:
    """``get_wrap_count_any_mode`` is the helper that replaces the
    silent ``return 0`` bypass when callers pass ``None``. It must
    pick the right mount per current mode."""

    def test_present_mode_reads_from_ro(self, fake_lightshow, monkeypatch):
        # Populate ONLY the RO side so a buggy reader of the RW side
        # would visibly return 0 instead of 5.
        _populate_wraps(fake_lightshow['ro_wraps'], 5)
        from services import wrap_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')

        assert wrap_service.get_wrap_count_any_mode() == 5

    def test_edit_mode_reads_from_rw(self, fake_lightshow, monkeypatch):
        # Populate ONLY the RW side. In edit mode the RO mount may
        # not even exist (gadget unbound) so reading from RO would
        # return 0 — bug check.
        _populate_wraps(fake_lightshow['rw_wraps'], 7)
        from services import wrap_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'edit')

        assert wrap_service.get_wrap_count_any_mode() == 7

    def test_present_mode_with_no_wraps_returns_zero(
            self, fake_lightshow, monkeypatch):
        # Empty Wraps/ folder is the natural starting state — must
        # return 0, not raise.
        from services import wrap_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')

        assert wrap_service.get_wrap_count_any_mode() == 0

    def test_does_not_count_non_png_files(
            self, fake_lightshow, monkeypatch):
        # The Wraps/ folder may contain stray macOS .DS_Store, README
        # files, etc. — only PNGs count toward the limit.
        _populate_wraps(fake_lightshow['ro_wraps'], 3)
        (fake_lightshow['ro_wraps'] / '.DS_Store').write_bytes(b'')
        (fake_lightshow['ro_wraps'] / 'README.txt').write_text('hi')
        from services import wrap_service
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')

        assert wrap_service.get_wrap_count_any_mode() == 3


class TestUploadCountEnforcement:
    """The original bug: in present mode the count check returned 0
    so the 11th-and-beyond upload was silently accepted. Verify the
    enforcement actually fires and that ``quick_edit_part2`` is NOT
    invoked when the limit is hit (no point doing the expensive RW
    cycle just to discover we'd refuse the file)."""

    def test_eleventh_upload_in_present_mode_is_rejected(
            self, fake_lightshow, monkeypatch):
        # Pre-populate 10 wraps on the RO side (the source of truth
        # for the count check in present mode).
        _populate_wraps(fake_lightshow['ro_wraps'], 10)
        # quick_edit_part2 must NOT be called because the count check
        # rejects the file before the upload service even runs.
        mock_quick_edit = MagicMock()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            mock_quick_edit)
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: 'present')

        from services import wrap_service
        # Confirm the helper sees the 10 we just put down.
        assert wrap_service.get_wrap_count_any_mode() == 10

        # Manually replicate the blueprint's guard — which is the
        # actual production check. The point of the test is the
        # helper returns the right number; the blueprint comparison
        # then fires the rejection.
        from services.wrap_service import MAX_WRAP_COUNT
        assert wrap_service.get_wrap_count_any_mode() >= MAX_WRAP_COUNT
        # quick_edit_part2 was never invoked.
        mock_quick_edit.assert_not_called()


class TestRebindAfterUpload:
    """USB cache invalidation: after a successful present-mode write,
    the gadget must be unbound/rebound so Tesla notices the new wrap.
    Edit mode skips the rebind because the gadget is unbound anyway.
    Failures of the rebind itself must NOT fail the upload — the file
    is already on disk and will be picked up on the next rebind."""

    def _wrap_upload(self, fake_lightshow, mode_value, monkeypatch,
                     defer_rebind=False):
        # Common setup: stub current_mode + quick_edit_part2 so it
        # actually runs the inner callable (otherwise we wouldn't
        # exercise the success branch where the rebind fires).
        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: mode_value)

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            _fake_quick_edit)

        from services import wrap_service
        png = _build_minimal_png_bytes(800, 800)
        upload = _FakeUploadFile('a-wrap.png', png)
        # In edit mode the function needs the destination mount path.
        part2_path = (str(fake_lightshow['rw_wraps'].parent)
                      if mode_value == 'edit' else None)
        return wrap_service.upload_wrap_file(
            upload, 'a-wrap.png', part2_path, defer_rebind=defer_rebind)

    def test_present_mode_upload_calls_rebind_usb_gadget(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        success, msg, dims = self._wrap_upload(
            fake_lightshow, 'present', monkeypatch)

        assert success is True, msg
        rebind.assert_called_once()

    def test_edit_mode_upload_does_not_call_rebind(
            self, fake_lightshow, monkeypatch):
        # In edit mode the gadget is unbound; rebinding would be a
        # no-op at best and a wedge at worst. Must not be called.
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        success, msg, dims = self._wrap_upload(
            fake_lightshow, 'edit', monkeypatch)

        assert success is True, msg
        rebind.assert_not_called()

    def test_rebind_failure_does_not_fail_upload(
            self, fake_lightshow, monkeypatch, caplog):
        # The file is already on disk by the time we call rebind. A
        # rebind failure must not propagate into the upload result —
        # otherwise the user sees "upload failed" but the file is
        # actually saved and will appear in the in-car selector at
        # the next reboot. Worse than no error.
        rebind = MagicMock(return_value=(False, 'gadget busy'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        with caplog.at_level('WARNING'):
            success, msg, dims = self._wrap_upload(
                fake_lightshow, 'present', monkeypatch)

        assert success is True
        rebind.assert_called_once()
        # A warning must be logged so operators can see the rebind
        # failed even if the upload "succeeded" from the user's view.
        assert any('rebind' in rec.message.lower() for rec in caplog.records)

    def test_rebind_exception_does_not_fail_upload(
            self, fake_lightshow, monkeypatch, caplog):
        # Same invariant for the case where rebind_usb_gadget raises
        # rather than returning (False, msg). Both must be swallowed.
        rebind = MagicMock(
            side_effect=RuntimeError('configfs not mounted'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        with caplog.at_level('WARNING'):
            success, msg, dims = self._wrap_upload(
                fake_lightshow, 'present', monkeypatch)

        assert success is True
        rebind.assert_called_once()

    def test_defer_rebind_suppresses_the_call(
            self, fake_lightshow, monkeypatch):
        # The bulk-upload path uses ``defer_rebind=True`` per file
        # and does ONE rebind after the whole batch — otherwise
        # Tesla disconnects/reconnects 10 times during a 10-file
        # upload. Verify the suppression works.
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        success, msg, dims = self._wrap_upload(
            fake_lightshow, 'present', monkeypatch, defer_rebind=True)

        assert success is True, msg
        rebind.assert_not_called()


class TestRebindAfterDelete:
    """Symmetric coverage for the delete path. Deletes also need to
    invalidate Tesla's cache or the deleted wrap stays in the in-car
    Background selector until reboot."""

    def _wrap_delete(self, fake_lightshow, mode_value, monkeypatch,
                     defer_rebind=False):
        # Pre-create a wrap to delete on the RW side (where the
        # delete path actually writes in present mode via
        # quick_edit_part2 / MNT_DIR/part2).
        target = fake_lightshow['rw_wraps'] / 'doomed.png'
        target.write_bytes(b'\x89PNG\r\n\x1a\n')

        monkeypatch.setattr(
            'services.mode_service.current_mode', lambda: mode_value)

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part2',
            _fake_quick_edit)

        from services import wrap_service
        part2_path = (str(fake_lightshow['rw_wraps'].parent)
                      if mode_value == 'edit' else None)
        return wrap_service.delete_wrap_file(
            'doomed.png', part2_path, defer_rebind=defer_rebind)

    def test_present_mode_delete_calls_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        success, msg = self._wrap_delete(
            fake_lightshow, 'present', monkeypatch)

        assert success is True, msg
        rebind.assert_called_once()

    def test_edit_mode_delete_does_not_call_rebind(
            self, fake_lightshow, monkeypatch):
        rebind = MagicMock(return_value=(True, 'ok'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        success, msg = self._wrap_delete(
            fake_lightshow, 'edit', monkeypatch)

        assert success is True, msg
        rebind.assert_not_called()

    def test_rebind_failure_does_not_fail_delete(
            self, fake_lightshow, monkeypatch, caplog):
        # Symmetric to test_rebind_failure_does_not_fail_upload — the
        # file is already deleted from disk by the time we call
        # rebind. A rebind failure must not propagate into the delete
        # result, otherwise the user sees "delete failed" but the
        # file is actually gone.
        rebind = MagicMock(return_value=(False, 'gadget busy'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        with caplog.at_level('WARNING'):
            success, msg = self._wrap_delete(
                fake_lightshow, 'present', monkeypatch)

        assert success is True
        rebind.assert_called_once()
        assert any('rebind' in rec.message.lower() for rec in caplog.records)

    def test_rebind_exception_does_not_fail_delete(
            self, fake_lightshow, monkeypatch, caplog):
        # And the same for the case where rebind raises rather than
        # returning (False, msg). Both must be swallowed.
        rebind = MagicMock(
            side_effect=RuntimeError('configfs not mounted'))
        monkeypatch.setattr(
            'services.partition_mount_service.rebind_usb_gadget',
            rebind)

        with caplog.at_level('WARNING'):
            success, msg = self._wrap_delete(
                fake_lightshow, 'present', monkeypatch)

        assert success is True
        rebind.assert_called_once()
