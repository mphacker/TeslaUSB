from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
from teslausb_web.config import PathsSection, SambaSection, SambaShareConfig, WebConfig, WebSection
from teslausb_web.services.samba_service import (
    SambaCommandError,
    SambaConfigError,
    SambaNotInstalledError,
    SambaService,
    SambaServiceConfig,
    SambaShare,
    make_samba_service,
)


class RunCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


class PopenCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> FakeProcess: ...


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 4321,
        poll_result: int | None = None,
        wait_timeout: bool = False,
    ) -> None:
        self.pid = pid
        self._poll_result = poll_result
        self._wait_timeout = wait_timeout
        self.kill_calls = 0
        self.terminate_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self._poll_result

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._wait_timeout:
            raise subprocess.TimeoutExpired(
                cmd="smbd", timeout=timeout if timeout is not None else 0.0
            )
        self._poll_result = 0
        return 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._poll_result = -9

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._wait_timeout:
            self._poll_result = 0


@pytest.fixture
def share_root(tmp_path: Path) -> Path:
    path = tmp_path / "backing"
    path.mkdir()
    return path


@pytest.fixture
def samba_share(share_root: Path) -> SambaShare:
    return SambaShare(name="TeslaCam", path=share_root / "TeslaCam", comment="TeslaUSB")


@pytest.fixture
def service_config(tmp_path: Path, samba_share: SambaShare, share_root: Path) -> SambaServiceConfig:
    return SambaServiceConfig(
        config_path=tmp_path / "etc" / "smb.conf",
        shares=(samba_share,),
        allowed_roots=(share_root, tmp_path / "state"),
    )


def _service(
    config: SambaServiceConfig,
    *,
    which: Callable[[str], str | None],
    run_command: object | None = None,
    popen_command: object | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SambaService:
    return SambaService(
        config,
        which=which,
        run_command=(None if run_command is None else cast("RunCommand", run_command)),
        popen_command=(None if popen_command is None else cast("PopenCommand", popen_command)),
        monotonic=monotonic,
    )


def _completed(
    returncode: int, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestSambaServiceRender:
    def test_render_config_writes_file(self, service_config: SambaServiceConfig) -> None:
        service = _service(service_config, which=lambda _name: None)
        result = service.render_config()
        assert result.config_path.read_text(encoding="utf-8") == result.rendered_text
        assert result.rendered_text.endswith("\n")

    def test_render_config_includes_global_block(self, service_config: SambaServiceConfig) -> None:
        service = _service(service_config, which=lambda _name: None)
        rendered = service.render_config().rendered_text
        assert "[global]" in rendered
        assert "server role = standalone server" in rendered

    def test_render_config_includes_share_block(self, service_config: SambaServiceConfig) -> None:
        service = _service(service_config, which=lambda _name: None)
        rendered = service.render_config().rendered_text
        assert "[TeslaCam]" in rendered
        assert "comment = TeslaUSB" in rendered
        assert "read only = no" in rendered

    def test_render_config_renders_guest_and_read_only_flags(
        self, tmp_path: Path, share_root: Path
    ) -> None:
        share = SambaShare(
            name="Media",
            path=share_root / "Media",
            comment="Media share",
            read_only=True,
            guest_ok=True,
            browseable=False,
        )
        service = _service(
            SambaServiceConfig(
                config_path=tmp_path / "smb.conf",
                shares=(share,),
                allowed_roots=(share_root,),
            ),
            which=lambda _name: None,
        )
        rendered = service.render_config().rendered_text
        assert "guest ok = yes" in rendered
        assert "read only = yes" in rendered
        assert "browseable = no" in rendered

    def test_render_config_creates_share_directory(
        self, service_config: SambaServiceConfig, share_root: Path
    ) -> None:
        service = _service(service_config, which=lambda _name: None)
        service.render_config()
        assert (share_root / "TeslaCam").is_dir()

    def test_render_config_creates_parent_config_directory(
        self, service_config: SambaServiceConfig
    ) -> None:
        service = _service(service_config, which=lambda _name: None)
        service.render_config()
        assert service_config.config_path.parent.is_dir()

    def test_render_config_rejects_share_outside_allowed_roots(
        self, tmp_path: Path, share_root: Path
    ) -> None:
        service = _service(
            SambaServiceConfig(
                config_path=tmp_path / "smb.conf",
                shares=(SambaShare(name="Bad", path=tmp_path / ".." / "escape", comment="nope"),),
                allowed_roots=(share_root,),
            ),
            which=lambda _name: None,
        )
        with pytest.raises(SambaConfigError, match="outside allowed roots"):
            service.render_config()

    def test_render_config_rejects_duplicate_share_names(
        self, tmp_path: Path, share_root: Path
    ) -> None:
        share_a = SambaShare(name="TeslaCam", path=share_root / "a", comment="a")
        share_b = SambaShare(name="TeslaCam", path=share_root / "b", comment="b")
        with pytest.raises(SambaConfigError, match="duplicate share name"):
            SambaServiceConfig(
                config_path=tmp_path / "smb.conf",
                shares=(share_a, share_b),
                allowed_roots=(share_root,),
            )

    def test_config_rejects_relative_config_path(
        self, share_root: Path, samba_share: SambaShare
    ) -> None:
        with pytest.raises(SambaConfigError, match="config_path"):
            SambaServiceConfig(
                config_path=Path("relative.conf"),
                shares=(samba_share,),
                allowed_roots=(share_root,),
            )

    def test_factory_uses_configured_share_overrides(self, tmp_path: Path) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            samba=SambaSection(
                config_path=tmp_path / "etc" / "smb.conf",
                shares=(
                    SambaShareConfig(
                        name="Media",
                        path=tmp_path / "backing" / "Media",
                        read_only=True,
                        guest_ok=True,
                    ),
                ),
                binary_smbd="/custom/smbd",
                binary_smbcontrol="/custom/smbcontrol",
                binary_systemctl="/custom/systemctl",
            ),
        )
        service = make_samba_service(cfg)
        assert service.config.config_path == tmp_path / "etc" / "smb.conf"
        assert service.config.binary_smbd == "/custom/smbd"
        assert service.config.shares[0].name == "Media"
        assert service.config.shares[0].guest_ok is True

    def test_factory_falls_back_to_default_share(self, tmp_path: Path) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
        )
        service = make_samba_service(cfg)
        assert service.config.shares[0].name == "TeslaCam"
        assert service.config.shares[0].path == tmp_path / "backing" / "TeslaCam"


