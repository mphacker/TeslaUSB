"""Tests for ``teslausb_web.config`` — TOML loading + validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from teslausb_web.config import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    BoomboxSection,
    ChimesSection,
    ConfigError,
    LightShowsSection,
    MusicSection,
    WebConfig,
    WrapsSection,
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
    assert cfg.web.port == 80
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
    assert cfg.web.port == 80
    assert cfg.paths.backing_root == Path("/srv/teslausb")


def test_paths_state_dir_defaults_to_standard_state_path(tmp_path: Path) -> None:
    cfg_file = tmp_path / "state_dir_default.toml"
    _write(cfg_file, "")
    cfg = load_config(cfg_file)
    assert cfg.paths.state_dir == Path("/var/lib/teslausb")


def test_chimes_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "chimes_defaults.toml"
    _write(cfg_file, "[chimes]\n")
    cfg = load_config(cfg_file)
    assert cfg.chimes == ChimesSection()
    assert cfg.chimes.lock_chime_filename == "LockChime.wav"
    assert cfg.chimes.chimes_folder == "Chimes"
    assert cfg.chimes.schedules_file_relpath == "chime_schedules.json"


def test_invalid_chimes_speed_range_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bad_chimes_speed.toml"
    _write(
        cfg_file,
        """
[chimes]
speed_range_min = 2.0
speed_range_max = 1.0
""",
    )
    with pytest.raises(ConfigError, match="speed_range_min must be < speed_range_max"):
        load_config(cfg_file)


def test_full_chimes_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "full_chimes.toml"
    _write(
        cfg_file,
        """
[chimes]
lock_chime_filename = "CustomLock.wav"
chimes_folder = "CustomChimes"
max_lock_chime_size = 999999
max_lock_chime_duration = 4
min_lock_chime_duration = 1
speed_range_min = 0.7
speed_range_max = 1.7
speed_step = 0.2
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.chimes == ChimesSection(
        lock_chime_filename="CustomLock.wav",
        chimes_folder="CustomChimes",
        max_lock_chime_size=999999,
        max_lock_chime_duration=4,
        min_lock_chime_duration=1,
        speed_range_min=0.7,
        speed_range_max=1.7,
        speed_step=0.2,
    )


def test_light_shows_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "light_shows_defaults.toml"
    _write(cfg_file, "[light_shows]\n")
    cfg = load_config(cfg_file)
    assert cfg.light_shows == LightShowsSection()
    assert cfg.light_shows.folder == "LightShow"
    assert cfg.light_shows.active_show_relpath == "lightshow_active.json"
    assert cfg.light_shows.allowed_extensions == (".fseq", ".mp3", ".wav")


