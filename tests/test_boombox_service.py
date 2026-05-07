"""Tests for the boombox_service module — validation, count enforcement
in BOTH modes (explicit regression for the wraps-style bypass that was
fixed in PR #60), atomic upload + delete, and the MP3/WAV magic-byte
sniff that catches files renamed to .mp3/.wav.
"""

import io
import os
import struct
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _build_minimal_wav_bytes(payload_size: int = 64) -> bytes:
    """Return a syntactically-valid RIFF/WAVE file of the requested length.

    Only the magic bytes matter for the validator's sniff check, but we
    produce a header that any sane WAV parser will accept too.
    """
    fmt_chunk = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 8000, 1, 8)
    data = b'\x00' * payload_size
    data_chunk = b'data' + struct.pack('<I', len(data)) + data
    payload = fmt_chunk + data_chunk
    riff = b'RIFF' + struct.pack('<I', 4 + len(payload)) + b'WAVE' + payload
    return riff


def _build_minimal_mp3_bytes(prefix: str = 'sync') -> bytes:
    """Return MP3-shaped bytes the sniffer will accept.

    ``prefix='sync'``  -> starts with 0xFF E0 (MPEG frame sync)
    ``prefix='id3'``   -> starts with the ASCII tag ``ID3``
    """
    if prefix == 'id3':
        # ID3v2 header: "ID3" + version (2 bytes) + flags + size (4 bytes)
        return b'ID3' + b'\x03\x00' + b'\x00' + b'\x00\x00\x00\x00' + b'\x00' * 256
    # Frame sync: 0xFF + 0xE0 mask in second byte
    return b'\xff\xfb\x90\x00' + b'\x00' * 256


class _FakeUploadFile:
    """Mimic the bits of ``werkzeug.FileStorage`` that
    ``upload_boombox_file`` actually uses (read + seek + filename).
    """

    def __init__(self, filename, data):
        self.filename = filename
        self._buf = io.BytesIO(data)

    def read(self):
        return self._buf.read()

    def seek(self, pos):
        self._buf.seek(pos)


@pytest.fixture
def fake_music_drive(tmp_path, monkeypatch):
    """Build a fake Music drive layout matching what the boombox service
    expects in both modes:

        <root>/part3-ro/Boombox/   (present-mode RO mount; count source)
        <root>/part3/Boombox/      (edit-mode RW mount; also the
                                    destination from quick_edit_part3)

    Patch ``MNT_DIR`` everywhere it's already imported so neither the
    service nor the music helpers it reuses touch real mounts.
    """
    root = tmp_path / 'mnt' / 'gadget'
    (root / 'part3-ro' / 'Boombox').mkdir(parents=True)
    (root / 'part3' / 'Boombox').mkdir(parents=True)

    from services import boombox_service
    monkeypatch.setattr('config.MNT_DIR', str(root), raising=False)
    # Service modules cached MNT_DIR at import time (top-level
    # ``from config import MNT_DIR``); patch their references too.
    monkeypatch.setattr(boombox_service, 'MNT_DIR', str(root),
                        raising=False)

    return {
        'root': root,
        'ro_boombox': root / 'part3-ro' / 'Boombox',
        'rw_boombox': root / 'part3' / 'Boombox',
    }


def _set_mode(monkeypatch, mode_value):
    """Patch ``current_mode`` everywhere callers might look it up.

    ``boombox_service`` does ``from services.mode_service import
    current_mode`` at import time, so we have to overwrite the bound
    name on the service module — patching the source module alone
    doesn't reach the alias. We patch both for belt-and-suspenders.
    """
    from services import boombox_service
    monkeypatch.setattr(boombox_service, 'current_mode',
                        lambda: mode_value)
    monkeypatch.setattr('services.mode_service.current_mode',
                        lambda: mode_value)


def _populate(folder, count, prefix='sound', ext='.mp3'):
    """Create ``count`` placeholder boombox files in ``folder``."""
    for i in range(count):
        (folder / f'{prefix}_{i}{ext}').write_bytes(_build_minimal_mp3_bytes())


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------

