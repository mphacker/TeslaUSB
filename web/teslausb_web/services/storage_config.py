"""Unified storage + cleanup configuration (`/etc/teslausb/teslausb.toml`).

Single source of truth for:

* Partition-reported sizes (TeslaCam, Media) — operator-tunable via the
  web UI. The single `/etc/teslausb/teslafat-0.toml` DiskConfig (one disk,
  two exFAT partitions) is DERIVED from this file by the resize helper
  (`AC.3`), which rewrites the matching `[[partition]]` size.
* The safety buffer (`safety_buffer_gb`) — held back on top of the
  *measured* OS/non-partition SD usage so partition allocation can
  never starve the rootfs. Documented in `docs/06-OPERATIONS.md`;
  operators may edit by hand but the web UI enforces the same minimum.
* The auto-cleanup knobs consumed by the Rust worker
  (`teslausb-worker/src/cleanup.rs`): `target_free_pct`,
  `sentry_max_age_days`, `preserve_with_gps`.

Why a new TOML file instead of folding into `teslausb-web.toml`?
The same data must be read by BOTH the Flask process AND the Rust
worker (which already loads its own `worker.toml`). Putting it in a
dedicated file keeps each consumer's primary config small and lets
the worker reload storage settings on a `SIGHUP` without re-parsing
the entire web config.

Atomic-write contract: callers MUST go through `save()`, which
writes to a sibling `.tmp` file and `os.replace()`s it into place.
Partial writes are therefore invisible to the worker, which polls
this file.
"""

from __future__ import annotations

import logging
import threading
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH: Final[Path] = Path("/etc/teslausb/teslausb.toml")

# Safety buffer floor — held back ON TOP OF the *measured* OS/non-partition
# SD usage so the card can never physically reach 0 (which would crash the
# rootfs and stop TeslaCam writes). This is the OS-growth budget cushion:
# even after both partitions fill to their advertised caps, at least this
# many GB stay free for journal/index/swap growth. Replaces the legacy flat
# ``os_reserve_gb`` (which assumed a fixed OS footprint); the reserve is now
# ``measured_os_usage + safety_buffer_gb``. Matches ``SAFETY_BUFFER_*`` in
# ``rust/.../storage_config.rs`` and the ``teslausb-resize-lun`` helper.
SAFETY_BUFFER_MIN_GB: Final[int] = 5
SAFETY_BUFFER_DEFAULT_GB: Final[int] = 5

# LUN size bounds match the teslafat backend's accepted range
# (`setup-lib/11-gadget.sh::_b1_validate_size_gb`).
LUN_MIN_GB: Final[int] = 4
LUN_MAX_GB: Final[int] = 2048

# Free-space target bounds. 0 = auto-tune from indexer median.
TARGET_FREE_PCT_MIN: Final[int] = 0
TARGET_FREE_PCT_MAX: Final[int] = 50

# Sentry max-age bounds. 0 = unlimited (never auto-delete based on
# age alone; sentry is still last-resort fodder when A+B exhausted).
SENTRY_MAX_AGE_MIN: Final[int] = 0
SENTRY_MAX_AGE_MAX: Final[int] = 3650


class StorageConfigError(ValueError):
    """Storage configuration is invalid or could not be parsed."""


@dataclass(frozen=True, slots=True)
class StorageSection:
    """Partition sizing + safety-buffer guard.

    ``safety_buffer_gb`` is held back on top of the *measured* OS/non-
    partition SD usage; the resize cap enforces
    ``teslacam_gb + media_gb + measured_os_usage + safety_buffer_gb
    <= sd_total_gb`` so the card can never physically overfill.
    """

    safety_buffer_gb: int = SAFETY_BUFFER_DEFAULT_GB
    teslacam_gb: int = 64
    media_gb: int = 32


@dataclass(frozen=True, slots=True)
class CleanupSection:
    """Auto-cleanup knobs consumed by the Rust worker."""

    target_free_pct: int = 0
    sentry_max_age_days: int = 0
    preserve_with_gps: bool = True


@dataclass(frozen=True, slots=True)
class TeslausbConfig:
    """Top-level snapshot of `/etc/teslausb/teslausb.toml`."""

    storage: StorageSection
    cleanup: CleanupSection


_LOCK = threading.RLock()


def default_config() -> TeslausbConfig:
    """Return the built-in defaults (used when the file is absent)."""
    return TeslausbConfig(storage=StorageSection(), cleanup=CleanupSection())


