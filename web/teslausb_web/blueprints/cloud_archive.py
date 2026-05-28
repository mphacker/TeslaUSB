"""Cloud-archive blueprint for the B-1 web UI."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from teslausb_web.services.cloud_archive import (
    CloudArchiveConfigError,
    CloudArchiveDBError,
    CloudArchiveError,
    CloudArchiveQueries,
    CloudArchiveService,
    SyncHistoryEntry,
    SyncStats,
    SyncStatus,
    make_cloud_archive_queries,
)
from teslausb_web.services.cloud_archive.paths import VALID_SYNC_FOLDERS
from teslausb_web.services.cloud_archive.settings import (
    BWLIMIT_KBPS_MAX,
    BWLIMIT_KBPS_MIN,
    CLOUD_RESERVE_GB_MAX,
    CLOUD_RESERVE_GB_MIN,
    _read_auto_sync_enabled_setting,
    _read_bwlimit_kbps_setting,
    _read_cloud_auto_cleanup_setting,
    _read_cloud_reserve_gb_setting,
    _read_keep_clips_until_synced_setting,
    _read_priority_order_setting,
    _read_remote_path_setting,
    _read_retry_max_attempts_setting,
    _read_sync_folders_setting,
    _read_sync_recent_with_telemetry_setting,
)
from teslausb_web.services.cloud_oauth_service import (
    CloudOAuthService,
    DisconnectResult,
    OAuthCredentials,
    OAuthError,
)
from teslausb_web.services.cloud_generic_remote_service import (
    GenericRemoteError,
    GenericRemoteService,
)
from teslausb_web.services.cloud_rclone_service import (
    CloudRcloneService,
    RcloneConfigError,
    RcloneError,
    RcloneListing,
    RcloneStats,
    RcloneTransferProgress,
    RcloneTransferResult,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

cloud_archive_bp = Blueprint("cloud_archive", __name__, url_prefix="/cloud")

_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_DEFAULT_HISTORY_LIMIT: Final[int] = 20
_DEFAULT_DEAD_LETTER_LIMIT: Final[int] = 100
_DEFAULT_STATUS_CACHE_TTL_SECONDS: Final[float] = 10.0
_DEFAULT_REMOTE_PATH: Final[str] = ""
_ALLOWED_EVENT_FOLDERS: Final[frozenset[str]] = frozenset(
    {"RecentClips", "SavedClips", "SentryClips"}
)
_OAUTH_PROVIDER_SESSION_KEY: Final[str] = "cloud_archive_provider"
_OAUTH_SESSION_ID_SESSION_KEY: Final[str] = "cloud_archive_oauth_session_id"

_stats_cache: dict[str, object] = {"stats": None, "timestamp": 0.0}


class CloudArchiveRequestError(ValueError):
    """Raised when a cloud archive request is invalid."""


class CloudArchiveNotFoundError(FileNotFoundError):
    """Raised when a requested local event path does not exist."""


@dataclass(frozen=True, slots=True)
class _CloudArchivePageContext:
    page: str
    cloud_archive_available: bool
    sync_status: dict[str, object]
    sync_stats: dict[str, object]
    sync_history: list[dict[str, object]]
    provider: str
    provider_connected: bool
    token_expiry: str | None
    sync_enabled: bool
    sync_folders: tuple[str, ...]
    priority_order: tuple[str, ...]
    max_upload_mbps: int
    remote_path: str
    cloud_reserve_gb: float
    cloud_auto_cleanup: bool
    cloud_retry_max_attempts: int
    keep_clips_until_synced: bool
    kept_unsynced_count: int
    sync_recent_with_telemetry: bool
    providers: tuple[str, ...]
    pending_authorization: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class _ConnectionStatusDto:
    connected: bool
    provider: str | None
    token_expiry: str | None
    remote: dict[str, object] | None
    pending_authorization: dict[str, object] | None
    supported_providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BrowseResponseDto:
    success: bool
    path: str
    folders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ArchiveStatusDto:
    running: bool
    progress: dict[str, object] | None


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _invalidate_caches(app: Flask) -> None:
    invalidator = app.extensions.get("cache_invalidator")
    if invalidator is not None:
        invalidator.schedule()


def _get_oauth_service() -> CloudOAuthService:
    service = current_app.extensions["cloud_oauth_service"]
    if not isinstance(service, CloudOAuthService):
        raise RuntimeError("cloud_oauth_service extension is not configured")
    return service


def _get_rclone_service() -> CloudRcloneService:
    service = current_app.extensions["cloud_rclone_service"]
    if not isinstance(service, CloudRcloneService):
        raise RuntimeError("cloud_rclone_service extension is not configured")
    return service


def _get_generic_remote_service() -> GenericRemoteService:
    service = current_app.extensions.get("cloud_generic_remote_service")
    if not isinstance(service, GenericRemoteService):
        raise RuntimeError("cloud_generic_remote_service extension is not configured")
    return service


def _get_archive_service() -> CloudArchiveService:
    service = current_app.extensions["cloud_archive_service"]
    if not isinstance(service, CloudArchiveService):
        raise RuntimeError("cloud_archive_service extension is not configured")
    return service


def _get_queries() -> CloudArchiveQueries:
    return make_cloud_archive_queries(_cfg())


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE or request.is_json


def _json_payload() -> dict[str, object]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def _request_str(*names: str) -> str:
    payload = _json_payload()
    for name in names:
        value = payload.get(name)
        if value is not None:
            return str(value)
    for name in names:
        value = request.form.get(name)
        if value is not None:
            return value
    for name in names:
        value = request.args.get(name)
        if value is not None:
            return value
    return ""


def _request_bool(name: str, *, default: bool = False) -> bool:
    payload = _json_payload()
    if name in payload:
        value = payload[name]
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if name in request.form:
        return request.form.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
    return default


def _coerce_limit(value: str, *, default: int, cap: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CloudArchiveRequestError("limit must be an integer") from exc
    if parsed <= 0:
        return default
    return min(parsed, cap)


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_message_payload(*, success: bool, message: str, **fields: object) -> Response:
    return jsonify({"success": success, "message": message, **fields})


def _json_ready(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    return value


def _redirect_to_cloud_archive(*, cache_bust: str | None = None) -> Response:
    if cache_bust is None:
        return cast("Response", redirect(url_for("cloud_archive.index")))
    return cast("Response", redirect(url_for("cloud_archive.index", _=cache_bust)))


def _cloud_response(
    *,
    success: bool,
    message: str,
    status: HTTPStatus,
    **fields: object,
) -> ResponseReturnValue:
    if _wants_json_response():
        return _json_message_payload(success=success, message=message, **fields), status
    flash(message, "success" if success else "error")
    return _redirect_to_cloud_archive(cache_bust=request.args.get("_", "0"))


def _handle_request_error(exc: Exception) -> ResponseReturnValue:
    return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST


def _handle_not_found(exc: Exception) -> ResponseReturnValue:
    return _json_error_payload(str(exc)), HTTPStatus.NOT_FOUND


def _handle_service_error(exc: Exception) -> ResponseReturnValue:
    logger.warning("cloud archive route failed: %s", exc)
    return _json_error_payload(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR


def _clear_status_cache() -> None:
    _stats_cache["stats"] = None
    _stats_cache["timestamp"] = 0.0


def _cached_sync_stats(service: CloudArchiveService) -> SyncStats:
    cached = _stats_cache.get("stats")
    timestamp = _stats_cache.get("timestamp")
    now = time.monotonic()
    if (
        isinstance(cached, SyncStats)
        and isinstance(timestamp, float)
        and now - timestamp <= _DEFAULT_STATUS_CACHE_TTL_SECONDS
    ):
        return cached
    fresh = service.get_sync_stats()
    _stats_cache["stats"] = fresh
    _stats_cache["timestamp"] = now
    return fresh


def _empty_sync_status() -> SyncStatus:
    return SyncStatus(
        running=False,
        progress="",
        files_total=0,
        files_done=0,
        bytes_transferred=0,
        total_bytes=0,
        current_file="",
        current_file_size=0,
        started_at=None,
        last_run=None,
        error=None,
        worker_running=False,
        wake_count=0,
        drain_count=0,
        eta_seconds=None,
        throughput_bps=None,
    )


def _empty_sync_stats() -> SyncStats:
    return SyncStats(
        total_synced=0,
        total_pending=0,
        total_failed=0,
        total_dead_letter=0,
        total_bytes=0,
        stats_baseline_at=None,
    )


def _provider_from_request(oauth_service: CloudOAuthService) -> str:
    provider = _request_str("provider").strip()
    if not provider:
        stored = session.get(_OAUTH_PROVIDER_SESSION_KEY)
        provider = stored if isinstance(stored, str) else ""
    if not provider:
        raise CloudArchiveRequestError("Missing provider.")
    if provider not in oauth_service.supported_providers():
        raise CloudArchiveRequestError(f"Unknown provider: {provider}")
    return provider


def _safe_segment(value: str, *, field_name: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise CloudArchiveRequestError(f"{field_name} is required")
    if Path(candidate).name != candidate or candidate in {".", ".."}:
        raise CloudArchiveRequestError(f"Invalid {field_name}")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_event_path(folder: str, event_name: str) -> Path:
    safe_folder = _safe_segment(folder, field_name="folder")
    if safe_folder not in _ALLOWED_EVENT_FOLDERS:
        raise CloudArchiveRequestError("Invalid folder")
    safe_event = _safe_segment(event_name, field_name="event")
    teslacam_root = _cfg().cloud.teslacam_path.resolve(strict=False)
    candidate = (teslacam_root / safe_folder / safe_event).resolve(strict=False)
    if not _is_relative_to(candidate, teslacam_root):
        raise CloudArchiveRequestError("Invalid event path")
    if not candidate.exists():
        raise CloudArchiveNotFoundError("TeslaCam event not found")
    return candidate


def _as_remote_path(folder: str, event_name: str) -> str:
    safe_folder = _safe_segment(folder, field_name="folder")
    safe_event = _safe_segment(event_name, field_name="event")
    return f"{safe_folder}/{safe_event}"


def _pending_authorization_dict(oauth_service: CloudOAuthService) -> dict[str, object] | None:
    pending = oauth_service.get_pending_authorization()
    if pending is None:
        return None
    return asdict(pending)


def _connection_status_dto() -> _ConnectionStatusDto:
    oauth_service = _get_oauth_service()
    rclone_service = _get_rclone_service()
    credentials = oauth_service.load_credentials()
    generic_record: dict[str, str] | None = None
    try:
        generic_record = _get_generic_remote_service().load()
    except RuntimeError:
        generic_record = None
    remote: dict[str, object] | None = None
    has_remote = credentials is not None or generic_record is not None
    if has_remote:
        try:
            remote = asdict(rclone_service.render_config())
        except (OAuthError, RcloneError, RuntimeError) as exc:
            logger.debug("connection_status remote render skipped: %s", exc)
    provider_label: str | None = None
    if credentials is not None:
        provider_label = credentials.provider
    elif generic_record is not None:
        provider_label = f"generic:{generic_record.get('type', '')}"
    return _ConnectionStatusDto(
        connected=has_remote,
        provider=provider_label,
        token_expiry=None if credentials is None else credentials.expires_at,
        remote=remote,
        pending_authorization=_pending_authorization_dict(oauth_service),
        supported_providers=oauth_service.supported_providers(),
    )


def _browse_response_dto(listing: RcloneListing) -> _BrowseResponseDto:
    folders = tuple(entry.name for entry in listing.entries if entry.is_dir)
    return _BrowseResponseDto(success=True, path=listing.path, folders=folders)


def _archive_status_dto(progress: RcloneTransferProgress | None) -> _ArchiveStatusDto:
    return _ArchiveStatusDto(
        running=progress is not None,
        progress=None if progress is None else asdict(progress),
    )


def _page_context() -> _CloudArchivePageContext:
    oauth_service = _get_oauth_service()
    archive_service = _get_archive_service()
    credentials: OAuthCredentials | None = None
    generic_record: dict[str, str] | None = None
    sync_status = _empty_sync_status()
    sync_stats = _empty_sync_stats()
    sync_history: tuple[SyncHistoryEntry, ...] = ()
    live_sync_folders: tuple[str, ...] = tuple(archive_service.config.sync_folders)
    live_priority: tuple[str, ...] = tuple(archive_service.config.priority_folders)
    live_retry_max: int = archive_service.config.max_retry_attempts
    live_recent_telemetry: bool = archive_service.config.sync_recent_with_telemetry
    live_bwlimit_kbps: int = getattr(archive_service.config, "bwlimit_kbps", 0)
    live_cloud_reserve_gb: float = archive_service.config.cloud_reserve_gb
    live_cloud_auto_cleanup: bool = archive_service.config.cloud_auto_cleanup
    live_keep_clips_until_synced: bool = archive_service.config.keep_clips_until_synced
    live_auto_sync_enabled: bool = archive_service.is_auto_sync_enabled()
    live_remote_path: str = ""
    try:
        credentials = oauth_service.load_credentials()
        try:
            generic_record = _get_generic_remote_service().load()
        except RuntimeError:
            generic_record = None
        sync_status = archive_service.get_sync_status()
        sync_stats = archive_service.get_sync_stats()
        sync_history = _get_queries().get_sync_history(_DEFAULT_HISTORY_LIMIT)
        with archive_service.open_db() as connection:
            live_sync_folders = _read_sync_folders_setting(
                archive_service.config, connection
            )
            live_priority = _read_priority_order_setting(
                archive_service.config, connection
            )
            live_retry_max = _read_retry_max_attempts_setting(
                archive_service.config, connection
            )
            live_recent_telemetry = _read_sync_recent_with_telemetry_setting(
                archive_service.config, connection
            )
            live_bwlimit_kbps = _read_bwlimit_kbps_setting(
                archive_service.config, connection
            )
            live_cloud_reserve_gb = _read_cloud_reserve_gb_setting(
                archive_service.config, connection
            )
            live_cloud_auto_cleanup = _read_cloud_auto_cleanup_setting(
                archive_service.config, connection
            )
            live_keep_clips_until_synced = _read_keep_clips_until_synced_setting(
                archive_service.config, connection
            )
            live_auto_sync_enabled = _read_auto_sync_enabled_setting(
                archive_service.config, connection
            )
            live_remote_path = _read_remote_path_setting(
                archive_service.config, connection
            )
    except (
        CloudArchiveConfigError,
        CloudArchiveDBError,
        CloudArchiveError,
        OAuthError,
        RuntimeError,
        ValueError,
    ) as exc:
        logger.warning("cloud archive index bootstrap degraded: %s", exc)
    cfg = _cfg()
    return _CloudArchivePageContext(
        page="cloud",
        cloud_archive_available=cfg.features.cloud_archive_enabled,
        sync_status=asdict(sync_status),
        sync_stats=asdict(sync_stats),
        sync_history=[asdict(entry) for entry in sync_history],
        provider=(
            ""
            if credentials is None and generic_record is None
            else (
                credentials.provider
                if credentials is not None
                else f"generic:{generic_record.get('type', '') if generic_record else ''}"
            )
        ),
        provider_connected=credentials is not None or generic_record is not None,
        token_expiry=None if credentials is None else credentials.expires_at,
        sync_enabled=live_auto_sync_enabled,
        sync_folders=tuple(live_sync_folders),
        priority_order=tuple(live_priority),
        max_upload_mbps=max(0, live_bwlimit_kbps // 1024),
        remote_path=live_remote_path,
        cloud_reserve_gb=live_cloud_reserve_gb,
        cloud_auto_cleanup=live_cloud_auto_cleanup,
        cloud_retry_max_attempts=live_retry_max,
        keep_clips_until_synced=live_keep_clips_until_synced,
        kept_unsynced_count=0,
        sync_recent_with_telemetry=live_recent_telemetry,
        providers=oauth_service.supported_providers(),
        pending_authorization=_pending_authorization_dict(oauth_service),
    )


@cloud_archive_bp.before_request
def _require_cloud_archive_enabled() -> ResponseReturnValue | None:
    if _cfg().features.cloud_archive_enabled:
        return None
    message = "Cloud archive not enabled"
    if request.path.startswith("/cloud/api/") or _wants_json_response():
        return _json_error_payload(message), HTTPStatus.SERVICE_UNAVAILABLE
    return message, HTTPStatus.SERVICE_UNAVAILABLE


@cloud_archive_bp.route("/")
def index() -> ResponseReturnValue:
    return render_template("cloud_archive.html", **asdict(_page_context()))


@cloud_archive_bp.route("/settings", methods=["POST"])
def save_settings() -> ResponseReturnValue:
    archive_service = _get_archive_service()

    raw_folders = request.form.getlist("sync_folders")
    selected_folders: list[str] = []
    for value in raw_folders:
        folder = (value or "").strip()
        if folder and folder in VALID_SYNC_FOLDERS and folder not in selected_folders:
            selected_folders.append(folder)

    raw_priority = (request.form.get("priority_order") or "").strip()
    priority_folders: list[str] = []
    if raw_priority:
        for value in raw_priority.split(","):
            folder = value.strip()
            if folder and folder in VALID_SYNC_FOLDERS and folder not in priority_folders:
                priority_folders.append(folder)

    sync_recent_with_telemetry = bool(request.form.get("sync_recent_with_telemetry"))

    retry_raw = (request.form.get("cloud_retry_max_attempts") or "").strip()
    retry_max_attempts: int | None = None
    if retry_raw:
        try:
            retry_max_attempts = int(retry_raw)
        except ValueError:
            flash("Retry attempts must be a whole number.", "error")
            return redirect(url_for("cloud_archive.index"))

    bw_raw = (request.form.get("max_upload_mbps") or "").strip()
    bwlimit_kbps: int | None = None
    if bw_raw:
        try:
            bw_mbps = int(bw_raw)
        except ValueError:
            flash("Bandwidth limit must be a whole number.", "error")
            return redirect(url_for("cloud_archive.index"))
        bwlimit_kbps = max(BWLIMIT_KBPS_MIN, min(BWLIMIT_KBPS_MAX, bw_mbps * 1024))

    reserve_raw = (request.form.get("cloud_reserve_gb") or "").strip()
    cloud_reserve_gb: float | None = None
    if reserve_raw:
        try:
            cloud_reserve_gb = float(reserve_raw)
        except ValueError:
            flash("Cloud reserve must be a number.", "error")
            return redirect(url_for("cloud_archive.index"))
        if not CLOUD_RESERVE_GB_MIN <= cloud_reserve_gb <= CLOUD_RESERVE_GB_MAX:
            flash(
                f"Cloud reserve must be between {CLOUD_RESERVE_GB_MIN:g} "
                f"and {CLOUD_RESERVE_GB_MAX:g} GB.",
                "error",
            )
            return redirect(url_for("cloud_archive.index"))

    cloud_auto_cleanup = bool(request.form.get("cloud_auto_cleanup"))
    keep_clips_until_synced = bool(request.form.get("keep_clips_until_synced"))

    if sync_recent_with_telemetry and "RecentClips" not in selected_folders:
        selected_folders.append("RecentClips")

    try:
        archive_service.update_settings(
            sync_folders=tuple(selected_folders),
            priority_folders=tuple(priority_folders) if priority_folders else None,
            sync_non_event=False,
            sync_recent_with_telemetry=sync_recent_with_telemetry,
            max_retry_attempts=retry_max_attempts,
            bwlimit_kbps=bwlimit_kbps,
            cloud_reserve_gb=cloud_reserve_gb,
            cloud_auto_cleanup=cloud_auto_cleanup,
            keep_clips_until_synced=keep_clips_until_synced,
        )
    except CloudArchiveConfigError as exc:
        flash(f"Could not save settings: {exc}", "error")
        return redirect(url_for("cloud_archive.index"))
    except (CloudArchiveDBError, CloudArchiveError) as exc:
        logger.warning("cloud archive settings persistence failed: %s", exc)
        flash("Could not save settings — see logs for details.", "error")
        return redirect(url_for("cloud_archive.index"))

    archive_service.wake()
    flash("Cloud sync settings saved.", "success")
    return redirect(url_for("cloud_archive.index"))


@cloud_archive_bp.route("/api/sync_now", methods=["POST"])
def api_sync_now() -> ResponseReturnValue:
    try:
        ok, message = _get_archive_service().start_sync(trigger="manual")
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if ok:
        _invalidate_caches(current_app)
    _clear_status_cache()
    return jsonify({"success": ok, "message": message})


@cloud_archive_bp.route("/api/wake", methods=["POST"])
def api_wake() -> ResponseReturnValue:
    try:
        service = _get_archive_service()
        service.wake()
        status = service.get_sync_status()
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify(
        {
            "success": True,
            "enabled": True,
            "worker_running": status.worker_running,
            "wake_count": status.wake_count,
            "drain_running": status.running,
        }
    )


@cloud_archive_bp.route("/api/sync_stop", methods=["POST"])
def api_sync_stop() -> ResponseReturnValue:
    try:
        ok, message = _get_archive_service().stop_sync()
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if ok:
        _invalidate_caches(current_app)
    return jsonify({"success": ok, "message": message})


@cloud_archive_bp.route("/api/status")
def api_status() -> ResponseReturnValue:
    try:
        service = _get_archive_service()
        status = service.get_sync_status()
        stats = _cached_sync_stats(service)
        shadow = service.get_cloud_shadow_telemetry()
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify({"status": asdict(status), "stats": asdict(stats), "shadow": asdict(shadow)})


@cloud_archive_bp.route("/api/history")
def api_history() -> ResponseReturnValue:
    try:
        history = _get_queries().get_sync_history(
            _coerce_limit(_request_str("limit"), default=_DEFAULT_HISTORY_LIMIT, cap=500)
        )
    except (CloudArchiveRequestError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveDBError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify({"history": [asdict(entry) for entry in history]})


@cloud_archive_bp.route("/api/reset_stats", methods=["POST"])
def api_reset_stats() -> ResponseReturnValue:
    try:
        ok, baseline = _get_archive_service().reset_stats_baseline()
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if not ok:
        return jsonify({"success": False, "message": baseline}), HTTPStatus.INTERNAL_SERVER_ERROR
    _clear_status_cache()
    _invalidate_caches(current_app)
    return jsonify({"success": True, "stats_baseline_at": baseline})


@cloud_archive_bp.route("/api/provider", methods=["POST"])
def api_save_provider() -> ResponseReturnValue:
    try:
        provider = _provider_from_request(_get_oauth_service())
    except CloudArchiveRequestError as exc:
        return _handle_request_error(exc)
    session[_OAUTH_PROVIDER_SESSION_KEY] = provider
    _invalidate_caches(current_app)
    return jsonify({"success": True})


def _complete_connect_provider(
    oauth_service: CloudOAuthService,
    *,
    session_id: str,
    redirect_payload: str,
) -> ResponseReturnValue:
    try:
        credentials = oauth_service.exchange_code(session_id, redirect_payload)
    except (CloudArchiveRequestError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RuntimeError) as exc:
        return _handle_service_error(exc)
    session.pop(_OAUTH_SESSION_ID_SESSION_KEY, None)
    session[_OAUTH_PROVIDER_SESSION_KEY] = credentials.provider
    _invalidate_caches(current_app)
    return jsonify(
        {
            "success": True,
            "message": "Connected successfully.",
            "provider": credentials.provider,
            "token_expiry": credentials.expires_at,
        }
    )


@cloud_archive_bp.route("/api/connect", methods=["POST"])
def api_connect_provider() -> ResponseReturnValue:
    oauth_service = _get_oauth_service()
    session_id = _request_str("session_id")
    redirect_payload = _request_str("redirect_url", "callback_url", "code")
    if session_id and redirect_payload:
        return _complete_connect_provider(
            oauth_service,
            session_id=session_id,
            redirect_payload=redirect_payload,
        )
    # Generic (non-OAuth) rclone remote — S3 / B2 / Wasabi / SFTP /
    # WebDAV / SMB / FTP / Azure Blob / Swift. The UI sends one of:
    #   {provider:'generic', rclone_type:..., fields:{...}, obscure_keys:[...]}
    #   {provider:'generic', config_block:'[my-nas]\ntype=sftp\n...'}
    # Either shape persists to cloud_generic_remote.json; the rclone
    # service then renders the conf body from that record at use time.
    payload = _json_payload()
    provider_field = str(payload.get("provider") or "").strip().lower()
    if provider_field == "generic":
        return _connect_generic_provider(payload)
    # Operator pasted the JSON blob from `rclone authorize "<provider>"`.
    # This is the documented "How to connect" flow in the UI — before
    # this branch existed the request silently fell through to
    # start_authorization, which returned {success:true,
    # authorization_url:...}; the JS only checked .success and reported
    # "Connected!", but nothing was actually persisted.
    rclone_token = _request_str("token", "rclone_token")
    if rclone_token:
        try:
            provider = _provider_from_request(oauth_service)
            credentials = oauth_service.import_rclone_token(provider, rclone_token)
        except CloudArchiveRequestError as exc:
            return _handle_request_error(exc)
        except (OAuthError, RuntimeError) as exc:
            return _handle_service_error(exc)
        # Importing OAuth credentials replaces any prior generic remote;
        # the device can only target one cloud destination at a time.
        try:
            _get_generic_remote_service().clear()
        except GenericRemoteError as exc:
            logger.debug("could not clear stale generic remote: %s", exc)
        session[_OAUTH_PROVIDER_SESSION_KEY] = credentials.provider
        session.pop(_OAUTH_SESSION_ID_SESSION_KEY, None)
        _invalidate_caches(current_app)
        return jsonify(
            {
                "success": True,
                "message": "Connected successfully.",
                "provider": credentials.provider,
                "token_expiry": credentials.expires_at,
            }
        )
    try:
        provider = _provider_from_request(oauth_service)
        started = oauth_service.start_authorization(provider)
    except CloudArchiveRequestError as exc:
        return _handle_request_error(exc)
    except (OAuthError, RuntimeError) as exc:
        return _handle_service_error(exc)
    session[_OAUTH_PROVIDER_SESSION_KEY] = provider
    session[_OAUTH_SESSION_ID_SESSION_KEY] = started.session_id
    _invalidate_caches(current_app)
    if _wants_json_response():
        return jsonify({"success": True, **asdict(started)})
    return redirect(started.authorization_url)


def _connect_generic_provider(payload: dict[str, object]) -> ResponseReturnValue:
    """Persist a generic-rclone connection from either UI shape.

    Form shape: ``{provider:'generic', rclone_type:'s3', fields:{...},
    obscure_keys:[...]}``. Paste shape: ``{provider:'generic',
    config_block:'[my-nas]\\ntype=sftp\\n...'}``. On success the device
    is treated as connected (``provider_connected=True``); the OAuth
    credentials file is cleared so the rclone service uses the new
    generic remote.
    """
    generic_service = _get_generic_remote_service()
    config_block = str(payload.get("config_block") or "").strip()
    rclone_type = str(payload.get("rclone_type") or "").strip().lower()
    try:
        if config_block:
            record = generic_service.import_config_block(config_block, source="paste")
        elif rclone_type:
            raw_fields = payload.get("fields")
            if not isinstance(raw_fields, dict):
                raise GenericRemoteError("fields must be a JSON object")
            obscure_value = payload.get("obscure_keys")
            obscure_keys: list[str] | None
            if obscure_value is None:
                obscure_keys = None
            elif isinstance(obscure_value, list):
                obscure_keys = [str(entry) for entry in obscure_value]
            else:
                raise GenericRemoteError("obscure_keys must be a JSON array")
            record = generic_service.import_form(
                rclone_type,
                {str(k): v for k, v in raw_fields.items()},
                obscure_keys=obscure_keys,
                source="form",
            )
        else:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": (
                            "Provide either a config_block or a rclone_type "
                            "with fields for a generic remote."
                        ),
                    }
                ),
                HTTPStatus.BAD_REQUEST,
            )
    except GenericRemoteError as exc:
        return jsonify({"success": False, "message": str(exc)}), HTTPStatus.BAD_REQUEST
    # A new generic remote replaces any prior OAuth credentials so the
    # rclone service always uses the most recently configured backend.
    try:
        _get_oauth_service().disconnect()
    except (OAuthError, RuntimeError) as exc:
        logger.debug("could not clear stale OAuth credentials: %s", exc)
    session[_OAUTH_PROVIDER_SESSION_KEY] = f"generic:{record.get('type', '')}"
    session.pop(_OAUTH_SESSION_ID_SESSION_KEY, None)
    _invalidate_caches(current_app)
    return jsonify(
        {
            "success": True,
            "message": "Connected successfully.",
            "provider": f"generic:{record.get('type', '')}",
            "rclone_type": record.get("type", ""),
        }
    )


@cloud_archive_bp.route("/api/disconnect", methods=["POST"])
def api_disconnect_provider() -> ResponseReturnValue:
    try:
        provider = _request_str("provider") or None
        result: DisconnectResult = _get_oauth_service().disconnect(provider=provider)
    except (OAuthError, RuntimeError) as exc:
        return _handle_service_error(exc)
    # Also clear any generic-remote record so the device is fully
    # disconnected. Disconnect is a single "forget everything" action.
    try:
        _get_generic_remote_service().clear()
    except (GenericRemoteError, RuntimeError) as exc:
        logger.debug("could not clear generic remote on disconnect: %s", exc)
    session.pop(_OAUTH_PROVIDER_SESSION_KEY, None)
    session.pop(_OAUTH_SESSION_ID_SESSION_KEY, None)
    _invalidate_caches(current_app)
    return jsonify(
        {
            "success": result.disconnected,
            "message": result.message,
            "revoked": result.revoked,
        }
    )


@cloud_archive_bp.route("/api/test_connection", methods=["POST"])
def api_test_connection() -> ResponseReturnValue:
    try:
        remotes = _get_rclone_service().list_remotes()
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if remotes:
        return jsonify({"success": True, "message": "Connected successfully."})
    return jsonify({"success": False, "message": "No configured remote."}), HTTPStatus.BAD_REQUEST


@cloud_archive_bp.route("/api/connection_status")
def api_connection_status() -> ResponseReturnValue:
    try:
        status = _connection_status_dto()
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveError, OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify(_json_ready(asdict(status)))


@cloud_archive_bp.route("/api/storage_usage")
def api_storage_usage() -> ResponseReturnValue:
    try:
        stats: RcloneStats = _get_rclone_service().get_stats(_request_str("path"))
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        logger.debug("cloud storage_usage unavailable: %s", exc)
        return jsonify({"available": False, "reason": str(exc)})
    payload = _json_ready(asdict(stats))
    if isinstance(payload, dict):
        payload["available"] = True
    return jsonify(payload)


@cloud_archive_bp.route("/api/browse")
def api_browse_folders() -> ResponseReturnValue:
    path = _request_str("path")
    try:
        listing = _get_rclone_service().list_directory(path)
        payload = _browse_response_dto(listing)
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify(asdict(payload))


@cloud_archive_bp.route("/api/mkdir", methods=["POST"])
def api_create_folder() -> ResponseReturnValue:
    path = _request_str("path")
    if not path:
        return jsonify({"success": False, "message": "Missing path."}), HTTPStatus.BAD_REQUEST
    try:
        _get_rclone_service().mkdir(path)
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify({"success": True, "message": f"Created {path}", "path": path})


@cloud_archive_bp.route("/api/set_remote_path", methods=["POST"])
def api_set_remote_path() -> ResponseReturnValue:
    raw_path = _request_str("path")
    cleaned = raw_path.strip().replace("\\", "/").strip("/")
    if not cleaned and raw_path.strip():
        return (
            jsonify({"success": False, "message": "Invalid path."}),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        _get_archive_service().update_settings(remote_path=cleaned)
    except CloudArchiveConfigError as exc:
        return _handle_request_error(exc)
    except (CloudArchiveDBError, CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify({"success": True, "message": "Remote folder updated.", "path": cleaned})


@cloud_archive_bp.route("/api/toggle_sync", methods=["POST"])
def api_toggle_sync() -> ResponseReturnValue:
    payload = _json_payload()
    raw = payload.get("enabled") if isinstance(payload, dict) else None
    if raw is None:
        raw = request.form.get("enabled")
    if isinstance(raw, bool):
        enabled = raw
    elif isinstance(raw, str):
        enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
    elif isinstance(raw, (int, float)):
        enabled = bool(raw)
    else:
        return (
            jsonify({"success": False, "message": "Missing 'enabled' flag."}),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        service = _get_archive_service()
        service.update_settings(enabled=enabled)
    except CloudArchiveConfigError as exc:
        return _handle_request_error(exc)
    except (CloudArchiveDBError, CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if enabled:
        # Wake the worker so it can start a drain immediately rather than
        # waiting for the polling-loop timeout.
        service.wake()
    _invalidate_caches(current_app)
    return jsonify({"success": True, "enabled": enabled})


@cloud_archive_bp.route("/api/sync_status_batch", methods=["POST"])
def api_sync_status_batch() -> ResponseReturnValue:
    payload = _json_payload()
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return jsonify({"statuses": {}})
    try:
        statuses = _get_queries().get_sync_status_for_events([str(item) for item in raw_events])
    except (CloudArchiveDBError, RuntimeError) as exc:
        logger.warning("cloud sync batch status failed: %s", exc)
        return jsonify({"statuses": {}})
    return jsonify({"statuses": statuses})


@cloud_archive_bp.route("/api/queue_event", methods=["POST"])
def api_queue_event() -> ResponseReturnValue:
    folder = _request_str("folder")
    event_name = _request_str("event")
    if not folder or not event_name:
        return (
            jsonify({"success": False, "message": "Missing folder or event"}),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        ok, message = _get_archive_service().queue_event_for_sync(
            folder,
            event_name,
            priority=_request_bool("priority"),
        )
    except (CloudArchiveConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if ok:
        _invalidate_caches(current_app)
        _clear_status_cache()
    return jsonify({"success": ok, "message": message})


@cloud_archive_bp.route("/api/queue")
def api_queue() -> ResponseReturnValue:
    try:
        queue = _get_queries().get_sync_queue()
    except (CloudArchiveDBError, RuntimeError) as exc:
        logger.warning("cloud queue fetch failed: %s", exc)
        return jsonify({"queue": [], "error": str(exc)})
    return jsonify({"queue": [asdict(item) for item in queue]})


@cloud_archive_bp.route("/api/queue/remove", methods=["POST"])
def api_queue_remove() -> ResponseReturnValue:
    file_path = _request_str("file_path")
    if not file_path:
        return jsonify({"success": False, "message": "No file path"})
    try:
        ok, message = _get_archive_service().remove_from_queue(file_path)
    except (CloudArchiveConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if ok:
        _invalidate_caches(current_app)
        _clear_status_cache()
    return jsonify({"success": ok, "message": message})


@cloud_archive_bp.route("/api/queue/clear", methods=["POST"])
def api_queue_clear() -> ResponseReturnValue:
    try:
        ok, message = _get_archive_service().clear_queue()
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    if ok:
        _invalidate_caches(current_app)
        _clear_status_cache()
    return jsonify({"success": ok, "message": message})


@cloud_archive_bp.route("/api/dead_letters")
def api_dead_letters() -> ResponseReturnValue:
    try:
        limit = _coerce_limit(_request_str("limit"), default=_DEFAULT_DEAD_LETTER_LIMIT, cap=500)
        queries = _get_queries()
        dead_letters = queries.list_dead_letters(limit)
        count = queries.count_dead_letters()
    except (CloudArchiveRequestError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveDBError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify({"dead_letters": [asdict(item) for item in dead_letters], "count": count})


@cloud_archive_bp.route("/api/dead_letters/retry", methods=["POST"])
def api_dead_letters_retry() -> ResponseReturnValue:
    try:
        count = _get_archive_service().retry_dead_letter(_request_str("file_path") or None)
    except (CloudArchiveConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    _clear_status_cache()
    return jsonify({"success": True, "count": count})


@cloud_archive_bp.route("/api/dead_letters/delete", methods=["POST", "DELETE"])
def api_dead_letters_delete() -> ResponseReturnValue:
    try:
        count = _get_archive_service().delete_dead_letter(_request_str("file_path") or None)
    except (CloudArchiveConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    _clear_status_cache()
    return jsonify({"success": True, "count": count})


@cloud_archive_bp.route("/api/archive_cleanup", methods=["POST"])
def api_archive_cleanup() -> ResponseReturnValue:
    from teslausb_web.services.cloud_archive.cloud_cleanup import run_cloud_cleanup

    try:
        result = run_cloud_cleanup(_get_archive_service())
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify(
        {
            "success": True,
            "triggered": result.triggered,
            "deleted_count": result.deleted_count,
            "bytes_freed": result.bytes_freed,
            "reason": result.reason,
        }
    )


@cloud_archive_bp.route("/api/archive_file", methods=["POST"])
def api_archive_file() -> ResponseReturnValue:
    folder = _request_str("folder")
    event_name = _request_str("event")
    if not folder or not event_name:
        return (
            jsonify({"success": False, "message": "Missing folder or event."}),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        local_path = _resolve_event_path(folder, event_name)
        result: RcloneTransferResult = _get_rclone_service().transfer(
            local_path,
            _as_remote_path(folder, event_name),
            operation="copy",
        )
    except CloudArchiveRequestError as exc:
        return _handle_request_error(exc)
    except CloudArchiveNotFoundError as exc:
        return _handle_not_found(exc)
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify({"success": True, "message": f"Archived to {result.destination}"})


@cloud_archive_bp.route("/api/archive_status")
def api_archive_status() -> ResponseReturnValue:
    return jsonify(asdict(_archive_status_dto(_get_rclone_service().current_progress())))


@cloud_archive_bp.route("/api/archive_cancel", methods=["POST"])
def api_archive_cancel() -> ResponseReturnValue:
    cancelled = _get_rclone_service().cancel_active_transfer()
    if cancelled:
        _invalidate_caches(current_app)
    return jsonify(
        {
            "success": cancelled,
            "message": "Archive cancelled" if cancelled else "No archive in progress",
        }
    )


@cloud_archive_bp.route("/api/bandwidth_test", methods=["POST"])
def api_bandwidth_test() -> ResponseReturnValue:
    payload = _json_payload() if request.is_json else {}
    raw_mb = payload.get("megabytes") if isinstance(payload, dict) else None
    try:
        megabytes = float(raw_mb) if raw_mb is not None else 10.0
    except (TypeError, ValueError):
        megabytes = 10.0
    megabytes = max(1.0, min(megabytes, 50.0))
    sample_bytes = int(megabytes * 1024 * 1024)
    try:
        measurement = _get_rclone_service().measure_upload_throughput(
            sample_bytes=sample_bytes
        )
    except (RcloneConfigError, ValueError) as exc:
        return _handle_request_error(exc)
    except (OAuthError, RcloneError, RuntimeError) as exc:
        return _handle_service_error(exc)
    return jsonify({"success": True, **measurement})


@cloud_archive_bp.route("/api/bandwidth_test/status")
def api_bandwidth_test_status() -> ResponseReturnValue:
    return jsonify({"running": False, "supported": True})


@cloud_archive_bp.route("/api/bandwidth_test/apply", methods=["POST"])
def api_bandwidth_test_apply() -> ResponseReturnValue:
    payload = _json_payload()
    raw = payload.get("bwlimit_kbps") if isinstance(payload, dict) else None
    if raw is None:
        raw = payload.get("kbps") if isinstance(payload, dict) else None
    try:
        kbps = int(raw)
    except (TypeError, ValueError):
        return (
            jsonify({"success": False, "message": "Missing or invalid bwlimit_kbps."}),
            HTTPStatus.BAD_REQUEST,
        )
    try:
        _get_archive_service().update_settings(bwlimit_kbps=kbps)
    except CloudArchiveConfigError as exc:
        return _handle_request_error(exc)
    except (CloudArchiveDBError, CloudArchiveError, RuntimeError) as exc:
        return _handle_service_error(exc)
    _invalidate_caches(current_app)
    return jsonify({"success": True, "bwlimit_kbps": kbps})


__all__ = ("cloud_archive_bp",)
