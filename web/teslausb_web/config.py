"""TOML-backed configuration loader for the Flask web app.

B-1 stores configuration in `/etc/teslausb/teslausb-web.toml` (root)
and `/etc/teslausb/worker.toml` (Rust daemon). Both files are TOML
per ADR-0001 — the YAML loader used in v1 is gone.

Locations searched (first match wins):

1. The path supplied via the ``TESLAUSB_WEB_CONFIG`` environment
   variable. Tests use this to point at a tmpdir.
2. ``/etc/teslausb/teslausb-web.toml`` (production install path,
   written by ``setup.sh`` in Phase 6).
3. Built-in defaults — only used when the file is absent AND the
   caller passed ``allow_defaults=True``. In production we want a
   hard failure if the config is missing so misdeploys are loud.

Every key is mapped onto a small dataclass tree so the rest of the
app can use typed access (``config.web.port``) rather than
stringly-typed dict lookups (charter §"Anti-patterns / Stringly-typed
code"). Validation lives in ``WebConfig.validate`` and runs at load
time; misconfigurations raise ``ConfigError`` with a path-anchored
message so the operator sees exactly which key is wrong.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

ENV_CONFIG_PATH: str = "TESLAUSB_WEB_CONFIG"
DEFAULT_CONFIG_PATH: Path = Path("/etc/teslausb/teslausb-web.toml")

# Match v1's defaults so the screenshot-diff acceptance gate has a
# fighting chance: a freshly installed B-1 device serves the same
# port and accepts the same upload sizes a v1 device did. Port 80
# matches v1's "Flask as root on port 80" topology; in B-1 the
# production layout puts gunicorn on a Unix socket behind nginx
# (see config/gunicorn.conf.py + config/nginx-teslausb.conf), so
# this default is only consulted when the Flask app binds TCP
# directly (developer laptop / pytest scenarios).
_DEFAULT_PORT: int = 80
_DEFAULT_HOST: str = "127.0.0.1"
_DEFAULT_MAX_UPLOAD_MB: int = 512
_DEFAULT_MAX_CHUNK_MB: int = 64
_DEFAULT_BACKING_ROOT: Path = Path("/srv/teslausb")
_DEFAULT_STATE_DIR: Path = Path("/var/lib/teslausb")
_DEFAULT_DB_PATH: Path = Path("/var/lib/teslausb/index.sqlite3")
_DEFAULT_IPC_SOCKET: Path = Path("/run/teslausb/worker.sock")
_DEFAULT_CACHE_SCRIPT: Path = Path("/usr/local/bin/tesla_cache_invalidate.sh")
_DEFAULT_LOCK_CHIME_FILENAME: Final[str] = "LockChime.wav"
_DEFAULT_CHIMES_FOLDER: Final[str] = "Chimes"
_DEFAULT_GROUPS_FILE_RELPATH: Final[str] = "chime_groups.json"
_DEFAULT_RANDOM_CONFIG_RELPATH: Final[str] = "chime_random_config.json"
_DEFAULT_SCHEDULES_FILE_RELPATH: Final[str] = "chime_schedules.json"
_DEFAULT_MAX_LOCK_CHIME_SIZE: Final[int] = 1_048_576
_DEFAULT_MAX_LOCK_CHIME_DURATION: Final[int] = 5
_DEFAULT_MIN_LOCK_CHIME_DURATION: Final[int] = 1
_DEFAULT_SPEED_RANGE_MIN: Final[float] = 0.5
_DEFAULT_SPEED_RANGE_MAX: Final[float] = 2.0
_DEFAULT_SPEED_STEP: Final[float] = 0.1
_DEFAULT_LIGHT_SHOWS_FOLDER: Final[str] = "LightShow"
_DEFAULT_ACTIVE_SHOW_RELPATH: Final[str] = "lightshow_active.json"
_DEFAULT_LIGHT_SHOW_MAX_UPLOAD_SIZE: Final[int] = 100 * 1024 * 1024
_DEFAULT_LIGHT_SHOW_MAX_ZIP_SIZE: Final[int] = 500 * 1024 * 1024
_DEFAULT_LIGHT_SHOW_ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (".fseq", ".mp3", ".wav")
_DEFAULT_WRAPS_FOLDER: Final[str] = "Wraps"
_DEFAULT_WRAP_MAX_SIZE: Final[int] = 1 * 1024 * 1024
_DEFAULT_WRAP_MIN_DIMENSION: Final[int] = 512
_DEFAULT_WRAP_MAX_DIMENSION: Final[int] = 1024
_DEFAULT_WRAP_MAX_FILENAME_LENGTH: Final[int] = 30
_DEFAULT_WRAP_MAX_UPLOAD_COUNT: Final[int] = 10
_DEFAULT_WRAP_ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (".png",)
_DEFAULT_MUSIC_FOLDER: Final[str] = "Music"
_DEFAULT_MUSIC_MAX_FILE_SIZE: Final[int] = 2_048 * 1_024 * 1_024
_DEFAULT_MUSIC_CHUNK_SIZE: Final[int] = 16 * 1_024 * 1_024
_DEFAULT_MUSIC_FREE_SPACE_RESERVE: Final[int] = 4 * 1_024 * 1_024
_DEFAULT_MUSIC_STALE_CHUNK_AGE: Final[int] = 3_600
_DEFAULT_MUSIC_ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (
    ".mp3",
    ".flac",
    ".wav",
    ".aac",
    ".m4a",
)
_DEFAULT_BOOMBOX_BASE_DIR: Final[str] = "Boombox"
_DEFAULT_BOOMBOX_MAX_FILE_BYTES: Final[int] = 1 * 1024 * 1024
_DEFAULT_BOOMBOX_MAX_FILES: Final[int] = 5
_DEFAULT_BOOMBOX_ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (".mp3", ".wav")
_DEFAULT_LICENSE_PLATES_DB_NAME: Final[str] = "license_plates.db"
_DEFAULT_LICENSE_PLATES_DB_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_LICENSE_PLATES_DB_NAME
_DEFAULT_LICENSE_PLATES_REDACTION_ENABLED: Final[bool] = False
_DEFAULT_LICENSE_PLATES_MAX_PLATE_LENGTH: Final[int] = 16
_DEFAULT_LICENSE_PLATES_MAX_LABEL_LENGTH: Final[int] = 64
_DEFAULT_LICENSE_PLATES_MAX_NOTES_LENGTH: Final[int] = 240
_DEFAULT_RETENTION_POLICY_FILENAME: Final[str] = "retention_policy.json"
_DEFAULT_RETENTION_POLICY_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_RETENTION_POLICY_FILENAME
_DEFAULT_RETENTION_MAX_AGE_DAYS: Final[int] = 30
_DEFAULT_RETENTION_TARGET_FREE_PCT: Final[int] = 10
_DEFAULT_RETENTION_TARGET_FREE_PCT_MIN: Final[int] = 5
_DEFAULT_RETENTION_TARGET_FREE_PCT_MAX: Final[int] = 50
_DEFAULT_RETENTION_MAX_ARCHIVE_SIZE_GB: Final[int] = 0
_DEFAULT_RETENTION_WARNING_DAYS: Final[int] = 7
_DEFAULT_CLEANUP_HISTORY_DB_NAME: Final[str] = "cleanup_history.db"
_DEFAULT_CLEANUP_HISTORY_DB_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_CLEANUP_HISTORY_DB_NAME
_DEFAULT_CLEANUP_MAX_CONCURRENT_RUNS: Final[int] = 1
_DEFAULT_CLEANUP_DRY_RUN_DEFAULT: Final[bool] = True
_DEFAULT_CLEANUP_ORPHAN_SCAN_BATCH_SIZE: Final[int] = 500
_DEFAULT_CLEANUP_SAMPLE_PATH_LIMIT: Final[int] = 12
_DEFAULT_CLEANUP_RECENT_PROTECTION_HOURS: Final[int] = 1
_DEFAULT_CLEANUP_DELETE_GPS_TAGGED_CLIPS: Final[bool] = False
_DEFAULT_CLEANUP_ORPHAN_MIN_AGE_SECONDS: Final[int] = 300
_DEFAULT_CLEANUP_REPORT_LIMIT: Final[int] = 20
_DEFAULT_SYSTEM_SETTINGS_FILENAME: Final[str] = "system_settings.json"
_DEFAULT_SYSTEM_SETTINGS_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_SYSTEM_SETTINGS_FILENAME
_DEFAULT_SYSTEM_SETTINGS_LOG_LEVEL: Final[str] = "INFO"
_DEFAULT_SAMBA_CONFIG_PATH: Path = Path("/etc/samba/smb.conf")
_DEFAULT_SAMBA_BINARY_SMBD: Final[str] = "smbd"
_DEFAULT_SAMBA_BINARY_SMBCONTROL: Final[str] = "smbcontrol"
_DEFAULT_SAMBA_BINARY_SYSTEMCTL: Final[str] = "systemctl"
_DEFAULT_SAMBA_INVALIDATE_DEBOUNCE_MS: Final[int] = 500
_DEFAULT_SAMBA_WATCHER_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_DEFAULT_SAMBA_IGNORE_EXTENSIONS: Final[tuple[str, ...]] = (
    ".tmp",
    ".part",
    ".swp",
    ".crdownload",
)
_DEFAULT_WIFI_CREDENTIALS_FILENAME: Final[str] = "wifi_credentials.json"
_DEFAULT_WIFI_CREDENTIALS_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_WIFI_CREDENTIALS_FILENAME
_DEFAULT_WIFI_AP_SSID: Final[str] = "TeslaUSB-Setup"
_DEFAULT_WIFI_AP_PASSPHRASE: Final[str] = ""
_DEFAULT_WIFI_AP_IDLE_TIMEOUT_SECONDS: Final[int] = 600
_WIFI_PASSPHRASE_MIN_LENGTH: Final[int] = 8
_WIFI_PASSPHRASE_MAX_LENGTH: Final[int] = 63
_DEFAULT_WIFI_NMCLI_BINARY: Final[str] = "nmcli"
_DEFAULT_WIFI_IWLIST_BINARY: Final[str] = "iwlist"
_DEFAULT_WIFI_IWCONFIG_BINARY: Final[str] = "iwconfig"
_DEFAULT_WIFI_WPA_CLI_BINARY: Final[str] = "wpa_cli"
_DEFAULT_CLOUD_CREDENTIALS_FILENAME: Final[str] = "cloud_oauth_credentials.json"
_DEFAULT_CLOUD_STATE_FILENAME: Final[str] = "cloud_oauth_state.json"
_DEFAULT_CLOUD_RCLONE_CONFIG_DIRNAME: Final[str] = "rclone"
_DEFAULT_CLOUD_RCLONE_LOG_FILENAME: Final[str] = "rclone.log"
_DEFAULT_CLOUD_REFRESH_WINDOW_SECONDS: Final[int] = 300
_DEFAULT_CLOUD_TRANSFER_TIMEOUT_SECONDS: Final[int] = 3600
_DEFAULT_CLOUD_BWLIMIT_KBPS: Final[int] = 0
_DEFAULT_CLOUD_RETRIES: Final[int] = 3
_DEFAULT_CLOUD_RCLONE_BINARY: Final[str] = "rclone"
_DEFAULT_CLOUD_CREDENTIALS_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_CLOUD_CREDENTIALS_FILENAME
_DEFAULT_CLOUD_STATE_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_CLOUD_STATE_FILENAME
_DEFAULT_CLOUD_RCLONE_CONFIG_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_CLOUD_RCLONE_CONFIG_DIRNAME
_DEFAULT_CLOUD_RCLONE_LOG_PATH: Path = (
    _DEFAULT_CLOUD_RCLONE_CONFIG_PATH / _DEFAULT_CLOUD_RCLONE_LOG_FILENAME
)
_DEFAULT_CLOUD_ARCHIVE_DB_NAME: Final[str] = "cloud_sync.db"
_DEFAULT_CLOUD_ARCHIVE_DB_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_CLOUD_ARCHIVE_DB_NAME
_DEFAULT_CLOUD_TESLACAM_PATH: Path = _DEFAULT_BACKING_ROOT
_DEFAULT_CLOUD_WORKER_IDLE_SECONDS: Final[float] = 300.0
_DEFAULT_CLOUD_BACKOFF_INITIAL_SECONDS: Final[float] = 60.0
_DEFAULT_CLOUD_BACKOFF_MAX_SECONDS: Final[float] = 300.0
_DEFAULT_CLOUD_MAX_RETRY_ATTEMPTS: Final[int] = 5
_DEFAULT_CLOUD_WIFI_CHECK_REQUIRED: Final[bool] = True
_DEFAULT_CLOUD_PRIORITY_FOLDERS: Final[tuple[str, ...]] = (
    "SentryClips",
    "SavedClips",
)
_DEFAULT_CLOUD_SYNC_FOLDERS: Final[tuple[str, ...]] = (
    "SentryClips",
    "SavedClips",
)
_DEFAULT_CLOUD_DEAD_LETTER_MAX_AGE_DAYS: Final[int] = 30
_DEFAULT_MAPPING_DB_NAME: Final[str] = "mapping.db"
_DEFAULT_MAPPING_BACKUP_DIRNAME: Final[str] = "mapping-backups"
_DEFAULT_MAPPING_BACKUP_RETENTION: Final[int] = 3
_DEFAULT_MAPPING_SAMPLE_RATE: Final[int] = 30
_DEFAULT_MAPPING_TRIP_GAP_MINUTES: Final[int] = 5
_DEFAULT_MAPPING_INDEX_TOO_NEW_SECONDS: Final[int] = 120
_DEFAULT_MAPPING_HARSH_BRAKE_THRESHOLD: Final[float] = -4.0
_DEFAULT_MAPPING_EMERGENCY_BRAKE_THRESHOLD: Final[float] = -7.0
_DEFAULT_MAPPING_HARD_ACCEL_THRESHOLD: Final[float] = 3.5
_DEFAULT_MAPPING_SHARP_TURN_LATERAL_MPS2: Final[float] = 4.0
_DEFAULT_MAPPING_SPEED_LIMIT_MPS: Final[float] = 35.76
_DEFAULT_MAPPING_STALE_SCAN_INTERVAL_SECONDS: Final[int] = 30 * 24 * 60 * 60
_DEFAULT_MAPPING_STALE_SCAN_JITTER_SECONDS: Final[int] = 24 * 60 * 60
_DEFAULT_MAPPING_INITIAL_STALE_SCAN_BASE_SECONDS: Final[int] = 5 * 60
_DEFAULT_MAPPING_INITIAL_STALE_SCAN_JITTER_SECONDS: Final[int] = 5 * 60
_DEFAULT_MAPPING_STALE_SCAN_DEBOUNCE_SECONDS: Final[int] = 10 * 60
_DEFAULT_MAPPING_DB_PATH: Path = _DEFAULT_STATE_DIR / _DEFAULT_MAPPING_DB_NAME
_DEFAULT_MAPPING_BACKUP_DIR: Path = _DEFAULT_STATE_DIR / _DEFAULT_MAPPING_BACKUP_DIRNAME
_DEFAULT_MAPPING_MEDIA_ROOT: Path = _DEFAULT_BACKING_ROOT

# Analytics dashboard thresholds. Percent-of-disk-used bands that
# escalate the storage-health banner from healthy → caution → warning
# → critical. Defaults mirror v1's hardcoded ladder (80 / 90 / 95).
# ``theoretical_gb_per_hour`` is the dashcam record rate we fall back
# to when the mapping DB has no clips yet (Tesla nominal: 4 cameras
# at 1080p ~= 400 MB/hour = 0.4 GB/hour).
_DEFAULT_ANALYTICS_CAUTION_PCT: Final[float] = 80.0
_DEFAULT_ANALYTICS_WARNING_PCT: Final[float] = 90.0
_DEFAULT_ANALYTICS_CRITICAL_PCT: Final[float] = 95.0
_DEFAULT_ANALYTICS_THEORETICAL_GB_PER_HOUR: Final[float] = 0.4

# Highest valid TCP/UDP port number per RFC 793. Named to silence
# the magic-value lint and document intent at the call site.
_TCP_PORT_MAX: int = 65_535


class ConfigError(ValueError):
    """A configuration file could not be loaded or failed validation.

    Subclasses ``ValueError`` (not a bare ``Exception``) so callers
    can catch precisely without violating charter §3 "no blind
    except". Carries the original path so the error message in
    journalctl points the operator at the right file.
    """

    def __init__(self, path: Path | None, message: str) -> None:
        if path is None:
            super().__init__(message)
        else:
            super().__init__(f"{path}: {message}")
        self.path = path


@dataclass(frozen=True, slots=True)
class WebSection:
    """Settings that govern the Flask app surface itself."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    secret_key: str = ""
    max_upload_mb: int = _DEFAULT_MAX_UPLOAD_MB
    max_chunk_mb: int = _DEFAULT_MAX_CHUNK_MB

    def validate(self) -> None:
        if not (1 <= self.port <= _TCP_PORT_MAX):
            raise ConfigError(
                None,
                f"[web] port {self.port} outside 1..{_TCP_PORT_MAX}",
            )
        if self.max_upload_mb <= 0:
            raise ConfigError(None, "[web] max_upload_mb must be > 0")
        if self.max_chunk_mb <= 0:
            raise ConfigError(None, "[web] max_chunk_mb must be > 0")
        if self.max_chunk_mb > self.max_upload_mb:
            raise ConfigError(
                None,
                f"[web] max_chunk_mb ({self.max_chunk_mb}) "
                f"cannot exceed max_upload_mb ({self.max_upload_mb})",
            )
        # `secret_key == ""` is allowed at load time so test fixtures
        # don't need to bake a real key in; the app factory generates
        # one at startup if the key is empty. Production setup.sh
        # writes a 64-char hex key during initial install.