class TestValidateBoomboxFilename:

    def test_empty_filename_rejected(self):
        from services.boombox_service import validate_boombox_filename
        ok, err = validate_boombox_filename("")
        assert not ok
        assert "empty" in err.lower()

    def test_whitespace_only_rejected(self):
        from services.boombox_service import validate_boombox_filename
        ok, err = validate_boombox_filename("   ")
        assert not ok

    def test_too_long_rejected(self):
        from services.boombox_service import (
            MAX_FILENAME_LENGTH, validate_boombox_filename,
        )
        # 65 chars total (above the 64-char cap), still ending in .mp3
        name = ('a' * (MAX_FILENAME_LENGTH - 4 + 1)) + '.mp3'
        ok, err = validate_boombox_filename(name)
        assert not ok
        assert str(MAX_FILENAME_LENGTH) in err

    def test_disallowed_chars_rejected(self):
        from services.boombox_service import validate_boombox_filename
        # Pipe/star/question mark are not in the allowed character set.
        # NB: ``_sanitize_name`` already strips colons, slashes, and
        # control chars before validation, so those don't appear here —
        # the regex only sees what survived sanitization.
        for bad in ['hello*.mp3', 'a?b.mp3', 'pipe|name.mp3',
                    'paren(name).mp3']:
            ok, err = validate_boombox_filename(bad)
            assert not ok, f"Should have rejected {bad}"

    def test_wrong_extension_rejected(self):
        from services.boombox_service import validate_boombox_filename
        for bad in ['song.aac', 'song.flac', 'song.m4a', 'song.txt',
                    'song']:
            ok, err = validate_boombox_filename(bad)
            assert not ok, f"Should have rejected {bad}"
            assert 'mp3' in err.lower() or 'wav' in err.lower()

    def test_valid_names_accepted(self):
        from services.boombox_service import validate_boombox_filename
        for good in ['horn.mp3', 'la cucaracha.wav',
                     'duck-quack_v2.mp3',
                     'A.MP3', 'song.WAV', 'name.with.dots.mp3']:
            ok, err = validate_boombox_filename(good)
            assert ok, f"Should have accepted {good}: {err}"


# ---------------------------------------------------------------------------
# Magic-byte sniffing
# ---------------------------------------------------------------------------

class TestMagicByteSniff:

    def test_wav_riff_wave_accepted(self):
        from services.boombox_service import validate_boombox_file
        wav = _build_minimal_wav_bytes()
        ok, err = validate_boombox_file(wav, 'horn.wav')
        assert ok, err

    def test_wav_with_wrong_magic_rejected(self):
        from services.boombox_service import validate_boombox_file
        # Looks like nothing — definitely not RIFF/WAVE
        bogus = b'NOTAWAVE' + b'\x00' * 32
        ok, err = validate_boombox_file(bogus, 'horn.wav')
        assert not ok
        assert 'wav' in err.lower()

    def test_mp3_id3_tag_accepted(self):
        from services.boombox_service import validate_boombox_file
        mp3 = _build_minimal_mp3_bytes(prefix='id3')
        ok, err = validate_boombox_file(mp3, 'horn.mp3')
        assert ok, err

    def test_mp3_frame_sync_accepted(self):
        from services.boombox_service import validate_boombox_file
        mp3 = _build_minimal_mp3_bytes(prefix='sync')
        ok, err = validate_boombox_file(mp3, 'horn.mp3')
        assert ok, err

    def test_mp3_with_wrong_magic_rejected(self):
        from services.boombox_service import validate_boombox_file
        # AAC ADTS sync (0xFF, 0xF1) does NOT match MPEG-1/2 audio.
        # Wait — 0xF1 IS top three bits set. Use a clearly non-MP3 one:
        # plain text bytes.
        bogus = b'<html>' + b'\x00' * 32
        ok, err = validate_boombox_file(bogus, 'horn.mp3')
        assert not ok
        assert 'mp3' in err.lower()

    def test_aac_renamed_to_mp3_rejected(self):
        # User saved an AAC file with a .mp3 extension. The first byte
        # might happen to be 0xFF, but only if it's an ADTS-framed stream
        # — and even then the second byte's mask differs. A plain .aac
        # MP4-style file starts with 'ftyp' which fails the sync test.
        from services.boombox_service import validate_boombox_file
        fake_aac = b'\x00\x00\x00\x20ftypM4A ' + b'\x00' * 128
        ok, err = validate_boombox_file(fake_aac, 'song.mp3')
        assert not ok


