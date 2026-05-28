"""Tests for ``teslausb_web.services.cloud_rclone_service``."""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.config import CloudSection, PathsSection, WebConfig, WebSection, load_config
from teslausb_web.services.cloud_oauth_service import CloudOAuthService, OAuthConfig
from teslausb_web.services.cloud_rclone_service import (
    CloudRcloneService,
    RcloneAuthError,
    RcloneConfigError,
    RcloneError,
    RcloneNotInstalledError,
    RcloneServiceConfig,
    RcloneTransferError,
    make_rclone_service,
)


class _FakePopen:
    def __init__(
        self,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        blocked: bool = False,
    ) -> None:
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.pid = 4321
        self._returncode = returncode
        self.returncode = None if blocked else returncode
        self._done = threading.Event()
        self.killed = False
        if not blocked:
            self._done.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=["rclone"], timeout=timeout or 0.0)
        if self.returncode is None:
            self.returncode = self._returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self.returncode = -9
        self._done.set()

    def poll(self) -> int | None:
        return self.returncode if self._done.is_set() else None


class _TimeoutPopen:
    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.pid = 9999
        self.returncode: int | None = None
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd=["rclone"], timeout=timeout or 0.0)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def poll(self) -> int | None:
        return self.returncode


class _StubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


def _oauth_service(tmp_path: Path) -> CloudOAuthService:
    return CloudOAuthService(
        OAuthConfig(
            credentials_path=tmp_path / "oauth-credentials.json",
            oauth_state_path=tmp_path / "oauth-state.json",
            rclone_config_path=tmp_path / "oauth-rclone",
        )
    )


def _write_oauth_credentials(
    tmp_path: Path,
    *,
    provider: str = "google-drive",
    expires_at: str = "2099-01-01T00:00:00Z",
    refresh_token: str = "refresh-123",
) -> None:
    (tmp_path / "oauth-credentials.json").write_text(
        json.dumps(
            {
                "provider": provider,
                "access_token": "access-123",
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_at": expires_at,
            }
        ),
        encoding="utf-8",
    )


def _service(
    tmp_path: Path,
    *,
    provider: str = "google-drive",
    transfer_timeout_seconds: int = 3600,
    bwlimit_kbps: int = 0,
    retries: int = 3,
) -> tuple[CloudRcloneService, CloudOAuthService, Path, Path, Path]:
    source_root = tmp_path / "backing-root"
    source_root.mkdir()
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    oauth_service = _oauth_service(tmp_path)
    _write_oauth_credentials(tmp_path, provider=provider)
    service = CloudRcloneService(
        RcloneServiceConfig(
            rclone_config_dir=tmp_path / "rclone-dir",
            rclone_log_path=tmp_path / "logs" / "rclone.log",
            allowed_local_roots=(source_root, state_root),
            transfer_timeout_seconds=transfer_timeout_seconds,
            bwlimit_kbps=bwlimit_kbps,
            retries=retries,
        ),
        oauth_service,
    )
    binary_path = tmp_path / "bin" / "rclone"
    binary_path.parent.mkdir()
    binary_path.write_text("binary", encoding="utf-8")
    source_file = source_root / "clip.mp4"
    source_file.write_text("video", encoding="utf-8")
    return service, oauth_service, source_root, state_root, binary_path


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_rclone_service_config_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(RcloneConfigError, match="rclone_config_dir"):
        RcloneServiceConfig(
            rclone_config_dir=Path("relative"),
            rclone_log_path=tmp_path / "rclone.log",
            allowed_local_roots=(tmp_path,),
        )


def test_rclone_service_config_rejects_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(RcloneConfigError, match="transfer_timeout_seconds"):
        RcloneServiceConfig(
            rclone_config_dir=tmp_path / "rclone",
            rclone_log_path=tmp_path / "rclone.log",
            allowed_local_roots=(tmp_path,),
            transfer_timeout_seconds=0,
        )
    with pytest.raises(RcloneConfigError, match="bwlimit_kbps"):
        RcloneServiceConfig(
            rclone_config_dir=tmp_path / "rclone",
            rclone_log_path=tmp_path / "rclone.log",
            allowed_local_roots=(tmp_path,),
            bwlimit_kbps=-1,
        )
    with pytest.raises(RcloneConfigError, match="retries"):
        RcloneServiceConfig(
            rclone_config_dir=tmp_path / "rclone",
            rclone_log_path=tmp_path / "rclone.log",
            allowed_local_roots=(tmp_path,),
            retries=-1,
        )


