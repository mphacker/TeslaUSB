"""Storage statistics + apply service (AC.4).

Bridges the Flask UI to the on-disk configuration without
needing a worker-side IPC channel:

* :func:`get_storage_stats` returns a snapshot of partition
  sizing (from :mod:`storage_config`), per-partition backing-root
  disk usage (via :func:`shutil.disk_usage`), and a total-SD
  capacity figure suitable for the cap calculation the web
  form enforces.

* :func:`apply_storage_config` writes a new
  :class:`~storage_config.TeslausbConfig` to disk and invokes
  the AC.3 ``teslausb-resize-lun`` helper for any partition whose
  size actually changed (the helper rewrites the matching
  ``[[partition]]`` size in the single ``teslafat-0.toml``
  DiskConfig). Cleanup-section changes propagate to the Rust
  worker via the next read of ``teslausb.toml`` (the worker
  re-reads the file every cleanup tick).

This keeps the Python-layer storage workflow pure in terms of
files + a narrow sudoers shellout — no socket protocol, no
schema-version negotiation, no daemon-restart dance.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teslausb_web.services import storage_config as sc

logger = logging.getLogger(__name__)

# 1 GB == 1024^3 bytes. Matches the GB convention used by the
# AC.3 helper (which compares ``du -sb`` bytes to GB-sized LUN
# advertised sizes).
GB_BYTES: Final[int] = 1024 * 1024 * 1024

# Backing roots that hold the LUN data. Mirrors the constants
# baked into the AC.3 helper; kept here as a separate source so
# Python-side tests don't need to shell out.
TESLACAM_BACKING_ROOT: Final[Path] = Path("/srv/teslausb/teslacam")
MEDIA_BACKING_ROOT: Final[Path] = Path("/srv/teslausb/media")
SD_ROOT: Final[Path] = Path("/srv/teslausb")

# Path to the AC.3 resize helper. Invoked via ``sudo -n``; the
# sudoers fragment installed alongside the helper limits the
# Flask user (``gadget_web``) to NOPASSWD on exactly this path.
RESIZE_HELPER: Final[Path] = Path("/usr/local/bin/teslausb-resize-lun")


@dataclass(frozen=True, slots=True)
class LunStats:
    """Per-partition usage snapshot.

    All bytes; UI converts to GB for display. ``max_allocatable_gb`` is
    the largest size the operator may set this partition to right now,
    given the other partition's advertised size, the measured OS usage,
    and the safety buffer (see :func:`get_storage_stats`).
    """

    name: str
    backing_root: Path
    advertised_gb: int
    used_bytes: int
    free_bytes: int
    max_allocatable_gb: int = 0


@dataclass(frozen=True, slots=True)
class StorageStats:
    """Top-level storage snapshot for the settings page."""

    teslacam: LunStats
    media: LunStats
    safety_buffer_gb: int
    os_usage_gb: int
    sd_total_gb: int
    sd_free_gb: int
    target_free_pct: int
    sentry_max_age_days: int
    preserve_with_gps: bool

    @property
    def reserve_gb(self) -> int:
        """Total held back from partitions: measured OS usage + buffer."""
        return self.os_usage_gb + self.safety_buffer_gb

    @property
    def allocated_gb(self) -> int:
        """Sum of the two advertised partition sizes."""
        return self.teslacam.advertised_gb + self.media.advertised_gb

    @property
    def remaining_alloc_gb(self) -> int:
        """How many GB the operator may still allocate across partitions."""
        return max(0, self.sd_total_gb - self.allocated_gb - self.reserve_gb)


def _safe_disk_usage(path: Path) -> tuple[int, int]:
    """Return ``(used_bytes, free_bytes)`` or ``(0, 0)`` on error."""
    try:
        usage = shutil.disk_usage(path)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("storage_stats: disk_usage(%s) failed: %s", path, exc)
        return (0, 0)
    return (usage.used, usage.free)


def _tree_size_bytes(root: Path) -> int:
    """Recursively sum file sizes under ``root`` (the ``du -sb`` answer).

    ``shutil.disk_usage(root)`` reports the *filesystem* used/free,
    which gives the same value for every path on the SD card —
    useless for per-LUN accounting because TeslaCam and Media share
    a backing filesystem. This walks the directory tree once.

    Symlinks are not followed (mirrors ``du -sb`` default). Files
    that vanish between ``scandir`` and ``stat`` (worker deletes
    during a sweep) are silently skipped so a transient race never
    raises into the Flask handler. Returns ``0`` if ``root`` is
    missing or unreadable.
    """
    if not root.exists():
        return 0
    total = 0
    stack: list[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.warning("storage_stats: scandir(%s) failed: %s", cur, exc)
            continue
    return total


def _max_partition_gb(
    sd_total_bytes: int,
    other_advertised_gb: int,
    os_used_bytes: int,
    buffer_bytes: int,
) -> int:
    """Largest GB this partition may advertise without overcommitting.

    Mirrors the authoritative byte-precise check in the ``teslausb-resize-lun``
    helper: ``this_cap + other_cap + os_used + buffer <= sd_total``. Floors to
    GiB (conservative) and clamps to the absolute LUN bounds. Returns 0 when
    the card is unknown so the UI never advertises a bogus max.
    """
    if sd_total_bytes <= 0:
        return 0
    avail = sd_total_bytes - (other_advertised_gb * GB_BYTES) - os_used_bytes - buffer_bytes
    max_gb = avail // GB_BYTES  # floor
    if max_gb < 0:
        return 0
    return min(int(max_gb), sc.LUN_MAX_GB)


def get_storage_stats(
    config: sc.TeslausbConfig | None = None,
    *,
    config_path: Path | None = None,
) -> StorageStats:
    """Snapshot the current storage configuration + usage.

    :param config: pre-loaded config. If absent the file at
        ``config_path`` (or :data:`storage_config.DEFAULT_CONFIG_PATH`)
        is loaded fresh.
    :param config_path: override for the config path. Ignored
        when ``config`` is provided.
    """
    if config is None:
        path = config_path or sc.DEFAULT_CONFIG_PATH
        config = sc.load(path)

    tc_used = _tree_size_bytes(TESLACAM_BACKING_ROOT)
    md_used = _tree_size_bytes(MEDIA_BACKING_ROOT)
    sd_total, sd_free = _sd_capacity_bytes()

    # measured OS/non-partition usage = everything physically on the card
    # that isn't the two backing trees (OS, journal, swap, index, tmp).
    os_used_bytes = max(0, sd_total - sd_free - tc_used - md_used)
    buffer_gb = config.storage.safety_buffer_gb
    buffer_bytes = buffer_gb * GB_BYTES

    teslacam = LunStats(
        name="teslacam",
        backing_root=TESLACAM_BACKING_ROOT,
        advertised_gb=config.storage.teslacam_gb,
        used_bytes=tc_used,
        free_bytes=max(0, (config.storage.teslacam_gb * GB_BYTES) - tc_used),
        max_allocatable_gb=_max_partition_gb(
            sd_total, config.storage.media_gb, os_used_bytes, buffer_bytes
        ),
    )
    media = LunStats(
        name="media",
        backing_root=MEDIA_BACKING_ROOT,
        advertised_gb=config.storage.media_gb,
        used_bytes=md_used,
        free_bytes=max(0, (config.storage.media_gb * GB_BYTES) - md_used),
        max_allocatable_gb=_max_partition_gb(
            sd_total, config.storage.teslacam_gb, os_used_bytes, buffer_bytes
        ),
    )
    return StorageStats(
        teslacam=teslacam,
        media=media,
        safety_buffer_gb=buffer_gb,
        os_usage_gb=(os_used_bytes + GB_BYTES - 1) // GB_BYTES,  # ceil (conservative)
        sd_total_gb=sd_total // GB_BYTES,
        sd_free_gb=sd_free // GB_BYTES,
        target_free_pct=config.cleanup.target_free_pct,
        sentry_max_age_days=config.cleanup.sentry_max_age_days,
        preserve_with_gps=config.cleanup.preserve_with_gps,
    )


def _sd_capacity_bytes() -> tuple[int, int]:
    """``(total_bytes, free_bytes)`` for the SD-card filesystem."""
    try:
        usage = shutil.disk_usage(SD_ROOT)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("storage_stats: disk_usage(%s) failed: %s", SD_ROOT, exc)
        return (0, 0)
    return (usage.total, usage.free)


class ApplyError(RuntimeError):
    """Raised when :func:`apply_storage_config` fails.

    Distinct from :class:`storage_config.StorageConfigError` so
    callers can show "save succeeded, resize failed" partial-state
    diagnostics.
    """


def apply_storage_config(
    new_config: sc.TeslausbConfig,
    *,
    config_path: Path | None = None,
    helper: Path = RESIZE_HELPER,
    use_sudo: bool = True,
) -> list[str]:
    """Persist ``new_config`` and run the resize helper as needed.

    Returns a list of human-readable status messages describing
    what was changed (empty when nothing changed). Raises
    :class:`ApplyError` if the helper exits non-zero on either
    LUN; the config is written first, so a helper failure leaves
    the file mutated and the operator can retry from the UI.
    """
    path = config_path or sc.DEFAULT_CONFIG_PATH

    old_config = sc.load(path) if path.exists() else sc.default_config()

    # Validate the no-overcommit cap against MEASURED usage before writing,
    # so an over-cap submission is rejected cleanly (the resize helper
    # re-checks authoritatively at apply time, but rejecting here avoids a
    # half-applied config file).
    tc_used = _tree_size_bytes(TESLACAM_BACKING_ROOT)
    md_used = _tree_size_bytes(MEDIA_BACKING_ROOT)
    sd_total, sd_free = _sd_capacity_bytes()
    os_used_bytes = max(0, sd_total - sd_free - tc_used - md_used)
    sc.validate_against_capacity(
        new_config,
        sd_total // GB_BYTES,
        (os_used_bytes + GB_BYTES - 1) // GB_BYTES,  # ceil (conservative)
    )

    # Shrink guard, mirroring the authoritative check in the resize helper:
    # refuse to persist a partition size below the data it currently stores.
    # Done BEFORE save() so a size the helper would reject never leaves the
    # on-disk config diverged from the live geometry.
    if new_config.storage.teslacam_gb * GB_BYTES < tc_used:
        raise sc.StorageConfigError(
            f"teslacam_gb={new_config.storage.teslacam_gb} GB is smaller than "
            f"current TeslaCam usage ({(tc_used + GB_BYTES - 1) // GB_BYTES} GB)",
        )
    if new_config.storage.media_gb * GB_BYTES < md_used:
        raise sc.StorageConfigError(
            f"media_gb={new_config.storage.media_gb} GB is smaller than "
            f"current Media usage ({(md_used + GB_BYTES - 1) // GB_BYTES} GB)",
        )

    # save() runs the same bounds checks load() does, so we can
    # rely on it to raise on invalid input.
    sc.save(new_config, path)

    messages: list[str] = []

    if new_config.storage.teslacam_gb != old_config.storage.teslacam_gb:
        messages.append(
            _invoke_helper(
                helper,
                "teslacam",
                new_config.storage.teslacam_gb,
                use_sudo=use_sudo,
            )
        )
    if new_config.storage.media_gb != old_config.storage.media_gb:
        messages.append(
            _invoke_helper(
                helper,
                "media",
                new_config.storage.media_gb,
                use_sudo=use_sudo,
            )
        )
    if new_config.cleanup != old_config.cleanup:
        messages.append(
            "cleanup knobs updated; Rust worker will re-read on next tick"
        )

    return messages


def _invoke_helper(
    helper: Path, lun: str, size_gb: int, *, use_sudo: bool
) -> str:
    cmd: list[str] = []
    if use_sudo:
        cmd.extend(["sudo", "-n"])
    cmd.extend([str(helper), "--lun", lun, "--size-gb", str(size_gb)])
    try:
        result = subprocess.run(  # noqa: S603 — fixed arg list
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplyError(f"resize-lun {lun}={size_gb}: {exc}") from exc
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or "(no output)"
        raise ApplyError(
            f"resize-lun {lun}={size_gb} exited {result.returncode}: {msg}"
        )
    return f"resize {lun} -> {size_gb} GB: " + (result.stdout.strip() or "ok")