# ---------------------------------------------------------------------------
# Size enforcement
# ---------------------------------------------------------------------------

class TestSizeEnforcement:

    def test_just_under_one_mb_accepted(self):
        from services.boombox_service import (
            MAX_FILE_SIZE, validate_boombox_file,
        )
        # 1 MB minus 100 bytes — should pass.
        size = MAX_FILE_SIZE - 100
        # Build a syntactically valid MP3 of that size.
        mp3 = _build_minimal_mp3_bytes(prefix='id3')
        if len(mp3) < size:
            mp3 = mp3 + b'\x00' * (size - len(mp3))
        else:
            mp3 = mp3[:size]
        ok, err = validate_boombox_file(mp3, 'horn.mp3')
        assert ok, err

    def test_just_over_one_mb_rejected(self):
        from services.boombox_service import (
            MAX_FILE_SIZE, validate_boombox_file,
        )
        # 1 MB plus 1 byte.
        oversized = _build_minimal_mp3_bytes(prefix='id3') + b'\x00' * (
            MAX_FILE_SIZE + 1)
        ok, err = validate_boombox_file(oversized, 'horn.mp3')
        assert not ok
        assert '1 MB' in err

    def test_zero_byte_file_rejected(self):
        from services.boombox_service import validate_boombox_file
        ok, err = validate_boombox_file(b'', 'horn.mp3')
        assert not ok
        assert 'empty' in err.lower()


# ---------------------------------------------------------------------------
# Count helpers
# ---------------------------------------------------------------------------

class TestGetBoomboxCountAnyMode:
    """The count helper must read from the mode-appropriate mount.

    Counting against ``None`` (which is what callers would pass in
    present mode if they used the upload destination) silently returned
    0 in the wraps service — that bug let the user fill the drive past
    the limit. ``get_boombox_count_any_mode`` is the same defense.
    """

    def test_present_mode_reads_from_ro(self, fake_music_drive,
                                        monkeypatch):
        # Populate ONLY the RO side. A bug that read from RW would
        # observably return 0 instead of 5.
        _populate(fake_music_drive['ro_boombox'], 5)
        from services import boombox_service
        _set_mode(monkeypatch, 'present')

        assert boombox_service.get_boombox_count_any_mode() == 5

    def test_edit_mode_reads_from_rw(self, fake_music_drive, monkeypatch):
        # In edit mode the RO mount may not exist (gadget unbound).
        _populate(fake_music_drive['rw_boombox'], 3)
        from services import boombox_service
        _set_mode(monkeypatch, 'edit')

        assert boombox_service.get_boombox_count_any_mode() == 3

    def test_does_not_count_non_audio_files(self, fake_music_drive,
                                            monkeypatch):
        # Stray macOS .DS_Store / README / .txt files don't count.
        _populate(fake_music_drive['ro_boombox'], 2)
        (fake_music_drive['ro_boombox'] / '.DS_Store').write_bytes(b'')
        (fake_music_drive['ro_boombox'] / 'README.txt').write_text('hi')
        from services import boombox_service
        _set_mode(monkeypatch, 'present')

        assert boombox_service.get_boombox_count_any_mode() == 2

    def test_missing_folder_returns_zero(self, fake_music_drive,
                                         monkeypatch):
        # Fresh device — no /Boombox/ yet.
        from services import boombox_service
        # Remove the folder we created in the fixture for this test only.
        import shutil
        shutil.rmtree(fake_music_drive['ro_boombox'])
        shutil.rmtree(fake_music_drive['rw_boombox'])
        _set_mode(monkeypatch, 'present')

        assert boombox_service.get_boombox_count_any_mode() == 0


# ---------------------------------------------------------------------------
# Count-enforcement regressions — the wraps-style bypass
# ---------------------------------------------------------------------------