def load(path: Path | None = None) -> TeslausbConfig:
    """Read and validate the config. Returns defaults if file is absent.

    Raises `StorageConfigError` on parse failure or invalid values.
    Bounds-checks every field but does NOT enforce the cross-field
    `teslacam + media + os_usage + safety_buffer <= sd_total` constraint
    — that requires knowing the SD card capacity and is checked by
    `validate_against_capacity()`.
    """
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        return default_config()

    try:
        with target.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StorageConfigError(f"failed to read {target}: {exc}") from exc

    storage = _parse_storage(payload.get("storage", {}))
    cleanup = _parse_cleanup(payload.get("cleanup", {}))
    return TeslausbConfig(storage=storage, cleanup=cleanup)


def save(config: TeslausbConfig, path: Path | None = None) -> None:
    """Atomically write `config` to `path`. Creates parent dir if missing.

    Performs the same bounds checks as `load()` so a programmatic
    caller can't write something `load()` would later reject.
    """
    _validate_storage(config.storage)
    _validate_cleanup(config.cleanup)
    target = path or DEFAULT_CONFIG_PATH
    body = _render(config)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        # Mode 0664 so the file remains group-writable for the
        # `teslausb` group after a subsequent atomic rename. The
        # web service runs as a member of that group (see setup-lib
        # `_install_storage_config_perms`); without this chmod the
        # default umask would yield 0644 and the next save() would
        # fail with EACCES.
        tmp.chmod(0o664)
        tmp.replace(target)
    logger.info("storage_config: wrote %s", target)


def validate_against_capacity(
    config: TeslausbConfig,
    sd_total_gb: int,
    os_usage_gb: int = 0,
) -> None:
    """Cross-field check: partitions + measured OS usage + safety buffer
    must fit within the SD card.

    The reserve held back is ``os_usage_gb`` (the *measured* non-partition
    SD usage: OS install, journal, swap, index, tmp) plus the configured
    ``safety_buffer_gb`` cushion. Enforcing
    ``teslacam + media + os_usage + safety_buffer <= sd_total`` guarantees
    that even if both partitions fill to their advertised caps, at least
    ``safety_buffer_gb`` stays physically free, so the card can never crash
    the rootfs.

    Raises `StorageConfigError` with a human-readable message on failure.
    `sd_total_gb=0` is treated as "unknown" and skips the check (e.g., in
    unit tests with no backing filesystem). `os_usage_gb` defaults to 0 so
    callers without a measurement still get the buffer-only floor.
    """
    if sd_total_gb <= 0:
        return
    storage = config.storage
    reserve = max(0, os_usage_gb) + storage.safety_buffer_gb
    usable = sd_total_gb - reserve
    requested = storage.teslacam_gb + storage.media_gb
    if requested > usable:
        raise StorageConfigError(
            f"teslacam_gb + media_gb = {requested} GB exceeds usable "
            f"capacity {usable} GB (sd_total={sd_total_gb} GB minus "
            f"measured OS usage {max(0, os_usage_gb)} GB minus safety "
            f"buffer {storage.safety_buffer_gb} GB)",
        )


def with_storage(
    config: TeslausbConfig,
    *,
    safety_buffer_gb: int | None = None,
    teslacam_gb: int | None = None,
    media_gb: int | None = None,
) -> TeslausbConfig:
    """Return a copy with the named storage fields replaced."""
    updated = replace(
        config.storage,
        safety_buffer_gb=(
            safety_buffer_gb if safety_buffer_gb is not None else config.storage.safety_buffer_gb
        ),
        teslacam_gb=teslacam_gb if teslacam_gb is not None else config.storage.teslacam_gb,
        media_gb=media_gb if media_gb is not None else config.storage.media_gb,
    )
    return replace(config, storage=updated)