def test_make_rclone_service_uses_cloud_section(tmp_path: Path) -> None:
    oauth_service = _oauth_service(tmp_path)
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(backing_root=tmp_path / "backing", state_dir=tmp_path / "state"),
        cloud=CloudSection(
            credentials_path=tmp_path / "creds.json",
            oauth_state_path=tmp_path / "state.json",
            rclone_config_path=tmp_path / "rclone-dir",
            rclone_log_path=tmp_path / "rclone-dir" / "log.txt",
            transfer_timeout_seconds=77,
            bwlimit_kbps=88,
            retries=9,
            rclone_binary="custom-rclone",
        ),
    )
    service = make_rclone_service(cfg, oauth_service)
    assert service.config_file_path == tmp_path / "rclone-dir" / "rclone.conf"
    assert service.log_path == tmp_path / "rclone-dir" / "log.txt"
    assert service.oauth_service is oauth_service


def test_load_config_parses_cloud_rclone_fields(tmp_path: Path) -> None:
    cfg_file = tmp_path / "web.toml"
    cfg_file.write_text(
        """
[paths]
backing_root = "/srv/teslausb"
state_dir = "/var/lib/teslausb"
db_path = "/var/lib/teslausb/index.sqlite3"
ipc_socket = "/run/teslausb/worker.sock"
cache_invalidate_script = "/usr/local/bin/tesla_cache_invalidate.sh"

[cloud]
rclone_config_path = "/var/lib/teslausb/rclone-dir"
rclone_log_path = "/var/lib/teslausb/rclone-dir/rclone.log"
transfer_timeout_seconds = 44
bwlimit_kbps = 55
retries = 6
rclone_binary = "/usr/bin/rclone"
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.cloud.rclone_config_path == Path("/var/lib/teslausb/rclone-dir")
    assert cfg.cloud.rclone_log_path == Path("/var/lib/teslausb/rclone-dir/rclone.log")
    assert cfg.cloud.transfer_timeout_seconds == 44
    assert cfg.cloud.bwlimit_kbps == 55
    assert cfg.cloud.retries == 6
    assert cfg.cloud.rclone_binary == "/usr/bin/rclone"


def test_create_app_registers_cloud_rclone_service(tmp_path: Path) -> None:
    app = create_app(
        WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(backing_root=tmp_path / "backing", state_dir=tmp_path / "state"),
            cloud=CloudSection(
                credentials_path=tmp_path / "creds.json",
                oauth_state_path=tmp_path / "oauth-state.json",
                rclone_config_path=tmp_path / "rclone-dir",
                rclone_log_path=tmp_path / "rclone-dir" / "rclone.log",
            ),
        )
    )
    from teslausb_web.services.cloud_rclone_service import CloudRcloneService as ServiceType

    assert isinstance(app.extensions["cloud_rclone_service"], ServiceType)
    assert (
        app.extensions["cloud_rclone_service"].oauth_service
        is app.extensions["cloud_oauth_service"]
    )


def test_get_version_parses_json_output(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout='{"version":"v1.68.0"}')),
    ):
        version = service.get_version()
    assert version.binary_path == binary_path
    assert version.version == "v1.68.0"


def test_get_version_falls_back_to_plain_text(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="rclone v1.69.1\n- os/version")),
    ):
        version = service.get_version()
    assert version.version == "v1.69.1"


def test_get_version_raises_when_binary_missing(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, _binary_path = _service(tmp_path)
    with (
        patch("teslausb_web.services.cloud_rclone_service.shutil.which", return_value=None),
        pytest.raises(RcloneNotInstalledError, match="PATH"),
    ):
        service.get_version()


def test_render_config_writes_google_drive_remote(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with patch(
        "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
    ):
        remote = service.render_config()
    config_text = service.config_file_path.read_text(encoding="utf-8")
    assert remote.provider == "google-drive"
    assert remote.backend == "drive"
    assert remote.root == "teslausb:"
    assert "[teslausb]" in config_text
    assert "type = drive" in config_text
    assert '"expiry": "2099-01-01T00:00:00Z"' in config_text


def test_render_config_writes_dropbox_remote(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(
        tmp_path, provider="dropbox"
    )
    with patch(
        "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
    ):
        remote = service.render_config()
    config_text = service.config_file_path.read_text(encoding="utf-8")
    assert remote.backend == "dropbox"
    assert "type = dropbox" in config_text


def test_render_config_adds_onedrive_drive_id_when_available(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(
        tmp_path, provider="onedrive"
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "teslausb_web.services.cloud_rclone_service.urlopen",
            return_value=_StubResponse({"id": "drive-123"}),
        ),
    ):
        service.render_config()
    config_text = service.config_file_path.read_text(encoding="utf-8")
    assert "type = onedrive" in config_text
    assert "drive_type = personal" in config_text
    assert "drive_id = drive-123" in config_text


def test_render_config_raises_when_onedrive_drive_id_lookup_fails(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(
        tmp_path, provider="onedrive"
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "teslausb_web.services.cloud_rclone_service.urlopen",
            side_effect=OSError("boom"),
        ),
        pytest.raises(RcloneAuthError, match="OneDrive"),
    ):
        service.render_config()
    assert not service.config_file_path.exists() or "drive_id =" not in service.config_file_path.read_text(
        encoding="utf-8"
    )


def test_render_config_raises_without_credentials(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    (tmp_path / "oauth-credentials.json").unlink()
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        pytest.raises(RcloneAuthError, match="No stored"),
    ):
        service.render_config()


def test_render_config_raises_for_unsupported_provider(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    _write_oauth_credentials(tmp_path, provider="box")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        pytest.raises(RcloneAuthError, match="Unsupported"),
    ):
        service.render_config()


def test_render_config_refreshes_near_expiry(tmp_path: Path) -> None:
    service, oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    _write_oauth_credentials(tmp_path, expires_at="2000-01-01T00:00:00Z")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "teslausb_web.services.cloud_oauth_service._open_url",
            return_value=_StubResponse(
                {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "token_type": "Bearer",
                    "expires_in": 1200,
                }
            ),
        ),
    ):
        service.render_config()
    credentials = oauth_service_obj.load_credentials()
    assert credentials is not None
    assert credentials.access_token == "fresh-access"


def test_list_remotes_returns_single_configured_remote(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="teslausb:\n")),
    ):
        remotes = service.list_remotes()
    assert len(remotes) == 1
    assert remotes[0].name == "teslausb"


def test_list_remotes_raises_on_nonzero_exit(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(returncode=1, stderr="nope")),
        pytest.raises(RcloneError, match="listremotes"),
    ):
        service.list_remotes()


def test_list_directory_parses_entries_sorted(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    payload = json.dumps(
        [
            {"Name": "zeta.txt", "IsDir": False, "Size": 10, "MimeType": "text/plain"},
            {"Name": "Alpha", "IsDir": True, "Size": 0},
        ]
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout=payload)),
    ):
        listing = service.list_directory("clips")
    assert [entry.name for entry in listing.entries] == ["Alpha", "zeta.txt"]
    assert listing.entries[0].path == "clips/Alpha"
    assert listing.entries[1].size_bytes == 10


def test_list_files_uses_files_only_flag(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="[]")) as mock_run,
    ):
        service.list_files("clips")
    command = mock_run.call_args.args[0]
    assert "--files-only" in command
    assert "teslausb:clips" in command


def test_list_directory_honors_remote_path_override(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    service.set_remote_path_override("TeslaUSB")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="[]")) as mock_run,
    ):
        service.list_directory("SentryClips")
    command = mock_run.call_args.args[0]
    # Reconcile lists each event-folder relative to the configured destination;
    # without the join, it would query the wrong remote root and miss every
    # already-uploaded clip.
    assert "teslausb:TeslaUSB/SentryClips" in command


def test_list_directory_returns_empty_when_remote_path_missing(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "subprocess.run",
            return_value=_completed(returncode=3, stderr="directory not found"),
        ),
    ):
        listing = service.list_directory("missing")
    assert listing.entries == ()


def test_list_directory_raises_on_auth_error(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "subprocess.run",
            return_value=_completed(returncode=3, stderr="401 Unauthorized"),
        ),
        pytest.raises(RcloneAuthError, match="exit 3"),
    ):
        service.list_directory()


def test_list_directory_raises_on_invalid_json(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="{not-json}")),
        pytest.raises(RcloneError, match="lsjson returned invalid JSON"),
    ):
        service.list_directory()


def test_list_directory_rejects_path_traversal(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, _binary_path = _service(tmp_path)
    with pytest.raises(RcloneConfigError, match="traversal"):
        service.list_directory("../escape")


def test_get_stats_merges_about_and_size(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch(
            "subprocess.run",
            side_effect=[
                _completed(stdout=json.dumps({"total": 100, "used": 60, "free": 40, "trashed": 1})),
                _completed(stdout=json.dumps({"count": 4, "bytes": 1234})),
            ],
        ),
    ):
        stats = service.get_stats("clips")
    assert stats.total_bytes == 100
    assert stats.used_bytes == 60
    assert stats.free_bytes == 40
    assert stats.trashed_bytes == 1
    assert stats.object_count == 4
    assert stats.size_bytes == 1234


def test_get_stats_raises_on_invalid_json(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout="{bad-json}")),
        pytest.raises(RcloneError, match="about returned invalid JSON"),
    ):
        service.get_stats()


def test_transfer_copy_builds_expected_command(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(
        tmp_path, bwlimit_kbps=512, retries=7
    )
    fake_process = _FakePopen(
        stdout_text="done\n", stderr_text="Transferred: 1 MiB / 1 MiB, 100%, 1 MiB/s, ETA 0s\n"
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process) as mock_popen,
    ):
        result = service.transfer(source_root / "clip.mp4", "archive/folder/clip.mp4", operation="copy")
    command = mock_popen.call_args.args[0]
    assert command[0] == str(binary_path)
    assert "--bwlimit" in command
    assert "512k" in command
    assert command[command.index("--retries") + 1] == "7"
    # `copy` must be translated to `copyto` so the file lands at the exact
    # destination path instead of inside a `<filename>/` wrapper folder.
    assert "copyto" in command
    assert "copy" not in [c for c in command if c == "copy"]
    assert command[-2] == str((source_root / "clip.mp4").resolve())
    assert command[-1] == "teslausb:archive/folder/clip.mp4"
    assert result.progress is not None
    assert result.progress.percent == 100.0


def test_transfer_move_uses_moveto_subcommand(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(stderr_text="Transferred: 1 MiB / 1 MiB, 100%, 1 MiB/s, ETA 0s\n")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process) as mock_popen,
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder/clip.mp4", operation="move")
    command = mock_popen.call_args.args[0]
    assert "moveto" in command
    assert "move" not in [c for c in command if c == "move"]


def test_transfer_sync_builds_sync_command(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(stderr_text="Transferred: 2 MiB / 2 MiB, 100%, 1 MiB/s, ETA 0s\n")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process) as mock_popen,
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder", operation="sync")
    assert "sync" in mock_popen.call_args.args[0]


def test_transfer_reports_progress_callback(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(
        stderr_text=("Progress: checking\nTransferred: 5 MiB / 10 MiB, 50%, 1 MiB/s, ETA 5s\n")
    )
    seen: list[float | None] = []
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
    ):
        service.transfer(
            source_root / "clip.mp4",
            "archive/folder",
            progress_callback=lambda progress: seen.append(progress.percent),
        )
    assert seen[-1] == 50.0


def test_transfer_raises_on_nonzero_exit(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(stderr_text="copy failed\n", returncode=4)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
        pytest.raises(RcloneTransferError, match="exit 4"),
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder")


def test_transfer_raises_auth_error_on_unauthorized_exit(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(stderr_text="401 Unauthorized\n", returncode=9)
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
        pytest.raises(RcloneAuthError, match="exit 9"),
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder")


def test_transfer_rejects_paths_outside_allowed_roots(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, _binary_path = _service(tmp_path)
    outsider = tmp_path / "elsewhere.mp4"
    outsider.write_text("video", encoding="utf-8")
    with pytest.raises(RcloneConfigError, match="configured roots"):
        service.transfer(outsider, "archive/folder")


def test_transfer_rejects_missing_source_path(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, _binary_path = _service(tmp_path)
    with pytest.raises(RcloneConfigError, match="does not exist"):
        service.transfer(source_root / "missing.mp4", "archive/folder")


def test_transfer_rejects_invalid_operation(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, _binary_path = _service(tmp_path)
    with pytest.raises(RcloneConfigError, match="Unsupported"):
        service.transfer(source_root / "clip.mp4", "archive/folder", operation="delete")


def test_transfer_rejects_remote_path_traversal(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, _binary_path = _service(tmp_path)
    with pytest.raises(RcloneConfigError, match="traversal"):
        service.transfer(source_root / "clip.mp4", "../escape")


def test_transfer_timeout_kills_process(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(
        tmp_path, transfer_timeout_seconds=1
    )
    fake_process = _TimeoutPopen()
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
        pytest.raises(RcloneTransferError, match="timed out"),
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder")
    assert fake_process.killed is True


def test_cancel_active_transfer_returns_false_when_idle(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, _binary_path = _service(tmp_path)
    assert service.cancel_active_transfer() is False


def test_cancel_active_transfer_kills_process_and_returns_cancelled_result(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    fake_process = _FakePopen(
        stderr_text="Transferred: 1 MiB / 5 MiB, 20%, 1 MiB/s, ETA 4s\n", blocked=True
    )
    results: list[object] = []

    def _run_transfer() -> None:
        results.append(service.transfer(source_root / "clip.mp4", "archive/folder"))

    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
    ):
        worker = threading.Thread(target=_run_transfer)
        worker.start()
        while service.current_progress() is None:
            pass
        assert service.cancel_active_transfer() is True
        worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert fake_process.killed is True
    assert len(results) == 1
    result = results[0]
    assert hasattr(result, "cancelled")
    assert result.cancelled is True


def test_transfer_rotates_existing_log_file(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    service.log_path.parent.mkdir(parents=True, exist_ok=True)
    service.log_path.write_text("old-log", encoding="utf-8")
    fake_process = _FakePopen(stderr_text="Transferred: 1 MiB / 1 MiB, 100%, 1 MiB/s, ETA 0s\n")
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.Popen", return_value=fake_process),
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder")
    rotated = service.log_path.with_name(f"{service.log_path.name}.1")
    assert rotated.read_text(encoding="utf-8") == "old-log"


def test_transfer_raises_when_binary_missing_for_relative_command(tmp_path: Path) -> None:
    service, _oauth_service_obj, source_root, _state_root, _binary_path = _service(tmp_path)
    with (
        patch("teslausb_web.services.cloud_rclone_service.shutil.which", return_value=None),
        pytest.raises(RcloneNotInstalledError, match="PATH"),
    ):
        service.transfer(source_root / "clip.mp4", "archive/folder")


def test_transfer_uses_absolute_binary_without_path_lookup(tmp_path: Path) -> None:
    _service_obj, _oauth_service_obj, source_root, _state_root, binary_path = _service(tmp_path)
    absolute_service = CloudRcloneService(
        RcloneServiceConfig(
            rclone_config_dir=tmp_path / "rclone-dir-2",
            rclone_log_path=tmp_path / "logs-2" / "rclone.log",
            allowed_local_roots=(source_root,),
            rclone_binary=str(binary_path),
        ),
        _oauth_service_obj,
    )
    fake_process = _FakePopen(stderr_text="Transferred: 1 MiB / 1 MiB, 100%, 1 MiB/s, ETA 0s\n")
    with (
        patch("teslausb_web.services.cloud_rclone_service.shutil.which") as mock_which,
        patch("subprocess.Popen", return_value=fake_process) as mock_popen,
    ):
        absolute_service.transfer(source_root / "clip.mp4", "archive/folder")
    mock_which.assert_not_called()
    assert mock_popen.call_args.args[0][0] == str(binary_path)


def test_short_commands_use_configured_timeout_cap(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(
        tmp_path, transfer_timeout_seconds=5
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout='{"version":"v1.0"}')) as mock_run,
    ):
        service.get_version()
    assert mock_run.call_args.kwargs["timeout"] == 5.0


def test_short_commands_cap_timeout_at_thirty_seconds(tmp_path: Path) -> None:
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(
        tmp_path, transfer_timeout_seconds=3600
    )
    with (
        patch(
            "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
        ),
        patch("subprocess.run", return_value=_completed(stdout='{"version":"v1.0"}')) as mock_run,
    ):
        service.get_version()
    assert mock_run.call_args.kwargs["timeout"] == 30.0


def test_posix_rendered_config_is_private(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    service, _oauth_service_obj, _source_root, _state_root, binary_path = _service(tmp_path)
    with patch(
        "teslausb_web.services.cloud_rclone_service.shutil.which", return_value=str(binary_path)
    ):
        service.render_config()
    mode = service.config_file_path.stat().st_mode & 0o777
    assert mode == 0o600