class TestUploadCountEnforcement:
    """The original wraps-service bug: in present mode the count check
    silently returned 0 so the 11th upload was accepted. We must not
    reproduce that here.
    """

    def _stub_quick_edit(self, monkeypatch):
        """Make ``quick_edit_part3`` execute its callback inline so we
        actually exercise the inner save logic in tests.
        """
        from services import boombox_service

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr(
            'services.partition_mount_service.quick_edit_part3',
            _fake_quick_edit)
        # Patch the bound name on the service too — boombox_service did
        # ``from services.partition_mount_service import quick_edit_part3``
        # at import time, so patching the source module doesn't reach
        # the alias and the real preflight check would run.
        monkeypatch.setattr(boombox_service, 'quick_edit_part3',
                            _fake_quick_edit)
        # Stub the rebind so we don't poke configfs. Same alias-patch
        # rule applies to ``safe_rebind_usb_gadget``.
        monkeypatch.setattr(
            'services.wrap_service.safe_rebind_usb_gadget',
            lambda: None)
        monkeypatch.setattr(
            boombox_service, 'safe_rebind_usb_gadget', lambda: None)
        # Stub samba close — it tries to talk to a real socket otherwise.
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)
        return boombox_service

    def test_sixth_upload_present_mode_rejected(self, fake_music_drive,
                                                monkeypatch):
        # Pre-populate 5 sounds on the RO side (count source of truth
        # in present mode).
        _populate(fake_music_drive['ro_boombox'], 5)
        bs = self._stub_quick_edit(monkeypatch)
        _set_mode(monkeypatch, 'present')

        upload = _FakeUploadFile(
            'sixth.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = bs.upload_boombox_file(upload, 'sixth.mp3')

        assert not ok
        assert 'maximum' in msg.lower() or '5' in msg

    def test_sixth_upload_edit_mode_rejected(self, fake_music_drive,
                                             monkeypatch):
        # Same defense in edit mode: 5 already on the RW mount.
        _populate(fake_music_drive['rw_boombox'], 5)
        bs = self._stub_quick_edit(monkeypatch)
        _set_mode(monkeypatch, 'edit')

        upload = _FakeUploadFile(
            'sixth.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = bs.upload_boombox_file(upload, 'sixth.mp3')

        assert not ok
        assert 'maximum' in msg.lower() or '5' in msg

    def test_fifth_upload_accepted_in_both_modes(self, fake_music_drive,
                                                 monkeypatch):
        # Only 4 present — the 5th should be accepted in both modes.
        bs = self._stub_quick_edit(monkeypatch)

        # Present mode: pre-populate 4 on RO. The new file lands on RW
        # (which is what quick_edit_part3 would mount).
        _populate(fake_music_drive['ro_boombox'], 4)
        _set_mode(monkeypatch, 'present')
        upload = _FakeUploadFile(
            'fifth.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = bs.upload_boombox_file(upload, 'fifth.mp3')
        assert ok, msg
        assert (fake_music_drive['rw_boombox'] / 'fifth.mp3').is_file()

        # Reset for edit-mode check: clear RW, populate 4 on RW.
        for f in fake_music_drive['rw_boombox'].iterdir():
            f.unlink()
        _populate(fake_music_drive['rw_boombox'], 4)
        _set_mode(monkeypatch, 'edit')
        upload = _FakeUploadFile(
            'fifth.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = bs.upload_boombox_file(upload, 'fifth.mp3')
        assert ok, msg


# ---------------------------------------------------------------------------
# Upload + delete cycle (edit mode, atomic on real tmp filesystem)
# ---------------------------------------------------------------------------

class TestUploadDeleteCycleEditMode:

    def test_upload_then_delete_round_trip(self, fake_music_drive,
                                           monkeypatch):
        from services import boombox_service
        _set_mode(monkeypatch, 'edit')
        # Stub the samba close call (needs a real socket).
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)

        # Upload
        wav = _build_minimal_wav_bytes(payload_size=128)
        upload = _FakeUploadFile('horn.wav', wav)
        ok, msg = boombox_service.upload_boombox_file(upload, 'horn.wav')
        assert ok, msg

        target = fake_music_drive['rw_boombox'] / 'horn.wav'
        assert target.is_file()
        assert target.read_bytes() == wav

        # No leftover .upload temp file in the boombox directory.
        leftovers = [p.name for p in fake_music_drive['rw_boombox'].iterdir()
                     if p.name.startswith('.')]
        assert not leftovers, f"Unexpected temp files: {leftovers}"

        # Delete
        ok, msg = boombox_service.delete_boombox_file('horn.wav')
        assert ok, msg
        assert not target.exists()

    def test_delete_missing_file_returns_error(self, fake_music_drive,
                                               monkeypatch):
        from services import boombox_service
        _set_mode(monkeypatch, 'edit')
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)

        ok, msg = boombox_service.delete_boombox_file('nonexistent.mp3')
        assert not ok
        assert 'not found' in msg.lower()

    def test_delete_rejects_path_traversal(self, fake_music_drive,
                                           monkeypatch):
        from services import boombox_service
        _set_mode(monkeypatch, 'edit')
        # _sanitize_name strips the path prefix; the basename ends up
        # being 'evil.mp3' — but it does not exist in /Boombox/, so the
        # delete returns "File not found" rather than reaching outside.
        ok, msg = boombox_service.delete_boombox_file('../../evil.mp3')
        assert not ok
        # The important guarantee: nothing outside /Boombox/ was touched.
        # (No file existed at the sanitized name, so we got "not found".)


# ---------------------------------------------------------------------------
# USB rebind on present-mode writes (PR #60 invariant for this service)
# ---------------------------------------------------------------------------

class TestRebindAfterPresentModeWrites:

    def test_present_upload_calls_safe_rebind(self, fake_music_drive,
                                              monkeypatch):
        from services import boombox_service

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr('services.partition_mount_service.quick_edit_part3', _fake_quick_edit)
        monkeypatch.setattr(boombox_service, 'quick_edit_part3', _fake_quick_edit)
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)
        rebind = MagicMock()
        # The service imported the function as a bound name, patch the
        # service's view of it.
        monkeypatch.setattr(
            boombox_service, 'safe_rebind_usb_gadget', rebind)
        _set_mode(monkeypatch, 'present')

        upload = _FakeUploadFile(
            'horn.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = boombox_service.upload_boombox_file(upload, 'horn.mp3')
        assert ok, msg
        rebind.assert_called_once()

    def test_edit_upload_does_not_call_safe_rebind(self, fake_music_drive,
                                                   monkeypatch):
        from services import boombox_service
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)
        rebind = MagicMock()
        monkeypatch.setattr(
            boombox_service, 'safe_rebind_usb_gadget', rebind)
        _set_mode(monkeypatch, 'edit')

        upload = _FakeUploadFile(
            'horn.mp3', _build_minimal_mp3_bytes(prefix='id3'))
        ok, msg = boombox_service.upload_boombox_file(upload, 'horn.mp3')
        assert ok, msg
        rebind.assert_not_called()

    def test_present_delete_calls_safe_rebind(self, fake_music_drive,
                                              monkeypatch):
        from services import boombox_service

        # Pre-create the file on the RW mount (where the delete writes
        # via the inline-stubbed quick_edit_part3).
        target = fake_music_drive['rw_boombox'] / 'horn.mp3'
        target.write_bytes(_build_minimal_mp3_bytes(prefix='id3'))

        def _fake_quick_edit(fn, timeout=None):
            return fn()
        monkeypatch.setattr('services.partition_mount_service.quick_edit_part3', _fake_quick_edit)
        monkeypatch.setattr(boombox_service, 'quick_edit_part3', _fake_quick_edit)
        monkeypatch.setattr(
            'services.samba_service.close_samba_share',
            lambda part: None)
        rebind = MagicMock()
        monkeypatch.setattr(
            boombox_service, 'safe_rebind_usb_gadget', rebind)
        _set_mode(monkeypatch, 'present')

        ok, msg = boombox_service.delete_boombox_file('horn.mp3')
        assert ok, msg
        rebind.assert_called_once()