def test_full_light_shows_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "full_light_shows.toml"
    _write(
        cfg_file,
        """
[light_shows]
folder = "CustomShows"
active_show_relpath = "active_show.json"
max_upload_size = 1234
max_zip_size = 5678
allowed_extensions = [".fseq", ".wav"]
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.light_shows == LightShowsSection(
        folder="CustomShows",
        active_show_relpath="active_show.json",
        max_upload_size=1234,
        max_zip_size=5678,
        allowed_extensions=(".fseq", ".wav"),
    )


@pytest.mark.parametrize("key", ["folder", "active_show_relpath"])
def test_light_show_relpaths_reject_path_traversal(tmp_path: Path, key: str) -> None:
    cfg_file = tmp_path / f"{key}.toml"
    _write(cfg_file, f'[light_shows]\n{key} = "../foo"\n')
    with pytest.raises(ConfigError, match="must not contain path separators"):
        load_config(cfg_file)


def test_light_show_allowed_extensions_must_be_string_array(tmp_path: Path) -> None:
    cfg_file = tmp_path / "light_show_allowed_extensions.toml"
    _write(cfg_file, '[light_shows]\nallowed_extensions = [".fseq", 3]\n')
    with pytest.raises(ConfigError, match="allowed_extensions must be an array of strings"):
        load_config(cfg_file)


def test_light_show_allowed_extensions_must_start_with_dot(tmp_path: Path) -> None:
    cfg_file = tmp_path / "light_show_bad_extension.toml"
    _write(cfg_file, '[light_shows]\nallowed_extensions = ["fseq"]\n')
    with pytest.raises(ConfigError, match=r"entries must start with '\.'"):
        load_config(cfg_file)


def test_light_show_upload_limit_must_not_exceed_zip_limit(tmp_path: Path) -> None:
    cfg_file = tmp_path / "light_show_bad_limits.toml"
    _write(
        cfg_file,
        """
[light_shows]
max_upload_size = 10
max_zip_size = 9
""",
    )
    with pytest.raises(ConfigError, match="max_upload_size must be <= max_zip_size"):
        load_config(cfg_file)


def test_wraps_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wraps_defaults.toml"
    _write(cfg_file, "[wraps]\n")
    cfg = load_config(cfg_file)
    assert cfg.wraps == WrapsSection()
    assert cfg.wraps.folder == "wraps"
    assert cfg.wraps.max_upload_count == 10
    assert cfg.wraps.allowed_extensions == (".png",)


def test_full_wraps_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "full_wraps.toml"
    _write(
        cfg_file,
        """
[wraps]
folder = "CustomWraps"
max_size = 1234
min_dimension = 600
max_dimension = 900
max_filename_length = 20
max_upload_count = 4
allowed_extensions = [".png"]
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.wraps == WrapsSection(
        folder="CustomWraps",
        max_size=1234,
        min_dimension=600,
        max_dimension=900,
        max_filename_length=20,
        max_upload_count=4,
        allowed_extensions=(".png",),
    )


@pytest.mark.parametrize("key", ["folder"])
def test_wrap_relpaths_reject_path_traversal(tmp_path: Path, key: str) -> None:
    cfg_file = tmp_path / f"wrap_{key}.toml"
    _write(cfg_file, f'[wraps]\n{key} = "../foo"\n')
    with pytest.raises(ConfigError, match="must not contain path separators"):
        load_config(cfg_file)


def test_wrap_allowed_extensions_must_start_with_dot(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrap_bad_extension.toml"
    _write(cfg_file, '[wraps]\nallowed_extensions = ["png"]\n')
    with pytest.raises(ConfigError, match=r"entries must start with '\.'"):
        load_config(cfg_file)


def test_wrap_dimension_bounds_must_be_ordered(tmp_path: Path) -> None:
    cfg_file = tmp_path / "wrap_bad_bounds.toml"
    _write(
        cfg_file,
        """
[wraps]
min_dimension = 1024
max_dimension = 512
""",
    )
    with pytest.raises(ConfigError, match="max_dimension must be >= min_dimension"):
        load_config(cfg_file)


def test_music_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "music_defaults.toml"
    _write(cfg_file, "[music]\n")
    cfg = load_config(cfg_file)
    assert cfg.music == MusicSection()
    assert cfg.music.folder == "Music"
    assert cfg.music.chunk_size == 16 * 1024 * 1024
    assert cfg.music.allowed_extensions == (".mp3", ".flac", ".wav", ".aac", ".m4a")


def test_full_music_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "full_music.toml"
    _write(
        cfg_file,
        """
[music]
folder = "Audio"
max_file_size = 999
chunk_size = 111
free_space_reserve = 222
stale_chunk_age = 333
allowed_extensions = [".mp3", ".wav"]
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.music == MusicSection(
        folder="Audio",
        max_file_size=999,
        chunk_size=111,
        free_space_reserve=222,
        stale_chunk_age=333,
        allowed_extensions=(".mp3", ".wav"),
    )


@pytest.mark.parametrize("key", ["folder"])
def test_music_relpaths_reject_path_traversal(tmp_path: Path, key: str) -> None:
    cfg_file = tmp_path / f"music_{key}.toml"
    _write(cfg_file, f'[music]\n{key} = "../foo"\n')
    with pytest.raises(ConfigError, match="must not contain path separators"):
        load_config(cfg_file)


def test_music_chunk_size_must_not_exceed_max_file_size(tmp_path: Path) -> None:
    cfg_file = tmp_path / "music_bad_limits.toml"
    _write(
        cfg_file,
        """
[music]
max_file_size = 10
chunk_size = 11
""",
    )
    with pytest.raises(ConfigError, match="chunk_size must be <= max_file_size"):
        load_config(cfg_file)


def test_boombox_defaults_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "boombox_defaults.toml"
    _write(cfg_file, "[boombox]\n")
    cfg = load_config(cfg_file)
    assert cfg.boombox == BoomboxSection()
    assert cfg.boombox.base_dir == "Boombox"
    assert cfg.boombox.max_file_bytes == 1 * 1024 * 1024
    assert cfg.boombox.max_files == 5
    assert cfg.boombox.allowed_extensions == (".mp3", ".wav")


def test_full_boombox_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "full_boombox.toml"
    _write(
        cfg_file,
        """
[boombox]
base_dir = "CustomBoom"
max_file_bytes = 2048
max_files = 7
allowed_extensions = [".wav"]
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.boombox == BoomboxSection(
        base_dir="CustomBoom",
        max_file_bytes=2048,
        max_files=7,
        allowed_extensions=(".wav",),
    )


def test_boombox_base_dir_rejects_path_traversal(tmp_path: Path) -> None:
    cfg_file = tmp_path / "boombox_base_dir.toml"
    _write(cfg_file, '[boombox]\nbase_dir = "../foo"\n')
    with pytest.raises(ConfigError, match="must not contain path separators"):
        load_config(cfg_file)


def test_boombox_max_files_must_be_positive(tmp_path: Path) -> None:
    cfg_file = tmp_path / "boombox_max_files.toml"
    _write(cfg_file, "[boombox]\nmax_files = 0\n")
    with pytest.raises(ConfigError, match="max_files must be > 0"):
        load_config(cfg_file)


def test_boombox_allowed_extensions_must_start_with_dot(tmp_path: Path) -> None:
    cfg_file = tmp_path / "boombox_extension.toml"
    _write(cfg_file, '[boombox]\nallowed_extensions = ["wav"]\n')
    with pytest.raises(ConfigError, match=r"entries must start with '\.'"):
        load_config(cfg_file)


def test_mapping_defaults_point_at_worker_db(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mapping_defaults.toml"
    _write(
        cfg_file,
        """
[paths]
state_dir = "/var/lib/custom-teslausb"
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.mapping.db_path == Path("/var/lib/teslausb/index.sqlite3")


def test_mapping_section_round_trip(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mapping_explicit.toml"
    _write(
        cfg_file,
        """
[mapping]
db_path = "/var/lib/teslausb/custom-index.sqlite3"
trip_gap_minutes = 10
""",
    )
    cfg = load_config(cfg_file)
    assert cfg.mapping.db_path == Path("/var/lib/teslausb/custom-index.sqlite3")
    assert cfg.mapping.trip_gap_minutes == 10


def test_mapping_trip_gap_must_be_positive(tmp_path: Path) -> None:
    cfg_file = tmp_path / "mapping_invalid.toml"
    _write(cfg_file, "[mapping]\ntrip_gap_minutes = 0\n")
    with pytest.raises(ConfigError, match="trip_gap_minutes must be > 0"):
        load_config(cfg_file)


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
    _write(cfg_file, "[web]\nport = 80\n")
    before = dict(os.environ)
    load_config(cfg_file)
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "key",
    ["groups_file_relpath", "random_config_relpath", "schedules_file_relpath"],
)
def test_chime_relpaths_reject_path_traversal(tmp_path: Path, key: str) -> None:
    cfg_file = tmp_path / f"{key}.toml"
    _write(cfg_file, f'[chimes]\n{key} = "../foo"\n')
    with pytest.raises(ConfigError, match="must not contain path separators"):
        load_config(cfg_file)
