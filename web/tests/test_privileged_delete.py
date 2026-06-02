"""Unit tests for ``teslausb_web.services.privileged_delete``.

Mocks ``subprocess.run`` so they exercise argv construction + the
success / nonzero / timeout / spawn-failure translation without touching
the filesystem or sudo. The real shell helper is covered end-to-end by
``test_teslausb_delete_clip_integration.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from teslausb_web.services.privileged_delete import (
    PrivilegedClipDeleter,
    PrivilegedDeleteError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_SCRIPT = Path("/usr/local/bin/teslausb_delete_clip.sh")
_TARGET = Path("/srv/teslausb/TeslaCam/SentryClips/2026-05-31_14-56-34")


def _ok(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_builds_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Sequence[str]] = {}

    def _fake_run(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = list(cmd)
        return _ok()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    deleter = PrivilegedClipDeleter(script=_SCRIPT, sudo_prefix=("sudo", "-n"))
    deleter.delete(_TARGET)

    assert captured["cmd"] == ["sudo", "-n", str(_SCRIPT), str(_TARGET)]


def test_empty_sudo_prefix_runs_script_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Sequence[str]] = {}

    def _fake_run(cmd: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = list(cmd)
        return _ok()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    PrivilegedClipDeleter(script=_SCRIPT).delete(_TARGET)

    assert captured["cmd"] == [str(_SCRIPT), str(_TARGET)]


def test_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _ok(returncode=5, stderr="not a clip"))
    deleter = PrivilegedClipDeleter(script=_SCRIPT, sudo_prefix=("sudo", "-n"))
    with pytest.raises(PrivilegedDeleteError, match="exited 5"):
        deleter.delete(_TARGET)


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="x", timeout=30.0)

    monkeypatch.setattr(subprocess, "run", _boom)
    deleter = PrivilegedClipDeleter(script=_SCRIPT, sudo_prefix=("sudo", "-n"))
    with pytest.raises(PrivilegedDeleteError, match="timed out"):
        deleter.delete(_TARGET)


def test_spawn_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise OSError("sudo: command not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    deleter = PrivilegedClipDeleter(script=_SCRIPT, sudo_prefix=("sudo", "-n"))
    with pytest.raises(PrivilegedDeleteError, match="failed to start"):
        deleter.delete(_TARGET)


def test_success_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _ok())
    deleter = PrivilegedClipDeleter(script=_SCRIPT, sudo_prefix=("sudo", "-n"))
    assert deleter.delete(_TARGET) is None


def test_privileged_delete_error_is_runtime() -> None:
    assert issubclass(PrivilegedDeleteError, RuntimeError)
