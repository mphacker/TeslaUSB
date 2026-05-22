"""Storage statistics + apply service (AC.4).

Bridges the Flask UI to the on-disk configuration without
needing a worker-side IPC channel:

* :func:`get_storage_stats` returns a snapshot of LUN sizing
  (from :mod:`storage_config`), per-LUN backing-root disk
  usage (via :func:`shutil.disk_usage`), and a total-SD
  capacity figure suitable for the cap calculation the web
  form enforces.

* :func:`apply_storage_config` writes a new
  :class:`~storage_config.TeslausbConfig` to disk and invokes
  the AC.3 ``teslausb-resize-lun`` helper for any LUN whose
  size actually changed. Cleanup-section changes propagate to
  the Rust worker via the next read of ``teslausb.toml``
  (the worker re-reads the file every cleanup tick).

This keeps the Python-layer storage workflow pure in terms of
files + a narrow sudoers shellout — no socket protocol, no
schema-version negotiation, no daemon-restart dance.
"""

from __future__ import annotations

import logging
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
    """Per-LUN usage snapshot.

    All bytes; UI converts to GB for display.
    """

    name: str
    backing_root: Path
    advertised_gb: int
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True, slots=True)
class StorageStats:
    """Top-level storage snapshot for the settings page."""

    teslacam: LunStats
    media: LunStats
    os_reserve_gb: int
    sd_total_gb: int
    sd_free_gb: int
    target_free_pct: int
    sentry_max_age_days: int
    preserve_with_gps: bool

    @property
    def allocated_gb(self) -> int:
        """Sum of the three reserve segments the cap enforces."""
        return (
            self.teslacam.advertised_gb
            + self.media.advertised_gb
            + self.os_reserve_gb
        )

    @property
    def remaining_alloc_gb(self) -> int:
        """How many GB the operator may still allocate."""
        return max(0, self.sd_total_gb - self.allocated_gb)


def _safe_disk_usage(path: Path) -> tuple[int, int]:
    """Return ``(used_bytes, free_bytes)`` or ``(0, 0)`` on error."""
    try:
        usage = shutil.disk_usage(path)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("storage_stats: disk_usage(%s) failed: %s", path, exc)
        return (0, 0)
    return (usage.used, usage.free)


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

    tc_used, _ = _safe_disk_usage(TESLACAM_BACKING_ROOT)
    md_used, _ = _safe_disk_usage(MEDIA_BACKING_ROOT)
    sd_total, sd_free = _sd_capacity_bytes()

    teslacam = LunStats(
        name="teslacam",
        backing_root=TESLACAM_BACKING_ROOT,
        advertised_gb=config.storage.teslacam_gb,
        used_bytes=tc_used,
        free_bytes=max(0, (config.storage.teslacam_gb * GB_BYTES) - tc_used),
    )
    media = LunStats(
        name="media",
        backing_root=MEDIA_BACKING_ROOT,
        advertised_gb=config.storage.media_gb,
        used_bytes=md_used,
        free_bytes=max(0, (config.storage.media_gb * GB_BYTES) - md_used),
    )
    return StorageStats(
        teslacam=teslacam,
        media=media,
        os_reserve_gb=config.storage.os_reserve_gb,
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
