"""Tests for SambaPasswordService — smbpasswd / pdbedit wrapper."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from teslausb_web.config import PathsSection, SambaSection, WebConfig, WebSection
from teslausb_web.services.samba_password_service import (
    SambaPasswordCommandError,
    SambaPasswordError,
    SambaPasswordNotInstalledError,
    SambaPasswordService,
    SambaPasswordServiceConfig,
    SambaPasswordValidationError,
    make_samba_password_service,
)


def _config(**overrides: Any) -> SambaPasswordServiceConfig:
    base: dict[str, Any] = {
        "username": "pi",
        "binary_smbpasswd": "smbpasswd",
        "binary_pdbedit": "pdbedit",
        "sudo_prefix": ("sudo", "-n"),
    }
    base.update(overrides)
    return SambaPasswordServiceConfig(**base)


def _service(
    *,
    which: Any = None,
    run_command: Any = None,
    config: SambaPasswordServiceConfig | None = None,
) -> SambaPasswordService:
    return SambaPasswordService(
        config or _config(),
        which=which or (lambda name: f"/usr/bin/{name}"),
        run_command=run_command or (lambda *_a, **_k: _completed(0)),
    )


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestConfigValidation:
    def test_blank_username_rejected(self) -> None:
        with pytest.raises(SambaPasswordError):
            _config(username="")

    def test_blank_binary_rejected(self) -> None:
        with pytest.raises(SambaPasswordError):
            _config(binary_smbpasswd="")
        with pytest.raises(SambaPasswordError):
            _config(binary_pdbedit="")

    def test_blank_sudo_token_rejected(self) -> None:
        with pytest.raises(SambaPasswordError):
            _config(sudo_prefix=("sudo", ""))

    def test_nonpositive_timeout_rejected(self) -> None:
        with pytest.raises(SambaPasswordError):
            _config(command_timeout_seconds=0)


class TestSetPasswordValidation:
    @pytest.mark.parametrize("pw", ["", "short", "1234567"])
    def test_too_short(self, pw: str) -> None:
        with pytest.raises(SambaPasswordValidationError):
            _service().set_password(pw)

    def test_too_long(self) -> None:
        with pytest.raises(SambaPasswordValidationError):
            _service().set_password("a" * 200)

    def test_non_ascii_rejected(self) -> None:
        with pytest.raises(SambaPasswordValidationError):
            _service().set_password("password\u00e9123")

    def test_control_char_rejected(self) -> None:
        with pytest.raises(SambaPasswordValidationError):
            _service().set_password("pass\nword1")


class TestSetPasswordHappyPath:
    def test_invokes_smbpasswd_with_sudo_and_stdin(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(0)

        svc = _service(run_command=fake_run)
        svc.set_password("CorrectHorseBattery9")

        # The binary path is normalised through Path(), so we compare via
        # PurePath to stay portable across Windows / POSIX dev boxes.
        from pathlib import PurePath

        cmd = captured["cmd"]
        assert cmd[:2] == ["sudo", "-n"]
        assert PurePath(cmd[2]) == PurePath("/usr/bin/smbpasswd")
        assert cmd[3:] == ["-s", "-a", "pi"]
        assert captured["kwargs"]["input"] == "CorrectHorseBattery9\nCorrectHorseBattery9\n"
        assert captured["kwargs"]["text"] is True
        assert captured["kwargs"]["capture_output"] is True
        assert captured["kwargs"]["check"] is False
        assert captured["kwargs"]["timeout"] == pytest.approx(30.0)

    def test_works_without_sudo_prefix(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return _completed(0)

        svc = _service(
            run_command=fake_run,
            config=_config(sudo_prefix=()),
        )
        svc.set_password("validpass123")
        from pathlib import PurePath

        assert PurePath(captured["cmd"][0]) == PurePath("/usr/bin/smbpasswd")


class TestSetPasswordErrors:
    def test_missing_binary_raises(self) -> None:
        svc = _service(which=lambda _name: None)
        with pytest.raises(SambaPasswordNotInstalledError):
            svc.set_password("validpass123")

    def test_nonzero_exit_raises_with_stderr(self) -> None:
        svc = _service(
            run_command=lambda *_a, **_k: _completed(1, stderr="Failed: oh no"),
        )
        with pytest.raises(SambaPasswordCommandError) as ei:
            svc.set_password("validpass123")
        assert "Failed: oh no" in str(ei.value)

    def test_timeout_raises(self) -> None:
        def fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="smbpasswd", timeout=30.0)

        svc = _service(run_command=fake_run)
        with pytest.raises(SambaPasswordCommandError) as ei:
            svc.set_password("validpass123")
        assert "timed out" in str(ei.value)

    def test_os_error_wrapped(self) -> None:
        def fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
            raise OSError("permission denied")

        svc = _service(run_command=fake_run)
        with pytest.raises(SambaPasswordCommandError):
            svc.set_password("validpass123")

    def test_plaintext_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        secret = "S3cretPass!42"
        with caplog.at_level("DEBUG"):
            svc = _service()
            svc.set_password(secret)
        for record in caplog.records:
            assert secret not in record.getMessage()


class TestUserExists:
    def test_returns_true_when_user_listed(self) -> None:
        out = "pi:1000:Pi User\nadmin:0:root\n"
        svc = _service(run_command=lambda *_a, **_k: _completed(0, stdout=out))
        assert svc.user_exists() is True

    def test_returns_false_when_user_missing(self) -> None:
        out = "admin:0:root\n"
        svc = _service(run_command=lambda *_a, **_k: _completed(0, stdout=out))
        assert svc.user_exists() is False

    def test_returns_false_on_nonzero_exit(self) -> None:
        svc = _service(run_command=lambda *_a, **_k: _completed(1, stderr="boom"))
        assert svc.user_exists() is False

    def test_returns_false_when_binary_missing(self) -> None:
        svc = _service(which=lambda _name: None)
        assert svc.user_exists() is False

    def test_returns_false_on_timeout(self) -> None:
        def fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="pdbedit", timeout=30.0)

        svc = _service(run_command=fake_run)
        assert svc.user_exists() is False

    def test_invokes_pdbedit_with_sudo(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return _completed(0, stdout="pi:1000:Pi\n")

        svc = _service(run_command=fake_run)
        svc.user_exists()
        from pathlib import PurePath

        cmd = captured["cmd"]
        assert cmd[:2] == ["sudo", "-n"]
        assert PurePath(cmd[2]) == PurePath("/usr/bin/pdbedit")
        assert cmd[3:] == ["-L"]


class TestFactory:
    def test_builds_from_web_config(self, tmp_path: Any) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            samba=SambaSection(
                password_username="bob",
                binary_smbpasswd="/opt/bin/smbpasswd",
                binary_pdbedit="/opt/bin/pdbedit",
                sudo_prefix=("doas",),
            ),
        )
        svc = make_samba_password_service(cfg)
        assert svc.config.username == "bob"
        assert svc.config.binary_smbpasswd == "/opt/bin/smbpasswd"
        assert svc.config.binary_pdbedit == "/opt/bin/pdbedit"
        assert svc.config.sudo_prefix == ("doas",)
