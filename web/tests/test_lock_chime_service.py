"""Tests for ``teslausb_web.services.lock_chime_service``."""

from __future__ import annotations

import shutil
import subprocess
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Final
from unittest.mock import patch

import pytest
from teslausb_web.services.lock_chime_service import (
    LockChimeAudioError,
    LockChimeFileError,
    delete_chime_file,
    list_chime_files,
    normalize_audio,
    reencode_wav_for_tesla,
    rename_chime_file,
    replace_lock_chime,
    save_pretrimmed_wav,
    set_active_chime,
    upload_chime_file,
    validate_tesla_wav,
)

_SAMPLE_FRAMES: Final[int] = 100
_MAX_SIZE_BYTES: Final[int] = 1_048_576


class _FakeFileStorage:
    def __init__(self, filename: str | None, payload: bytes) -> None:
        self.filename = filename
        self._payload = payload

    def save(self, dst: str | Path) -> None:
        Path(dst).write_bytes(self._payload)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._payload
        return self._payload[:size]


class _FakeWaveFile:
    def __init__(self, params: object) -> None:
        self._params = params

    def getparams(self) -> object:
        return self._params

    def close(self) -> None:
        return None


def _wav_bytes(*, channels: int = 1, sampwidth: int = 2, framerate: int = 44_100) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"\x00" * (_SAMPLE_FRAMES * channels * sampwidth))
    return buffer.getvalue()


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _ffmpeg_completed(*, returncode: int = 0, stderr: bytes | str = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


@pytest.fixture
def chimes_dir(tmp_path: Path) -> Path:
    path = tmp_path / "lightshow" / "Chimes"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def active_path(chimes_dir: Path) -> Path:
    return chimes_dir.parent / "LockChime.wav"


def test_validate_tesla_wav_accepts_valid_mono_pcm(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "mono.wav", _wav_bytes(channels=1)))
    assert result.ok is True
    assert result.message == "Valid"


def test_validate_tesla_wav_accepts_valid_stereo_pcm(tmp_path: Path) -> None:
    result = validate_tesla_wav(
        _write(tmp_path / "stereo.wav", _wav_bytes(channels=2, framerate=48_000))
    )
    assert result.ok is True


def test_validate_tesla_wav_rejects_wrong_bit_depth(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "8bit.wav", _wav_bytes(sampwidth=1)))
    assert result.ok is False
    assert "16-bit PCM" in result.message


def test_validate_tesla_wav_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "22k.wav", _wav_bytes(framerate=22_050)))
    assert result.ok is False
    assert "44.1 or 48 kHz" in result.message


def test_validate_tesla_wav_rejects_wrong_compression(tmp_path: Path) -> None:
    wav_path = _write(tmp_path / "compressed.wav", _wav_bytes())
    fake_params = SimpleNamespace(
        sampwidth=2,
        framerate=44_100,
        comptype="ULAW",
        nchannels=1,
    )
    with patch(
        "teslausb_web.services.lock_chime_service.wave.open",
        return_value=_FakeWaveFile(fake_params),
    ):
        result = validate_tesla_wav(wav_path)
    assert result.ok is False
    assert "uncompressed PCM" in result.message


def test_validate_tesla_wav_rejects_zero_byte_file(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "empty.wav", b""))
    assert result.ok is False
    assert result.message == "File is empty."


def test_validate_tesla_wav_rejects_oversize_file(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "big.wav", b"0" * (_MAX_SIZE_BYTES + 1)))
    assert result.ok is False
    assert "under 1 MB" in result.message


def test_validate_tesla_wav_rejects_invalid_file(tmp_path: Path) -> None:
    result = validate_tesla_wav(_write(tmp_path / "bad.wav", b"not-a-wav"))
    assert result.ok is False
    assert result.message == "Not a valid WAV file."


def test_reencode_wav_for_tesla_succeeds_on_first_strategy(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes(framerate=22_050))
    output = tmp_path / "output.wav"

    def _run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[-1]).write_bytes(_wav_bytes())
        return _ffmpeg_completed()

    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch("teslausb_web.services.lock_chime_service.subprocess.run", side_effect=_run),
    ):
        result = reencode_wav_for_tesla(source, output)
    assert result.ok is True
    assert result.attempt == 1
    assert result.strategy is not None
    assert output.exists()