@dataclass(frozen=True, slots=True)
class PathsSection:
    """Filesystem locations the web app reads or writes."""

    backing_root: Path = _DEFAULT_BACKING_ROOT
    state_dir: Path = _DEFAULT_STATE_DIR
    db_path: Path = _DEFAULT_DB_PATH
    ipc_socket: Path = _DEFAULT_IPC_SOCKET
    cache_invalidate_script: Path = _DEFAULT_CACHE_SCRIPT

    def validate(self) -> None:
        # Config paths are POSIX paths on the target device. We check
        # against PurePosixPath so the validation works the same on
        # Windows dev boxes (where Path("/srv").is_absolute() == False
        # because there's no drive letter) and on Linux production.
        for name, value in (
            ("backing_root", self.backing_root),
            ("state_dir", self.state_dir),
            ("db_path", self.db_path),
            ("ipc_socket", self.ipc_socket),
            ("cache_invalidate_script", self.cache_invalidate_script),
        ):
            if not PurePosixPath(value.as_posix()).is_absolute():
                raise ConfigError(None, f"[paths] {name} must be absolute, got {value!r}")


@dataclass(frozen=True, slots=True)
class FeaturesSection:
    """Optional feature flags read by individual blueprints.

    Default state mirrors v1: music + boombox + samba off, everything
    else on. Settings UI flips these at runtime; the values written
    here are only the initial defaults.
    """

    music_enabled: bool = False
    boombox_enabled: bool = False
    samba_enabled: bool = False
    cloud_archive_enabled: bool = True
    # IPC management daemon (envelope-protocol over AF_UNIX). Defaults
    # to OFF because no in-tree daemon currently binds the configured
    # `paths.ipc_socket`; the wire types live in `teslausb-core::ipc`
    # but the server-side accept loop is a phase-7 deliverable. When
    # OFF, the System Health card reports "Disabled" instead of the
    # noisy "Daemon socket missing" red dot; when ON, a missing
    # socket is a real ERROR. Flip to true ONLY after a daemon
    # actually listens on `paths.ipc_socket`.
    ipc_daemon_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ChimesSection:
    """Lock-chime audio constraints and folder naming."""

    lock_chime_filename: str = _DEFAULT_LOCK_CHIME_FILENAME
    chimes_folder: str = _DEFAULT_CHIMES_FOLDER
    groups_file_relpath: str = _DEFAULT_GROUPS_FILE_RELPATH
    random_config_relpath: str = _DEFAULT_RANDOM_CONFIG_RELPATH
    schedules_file_relpath: str = _DEFAULT_SCHEDULES_FILE_RELPATH
    max_lock_chime_size: int = _DEFAULT_MAX_LOCK_CHIME_SIZE
    max_lock_chime_duration: int = _DEFAULT_MAX_LOCK_CHIME_DURATION
    min_lock_chime_duration: int = _DEFAULT_MIN_LOCK_CHIME_DURATION
    speed_range_min: float = _DEFAULT_SPEED_RANGE_MIN
    speed_range_max: float = _DEFAULT_SPEED_RANGE_MAX
    speed_step: float = _DEFAULT_SPEED_STEP

    def validate(self) -> None:
        if not self.lock_chime_filename:
            raise ConfigError(None, "[chimes] lock_chime_filename must be non-empty")
        if not self.lock_chime_filename.lower().endswith(".wav"):
            raise ConfigError(None, "[chimes] lock_chime_filename must end with .wav")
        _validate_relpath_filename(self.groups_file_relpath, key="groups_file_relpath")
        _validate_relpath_filename(self.random_config_relpath, key="random_config_relpath")
        _validate_relpath_filename(self.schedules_file_relpath, key="schedules_file_relpath")
        if self.max_lock_chime_size <= 0:
            raise ConfigError(None, "[chimes] max_lock_chime_size must be > 0")
        if self.min_lock_chime_duration >= self.max_lock_chime_duration:
            raise ConfigError(
                None,
                "[chimes] min_lock_chime_duration must be < max_lock_chime_duration",
            )
        if self.speed_range_min >= self.speed_range_max:
            raise ConfigError(None, "[chimes] speed_range_min must be < speed_range_max")
        if self.speed_step <= 0:
            raise ConfigError(None, "[chimes] speed_step must be > 0")