def with_cleanup(
    config: TeslausbConfig,
    *,
    target_free_pct: int | None = None,
    sentry_max_age_days: int | None = None,
    preserve_with_gps: bool | None = None,
) -> TeslausbConfig:
    """Return a copy with the named cleanup fields replaced."""
    updated = replace(
        config.cleanup,
        target_free_pct=(
            target_free_pct if target_free_pct is not None else config.cleanup.target_free_pct
        ),
        sentry_max_age_days=(
            sentry_max_age_days
            if sentry_max_age_days is not None
            else config.cleanup.sentry_max_age_days
        ),
        preserve_with_gps=(
            preserve_with_gps if preserve_with_gps is not None else config.cleanup.preserve_with_gps
        ),
    )
    return replace(config, cleanup=updated)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _parse_storage(section: Mapping[str, object]) -> StorageSection:
    # Back-compat: a pre-existing teslausb.toml (before the safety-buffer
    # rework) carries ``os_reserve_gb`` instead of ``safety_buffer_gb``.
    # Map the legacy value onto the buffer (a larger held-back value is
    # always conservative/safe) so an old file still loads; the next save()
    # rewrites it with the new key.
    if "safety_buffer_gb" in section:
        buffer_gb = _as_int(section, "safety_buffer_gb", SAFETY_BUFFER_DEFAULT_GB)
    elif "os_reserve_gb" in section:
        buffer_gb = _as_int(section, "os_reserve_gb", SAFETY_BUFFER_DEFAULT_GB)
    else:
        buffer_gb = SAFETY_BUFFER_DEFAULT_GB
    candidate = StorageSection(
        safety_buffer_gb=buffer_gb,
        teslacam_gb=_as_int(section, "teslacam_gb", StorageSection().teslacam_gb),
        media_gb=_as_int(section, "media_gb", StorageSection().media_gb),
    )
    _validate_storage(candidate)
    return candidate


def _parse_cleanup(section: Mapping[str, object]) -> CleanupSection:
    candidate = CleanupSection(
        target_free_pct=_as_int(section, "target_free_pct", 0),
        sentry_max_age_days=_as_int(section, "sentry_max_age_days", 0),
        preserve_with_gps=_as_bool(section, "preserve_with_gps", default=True),
    )
    _validate_cleanup(candidate)
    return candidate


def _validate_storage(section: StorageSection) -> None:
    if section.safety_buffer_gb < SAFETY_BUFFER_MIN_GB:
        raise StorageConfigError(
            f"safety_buffer_gb must be >= {SAFETY_BUFFER_MIN_GB}, "
            f"got {section.safety_buffer_gb}",
        )
    _check_lun("teslacam_gb", section.teslacam_gb)
    _check_lun("media_gb", section.media_gb)


def _validate_cleanup(section: CleanupSection) -> None:
    if not TARGET_FREE_PCT_MIN <= section.target_free_pct <= TARGET_FREE_PCT_MAX:
        raise StorageConfigError(
            f"target_free_pct must be in "
            f"[{TARGET_FREE_PCT_MIN}, {TARGET_FREE_PCT_MAX}], "
            f"got {section.target_free_pct}",
        )
    if not SENTRY_MAX_AGE_MIN <= section.sentry_max_age_days <= SENTRY_MAX_AGE_MAX:
        raise StorageConfigError(
            f"sentry_max_age_days must be in "
            f"[{SENTRY_MAX_AGE_MIN}, {SENTRY_MAX_AGE_MAX}], "
            f"got {section.sentry_max_age_days}",
        )


def _check_lun(field_name: str, value: int) -> None:
    if not LUN_MIN_GB <= value <= LUN_MAX_GB:
        raise StorageConfigError(
            f"{field_name} must be in [{LUN_MIN_GB}, {LUN_MAX_GB}], got {value}",
        )


def _as_int(section: Mapping[str, object], key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageConfigError(f"{key} must be an integer, got {value!r}")
    return value


def _as_bool(section: Mapping[str, object], key: str, *, default: bool) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise StorageConfigError(f"{key} must be a boolean, got {value!r}")
    return value


def _render(config: TeslausbConfig) -> str:
    storage = config.storage
    cleanup = config.cleanup
    return (
        "# Managed by teslausb-b1 (web UI / setup.sh).\n"
        "# Documented in docs/06-OPERATIONS.md.\n"
        "\n"
        "[storage]\n"
        f"safety_buffer_gb = {storage.safety_buffer_gb}\n"
        f"teslacam_gb = {storage.teslacam_gb}\n"
        f"media_gb = {storage.media_gb}\n"
        "\n"
        "[cleanup]\n"
        f"target_free_pct = {cleanup.target_free_pct}\n"
        f"sentry_max_age_days = {cleanup.sentry_max_age_days}\n"
        f"preserve_with_gps = {'true' if cleanup.preserve_with_gps else 'false'}\n"
    )
