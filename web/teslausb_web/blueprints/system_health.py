"""System Health blueprint — single-poll snapshot for the Settings card.

Ports the v1 ``/api/system/health`` JSON contract so the existing
client-side card + nav-bar status dot work unchanged after the B-1
cutover. The subsystems probed here are the B-1 set:

* ``disk`` — free / total bytes on the btrfs backing root that holds
  the TeslaCam + LightShow subvolumes.
* ``daemon`` — ``teslafat`` daemon state via the Unix-socket IPC client
  built in Phase 5.5 (replaces v1's fsck/IMG widget; the corresponding
  btrfs-scrub widget will be wired in Phase 5.18 once the cleanup
  service exposes it).
* ``samba`` — whether the Samba feature is enabled in config. Full
  service-up probe lands in Phase 5.17 alongside ``samba_service``.

Design rules (from v1):

* **Cheap.** No subprocesses on the hot path. Daemon probe is one
  Unix-socket round-trip (sub-millisecond locally).
* **Fault-tolerant.** Any subsystem that raises is reported as
  ``severity: "unknown"`` with a one-line error; the page always
  renders.
* **Stable shape.** Every subsystem block has ``severity`` and
  ``message``; the dot colours itself from ``severity`` alone.
* **No identifier disclosure.** ``message`` strings stay short and
  user-facing — no absolute paths, no secret leaks (charter
  Pillar 3, mirrored from Failed Jobs page).
"""

from __future__ import annotations

import logging
import shutil
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from flask import Blueprint, current_app, jsonify