@dataclass(frozen=True, slots=True)
class LightShowsSection:
    """Light-show library paths and upload constraints."""

    folder: str = _DEFAULT_LIGHT_SHOWS_FOLDER
    active_show_relpath: str = _DEFAULT_ACTIVE_SHOW_RELPATH
    max_upload_size: int = _DEFAULT_LIGHT_SHOW_MAX_UPLOAD_SIZE
    max_zip_size: int = _DEFAULT_LIGHT_SHOW_MAX_ZIP_SIZE
    allowed_extensions: tuple[str, ...] = _DEFAULT_LIGHT_SHOW_ALLOWED_EXTENSIONS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_relpath_filename(self.folder, key="folder", section="light_shows")
        _validate_relpath_filename(
            self.active_show_relpath,
            key="active_show_relpath",
            section="light_shows",
        )
        if self.max_upload_size <= 0:
            raise ConfigError(None, "[light_shows] max_upload_size must be > 0")
        if self.max_zip_size <= 0:
            raise ConfigError(None, "[light_shows] max_zip_size must be > 0")
        if self.max_upload_size > self.max_zip_size:
            raise ConfigError(
                None,
                "[light_shows] max_upload_size must be <= max_zip_size",
            )
        if not self.allowed_extensions:
            raise ConfigError(None, "[light_shows] allowed_extensions must be non-empty")
        for extension in self.allowed_extensions:
            if not extension.strip():
                raise ConfigError(None, "[light_shows] allowed_extensions must be non-empty")
            if "/" in extension or "\\" in extension:
                raise ConfigError(
                    None,
                    "[light_shows] allowed_extensions must not contain path separators",
                )
            if not extension.startswith("."):
                raise ConfigError(
                    None,
                    "[light_shows] allowed_extensions entries must start with '.'",
                )


@dataclass(frozen=True, slots=True)
class WrapsSection:
    """Tesla custom-wrap library paths and upload constraints."""

    folder: str = _DEFAULT_WRAPS_FOLDER
    max_size: int = _DEFAULT_WRAP_MAX_SIZE
    min_dimension: int = _DEFAULT_WRAP_MIN_DIMENSION
    max_dimension: int = _DEFAULT_WRAP_MAX_DIMENSION
    max_filename_length: int = _DEFAULT_WRAP_MAX_FILENAME_LENGTH
    max_upload_count: int = _DEFAULT_WRAP_MAX_UPLOAD_COUNT
    allowed_extensions: tuple[str, ...] = _DEFAULT_WRAP_ALLOWED_EXTENSIONS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_relpath_filename(self.folder, key="folder", section="wraps")
        if self.max_size <= 0:
            raise ConfigError(None, "[wraps] max_size must be > 0")
        if self.min_dimension <= 0:
            raise ConfigError(None, "[wraps] min_dimension must be > 0")
        if self.max_dimension < self.min_dimension:
            raise ConfigError(None, "[wraps] max_dimension must be >= min_dimension")
        if self.max_filename_length <= 0:
            raise ConfigError(None, "[wraps] max_filename_length must be > 0")
        if self.max_upload_count <= 0:
            raise ConfigError(None, "[wraps] max_upload_count must be > 0")
        if not self.allowed_extensions:
            raise ConfigError(None, "[wraps] allowed_extensions must be non-empty")
        for extension in self.allowed_extensions:
            if not extension.strip():
                raise ConfigError(None, "[wraps] allowed_extensions must be non-empty")
            if "/" in extension or "\\" in extension:
                raise ConfigError(
                    None,
                    "[wraps] allowed_extensions must not contain path separators",
                )
            if not extension.startswith("."):
                raise ConfigError(
                    None,
                    "[wraps] allowed_extensions entries must start with '.'",
                )


@dataclass(frozen=True, slots=True)
class MusicSection:
    """Tesla music library paths and upload constraints."""

    folder: str = _DEFAULT_MUSIC_FOLDER
    max_file_size: int = _DEFAULT_MUSIC_MAX_FILE_SIZE
    chunk_size: int = _DEFAULT_MUSIC_CHUNK_SIZE
    free_space_reserve: int = _DEFAULT_MUSIC_FREE_SPACE_RESERVE
    stale_chunk_age: int = _DEFAULT_MUSIC_STALE_CHUNK_AGE
    allowed_extensions: tuple[str, ...] = _DEFAULT_MUSIC_ALLOWED_EXTENSIONS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_relpath_filename(self.folder, key="folder", section="music")
        if self.max_file_size <= 0:
            raise ConfigError(None, "[music] max_file_size must be > 0")
        if self.chunk_size <= 0:
            raise ConfigError(None, "[music] chunk_size must be > 0")
        if self.chunk_size > self.max_file_size:
            raise ConfigError(None, "[music] chunk_size must be <= max_file_size")
        if self.free_space_reserve < 0:
            raise ConfigError(None, "[music] free_space_reserve must be >= 0")
        if self.stale_chunk_age <= 0:
            raise ConfigError(None, "[music] stale_chunk_age must be > 0")
        if not self.allowed_extensions:
            raise ConfigError(None, "[music] allowed_extensions must be non-empty")
        for extension in self.allowed_extensions:
            if not extension.strip():
                raise ConfigError(None, "[music] allowed_extensions must be non-empty")
            if "/" in extension or "\\" in extension:
                raise ConfigError(
                    None,
                    "[music] allowed_extensions must not contain path separators",
                )
            if not extension.startswith("."):
                raise ConfigError(
                    None,
                    "[music] allowed_extensions entries must start with '.'",
                )


@dataclass(frozen=True, slots=True)
class BoomboxSection:
    """Tesla Boombox library paths and upload constraints."""

    base_dir: str = _DEFAULT_BOOMBOX_BASE_DIR
    max_file_bytes: int = _DEFAULT_BOOMBOX_MAX_FILE_BYTES
    max_files: int = _DEFAULT_BOOMBOX_MAX_FILES
    allowed_extensions: tuple[str, ...] = _DEFAULT_BOOMBOX_ALLOWED_EXTENSIONS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_relpath_filename(self.base_dir, key="base_dir", section="boombox")
        if self.max_file_bytes <= 0:
            raise ConfigError(None, "[boombox] max_file_bytes must be > 0")
        if self.max_files <= 0:
            raise ConfigError(None, "[boombox] max_files must be > 0")
        if not self.allowed_extensions:
            raise ConfigError(None, "[boombox] allowed_extensions must be non-empty")
        for extension in self.allowed_extensions:
            if not extension.strip():
                raise ConfigError(None, "[boombox] allowed_extensions must be non-empty")
            if "/" in extension or "\\" in extension:
                raise ConfigError(
                    None,
                    "[boombox] allowed_extensions must not contain path separators",
                )
            if not extension.startswith("."):
                raise ConfigError(
                    None,
                    "[boombox] allowed_extensions entries must start with '.'",
                )


@dataclass(frozen=True, slots=True)
class LicensePlateSection:
    """Tracked license-plate storage and default redaction settings."""

    db_path: Path = _DEFAULT_LICENSE_PLATES_DB_PATH
    default_redaction_enabled: bool = _DEFAULT_LICENSE_PLATES_REDACTION_ENABLED
    max_plate_length: int = _DEFAULT_LICENSE_PLATES_MAX_PLATE_LENGTH
    max_label_length: int = _DEFAULT_LICENSE_PLATES_MAX_LABEL_LENGTH
    max_notes_length: int = _DEFAULT_LICENSE_PLATES_MAX_NOTES_LENGTH

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.db_path.is_absolute()
            and not PurePosixPath(self.db_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[license_plates] db_path must be absolute, got {self.db_path!r}",
            )
        for field_name, value in (
            ("max_plate_length", self.max_plate_length),
            ("max_label_length", self.max_label_length),
            ("max_notes_length", self.max_notes_length),
        ):
            if value <= 0:
                raise ConfigError(None, f"[license_plates] {field_name} must be > 0")


