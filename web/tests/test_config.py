"""Tests for ``teslausb_web.config`` — TOML loading + validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from teslausb_web.config import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    ConfigError,
    WebConfig,
    load_config,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch  # noqa: PT013


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_defaults_when_no_file_and_allow_defaults(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    # Force the DEFAULT_CONFIG_PATH lookup to miss by pointing
    # tmpdir-roots at locations that don't exist. Easier: patch
    # the resolver to short-circuit. Here we just trust that
    # `/etc/teslausb/teslausb-web.toml` doesn't exist on the dev box.
    if DEFAULT_CONFIG_PATH.exists():
        pytest.skip("dev box has a real /etc/teslausb/teslausb-web.toml; skipping")
    cfg = load_config(allow_defaults=True)
    assert cfg.source_path is None
    assert cfg.web.port == 8080
    assert cfg.web.max_upload_mb > 0
    # On Windows the WindowsPath form of a POSIX absolute path returns
    # False from .is_absolute() because it lacks a drive letter — the
    # config validator uses PurePosixPath specifically to handle this.
    # Assert via the same lens.
    from pathlib import PurePosixPath

    assert PurePosixPath(cfg.paths.backing_root.as_posix()).is_absolute()
    assert cfg.features.samba_enabled is False


def test_absent_file_raises_when_defaults_disallowed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    if DEFAULT_CONFIG_PATH.exists():
        pytest.skip("dev box has a real /etc/teslausb/teslausb-web.toml; skipping")
    with pytest.raises(ConfigError, match="no config file found"):
        load_config(allow_defaults=False)


def test_load_from_explicit_path(tmp_path: Path) -> None:
    cfg_file = tmp_path / "web.toml"
    _write(
        cfg_file,
        """
[web]
host = "0.0.0.0"
port = 9090
secret_key = "test-secret-32chars-xxxxxxxxxxxxx"
max_upload_mb = 256
max_chunk_mb = 32

[paths]
backing_root = "/srv/teslausb"
db_path = "/var/lib/teslausb/index.sqlite3"
ipc_socket = "/run/teslausb/worker.sock"
cache_invalidate_script = "/usr/local/bin/tesla_cache_invalidate.sh"

[features]
samba_enabled = true
music_enabled = true
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.source_path == cfg_file
    assert cfg.web.host == "0.0.0.0"  # noqa: S104 — test config value, not a bind address
    assert cfg.web.port == 9090
    assert cfg.web.secret_key.startswith("test-secret")
    assert cfg.web.max_upload_mb == 256
    assert cfg.features.samba_enabled is True
    assert cfg.features.music_enabled is True
    assert cfg.features.boombox_enabled is False  # default kept