def test_reencode_wav_for_tesla_handles_missing_ffmpeg(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    with patch(
        "teslausb_web.services.lock_chime_service._ffmpeg_executable",
        side_effect=FileNotFoundError,
    ):
        result = reencode_wav_for_tesla(source, tmp_path / "out.wav")
    assert result.ok is False
    assert result.message == "FFmpeg is not installed on the system"


def test_reencode_wav_for_tesla_retries_after_timeout(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    output = tmp_path / "out.wav"
    calls = 0

    def _run(command: list[str], **_: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(cmd=command, timeout=30)
        Path(command[-1]).write_bytes(_wav_bytes())
        return _ffmpeg_completed()

    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch("teslausb_web.services.lock_chime_service.subprocess.run", side_effect=_run),
    ):
        result = reencode_wav_for_tesla(source, output)
    assert result.ok is True
    assert result.attempt == 2


def test_reencode_wav_for_tesla_reports_all_strategies_failed(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch(
            "teslausb_web.services.lock_chime_service.subprocess.run",
            return_value=_ffmpeg_completed(returncode=1, stderr=b"Error: bad audio"),
        ),
    ):
        result = reencode_wav_for_tesla(source, tmp_path / "out.wav")
    assert result.ok is False
    assert "All re-encoding attempts failed" in result.message


def test_reencode_wav_for_tesla_uses_trimmed_strategy_when_first_output_is_too_large(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    output = tmp_path / "out.wav"
    calls = 0

    def _run(command: list[str], **_: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(command[-1]).write_bytes(b"0" * (_MAX_SIZE_BYTES + 1))
        else:
            Path(command[-1]).write_bytes(_wav_bytes())
        return _ffmpeg_completed()

    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch("teslausb_web.services.lock_chime_service.subprocess.run", side_effect=_run),
    ):
        result = reencode_wav_for_tesla(source, output)
    assert result.ok is True
    assert result.attempt == 2
    assert result.strategy is not None
    assert "Trimmed" in result.strategy


def test_reencode_wav_for_tesla_reports_unexpected_exception(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch(
            "teslausb_web.services.lock_chime_service.subprocess.run",
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = reencode_wav_for_tesla(source, tmp_path / "out.wav")
    assert result.ok is False
    assert "Last error: Re-encoding error: boom" in result.message


def test_normalize_audio_returns_temp_file_on_success(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    loudnorm_json = (
        '{"input_i":"-20.0","input_lra":"3.0","input_tp":"-1.0",'
        '"input_thresh":"-30.0","target_offset":"0.5"}'
    )

    def _run(command: list[str], **_: object) -> SimpleNamespace:
        if command[-1] == "-":
            return _ffmpeg_completed(stderr=f"stats\n{loudnorm_json}")
        Path(command[-1]).write_bytes(_wav_bytes())
        return _ffmpeg_completed(stderr="")

    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch("teslausb_web.services.lock_chime_service.subprocess.run", side_effect=_run),
    ):
        normalized = normalize_audio(source)
    assert normalized.exists()
    normalized.unlink()


def test_normalize_audio_raises_when_ffmpeg_missing(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    with (
        patch(
            "teslausb_web.services.lock_chime_service._ffmpeg_executable",
            side_effect=FileNotFoundError,
        ),
        pytest.raises(LockChimeAudioError, match="FFmpeg is not installed"),
    ):
        normalize_audio(source)


def test_normalize_audio_raises_on_invalid_json(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch(
            "teslausb_web.services.lock_chime_service.subprocess.run",
            return_value=_ffmpeg_completed(stderr="not json"),
        ),
        pytest.raises(LockChimeAudioError, match="Could not find loudness analysis"),
    ):
        normalize_audio(source)


def test_normalize_audio_cleans_temp_file_on_second_pass_timeout(tmp_path: Path) -> None:
    source = _write(tmp_path / "input.wav", _wav_bytes())
    loudnorm_json = (
        '{"input_i":"-20.0","input_lra":"3.0","input_tp":"-1.0",'
        '"input_thresh":"-30.0","target_offset":"0.5"}'
    )

    def _run(command: list[str], **_: object) -> SimpleNamespace:
        if command[-1] == "-":
            return _ffmpeg_completed(stderr=loudnorm_json)
        raise subprocess.TimeoutExpired(cmd=command, timeout=30)

    before = set(tmp_path.parent.glob("**/*.wav"))
    with (
        patch("teslausb_web.services.lock_chime_service._ffmpeg_executable", return_value="ffmpeg"),
        patch("teslausb_web.services.lock_chime_service.subprocess.run", side_effect=_run),
        pytest.raises(LockChimeAudioError, match="timed out"),
    ):
        normalize_audio(source)
    after = set(tmp_path.parent.glob("**/*.wav"))
    assert source in after
    assert after == before | {source}


def test_replace_lock_chime_publishes_atomically(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.wav", _wav_bytes(channels=2))
    target = _write(tmp_path / "LockChime.wav", _wav_bytes(channels=1))
    result = replace_lock_chime(source, target)
    assert result.ok is True
    assert result.size_bytes == target.stat().st_size
    assert target.exists()
    assert not target.with_suffix(".wav.tmp").exists()
    assert result.md5 is not None


def test_replace_lock_chime_rolls_back_when_replace_fails(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.wav", _wav_bytes(channels=2))
    target = _write(tmp_path / "LockChime.wav", _wav_bytes(channels=1))
    original = target.read_bytes()
    with (
        patch(
            "teslausb_web.services.lock_chime_service.Path.replace",
            side_effect=OSError("nope"),
        ),
        pytest.raises(LockChimeFileError),
    ):
        replace_lock_chime(source, target)
    assert target.read_bytes() == original
    assert not target.with_suffix(".wav.tmp").exists()


def test_replace_lock_chime_rejects_empty_source(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.wav", b"")
    with pytest.raises(ValueError, match="empty"):
        replace_lock_chime(source, tmp_path / "LockChime.wav")


@pytest.mark.parametrize("name", ["../evil.wav", "a/b.wav", "a\\b.wav", "bad\x00.wav"])
def test_set_active_chime_rejects_path_traversal(
    name: str,
    chimes_dir: Path,
    active_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Invalid chime filename"):
        set_active_chime(name, chimes_dir, active_path)


@pytest.mark.parametrize("name", ["../evil.wav", "a/b.wav", "a\\b.wav", "bad\x00.wav"])
def test_delete_chime_file_rejects_path_traversal(
    name: str,
    chimes_dir: Path,
    active_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Invalid chime filename"):
        delete_chime_file(name, chimes_dir, active_path)


def test_set_active_chime_rejects_missing_source(chimes_dir: Path, active_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        set_active_chime("missing.wav", chimes_dir, active_path)


def test_set_active_chime_rejects_non_regular_file(chimes_dir: Path, active_path: Path) -> None:
    directory = chimes_dir / "not-a-file.wav"
    directory.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        set_active_chime(directory.name, chimes_dir, active_path)


def test_set_active_chime_rejects_invalid_wav(chimes_dir: Path, active_path: Path) -> None:
    bad = _write(chimes_dir / "bad.wav", b"not-a-wav")
    with pytest.raises(ValueError, match="Invalid chime file"):
        set_active_chime(bad.name, chimes_dir, active_path)


def test_set_active_chime_replaces_active_file(chimes_dir: Path, active_path: Path) -> None:
    source = _write(chimes_dir / "good.wav", _wav_bytes())
    result = set_active_chime(source.name, chimes_dir, active_path)
    assert result.ok is True
    assert active_path.read_bytes() == source.read_bytes()


def test_upload_chime_file_saves_sanitized_wav(chimes_dir: Path) -> None:
    result = upload_chime_file(
        _FakeFileStorage("../My Chime.wav", _wav_bytes()),
        chimes_dir,
        _MAX_SIZE_BYTES,
    )
    assert result.ok is True
    assert result.saved_path is not None
    assert result.saved_path.name == "My_Chime.wav"


def test_upload_chime_file_rejects_non_wav(chimes_dir: Path) -> None:
    result = upload_chime_file(_FakeFileStorage("track.mp3", b"mp3"), chimes_dir, _MAX_SIZE_BYTES)
    assert result.ok is False
    assert result.message == "Filename must end with .wav"


def test_upload_chime_file_rejects_lock_chime_filename(chimes_dir: Path) -> None:
    result = upload_chime_file(
        _FakeFileStorage("LockChime.wav", _wav_bytes()),
        chimes_dir,
        _MAX_SIZE_BYTES,
    )
    assert result.ok is False
    assert "Please rename your file" in result.message


def test_upload_chime_file_rejects_oversize_file(chimes_dir: Path) -> None:
    payload = b"0" * (_MAX_SIZE_BYTES + 1)
    result = upload_chime_file(_FakeFileStorage("big.wav", payload), chimes_dir, _MAX_SIZE_BYTES)
    assert result.ok is False
    assert result.size_bytes == len(payload)


def test_save_pretrimmed_wav_saves_valid_blob(tmp_path: Path) -> None:
    output = tmp_path / "trimmed.wav"
    result = save_pretrimmed_wav(_wav_bytes(), output)
    assert result.ok is True
    assert output.exists()


def test_save_pretrimmed_wav_rejects_invalid_header(tmp_path: Path) -> None:
    result = save_pretrimmed_wav(b"not-a-wav", tmp_path / "trimmed.wav")
    assert result.ok is False
    assert result.message == "Not a valid WAV file."


def test_delete_chime_file_reports_was_active(chimes_dir: Path) -> None:
    target = _write(chimes_dir / "active.wav", _wav_bytes())
    result = delete_chime_file(target.name, chimes_dir, target)
    assert result.ok is True
    assert result.was_active is True
    assert not target.exists()


def test_delete_chime_file_reports_not_active(chimes_dir: Path, active_path: Path) -> None:
    target = _write(chimes_dir / "other.wav", _wav_bytes())
    _write(active_path, _wav_bytes(channels=2))
    result = delete_chime_file(target.name, chimes_dir, active_path)
    assert result.ok is True
    assert result.was_active is False


def test_list_chime_files_returns_empty_tuple_for_missing_directory(tmp_path: Path) -> None:
    assert list_chime_files(tmp_path / "missing") == ()


def test_list_chime_files_skips_non_wav_files(chimes_dir: Path) -> None:
    _write(chimes_dir / "note.txt", b"ignore")
    assert list_chime_files(chimes_dir) == ()


def test_list_chime_files_sorts_and_marks_active(chimes_dir: Path, active_path: Path) -> None:
    alpha = _write(chimes_dir / "alpha.wav", _wav_bytes(channels=1))
    beta = _write(chimes_dir / "beta.wav", _wav_bytes(channels=2))
    _write(chimes_dir / "zeta.wav", _wav_bytes(framerate=48_000))
    _write(active_path, beta.read_bytes())
    infos = list_chime_files(chimes_dir)
    assert [info.name for info in infos] == ["alpha.wav", "beta.wav", "zeta.wav"]
    assert infos[1].is_active is True
    assert infos[0].is_active is False
    assert infos[0].size_bytes == alpha.stat().st_size
    assert infos[0].mtime_iso.endswith("+00:00")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_reencode_wav_for_tesla_real_ffmpeg_smoke(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.wav", _wav_bytes(channels=2, framerate=48_000))
    output = tmp_path / "real.wav"
    result = reencode_wav_for_tesla(source, output)
    assert result.ok is True
    validation = validate_tesla_wav(output)
    assert validation.ok is True


def test_rename_chime_file_moves_file_in_place(chimes_dir: Path) -> None:
    src = _write(chimes_dir / "old.wav", _wav_bytes())
    payload = src.read_bytes()
    result = rename_chime_file("old.wav", "new.wav", chimes_dir)
    assert result == chimes_dir / "new.wav"
    assert result.read_bytes() == payload
    assert not src.exists()


def test_rename_chime_file_missing_source_raises(chimes_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        rename_chime_file("nope.wav", "other.wav", chimes_dir)


def test_rename_chime_file_destination_exists_raises(chimes_dir: Path) -> None:
    _write(chimes_dir / "a.wav", _wav_bytes())
    _write(chimes_dir / "b.wav", _wav_bytes())
    with pytest.raises(FileExistsError):
        rename_chime_file("a.wav", "b.wav", chimes_dir)


def test_rename_chime_file_rejects_path_traversal(chimes_dir: Path) -> None:
    _write(chimes_dir / "a.wav", _wav_bytes())
    with pytest.raises(ValueError, match="Invalid chime filename"):
        rename_chime_file("a.wav", "../evil.wav", chimes_dir)


def test_rename_chime_file_rejects_non_wav_destination(chimes_dir: Path) -> None:
    _write(chimes_dir / "a.wav", _wav_bytes())
    with pytest.raises(ValueError, match=r"must end with \.wav"):
        rename_chime_file("a.wav", "b.mp3", chimes_dir)


def test_rename_chime_file_wraps_os_error_as_lock_chime_file_error(
    chimes_dir: Path,
) -> None:
    _write(chimes_dir / "a.wav", _wav_bytes())
    with (
        patch(
            "teslausb_web.services.lock_chime_service.Path.rename",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(LockChimeFileError, match="Failed to rename chime"),
    ):
        rename_chime_file("a.wav", "b.wav", chimes_dir)