@dataclass(frozen=True, slots=True)
class StorageRetentionSection:
    """Storage-retention policy persistence and default UI values."""

    policy_path: Path = _DEFAULT_RETENTION_POLICY_PATH
    default_max_age_days: int = _DEFAULT_RETENTION_MAX_AGE_DAYS
    default_target_free_pct: int = _DEFAULT_RETENTION_TARGET_FREE_PCT
    default_max_archive_size_gb: int = _DEFAULT_RETENTION_MAX_ARCHIVE_SIZE_GB
    default_short_retention_warning_days: int = _DEFAULT_RETENTION_WARNING_DAYS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.policy_path.is_absolute()
            and not PurePosixPath(self.policy_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[storage_retention] policy_path must be absolute, got {self.policy_path!r}",
            )
        if self.default_max_age_days <= 0:
            raise ConfigError(None, "[storage_retention] default_max_age_days must be > 0")
        if not (
            _DEFAULT_RETENTION_TARGET_FREE_PCT_MIN
            <= self.default_target_free_pct
            <= _DEFAULT_RETENTION_TARGET_FREE_PCT_MAX
        ):
            raise ConfigError(
                None,
                "[storage_retention] default_target_free_pct must be between 5 and 50",
            )
        if self.default_max_archive_size_gb < 0:
            raise ConfigError(
                None,
                "[storage_retention] default_max_archive_size_gb must be >= 0",
            )
        if self.default_short_retention_warning_days <= 0:
            raise ConfigError(
                None,
                "[storage_retention] default_short_retention_warning_days must be > 0",
            )


@dataclass(frozen=True, slots=True)
class CleanupSection:
    """Cleanup execution history, orphan scanning, and safety rails."""

    history_db_path: Path = _DEFAULT_CLEANUP_HISTORY_DB_PATH
    max_concurrent_runs: int = _DEFAULT_CLEANUP_MAX_CONCURRENT_RUNS
    dry_run_default: bool = _DEFAULT_CLEANUP_DRY_RUN_DEFAULT
    orphan_scan_batch_size: int = _DEFAULT_CLEANUP_ORPHAN_SCAN_BATCH_SIZE
    sample_path_limit: int = _DEFAULT_CLEANUP_SAMPLE_PATH_LIMIT
    recent_protection_hours: int = _DEFAULT_CLEANUP_RECENT_PROTECTION_HOURS
    delete_gps_tagged_clips: bool = _DEFAULT_CLEANUP_DELETE_GPS_TAGGED_CLIPS
    orphan_min_age_seconds: int = _DEFAULT_CLEANUP_ORPHAN_MIN_AGE_SECONDS
    report_limit: int = _DEFAULT_CLEANUP_REPORT_LIMIT

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.history_db_path.is_absolute()
            and not PurePosixPath(self.history_db_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[cleanup] history_db_path must be absolute, got {self.history_db_path!r}",
            )
        for key, value in (
            ("max_concurrent_runs", self.max_concurrent_runs),
            ("orphan_scan_batch_size", self.orphan_scan_batch_size),
            ("sample_path_limit", self.sample_path_limit),
            ("recent_protection_hours", self.recent_protection_hours),
            ("orphan_min_age_seconds", self.orphan_min_age_seconds),
            ("report_limit", self.report_limit),
        ):
            if value <= 0:
                raise ConfigError(None, f"[cleanup] {key} must be > 0")


@dataclass(frozen=True, slots=True)
class SystemSettingsSection:
    """Advanced-settings persistence and default expert toggles."""

    state_path: Path = _DEFAULT_SYSTEM_SETTINGS_PATH
    default_log_level: str = _DEFAULT_SYSTEM_SETTINGS_LOG_LEVEL

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.state_path.is_absolute()
            and not PurePosixPath(self.state_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[system_settings] state_path must be absolute, got {self.state_path!r}",
            )
        normalized_log_level = self.default_log_level.strip().upper()
        if normalized_log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(
                None,
                "[system_settings] default_log_level must be one of "
                "DEBUG, INFO, WARNING, ERROR, CRITICAL",
            )


@dataclass(frozen=True, slots=True)
class SambaShareConfig:
    """Declarative Samba share rooted inside the managed TeslaUSB trees."""

    name: str
    path: Path
    read_only: bool = False
    guest_ok: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError(None, "[samba.shares] name must be non-empty")
        if "/" in self.name or "\\" in self.name:
            raise ConfigError(None, "[samba.shares] name must not contain path separators")
        if not self.path.is_absolute() and not PurePosixPath(self.path.as_posix()).is_absolute():
            raise ConfigError(None, f"[samba.shares] path must be absolute, got {self.path!r}")


@dataclass(frozen=True, slots=True)
class SambaSection:
    """Samba daemon + watcher settings."""

    config_path: Path = _DEFAULT_SAMBA_CONFIG_PATH
    shares: tuple[SambaShareConfig, ...] = field(default_factory=tuple)
    binary_smbd: str = _DEFAULT_SAMBA_BINARY_SMBD
    binary_smbcontrol: str = _DEFAULT_SAMBA_BINARY_SMBCONTROL
    binary_systemctl: str = _DEFAULT_SAMBA_BINARY_SYSTEMCTL
    invalidate_debounce_ms: int = _DEFAULT_SAMBA_INVALIDATE_DEBOUNCE_MS
    watcher_poll_interval_seconds: float = _DEFAULT_SAMBA_WATCHER_POLL_INTERVAL_SECONDS
    ignore_extensions: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_SAMBA_IGNORE_EXTENSIONS
    )
    ignore_dotfiles: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.config_path.is_absolute()
            and not PurePosixPath(self.config_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[samba] config_path must be absolute, got {self.config_path!r}",
            )
        for field_name, value in (
            ("binary_smbd", self.binary_smbd),
            ("binary_smbcontrol", self.binary_smbcontrol),
            ("binary_systemctl", self.binary_systemctl),
        ):
            if not value.strip():
                raise ConfigError(None, f"[samba] {field_name} must be non-empty")
        if self.invalidate_debounce_ms < 0:
            raise ConfigError(None, "[samba] invalidate_debounce_ms must be >= 0")
        if self.watcher_poll_interval_seconds <= 0:
            raise ConfigError(None, "[samba] watcher_poll_interval_seconds must be > 0")
        for extension in self.ignore_extensions:
            if not extension.strip():
                raise ConfigError(None, "[samba] ignore_extensions entries must be non-empty")
            if "/" in extension or "\\" in extension:
                raise ConfigError(
                    None,
                    "[samba] ignore_extensions entries must not contain path separators",
                )
            if not extension.startswith("."):
                raise ConfigError(
                    None,
                    "[samba] ignore_extensions entries must start with '.'",
                )
        for share in self.shares:
            share.validate()


@dataclass(frozen=True, slots=True)
class WifiBinaryPaths:
    """Binary names or absolute paths for the Wi-Fi control surface."""

    nmcli: str = _DEFAULT_WIFI_NMCLI_BINARY
    iwlist: str = _DEFAULT_WIFI_IWLIST_BINARY
    iwconfig: str = _DEFAULT_WIFI_IWCONFIG_BINARY
    wpa_cli: str = _DEFAULT_WIFI_WPA_CLI_BINARY

    def validate(self) -> None:
        for name, value in (
            ("nmcli", self.nmcli),
            ("iwlist", self.iwlist),
            ("iwconfig", self.iwconfig),
            ("wpa_cli", self.wpa_cli),
        ):
            if not value.strip():
                raise ConfigError(None, f"[wifi.binary_paths] {name} must be non-empty")


@dataclass(frozen=True, slots=True)
class WifiSection:
    """Wi-Fi credentials, AP defaults, and binary resolution hints."""

    credentials_path: Path = _DEFAULT_WIFI_CREDENTIALS_PATH
    ap_ssid: str = _DEFAULT_WIFI_AP_SSID
    ap_passphrase: str = _DEFAULT_WIFI_AP_PASSPHRASE
    ap_idle_timeout_seconds: int = _DEFAULT_WIFI_AP_IDLE_TIMEOUT_SECONDS
    binary_paths: WifiBinaryPaths = field(default_factory=WifiBinaryPaths)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not self.credentials_path.is_absolute()
            and not PurePosixPath(self.credentials_path.as_posix()).is_absolute()
        ):
            raise ConfigError(
                None,
                f"[wifi] credentials_path must be absolute, got {self.credentials_path!r}",
            )
        if not self.ap_ssid.strip():
            raise ConfigError(None, "[wifi] ap_ssid must be non-empty")
        if self.ap_passphrase and not (
            _WIFI_PASSPHRASE_MIN_LENGTH <= len(self.ap_passphrase) <= _WIFI_PASSPHRASE_MAX_LENGTH
        ):
            raise ConfigError(
                None,
                "[wifi] ap_passphrase must be 8-63 characters or empty",
            )
        if self.ap_idle_timeout_seconds <= 0:
            raise ConfigError(None, "[wifi] ap_idle_timeout_seconds must be > 0")
        self.binary_paths.validate()