from teslausb_web.services.system_metrics import (
    collect_metrics,
    metrics_to_dict,
)
from teslausb_web.services.teslafat_client import (
    IpcDaemonError,
    IpcProtocolError,
    TeslaFatClient,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

SEV_OK: Final[str] = "ok"
SEV_WARN: Final[str] = "warn"
SEV_ERROR: Final[str] = "error"
SEV_UNKNOWN: Final[str] = "unknown"

_SEV_RANK: Final[dict[str, int]] = {
    SEV_OK: 0,
    SEV_UNKNOWN: 1,
    SEV_WARN: 2,
    SEV_ERROR: 3,
}

# Free-space thresholds. ``CRITICAL`` < ``WARNING``; both in megabytes.
# Defaults match v1's ``cloud_archive.disk_space_*`` so the System
# Health card uses identical numbers regardless of underlying worker.
_DISK_WARNING_MB: Final[int] = 500
_DISK_CRITICAL_MB: Final[int] = 100
_BYTES_PER_MIB: Final[int] = 1024 * 1024

_MESSAGE_MAX_CHARS: Final[int] = 120

system_health_bp = Blueprint("system_health", __name__)


def _truncate(message: str) -> str:
    if len(message) <= _MESSAGE_MAX_CHARS:
        return message
    return message[: _MESSAGE_MAX_CHARS - 1] + "…"


def _disk_block(cfg: WebConfig) -> dict[str, object]:
    """Free-space probe on the btrfs backing root."""
    target = cfg.paths.backing_root
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        logger.warning("disk probe failed for %s: %s", target, exc)
        return {
            "severity": SEV_UNKNOWN,
            "message": "Disk usage probe failed",
        }

    free_mb = usage.free // _BYTES_PER_MIB
    used_pct = (usage.used / usage.total * 100) if usage.total else 0.0

    if free_mb < _DISK_CRITICAL_MB:
        severity = SEV_ERROR
        message = f"Critical: only {free_mb} MB free"
    elif free_mb < _DISK_WARNING_MB:
        severity = SEV_WARN
        message = f"Low: only {free_mb} MB free"
    else:
        severity = SEV_OK
        message = f"{free_mb:,} MB free ({used_pct:.0f}% used)"

    return {
        "severity": severity,
        "message": message,
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_pct": round(used_pct, 1),
    }


def _daemon_block(cfg: WebConfig) -> dict[str, object]:
    """``teslafat`` daemon snapshot via the Unix-socket IPC client."""
    client = TeslaFatClient(cfg.paths.ipc_socket)
    try:
        body = client.status()
    except FileNotFoundError:
        return {"severity": SEV_ERROR, "message": "Daemon socket missing"}
    except (ConnectionError, TimeoutError, BlockingIOError) as exc:
        return {
            "severity": SEV_ERROR,
            "message": _truncate(f"Daemon unreachable: {exc}"),
        }
    except IpcDaemonError as exc:
        return {
            "severity": SEV_WARN,
            "message": _truncate(f"Daemon error: {exc.body.code}"),
        }
    except IpcProtocolError as exc:
        return {"severity": SEV_UNKNOWN, "message": _truncate(f"Protocol error: {exc}")}

    state = body.state
    severity_for_state: dict[str, str] = {
        "SERVING": SEV_OK,
        "INITIALIZING": SEV_WARN,
        "DRAINING": SEV_WARN,
        "STOPPED": SEV_ERROR,
    }
    severity = severity_for_state.get(state, SEV_UNKNOWN)
    message = f"{state.title()} — LUN {body.lun_id} ({body.volume_label})"
    return {
        "severity": severity,
        "message": message,
        "state": state,
        "lun_id": body.lun_id,
        "volume_label": body.volume_label,
        "uptime_seconds": body.uptime_seconds,
    }


def _samba_block(cfg: WebConfig) -> dict[str, object]:
    """Samba feature flag — full service probe lands in Phase 5.17."""
    if cfg.features.samba_enabled:
        return {"severity": SEV_OK, "message": "Enabled"}
    return {"severity": SEV_OK, "message": "Disabled"}


def _build_health(cfg: WebConfig) -> dict[str, object]:
    """Compose the full payload, isolating per-subsystem crashes."""
    blocks: tuple[tuple[str, Callable[[WebConfig], dict[str, object]]], ...] = (
        ("disk", _disk_block),
        ("daemon", _daemon_block),
        ("samba", _samba_block),
    )

    payload: dict[str, object] = {}
    worst_severity = SEV_OK
    worst_message = ""
    worst_subsystem: str | None = None

    for name, probe in blocks:
        try:
            block = probe(cfg)
        except Exception as exc:  # one bad block must not 500 the dashboard.
            logger.exception("system_health: %s block crashed", name)
            block = {
                "severity": SEV_UNKNOWN,
                "message": _truncate(f"Block error: {exc}"),
            }
        payload[name] = block

        block_severity = str(block.get("severity", SEV_UNKNOWN))
        if _SEV_RANK.get(block_severity, 0) > _SEV_RANK.get(worst_severity, 0):
            worst_severity = block_severity
            worst_message = str(block.get("message", ""))
            worst_subsystem = name

    payload["overall"] = {
        "severity": worst_severity,
        "message": (
            f"{worst_subsystem}: {worst_message}"
            if worst_severity != SEV_OK and worst_subsystem is not None
            else "All systems normal"
        ),
        "subsystem": worst_subsystem,
    }
    payload["generated_at"] = int(time.time())
    return payload


@system_health_bp.route("/api/system/health", methods=["GET"])
def api_system_health() -> ResponseReturnValue:
    """Return one JSON snapshot of every B-1 background subsystem.

    Used by the Settings system-health card and the nav-bar status
    dot. Both poll on a fixed interval, so this endpoint MUST stay
    sub-100 ms.
    """
    cfg: WebConfig = current_app.config["teslausb_config"]
    return jsonify(_build_health(cfg))


@system_health_bp.route("/api/system/metrics")
def api_system_metrics() -> ResponseReturnValue:
    """Live system metrics — backs the dashboard "Live Metrics" card.

    Cheap host counters (`psutil` + ``shutil.disk_usage`` only). The
    JS at ``templates/index.html`` polls this on a 5 s interval while
    the tab is visible. Per-subsystem fields the B-1 worker has not
    yet exposed (``task_coordinator`` / ``queues`` / ``peek_cache``)
    are omitted; the JS renders an em dash for those tiles. Wiring
    them up is tracked under Phase 6 (Rust daemon IPC).
    """
    cfg: WebConfig = current_app.config["teslausb_config"]
    metrics = collect_metrics(cfg.paths.backing_root)
    return jsonify(metrics_to_dict(metrics)), HTTPStatus.OK


@system_health_bp.route("/api/system/clear_lost_clips", methods=["POST"])
def api_clear_lost_clips() -> ResponseReturnValue:
    """Clear lost-clips counter. Stub — B-1 source_gone tracking TBD."""
    # TODO(#issue-needed): wire lost-clips counter to B-1 archive worker, Phase 5.x
    return jsonify({"success": True, "deleted": 0}), HTTPStatus.OK


__all__ = ("system_health_bp",)