class TestSambaServiceLifecycle:
    def test_start_prefers_systemctl_when_available(
        self, service_config: SambaServiceConfig
    ) -> None:
        commands: list[list[str]] = []

        def run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return _completed(0)

        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=run_command,
        )
        status = service.start()
        assert status.running is True
        assert Path(commands[0][0]).name == "systemctl"
        assert commands[0][1:] == ["start", "smbd"]

    def test_start_uses_popen_when_systemctl_missing(
        self, service_config: SambaServiceConfig
    ) -> None:
        started: list[list[str]] = []
        process = FakeProcess()

        def popen_command(command: list[str], **_kwargs: object) -> FakeProcess:
            started.append(command)
            return process

        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=popen_command,
        )
        status = service.start()
        assert status.running is True
        assert status.pid_or_none == process.pid
        assert Path(started[0][0]).name == "smbd"
        assert started[0][1] == "-FS"

    def test_start_missing_smbd_raises_not_installed(
        self, service_config: SambaServiceConfig
    ) -> None:
        service = _service(service_config, which=lambda _name: None)
        with pytest.raises(SambaNotInstalledError, match="smbd"):
            service.start()

    def test_start_returns_existing_status_when_already_running(
        self, service_config: SambaServiceConfig
    ) -> None:
        process = FakeProcess()
        starts = 0

        def popen_command(command: list[str], **_kwargs: object) -> FakeProcess:
            nonlocal starts
            starts += 1
            return process

        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=popen_command,
        )
        first = service.start()
        second = service.start()
        assert first.running is True
        assert second.pid_or_none == process.pid
        assert starts == 1

    def test_start_systemctl_failure_raises_command_error(
        self, service_config: SambaServiceConfig
    ) -> None:
        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=lambda _command, **_kwargs: _completed(1, stderr="boom"),
        )
        with pytest.raises(SambaCommandError, match="boom"):
            service.start()

    def test_start_immediate_popen_exit_raises_command_error(
        self, service_config: SambaServiceConfig
    ) -> None:
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: FakeProcess(poll_result=1),
        )
        with pytest.raises(SambaCommandError, match="immediately"):
            service.start()

    def test_stop_systemctl_runs_stop(self, service_config: SambaServiceConfig) -> None:
        commands: list[list[str]] = []
        responses = [_completed(0), _completed(0)]

        def run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return responses.pop(0)

        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=run_command,
        )
        service.start()
        status = service.stop()
        assert status.running is False
        assert Path(commands[1][0]).name == "systemctl"
        assert commands[1][1:] == ["stop", "smbd"]

    def test_stop_systemctl_failure_raises_command_error(
        self, service_config: SambaServiceConfig
    ) -> None:
        responses = [_completed(0), _completed(1, stderr="nope")]
        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=lambda _command, **_kwargs: responses.pop(0),
        )
        service.start()
        with pytest.raises(SambaCommandError, match="nope"):
            service.stop()

    def test_stop_process_uses_smbcontrol_when_available(
        self, service_config: SambaServiceConfig
    ) -> None:
        process = FakeProcess()
        commands: list[list[str]] = []

        def run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return _completed(0)

        def which(name: str) -> str | None:
            return {"smbd": "/usr/sbin/smbd", "smbcontrol": "/usr/bin/smbcontrol"}.get(name)

        service = _service(
            service_config,
            which=which,
            run_command=run_command,
            popen_command=lambda _command, **_kwargs: process,
        )
        service.start()
        service.stop()
        assert Path(commands[0][0]).name == "smbcontrol"
        assert commands[0][1:] == ["smbd", "shutdown"]
        assert process.wait_calls == 1

    def test_stop_process_terminates_when_smbcontrol_missing(
        self, service_config: SambaServiceConfig
    ) -> None:
        process = FakeProcess()
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: process,
        )
        service.start()
        service.stop()
        assert process.terminate_calls == 1

    def test_stop_timeout_force_kills_process(self, service_config: SambaServiceConfig) -> None:
        process = FakeProcess(wait_timeout=True)
        ticks = iter([0.0, 0.0, 0.0]).__next__
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: process,
            monotonic=ticks,
        )
        service.start()
        status = service.stop(timeout=0.1)
        assert process.kill_calls == 1
        assert status.running is False
        assert status.last_error_or_none is not None

    def test_stop_is_idempotent_when_not_running(self, service_config: SambaServiceConfig) -> None:
        service = _service(service_config, which=lambda _name: None)
        status = service.stop()
        assert status.running is False

    def test_stop_rejects_nonpositive_timeout(self, service_config: SambaServiceConfig) -> None:
        service = _service(service_config, which=lambda _name: None)
        with pytest.raises(SambaConfigError, match="timeout"):
            service.stop(timeout=0.0)

    def test_status_reflects_running_process(self, service_config: SambaServiceConfig) -> None:
        process = FakeProcess()
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: process,
        )
        service.start()
        status = service.status()
        assert status.running is True
        assert status.pid_or_none == process.pid

    def test_status_reflects_exited_process(self, service_config: SambaServiceConfig) -> None:
        process = FakeProcess(poll_result=5)
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: process,
        )
        with pytest.raises(SambaCommandError):
            service.start()

    def test_status_marks_unexpected_process_exit_after_start(
        self, service_config: SambaServiceConfig
    ) -> None:
        process = FakeProcess()
        service = _service(
            service_config,
            which=lambda name: "/usr/sbin/smbd" if name == "smbd" else None,
            popen_command=lambda _command, **_kwargs: process,
        )
        service.start()
        process._poll_result = 12
        status = service.status()
        assert status.running is False
        assert "return code 12" in (status.last_error_or_none or "")

    def test_status_uses_systemctl_for_active_service(
        self, service_config: SambaServiceConfig
    ) -> None:
        responses = [_completed(0), _completed(0)]
        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=lambda _command, **_kwargs: responses.pop(0),
        )
        service.start()
        assert service.status().running is True

    def test_status_uses_systemctl_for_inactive_service(
        self, service_config: SambaServiceConfig
    ) -> None:
        responses = [_completed(0), _completed(3)]
        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=lambda _command, **_kwargs: responses.pop(0),
        )
        service.start()
        assert service.status().running is False

    def test_status_records_systemctl_errors(self, service_config: SambaServiceConfig) -> None:
        responses = [_completed(0), _completed(1, stderr="bad state")]
        service = _service(
            service_config,
            which=lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
            run_command=lambda _command, **_kwargs: responses.pop(0),
        )
        service.start()
        status = service.status()
        assert status.running is False
        assert status.last_error_or_none == "bad state"