@dataclass(frozen=True, slots=True)
class CloudSection:
    """Cloud OAuth state plus cloud-archive runtime settings."""

    credentials_path: Path = _DEFAULT_CLOUD_CREDENTIALS_PATH
    oauth_state_path: Path = _DEFAULT_CLOUD_STATE_PATH
    rclone_config_path: Path = _DEFAULT_CLOUD_RCLONE_CONFIG_PATH
    rclone_log_path: Path = _DEFAULT_CLOUD_RCLONE_LOG_PATH
    refresh_window_seconds: int = _DEFAULT_CLOUD_REFRESH_WINDOW_SECONDS
    transfer_timeout_seconds: int = _DEFAULT_CLOUD_TRANSFER_TIMEOUT_SECONDS
    bwlimit_kbps: int = _DEFAULT_CLOUD_BWLIMIT_KBPS
    retries: int = _DEFAULT_CLOUD_RETRIES
    rclone_binary: str = _DEFAULT_CLOUD_RCLONE_BINARY
    db_path: Path = _DEFAULT_CLOUD_ARCHIVE_DB_PATH
    teslacam_path: Path = _DEFAULT_CLOUD_TESLACAM_PATH
    worker_idle_seconds: float = _DEFAULT_CLOUD_WORKER_IDLE_SECONDS
    backoff_initial_seconds: float = _DEFAULT_CLOUD_BACKOFF_INITIAL_SECONDS
    backoff_max_seconds: float = _DEFAULT_CLOUD_BACKOFF_MAX_SECONDS
    max_retry_attempts: int = _DEFAULT_CLOUD_MAX_RETRY_ATTEMPTS
    wifi_check_required: bool = _DEFAULT_CLOUD_WIFI_CHECK_REQUIRED
    priority_folders: tuple[str, ...] = _DEFAULT_CLOUD_PRIORITY_FOLDERS
    sync_folders: tuple[str, ...] = _DEFAULT_CLOUD_SYNC_FOLDERS
    dead_letter_max_age_days: int = _DEFAULT_CLOUD_DEAD_LETTER_MAX_AGE_DAYS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("credentials_path", self.credentials_path),
            ("oauth_state_path", self.oauth_state_path),
            ("rclone_config_path", self.rclone_config_path),
            ("rclone_log_path", self.rclone_log_path),
            ("db_path", self.db_path),
            ("teslacam_path", self.teslacam_path),
        ):
            if not value.is_absolute() and not PurePosixPath(value.as_posix()).is_absolute():
                raise ConfigError(None, f"[cloud] {name} must be absolute, got {value!r}")
        if self.refresh_window_seconds < 0:
            raise ConfigError(None, "[cloud] refresh_window_seconds must be >= 0")
        if self.transfer_timeout_seconds <= 0:
            raise ConfigError(None, "[cloud] transfer_timeout_seconds must be > 0")
        if self.bwlimit_kbps < 0:
            raise ConfigError(None, "[cloud] bwlimit_kbps must be >= 0")
        if self.retries < 0:
            raise ConfigError(None, "[cloud] retries must be >= 0")
        if not self.rclone_binary.strip():
            raise ConfigError(None, "[cloud] rclone_binary must be non-empty")
        if self.worker_idle_seconds <= 0:
            raise ConfigError(None, "[cloud] worker_idle_seconds must be > 0")
        if self.backoff_initial_seconds <= 0:
            raise ConfigError(None, "[cloud] backoff_initial_seconds must be > 0")
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            raise ConfigError(
                None,
                "[cloud] backoff_max_seconds must be >= backoff_initial_seconds",
            )
        if self.max_retry_attempts < 1:
            raise ConfigError(None, "[cloud] max_retry_attempts must be >= 1")
        if self.dead_letter_max_age_days < 1:
            raise ConfigError(None, "[cloud] dead_letter_max_age_days must be >= 1")


@dataclass(frozen=True, slots=True)
class MappingSection:
    """Mapping DB, media roots, indexing thresholds, and stale-scan cadence."""

    db_path: Path = _DEFAULT_MAPPING_DB_PATH
    backup_retention: int = _DEFAULT_MAPPING_BACKUP_RETENTION
    backup_dir: Path = _DEFAULT_MAPPING_BACKUP_DIR
    media_root: Path = _DEFAULT_MAPPING_MEDIA_ROOT
    sample_rate: int = _DEFAULT_MAPPING_SAMPLE_RATE
    trip_gap_minutes: int = _DEFAULT_MAPPING_TRIP_GAP_MINUTES
    index_too_new_seconds: int = _DEFAULT_MAPPING_INDEX_TOO_NEW_SECONDS
    harsh_brake_threshold: float = _DEFAULT_MAPPING_HARSH_BRAKE_THRESHOLD
    emergency_brake_threshold: float = _DEFAULT_MAPPING_EMERGENCY_BRAKE_THRESHOLD
    hard_accel_threshold: float = _DEFAULT_MAPPING_HARD_ACCEL_THRESHOLD
    sharp_turn_lateral_mps2: float = _DEFAULT_MAPPING_SHARP_TURN_LATERAL_MPS2
    speed_limit_mps: float = _DEFAULT_MAPPING_SPEED_LIMIT_MPS
    stale_scan_interval_seconds: int = _DEFAULT_MAPPING_STALE_SCAN_INTERVAL_SECONDS
    stale_scan_jitter_seconds: int = _DEFAULT_MAPPING_STALE_SCAN_JITTER_SECONDS
    initial_stale_scan_base_seconds: int = _DEFAULT_MAPPING_INITIAL_STALE_SCAN_BASE_SECONDS
    initial_stale_scan_jitter_seconds: int = _DEFAULT_MAPPING_INITIAL_STALE_SCAN_JITTER_SECONDS
    stale_scan_debounce_seconds: int = _DEFAULT_MAPPING_STALE_SCAN_DEBOUNCE_SECONDS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("db_path", self.db_path),
            ("backup_dir", self.backup_dir),
            ("media_root", self.media_root),
        ):
            if not value.is_absolute() and not PurePosixPath(value.as_posix()).is_absolute():
                raise ConfigError(None, f"[mapping] {name} must be absolute, got {value!r}")
        if self.backup_retention <= 0:
            raise ConfigError(None, "[mapping] backup_retention must be > 0")
        if self.sample_rate <= 0:
            raise ConfigError(None, "[mapping] sample_rate must be > 0")
        if self.trip_gap_minutes <= 0:
            raise ConfigError(None, "[mapping] trip_gap_minutes must be > 0")
        if self.index_too_new_seconds <= 0:
            raise ConfigError(None, "[mapping] index_too_new_seconds must be > 0")
        if self.speed_limit_mps < 0:
            raise ConfigError(None, "[mapping] speed_limit_mps must be >= 0")
        for int_name, int_value in (
            ("stale_scan_interval_seconds", self.stale_scan_interval_seconds),
            ("stale_scan_jitter_seconds", self.stale_scan_jitter_seconds),
            ("initial_stale_scan_base_seconds", self.initial_stale_scan_base_seconds),
            ("initial_stale_scan_jitter_seconds", self.initial_stale_scan_jitter_seconds),
            ("stale_scan_debounce_seconds", self.stale_scan_debounce_seconds),
        ):
            if int_value <= 0:
                raise ConfigError(None, f"[mapping] {int_name} must be > 0")