def test_load_via_env_var(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    cfg_file = tmp_path / "via_env.toml"
    _write(cfg_file, "[web]\nport = 7777\n")
    monkeypatch.setenv(ENV_CONFIG_PATH, str(cfg_file))
    cfg = load_config()
    assert cfg.source_path == cfg_file
    assert cfg.web.port == 7777


def test_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    env_file = tmp_path / "env.toml"
    explicit_file = tmp_path / "explicit.toml"
    _write(env_file, "[web]\nport = 1111\n")
    _write(explicit_file, "[web]\nport = 2222\n")
    monkeypatch.setenv(ENV_CONFIG_PATH, str(env_file))
    cfg = load_config(explicit_file)
    assert cfg.web.port == 2222


def test_invalid_toml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "broken.toml"
    _write(cfg_file, "this is = not [ valid toml")
    with pytest.raises(ConfigError, match="TOML parse error"):
        load_config(cfg_file)


def test_path_is_not_regular_file_raises(tmp_path: Path) -> None:
    # Pass a directory path. is_file() returns False.
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config(tmp_path)


def test_section_must_be_table(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrong.toml"
    _write(cfg_file, 'web = "should be a table"\n')
    with pytest.raises(ConfigError, match=r"\[web\] must be a table"):
        load_config(cfg_file)


def test_wrong_type_int(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrong_int.toml"
    _write(cfg_file, '[web]\nport = "not an int"\n')
    with pytest.raises(ConfigError, match="port must be an integer"):
        load_config(cfg_file)


def test_bool_is_not_an_int(tmp_path: Path) -> None:
    # TOML booleans must not be accepted where an int is expected,
    # because Python's `isinstance(True, int)` is True and that
    # silent coercion has bitten everyone at least once.
    cfg_file = tmp_path / "bool_as_int.toml"
    _write(cfg_file, "[web]\nport = true\n")
    with pytest.raises(ConfigError, match="port must be an integer"):
        load_config(cfg_file)


def test_wrong_type_bool(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrong_bool.toml"
    _write(cfg_file, '[features]\nsamba_enabled = "yes"\n')
    with pytest.raises(ConfigError, match="samba_enabled must be a boolean"):
        load_config(cfg_file)


def test_wrong_type_str(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrong_str.toml"
    _write(cfg_file, "[web]\nhost = 42\n")
    with pytest.raises(ConfigError, match="host must be a string"):
        load_config(cfg_file)


def test_wrong_type_path(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrong_path.toml"
    _write(cfg_file, "[paths]\nbacking_root = 42\n")
    with pytest.raises(ConfigError, match="backing_root must be a string path"):
        load_config(cfg_file)


def test_port_out_of_range(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad_port.toml"
    _write(cfg_file, "[web]\nport = 99999\n")
    with pytest.raises(ConfigError, match=r"outside 1\.\."):
        load_config(cfg_file)


def test_negative_upload_size(tmp_path: Path) -> None:
    cfg_file = tmp_path / "neg.toml"
    _write(cfg_file, "[web]\nmax_upload_mb = 0\n")
    with pytest.raises(ConfigError, match="max_upload_mb must be > 0"):
        load_config(cfg_file)


def test_negative_chunk_size(tmp_path: Path) -> None:
    cfg_file = tmp_path / "neg_chunk.toml"
    _write(cfg_file, "[web]\nmax_chunk_mb = 0\n")
    with pytest.raises(ConfigError, match="max_chunk_mb must be > 0"):
        load_config(cfg_file)


def test_chunk_exceeds_upload(tmp_path: Path) -> None:
    cfg_file = tmp_path / "chunk_too_big.toml"
    _write(cfg_file, "[web]\nmax_upload_mb = 10\nmax_chunk_mb = 20\n")
    with pytest.raises(ConfigError, match="cannot exceed max_upload_mb"):
        load_config(cfg_file)


def test_relative_path_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "rel.toml"
    _write(cfg_file, '[paths]\nbacking_root = "srv/teslausb"\n')
    with pytest.raises(ConfigError, match="must be absolute"):
        load_config(cfg_file)


def test_config_error_carries_source_path(tmp_path: Path) -> None:
    cfg_file = tmp_path / "src_path.toml"
    _write(cfg_file, "[web]\nport = 0\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    assert exc_info.value.path == cfg_file
    assert str(cfg_file) in str(exc_info.value)


def test_partial_section_uses_defaults_for_missing_keys(tmp_path: Path) -> None:
    cfg_file = tmp_path / "partial.toml"
    _write(cfg_file, "[web]\nport = 8081\n")
    cfg = load_config(cfg_file)
    assert cfg.web.port == 8081
    assert cfg.web.host == "127.0.0.1"  # default kept
    assert cfg.web.max_upload_mb > 0  # default kept


def test_empty_file_uses_all_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "empty.toml"
    _write(cfg_file, "")
    cfg = load_config(cfg_file)
    assert cfg.source_path == cfg_file
    assert cfg.web.port == 8080
    assert cfg.paths.backing_root == Path("/srv/teslausb")


def test_webconfig_dataclass_is_frozen() -> None:
    cfg = WebConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.source_path = Path("/etc/anywhere.toml")  # type: ignore[misc]


def test_default_config_path_lookup(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """When `/etc/teslausb/teslausb-web.toml` exists, it is loaded by default."""
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    fake_default = tmp_path / "teslausb-web.toml"
    _write(fake_default, "[web]\nport = 4242\n")
    # Patch the module-level constant so the loader looks at our tmp path.
    import teslausb_web.config as cfg_module

    monkeypatch.setattr(cfg_module, "DEFAULT_CONFIG_PATH", fake_default)
    cfg = load_config()
    assert cfg.source_path == fake_default
    assert cfg.web.port == 4242


def test_os_environ_not_mutated(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Loader must not write to ``os.environ``."""
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    cfg_file = tmp_path / "no_env_writes.toml"
    _write(cfg_file, "[web]\nport = 8080\n")
    before = dict(os.environ)
    load_config(cfg_file)
    assert dict(os.environ) == before