@dataclass(frozen=True, slots=True)
class AnalyticsSection:
    """Storage-analytics dashboard thresholds and recording-estimate fallback.

    The percent-of-disk-used bands escalate the storage-health banner
    from healthy → caution → warning → critical. The theoretical record
    rate is used only when the mapping DB has no clips yet (fresh
    install).
    """

    caution_pct_used: float = _DEFAULT_ANALYTICS_CAUTION_PCT
    warning_pct_used: float = _DEFAULT_ANALYTICS_WARNING_PCT
    critical_pct_used: float = _DEFAULT_ANALYTICS_CRITICAL_PCT
    theoretical_gb_per_hour: float = _DEFAULT_ANALYTICS_THEORETICAL_GB_PER_HOUR

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        # Bands MUST stay ordered so the "first matching" classifier
        # in analytics_service can rely on simple < comparisons.
        if not 0 < self.caution_pct_used < self.warning_pct_used < self.critical_pct_used < 100:  # noqa: PLR2004
            raise ConfigError(
                None,
                "[analytics] thresholds must satisfy "
                "0 < caution_pct_used < warning_pct_used < critical_pct_used < 100",
            )
        if self.theoretical_gb_per_hour <= 0:
            raise ConfigError(None, "[analytics] theoretical_gb_per_hour must be > 0")


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Root config dataclass — what the rest of the app sees."""

    web: WebSection = field(default_factory=WebSection)
    paths: PathsSection = field(default_factory=PathsSection)
    features: FeaturesSection = field(default_factory=FeaturesSection)
    chimes: ChimesSection = field(default_factory=ChimesSection)
    light_shows: LightShowsSection = field(default_factory=LightShowsSection)
    wraps: WrapsSection = field(default_factory=WrapsSection)
    music: MusicSection = field(default_factory=MusicSection)
    boombox: BoomboxSection = field(default_factory=BoomboxSection)
    license_plates: LicensePlateSection = field(default_factory=LicensePlateSection)
    storage_retention: StorageRetentionSection = field(default_factory=StorageRetentionSection)
    cleanup: CleanupSection = field(default_factory=CleanupSection)
    system_settings: SystemSettingsSection = field(default_factory=SystemSettingsSection)
    samba: SambaSection = field(default_factory=SambaSection)
    wifi: WifiSection = field(default_factory=WifiSection)
    cloud: CloudSection = field(default_factory=CloudSection)
    mapping: MappingSection = field(default_factory=MappingSection)
    analytics: AnalyticsSection = field(default_factory=AnalyticsSection)
    source_path: Path | None = None

    def validate(self) -> None:
        """Re-anchor sub-section ConfigErrors at ``source_path``."""
        try:
            self.web.validate()
            self.paths.validate()
            self.chimes.validate()
            self.light_shows.validate()
            self.wraps.validate()
            self.music.validate()
            self.boombox.validate()
            self.license_plates.validate()
            self.storage_retention.validate()
            self.cleanup.validate()
            self.system_settings.validate()
            self.samba.validate()
            self.wifi.validate()
            self.cloud.validate()
            self.mapping.validate()
            self.analytics.validate()
        except ConfigError as exc:
            raise ConfigError(self.source_path, str(exc).split(": ", 1)[-1]) from exc


def _resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    env_value = os.environ.get(ENV_CONFIG_PATH)
    if env_value:
        return Path(env_value)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def _as_mapping(raw: object, source: Path | None) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ConfigError(source, "top-level TOML document must be a table")
    return {str(key): value for key, value in raw.items()}


def _expect_section(raw: dict[str, object], name: str, source: Path | None) -> dict[str, object]:
    if name not in raw:
        return {}
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigError(source, f"section [{name}] must be a table, got {type(section).__name__}")
    return {str(key): value for key, value in section.items()}


def _coerce_int(section: dict[str, object], key: str, default: int, source: Path | None) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(source, f"{key} must be an integer, got {type(value).__name__}")
    return value


def _coerce_bool(
    section: dict[str, object],
    key: str,
    default: bool,  # noqa: FBT001 — internal coerce helper, not a public API
    source: Path | None,
) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(source, f"{key} must be a boolean, got {type(value).__name__}")
    return value


def _coerce_str(section: dict[str, object], key: str, default: str, source: Path | None) -> str:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(source, f"{key} must be a string, got {type(value).__name__}")
    return value


def _coerce_float(
    section: dict[str, object],
    key: str,
    default: float,
    source: Path | None,
) -> float:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(source, f"{key} must be a float, got {type(value).__name__}")
    return float(value)


def _coerce_path(section: dict[str, object], key: str, default: Path, source: Path | None) -> Path:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ConfigError(source, f"{key} must be a string path, got {type(value).__name__}")
    return Path(value)


def _coerce_table_array(
    section: dict[str, object],
    key: str,
    source: Path | None,
) -> tuple[dict[str, object], ...]:
    if key not in section:
        return ()
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(source, f"{key} must be an array of tables")
    return tuple(
        {str(item_key): item_value for item_key, item_value in item.items()} for item in value
    )


def _coerce_str_tuple(
    section: dict[str, object],
    key: str,
    default: tuple[str, ...],
    source: Path | None,
) -> tuple[str, ...]:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(source, f"{key} must be an array of strings")
    return tuple(value)


def _validate_relpath_filename(value: str, *, key: str, section: str = "chimes") -> None:
    if not value.strip():
        raise ConfigError(None, f"[{section}] {key} must be non-empty")
    if "/" in value or "\\" in value:
        raise ConfigError(
            None,
            f"[{section}] {key} must not contain path separators",
        )


def _parse_config(raw: dict[str, object], source: Path | None) -> WebConfig:
    web_raw = _expect_section(raw, "web", source)
    paths_raw = _expect_section(raw, "paths", source)
    features_raw = _expect_section(raw, "features", source)
    chimes_raw = _expect_section(raw, "chimes", source)
    light_shows_raw = _expect_section(raw, "light_shows", source)
    wraps_raw = _expect_section(raw, "wraps", source)
    music_raw = _expect_section(raw, "music", source)
    boombox_raw = _expect_section(raw, "boombox", source)
    license_plates_raw = _expect_section(raw, "license_plates", source)
    storage_retention_raw = _expect_section(raw, "storage_retention", source)
    cleanup_raw = _expect_section(raw, "cleanup", source)
    system_settings_raw = _expect_section(raw, "system_settings", source)
    samba_raw = _expect_section(raw, "samba", source)
    samba_shares_raw = _coerce_table_array(samba_raw, "shares", source)
    wifi_raw = _expect_section(raw, "wifi", source)
    wifi_binary_paths_raw = _expect_section(wifi_raw, "binary_paths", source)
    cloud_raw = _expect_section(raw, "cloud", source)
    mapping_raw = _expect_section(raw, "mapping", source)
    analytics_raw = _expect_section(raw, "analytics", source)

    web = WebSection(
        host=_coerce_str(web_raw, "host", _DEFAULT_HOST, source),
        port=_coerce_int(web_raw, "port", _DEFAULT_PORT, source),
        secret_key=_coerce_str(web_raw, "secret_key", "", source),
        max_upload_mb=_coerce_int(web_raw, "max_upload_mb", _DEFAULT_MAX_UPLOAD_MB, source),
        max_chunk_mb=_coerce_int(web_raw, "max_chunk_mb", _DEFAULT_MAX_CHUNK_MB, source),
    )
    paths_section = PathsSection(
        backing_root=_coerce_path(paths_raw, "backing_root", _DEFAULT_BACKING_ROOT, source),
        state_dir=_coerce_path(paths_raw, "state_dir", _DEFAULT_STATE_DIR, source),
        db_path=_coerce_path(paths_raw, "db_path", _DEFAULT_DB_PATH, source),
        ipc_socket=_coerce_path(paths_raw, "ipc_socket", _DEFAULT_IPC_SOCKET, source),
        cache_invalidate_script=_coerce_path(
            paths_raw,
            "cache_invalidate_script",
            _DEFAULT_CACHE_SCRIPT,
            source,
        ),
    )
    features = FeaturesSection(
        music_enabled=_coerce_bool(features_raw, "music_enabled", default=False, source=source),
        boombox_enabled=_coerce_bool(
            features_raw,
            "boombox_enabled",
            default=False,
            source=source,
        ),
        samba_enabled=_coerce_bool(features_raw, "samba_enabled", default=False, source=source),
        cloud_archive_enabled=_coerce_bool(
            features_raw,
            "cloud_archive_enabled",
            default=True,
            source=source,
        ),
        ipc_daemon_enabled=_coerce_bool(
            features_raw,
            "ipc_daemon_enabled",
            default=False,
            source=source,
        ),
    )
    chimes = ChimesSection(
        lock_chime_filename=_coerce_str(
            chimes_raw,
            "lock_chime_filename",
            _DEFAULT_LOCK_CHIME_FILENAME,
            source,
        ),
        chimes_folder=_coerce_str(chimes_raw, "chimes_folder", _DEFAULT_CHIMES_FOLDER, source),
        groups_file_relpath=_coerce_str(
            chimes_raw,
            "groups_file_relpath",
            _DEFAULT_GROUPS_FILE_RELPATH,
            source,
        ),
        random_config_relpath=_coerce_str(
            chimes_raw,
            "random_config_relpath",
            _DEFAULT_RANDOM_CONFIG_RELPATH,
            source,
        ),
        schedules_file_relpath=_coerce_str(
            chimes_raw,
            "schedules_file_relpath",
            _DEFAULT_SCHEDULES_FILE_RELPATH,
            source,
        ),
        max_lock_chime_size=_coerce_int(
            chimes_raw,
            "max_lock_chime_size",
            _DEFAULT_MAX_LOCK_CHIME_SIZE,
            source,
        ),
        max_lock_chime_duration=_coerce_int(
            chimes_raw,
            "max_lock_chime_duration",
            _DEFAULT_MAX_LOCK_CHIME_DURATION,
            source,
        ),
        min_lock_chime_duration=_coerce_int(
            chimes_raw,
            "min_lock_chime_duration",
            _DEFAULT_MIN_LOCK_CHIME_DURATION,
            source,
        ),
        speed_range_min=_coerce_float(
            chimes_raw,
            "speed_range_min",
            _DEFAULT_SPEED_RANGE_MIN,
            source,
        ),
        speed_range_max=_coerce_float(
            chimes_raw,
            "speed_range_max",
            _DEFAULT_SPEED_RANGE_MAX,
            source,
        ),
        speed_step=_coerce_float(chimes_raw, "speed_step", _DEFAULT_SPEED_STEP, source),
    )
    try:
        light_shows = LightShowsSection(
            folder=_coerce_str(light_shows_raw, "folder", _DEFAULT_LIGHT_SHOWS_FOLDER, source),
            active_show_relpath=_coerce_str(
                light_shows_raw,
                "active_show_relpath",
                _DEFAULT_ACTIVE_SHOW_RELPATH,
                source,
            ),
            max_upload_size=_coerce_int(
                light_shows_raw,
                "max_upload_size",
                _DEFAULT_LIGHT_SHOW_MAX_UPLOAD_SIZE,
                source,
            ),
            max_zip_size=_coerce_int(
                light_shows_raw,
                "max_zip_size",
                _DEFAULT_LIGHT_SHOW_MAX_ZIP_SIZE,
                source,
            ),
            allowed_extensions=_coerce_str_tuple(
                light_shows_raw,
                "allowed_extensions",
                _DEFAULT_LIGHT_SHOW_ALLOWED_EXTENSIONS,
                source,
            ),
        )
        wraps = WrapsSection(
            folder=_coerce_str(wraps_raw, "folder", _DEFAULT_WRAPS_FOLDER, source),
            max_size=_coerce_int(wraps_raw, "max_size", _DEFAULT_WRAP_MAX_SIZE, source),
            min_dimension=_coerce_int(
                wraps_raw,
                "min_dimension",
                _DEFAULT_WRAP_MIN_DIMENSION,
                source,
            ),
            max_dimension=_coerce_int(
                wraps_raw,
                "max_dimension",
                _DEFAULT_WRAP_MAX_DIMENSION,
                source,
            ),
            max_filename_length=_coerce_int(
                wraps_raw,
                "max_filename_length",
                _DEFAULT_WRAP_MAX_FILENAME_LENGTH,
                source,
            ),
            max_upload_count=_coerce_int(
                wraps_raw,
                "max_upload_count",
                _DEFAULT_WRAP_MAX_UPLOAD_COUNT,
                source,
            ),
            allowed_extensions=_coerce_str_tuple(
                wraps_raw,
                "allowed_extensions",
                _DEFAULT_WRAP_ALLOWED_EXTENSIONS,
                source,
            ),
        )
        music = MusicSection(
            folder=_coerce_str(music_raw, "folder", _DEFAULT_MUSIC_FOLDER, source),
            max_file_size=_coerce_int(
                music_raw,
                "max_file_size",
                _DEFAULT_MUSIC_MAX_FILE_SIZE,
                source,
            ),
            chunk_size=_coerce_int(
                music_raw,
                "chunk_size",
                _DEFAULT_MUSIC_CHUNK_SIZE,
                source,
            ),
            free_space_reserve=_coerce_int(
                music_raw,
                "free_space_reserve",
                _DEFAULT_MUSIC_FREE_SPACE_RESERVE,
                source,
            ),
            stale_chunk_age=_coerce_int(
                music_raw,
                "stale_chunk_age",
                _DEFAULT_MUSIC_STALE_CHUNK_AGE,
                source,
            ),
            allowed_extensions=_coerce_str_tuple(
                music_raw,
                "allowed_extensions",
                _DEFAULT_MUSIC_ALLOWED_EXTENSIONS,
                source,
            ),
        )
        boombox = BoomboxSection(
            base_dir=_coerce_str(boombox_raw, "base_dir", _DEFAULT_BOOMBOX_BASE_DIR, source),
            max_file_bytes=_coerce_int(
                boombox_raw,
                "max_file_bytes",
                _DEFAULT_BOOMBOX_MAX_FILE_BYTES,
                source,
            ),
            max_files=_coerce_int(
                boombox_raw,
                "max_files",
                _DEFAULT_BOOMBOX_MAX_FILES,
                source,
            ),
            allowed_extensions=_coerce_str_tuple(
                boombox_raw,
                "allowed_extensions",
                _DEFAULT_BOOMBOX_ALLOWED_EXTENSIONS,
                source,
            ),
        )
        license_plates = LicensePlateSection(
            db_path=_coerce_path(
                license_plates_raw,
                "db_path",
                paths_section.state_dir / _DEFAULT_LICENSE_PLATES_DB_NAME,
                source,
            ),
            default_redaction_enabled=_coerce_bool(
                license_plates_raw,
                "default_redaction_enabled",
                _DEFAULT_LICENSE_PLATES_REDACTION_ENABLED,
                source,
            ),
            max_plate_length=_coerce_int(
                license_plates_raw,
                "max_plate_length",
                _DEFAULT_LICENSE_PLATES_MAX_PLATE_LENGTH,
                source,
            ),
            max_label_length=_coerce_int(
                license_plates_raw,
                "max_label_length",
                _DEFAULT_LICENSE_PLATES_MAX_LABEL_LENGTH,
                source,
            ),
            max_notes_length=_coerce_int(
                license_plates_raw,
                "max_notes_length",
                _DEFAULT_LICENSE_PLATES_MAX_NOTES_LENGTH,
                source,
            ),
        )
        storage_retention = StorageRetentionSection(
            policy_path=_coerce_path(
                storage_retention_raw,
                "policy_path",
                paths_section.state_dir / _DEFAULT_RETENTION_POLICY_FILENAME,
                source,
            ),
            default_max_age_days=_coerce_int(
                storage_retention_raw,
                "default_max_age_days",
                _DEFAULT_RETENTION_MAX_AGE_DAYS,
                source,
            ),
            default_target_free_pct=_coerce_int(
                storage_retention_raw,
                "default_target_free_pct",
                _DEFAULT_RETENTION_TARGET_FREE_PCT,
                source,
            ),
            default_max_archive_size_gb=_coerce_int(
                storage_retention_raw,
                "default_max_archive_size_gb",
                _DEFAULT_RETENTION_MAX_ARCHIVE_SIZE_GB,
                source,
            ),
            default_short_retention_warning_days=_coerce_int(
                storage_retention_raw,
                "default_short_retention_warning_days",
                _DEFAULT_RETENTION_WARNING_DAYS,
                source,
            ),
        )
        cleanup = CleanupSection(
            history_db_path=_coerce_path(
                cleanup_raw,
                "history_db_path",
                paths_section.state_dir / _DEFAULT_CLEANUP_HISTORY_DB_NAME,
                source,
            ),
            max_concurrent_runs=_coerce_int(
                cleanup_raw,
                "max_concurrent_runs",
                _DEFAULT_CLEANUP_MAX_CONCURRENT_RUNS,
                source,
            ),
            dry_run_default=_coerce_bool(
                cleanup_raw,
                "dry_run_default",
                _DEFAULT_CLEANUP_DRY_RUN_DEFAULT,
                source,
            ),
            orphan_scan_batch_size=_coerce_int(
                cleanup_raw,
                "orphan_scan_batch_size",
                _DEFAULT_CLEANUP_ORPHAN_SCAN_BATCH_SIZE,
                source,
            ),
            sample_path_limit=_coerce_int(
                cleanup_raw,
                "sample_path_limit",
                _DEFAULT_CLEANUP_SAMPLE_PATH_LIMIT,
                source,
            ),
            recent_protection_hours=_coerce_int(
                cleanup_raw,
                "recent_protection_hours",
                _DEFAULT_CLEANUP_RECENT_PROTECTION_HOURS,
                source,
            ),
            delete_gps_tagged_clips=_coerce_bool(
                cleanup_raw,
                "delete_gps_tagged_clips",
                _DEFAULT_CLEANUP_DELETE_GPS_TAGGED_CLIPS,
                source,
            ),
            orphan_min_age_seconds=_coerce_int(
                cleanup_raw,
                "orphan_min_age_seconds",
                _DEFAULT_CLEANUP_ORPHAN_MIN_AGE_SECONDS,
                source,
            ),
            report_limit=_coerce_int(
                cleanup_raw,
                "report_limit",
                _DEFAULT_CLEANUP_REPORT_LIMIT,
                source,
            ),
        )
        system_settings = SystemSettingsSection(
            state_path=_coerce_path(
                system_settings_raw,
                "state_path",
                paths_section.state_dir / _DEFAULT_SYSTEM_SETTINGS_FILENAME,
                source,
            ),
            default_log_level=_coerce_str(
                system_settings_raw,
                "default_log_level",
                _DEFAULT_SYSTEM_SETTINGS_LOG_LEVEL,
                source,
            ),
        )
        samba = SambaSection(
            config_path=_coerce_path(
                samba_raw,
                "config_path",
                _DEFAULT_SAMBA_CONFIG_PATH,
                source,
            ),
            shares=tuple(
                SambaShareConfig(
                    name=_coerce_str(share_raw, "name", "", source),
                    path=_coerce_path(share_raw, "path", paths_section.backing_root, source),
                    read_only=_coerce_bool(share_raw, "read_only", default=False, source=source),
                    guest_ok=_coerce_bool(share_raw, "guest_ok", default=False, source=source),
                )
                for share_raw in samba_shares_raw
            ),
            binary_smbd=_coerce_str(
                samba_raw,
                "binary_smbd",
                _DEFAULT_SAMBA_BINARY_SMBD,
                source,
            ),
            binary_smbcontrol=_coerce_str(
                samba_raw,
                "binary_smbcontrol",
                _DEFAULT_SAMBA_BINARY_SMBCONTROL,
                source,
            ),
            binary_systemctl=_coerce_str(
                samba_raw,
                "binary_systemctl",
                _DEFAULT_SAMBA_BINARY_SYSTEMCTL,
                source,
            ),
            invalidate_debounce_ms=_coerce_int(
                samba_raw,
                "invalidate_debounce_ms",
                _DEFAULT_SAMBA_INVALIDATE_DEBOUNCE_MS,
                source,
            ),
            watcher_poll_interval_seconds=_coerce_float(
                samba_raw,
                "watcher_poll_interval_seconds",
                _DEFAULT_SAMBA_WATCHER_POLL_INTERVAL_SECONDS,
                source,
            ),
            ignore_extensions=_coerce_str_tuple(
                samba_raw,
                "ignore_extensions",
                _DEFAULT_SAMBA_IGNORE_EXTENSIONS,
                source,
            ),
            ignore_dotfiles=_coerce_bool(
                samba_raw,
                "ignore_dotfiles",
                default=True,
                source=source,
            ),
        )
        wifi = WifiSection(
            credentials_path=_coerce_path(
                wifi_raw,
                "credentials_path",
                paths_section.state_dir / _DEFAULT_WIFI_CREDENTIALS_FILENAME,
                source,
            ),
            ap_ssid=_coerce_str(wifi_raw, "ap_ssid", _DEFAULT_WIFI_AP_SSID, source),
            ap_passphrase=_coerce_str(
                wifi_raw,
                "ap_passphrase",
                _DEFAULT_WIFI_AP_PASSPHRASE,
                source,
            ),
            ap_idle_timeout_seconds=_coerce_int(
                wifi_raw,
                "ap_idle_timeout_seconds",
                _DEFAULT_WIFI_AP_IDLE_TIMEOUT_SECONDS,
                source,
            ),
            binary_paths=WifiBinaryPaths(
                nmcli=_coerce_str(
                    wifi_binary_paths_raw,
                    "nmcli",
                    _DEFAULT_WIFI_NMCLI_BINARY,
                    source,
                ),
                iwlist=_coerce_str(
                    wifi_binary_paths_raw,
                    "iwlist",
                    _DEFAULT_WIFI_IWLIST_BINARY,
                    source,
                ),
                iwconfig=_coerce_str(
                    wifi_binary_paths_raw,
                    "iwconfig",
                    _DEFAULT_WIFI_IWCONFIG_BINARY,
                    source,
                ),
                wpa_cli=_coerce_str(
                    wifi_binary_paths_raw,
                    "wpa_cli",
                    _DEFAULT_WIFI_WPA_CLI_BINARY,
                    source,
                ),
            ),
        )
        cloud = CloudSection(
            credentials_path=_coerce_path(
                cloud_raw,
                "credentials_path",
                paths_section.state_dir / _DEFAULT_CLOUD_CREDENTIALS_FILENAME,
                source,
            ),
            oauth_state_path=_coerce_path(
                cloud_raw,
                "oauth_state_path",
                paths_section.state_dir / _DEFAULT_CLOUD_STATE_FILENAME,
                source,
            ),
            rclone_config_path=_coerce_path(
                cloud_raw,
                "rclone_config_path",
                paths_section.state_dir / _DEFAULT_CLOUD_RCLONE_CONFIG_DIRNAME,
                source,
            ),
            rclone_log_path=_coerce_path(
                cloud_raw,
                "rclone_log_path",
                paths_section.state_dir
                / _DEFAULT_CLOUD_RCLONE_CONFIG_DIRNAME
                / _DEFAULT_CLOUD_RCLONE_LOG_FILENAME,
                source,
            ),
            refresh_window_seconds=_coerce_int(
                cloud_raw,
                "refresh_window_seconds",
                _DEFAULT_CLOUD_REFRESH_WINDOW_SECONDS,
                source,
            ),
            transfer_timeout_seconds=_coerce_int(
                cloud_raw,
                "transfer_timeout_seconds",
                _DEFAULT_CLOUD_TRANSFER_TIMEOUT_SECONDS,
                source,
            ),
            bwlimit_kbps=_coerce_int(
                cloud_raw,
                "bwlimit_kbps",
                _DEFAULT_CLOUD_BWLIMIT_KBPS,
                source,
            ),
            retries=_coerce_int(
                cloud_raw,
                "retries",
                _DEFAULT_CLOUD_RETRIES,
                source,
            ),
            rclone_binary=_coerce_str(
                cloud_raw,
                "rclone_binary",
                _DEFAULT_CLOUD_RCLONE_BINARY,
                source,
            ),
            db_path=_coerce_path(
                cloud_raw,
                "db_path",
                paths_section.state_dir / _DEFAULT_CLOUD_ARCHIVE_DB_NAME,
                source,
            ),
            teslacam_path=_coerce_path(
                cloud_raw,
                "teslacam_path",
                paths_section.backing_root,
                source,
            ),
            worker_idle_seconds=_coerce_float(
                cloud_raw,
                "worker_idle_seconds",
                _DEFAULT_CLOUD_WORKER_IDLE_SECONDS,
                source,
            ),
            backoff_initial_seconds=_coerce_float(
                cloud_raw,
                "backoff_initial_seconds",
                _DEFAULT_CLOUD_BACKOFF_INITIAL_SECONDS,
                source,
            ),
            backoff_max_seconds=_coerce_float(
                cloud_raw,
                "backoff_max_seconds",
                _DEFAULT_CLOUD_BACKOFF_MAX_SECONDS,
                source,
            ),
            max_retry_attempts=_coerce_int(
                cloud_raw,
                "max_retry_attempts",
                _DEFAULT_CLOUD_MAX_RETRY_ATTEMPTS,
                source,
            ),
            wifi_check_required=_coerce_bool(
                cloud_raw,
                "wifi_check_required",
                _DEFAULT_CLOUD_WIFI_CHECK_REQUIRED,
                source,
            ),
            priority_folders=_coerce_str_tuple(
                cloud_raw,
                "priority_folders",
                _DEFAULT_CLOUD_PRIORITY_FOLDERS,
                source,
            ),
            sync_folders=_coerce_str_tuple(
                cloud_raw,
                "sync_folders",
                _DEFAULT_CLOUD_SYNC_FOLDERS,
                source,
            ),
            dead_letter_max_age_days=_coerce_int(
                cloud_raw,
                "dead_letter_max_age_days",
                _DEFAULT_CLOUD_DEAD_LETTER_MAX_AGE_DAYS,
                source,
            ),
        )
        mapping = MappingSection(
            db_path=_coerce_path(
                mapping_raw,
                "db_path",
                paths_section.state_dir / _DEFAULT_MAPPING_DB_NAME,
                source,
            ),
            backup_retention=_coerce_int(
                mapping_raw,
                "backup_retention",
                _DEFAULT_MAPPING_BACKUP_RETENTION,
                source,
            ),
            backup_dir=_coerce_path(
                mapping_raw,
                "backup_dir",
                paths_section.state_dir / _DEFAULT_MAPPING_BACKUP_DIRNAME,
                source,
            ),
            media_root=_coerce_path(
                mapping_raw,
                "media_root",
                paths_section.backing_root,
                source,
            ),
            sample_rate=_coerce_int(
                mapping_raw,
                "sample_rate",
                _DEFAULT_MAPPING_SAMPLE_RATE,
                source,
            ),
            trip_gap_minutes=_coerce_int(
                mapping_raw,
                "trip_gap_minutes",
                _DEFAULT_MAPPING_TRIP_GAP_MINUTES,
                source,
            ),
            index_too_new_seconds=_coerce_int(
                mapping_raw,
                "index_too_new_seconds",
                _DEFAULT_MAPPING_INDEX_TOO_NEW_SECONDS,
                source,
            ),
            harsh_brake_threshold=_coerce_float(
                mapping_raw,
                "harsh_brake_threshold",
                _DEFAULT_MAPPING_HARSH_BRAKE_THRESHOLD,
                source,
            ),
            emergency_brake_threshold=_coerce_float(
                mapping_raw,
                "emergency_brake_threshold",
                _DEFAULT_MAPPING_EMERGENCY_BRAKE_THRESHOLD,
                source,
            ),
            hard_accel_threshold=_coerce_float(
                mapping_raw,
                "hard_accel_threshold",
                _DEFAULT_MAPPING_HARD_ACCEL_THRESHOLD,
                source,
            ),
            sharp_turn_lateral_mps2=_coerce_float(
                mapping_raw,
                "sharp_turn_lateral_mps2",
                _DEFAULT_MAPPING_SHARP_TURN_LATERAL_MPS2,
                source,
            ),
            speed_limit_mps=_coerce_float(
                mapping_raw,
                "speed_limit_mps",
                _DEFAULT_MAPPING_SPEED_LIMIT_MPS,
                source,
            ),
            stale_scan_interval_seconds=_coerce_int(
                mapping_raw,
                "stale_scan_interval_seconds",
                _DEFAULT_MAPPING_STALE_SCAN_INTERVAL_SECONDS,
                source,
            ),
            stale_scan_jitter_seconds=_coerce_int(
                mapping_raw,
                "stale_scan_jitter_seconds",
                _DEFAULT_MAPPING_STALE_SCAN_JITTER_SECONDS,
                source,
            ),
            initial_stale_scan_base_seconds=_coerce_int(
                mapping_raw,
                "initial_stale_scan_base_seconds",
                _DEFAULT_MAPPING_INITIAL_STALE_SCAN_BASE_SECONDS,
                source,
            ),
            initial_stale_scan_jitter_seconds=_coerce_int(
                mapping_raw,
                "initial_stale_scan_jitter_seconds",
                _DEFAULT_MAPPING_INITIAL_STALE_SCAN_JITTER_SECONDS,
                source,
            ),
            stale_scan_debounce_seconds=_coerce_int(
                mapping_raw,
                "stale_scan_debounce_seconds",
                _DEFAULT_MAPPING_STALE_SCAN_DEBOUNCE_SECONDS,
                source,
            ),
        )
    except ConfigError as exc:
        raise ConfigError(source, str(exc).split(": ", 1)[-1]) from exc
    try:
        analytics = AnalyticsSection(
            caution_pct_used=_coerce_float(
                analytics_raw,
                "caution_pct_used",
                _DEFAULT_ANALYTICS_CAUTION_PCT,
                source,
            ),
            warning_pct_used=_coerce_float(
                analytics_raw,
                "warning_pct_used",
                _DEFAULT_ANALYTICS_WARNING_PCT,
                source,
            ),
            critical_pct_used=_coerce_float(
                analytics_raw,
                "critical_pct_used",
                _DEFAULT_ANALYTICS_CRITICAL_PCT,
                source,
            ),
            theoretical_gb_per_hour=_coerce_float(
                analytics_raw,
                "theoretical_gb_per_hour",
                _DEFAULT_ANALYTICS_THEORETICAL_GB_PER_HOUR,
                source,
            ),
        )
    except ConfigError as exc:
        raise ConfigError(source, str(exc).split(": ", 1)[-1]) from exc
    cfg = WebConfig(
        web=web,
        paths=paths_section,
        features=features,
        chimes=chimes,
        light_shows=light_shows,
        wraps=wraps,
        music=music,
        boombox=boombox,
        license_plates=license_plates,
        storage_retention=storage_retention,
        cleanup=cleanup,
        system_settings=system_settings,
        samba=samba,
        wifi=wifi,
        cloud=cloud,
        mapping=mapping,
        analytics=analytics,
        source_path=source,
    )
    cfg.validate()
    return cfg


def load_config(path: Path | None = None, *, allow_defaults: bool = False) -> WebConfig:
    """Load the web-app config from TOML, or built-in defaults.

    Args:
        path: Explicit path to load. ``None`` consults
            ``TESLAUSB_WEB_CONFIG`` then ``/etc/teslausb/teslausb-web.toml``.
        allow_defaults: When ``True``, an absent config file yields
            a ``WebConfig`` populated with built-in defaults. When
            ``False`` (production), an absent file raises
            ``ConfigError`` so misdeploys fail loudly.

    Raises:
        ConfigError: when the resolved path does not exist (and
            ``allow_defaults`` is False), is not a regular file,
            cannot be parsed as TOML, contains a wrong-typed key,
            or fails validation.
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        if not allow_defaults:
            raise ConfigError(
                None,
                f"no config file found (checked ${ENV_CONFIG_PATH} and {DEFAULT_CONFIG_PATH})",
            )
        cfg = WebConfig(source_path=None)
        cfg.validate()
        return cfg

    if not resolved.is_file():
        raise ConfigError(resolved, "is not a regular file")

    try:
        with resolved.open("rb") as fh:
            raw = _as_mapping(tomllib.load(fh), resolved)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(resolved, f"TOML parse error: {exc}") from exc

    return _parse_config(raw, resolved)
