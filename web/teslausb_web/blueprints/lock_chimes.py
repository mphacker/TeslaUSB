"""Lock-chime blueprint.

Ports v1's lock-chime Flask blueprint onto the B-1 service layer.
The HTML template itself lands in Phase 5.8e; this module only wires
HTTP routes, service calls, JSON contracts, and cache invalidation.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from datetime import UTC, datetime
from enum import Enum
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.datastructures import FileStorage, ImmutableMultiDict

from teslausb_web.services.cache_invalidation import CacheInvalidator
from teslausb_web.services.chime_group_service import (
    ChimeGroup,
    ChimeGroupError,
    ChimeGroupManager,
    ChimeGroupStateError,
    RandomConfig,
    make_chime_group_manager,
)
from teslausb_web.services.chime_scheduler import (
    ChimeScheduleError,
    ChimeScheduler,
    ChimeScheduleStateError,
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    WeeklySchedule,
    format_last_run,
    format_schedule_display,
    make_chime_scheduler,
)
from teslausb_web.services.gadget_rebind import GadgetRebinder
from teslausb_web.services.lock_chime_service import (
    ChimeInfo,
    LockChimeAudioError,
    LockChimeFileError,
    delete_chime_file,
    list_chime_files,
    normalize_audio,
    reencode_wav_for_tesla,
    save_pretrimmed_wav,
    set_active_chime,
    upload_chime_file,
    validate_tesla_wav,
)

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

lock_chimes_bp = Blueprint("lock_chimes", __name__, url_prefix="/lock_chimes")


class ChimeRefresh(Enum):
    """How to make Tesla notice a media change after a chime mutation.

    ``REBIND`` triggers a full USB gadget re-enumeration — required when
    the active ``LockChime.wav`` itself changed, because Tesla caches the
    chime and only re-reads it on a simulated unplug/replug.
    ``SOFT_INVALIDATE`` schedules the cheaper SCSI medium-change, which is
    enough for directory-listing changes the car re-scans on its own.
    """

    REBIND = "rebind"
    SOFT_INVALIDATE = "soft_invalidate"

    @classmethod
    def for_active_change(cls, *, changed_active: bool) -> ChimeRefresh:
        """Pick the strategy for a mutation that may have changed the active chime."""
        return cls.REBIND if changed_active else cls.SOFT_INVALIDATE


_ALLOWED_UPLOAD_EXTENSIONS: Final[frozenset[str]] = frozenset({".mp3", ".wav"})
_ALLOWED_BULK_EXTENSIONS: Final[frozenset[str]] = frozenset({".wav"})
_ALLOWED_LUFS_PRESETS: Final[frozenset[int]] = frozenset({-23, -16, -14, -12})
_AUDIO_MIMETYPE: Final[str] = "audio/wav"
_BYTES_PER_KIB: Final[int] = 1024
_HOURS_PER_HALF_DAY: Final[int] = 12
_LIGHTSHOW_DIRNAME: Final[str] = "lightshow"
_RANDOM_DISABLED_MESSAGE: Final[str] = "Random mode disabled"
_RANDOM_ENABLED_MESSAGE: Final[str] = "Random mode enabled"
_TIME_DEFAULT: Final[str] = "00:00"
_XHR_HEADER_VALUE: Final[str] = "XMLHttpRequest"
_ZIP_SUFFIX: Final[str] = ".zip"


class _RouteError(ValueError):
    """Typed client-visible error raised by blueprint helpers."""

    def __init__(self, message: str, status: HTTPStatus) -> None:
        super().__init__(message)
        self.status = status


class _PathBackedUpload:
    """Minimal FileStorage-like wrapper around a prepared on-disk file."""

    def __init__(self, filename: str, source_path: Path) -> None:
        self.filename: str | None = filename
        self._source_path = source_path

    def save(self, dst: str | Path) -> None:
        shutil.copyfile(self._source_path, Path(dst))

    def read(self, size: int = -1) -> bytes:
        payload = self._source_path.read_bytes()
        if size < 0:
            return payload
        return payload[:size]


def _cfg() -> WebConfig:
    return cast("WebConfig", current_app.config["teslausb_config"])


def _lightshow_dir() -> Path:
    return _cfg().paths.media_root


def _chimes_dir() -> Path:
    cfg = _cfg()
    return _lightshow_dir() / cfg.chimes.chimes_folder


def _active_chime_path() -> Path:
    cfg = _cfg()
    return _lightshow_dir() / cfg.chimes.lock_chime_filename


def _scratch_dir() -> Path:
    return _cfg().paths.state_dir / ".lock_chimes_tmp"


def _is_xhr() -> bool:
    return request.headers.get("X-Requested-With") == _XHR_HEADER_VALUE


def _get_cache_invalidator() -> CacheInvalidator:
    raw = current_app.extensions["cache_invalidator"]
    if not isinstance(raw, CacheInvalidator):
        raise RuntimeError("cache_invalidator extension is not configured")
    return raw


def _get_gadget_rebinder() -> GadgetRebinder:
    raw = current_app.extensions["gadget_rebinder"]
    if not isinstance(raw, GadgetRebinder):
        raise RuntimeError("gadget_rebinder extension is not configured")
    return raw


def _refresh_for_active_chime() -> None:
    """Make Tesla re-read a just-changed ``LockChime.wav``.

    Activating (or clearing) the active chime changes the file at the
    root of the MEDIA LUN. Unlike directory listings, Tesla caches the
    lock chime and only re-reads it after a full USB *re-enumeration*, so
    a soft SCSI medium-change is not enough — we must rebind the gadget
    (simulated unplug/replug). This is synchronous so the user gets a
    definitive result. If the rebind fails for any reason we fall back to
    the weaker soft cache-invalidation rather than silently doing nothing.
    """
    result = _get_gadget_rebinder().rebind()
    if result.ok:
        return
    logger.warning(
        "gadget rebind failed (rc=%d); falling back to soft cache invalidation: %s",
        result.returncode,
        result.stderr.strip(),
    )
    _get_cache_invalidator().schedule()


def _get_group_manager() -> ChimeGroupManager:
    raw = current_app.extensions.get("chime_group_manager")
    if isinstance(raw, ChimeGroupManager):
        return raw
    manager = make_chime_group_manager(_cfg())
    current_app.extensions["chime_group_manager"] = manager
    return manager


def _get_scheduler() -> ChimeScheduler:
    raw = current_app.extensions.get("chime_scheduler")
    if isinstance(raw, ChimeScheduler):
        return raw
    scheduler = make_chime_scheduler(_cfg())
    current_app.extensions["chime_scheduler"] = scheduler
    return scheduler


def _json_error_payload(message: str) -> Response:
    return jsonify({"success": False, "error": message})


def _json_success_payload(**fields: object) -> Response:
    return jsonify({"success": True, **fields})


def _flash_or_json_error(message: str, status: HTTPStatus) -> ResponseReturnValue:
    logger.warning("lock_chimes request rejected: %s", message)
    if _is_xhr():
        return _json_error_payload(message), status.value
    flash(message, "error")
    return redirect(url_for("lock_chimes.lock_chimes"))


def _flash_or_json_success(
    message: str, *, refresh: ChimeRefresh = ChimeRefresh.SOFT_INVALIDATE, **fields: object
) -> ResponseReturnValue:
    if refresh is ChimeRefresh.REBIND:
        _refresh_for_active_chime()
    else:
        _get_cache_invalidator().schedule()
    if _is_xhr():
        return _json_success_payload(message=message, **fields), HTTPStatus.OK.value
    flash(message, "success")
    return redirect(url_for("lock_chimes.lock_chimes"))


def _json_mutation_success(message: str, **fields: object) -> ResponseReturnValue:
    _get_cache_invalidator().schedule()
    return _json_success_payload(message=message, **fields), HTTPStatus.OK.value


def _bool_from_value(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _safe_wav_name(filename: str) -> str:
    stripped = filename.strip()
    if not stripped:
        raise _RouteError("Chime filename is required", HTTPStatus.BAD_REQUEST)
    if Path(stripped).name != stripped or any(
        token in stripped for token in ("/", "\\", "..", "\x00")
    ):
        raise _RouteError(f"Invalid chime filename: {filename!r}", HTTPStatus.BAD_REQUEST)
    if Path(stripped).suffix.lower() != ".wav":
        raise _RouteError("Chime filenames must end with .wav", HTTPStatus.BAD_REQUEST)
    return stripped


def _status_for_service_error(message: str) -> HTTPStatus:
    lowered = message.casefold()
    if "not found" in lowered:
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.BAD_REQUEST


def _trimmed_text(raw: object) -> str:
    return "" if raw is None else str(raw).strip()


def _request_value(name: str) -> object:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload.get(name)
    return request.form.get(name)


def _group_error_status(message: str) -> HTTPStatus:
    if "not found" in message.casefold():
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.BAD_REQUEST


def _format_size_bytes(size_bytes: int) -> str:
    if size_bytes < _BYTES_PER_KIB:
        return f"{size_bytes} B"
    kib = size_bytes / _BYTES_PER_KIB
    if kib < _BYTES_PER_KIB:
        return f"{kib:.1f} KB"
    return f"{kib / _BYTES_PER_KIB:.2f} MB"


def _serialize_chime(info: ChimeInfo) -> dict[str, object]:
    return {
        "filename": info.name,
        "size": info.size_bytes,
        "size_str": _format_size_bytes(info.size_bytes),
        "mtime": info.mtime_iso,
        "md5": info.md5,
        "is_active": info.is_active,
        "is_valid": True,
        "validation_msg": "",
    }


def _serialize_group(group: ChimeGroup) -> dict[str, object]:
    chimes = list(group.chime_filenames)
    return {
        "id": group.id,
        "name": group.name,
        "description": "",
        "chime_filenames": chimes,
        "chimes": chimes,
        "chime_count": len(chimes),
        "created_at": group.created_at.isoformat(),
        "updated_at": group.updated_at.isoformat(),
    }


def _serialize_random_config(config: RandomConfig) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "group_id": config.group_id,
        "last_selected": config.last_selected,
        "last_selected_at": (
            None if config.last_selected_at is None else config.last_selected_at.isoformat()
        ),
    }


def _schedule_target(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> tuple[str | None, str | None]:
    return schedule.chime, schedule.group_id


def _schedule_type_name(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> str:
    if isinstance(schedule, WeeklySchedule):
        return "weekly"
    if isinstance(schedule, DateSchedule):
        return "date"
    if isinstance(schedule, HolidaySchedule):
        return "holiday"
    return "recurring"


def _schedule_display_text(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> str:
    return format_schedule_display(schedule).replace(" → ", " -> ")


def _serialize_schedule(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> dict[str, object]:
    chime, group_id = _schedule_target(schedule)
    payload: dict[str, object] = {
        "id": schedule.id,
        "name": _schedule_display_text(schedule),
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat(),
        "updated_at": schedule.updated_at.isoformat(),
        "last_run": None if schedule.last_run is None else schedule.last_run.isoformat(),
        "display": _schedule_display_text(schedule),
        "last_run_display": format_last_run(schedule.last_run),
        "chime_filename": chime,
        "group_id": group_id,
    }
    if isinstance(schedule, WeeklySchedule):
        payload.update(
            {"schedule_type": "weekly", "days": list(schedule.days), "time": schedule.time_hhmm}
        )
    elif isinstance(schedule, DateSchedule):
        payload.update(
            {
                "schedule_type": "date",
                "month": schedule.month,
                "day": schedule.day,
                "time": schedule.time_hhmm,
            }
        )
    elif isinstance(schedule, HolidaySchedule):
        payload.update(
            {
                "schedule_type": "holiday",
                "holiday": schedule.holiday_name,
                "time": schedule.time_hhmm,
            }
        )
    else:
        payload.update({"schedule_type": "recurring", "interval": schedule.interval})
    return payload


def _serialize_schedule_for_edit(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> dict[str, object]:
    if isinstance(schedule, RecurringSchedule):
        return {
            "id": schedule.id,
            "name": _schedule_display_text(schedule),
            "schedule_type": "recurring",
            "interval": schedule.interval,
            "chime_filename": schedule.chime,
            "group_id": schedule.group_id,
            "enabled": schedule.enabled,
        }
    hour_text, minute_text = schedule.time_hhmm.split(":", maxsplit=1)
    hour_24 = int(hour_text)
    am_pm = "AM" if hour_24 < _HOURS_PER_HALF_DAY else "PM"
    hour_12 = hour_24 % _HOURS_PER_HALF_DAY or _HOURS_PER_HALF_DAY
    payload: dict[str, object] = {
        "id": schedule.id,
        "name": _schedule_display_text(schedule),
        "time": schedule.time_hhmm,
        "hour_12": hour_12,
        "minute": minute_text,
        "am_pm": am_pm,
        "chime_filename": schedule.chime,
        "group_id": schedule.group_id,
        "enabled": schedule.enabled,
    }
    if isinstance(schedule, WeeklySchedule):
        payload.update({"schedule_type": "weekly", "days": list(schedule.days)})
    elif isinstance(schedule, DateSchedule):
        payload.update({"schedule_type": "date", "month": schedule.month, "day": schedule.day})
    else:
        payload.update({"schedule_type": "holiday", "holiday": schedule.holiday_name})
    return payload


def _active_chime_dict(chimes: tuple[ChimeInfo, ...]) -> dict[str, object] | None:
    for info in chimes:
        if info.is_active:
            return _serialize_chime(info)
    active_path = _active_chime_path()
    if not active_path.is_file():
        return None
    stat = active_path.stat()
    return {
        "filename": active_path.name,
        "size": stat.st_size,
        "size_str": _format_size_bytes(stat.st_size),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        "is_active": True,
        "is_valid": True,
        "validation_msg": "",
    }


def _prepare_audio_upload(
    uploaded_file: FileStorage,
    final_filename: str,
    *,
    pre_trimmed: bool,
    normalize_enabled: bool,
    target_lufs: int,
) -> Path:
    scratch_dir = _scratch_dir()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    base_id = uuid4().hex
    source_suffix = Path(uploaded_file.filename or "").suffix.lower() or ".bin"
    source_path = scratch_dir / f"{base_id}{source_suffix}"
    working_path = scratch_dir / f"{base_id}-{final_filename}"
    uploaded_file.save(source_path)
    if pre_trimmed:
        result = save_pretrimmed_wav(source_path.read_bytes(), working_path)
        if not result.ok:
            raise _RouteError(result.message, HTTPStatus.UNPROCESSABLE_ENTITY)
    else:
        validation = validate_tesla_wav(source_path)
        if source_suffix == ".wav" and validation.ok:
            shutil.copyfile(source_path, working_path)
        else:
            reencoded = reencode_wav_for_tesla(source_path, working_path)
            if not reencoded.ok:
                raise _RouteError(reencoded.message, HTTPStatus.UNPROCESSABLE_ENTITY)
    if not normalize_enabled:
        return working_path
    try:
        return normalize_audio(working_path, target_lufs)
    except LockChimeAudioError as exc:
        raise _RouteError(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY) from exc


def _upload_prepared_file(prepared_path: Path, final_filename: str) -> Path:
    result = upload_chime_file(
        _PathBackedUpload(final_filename, prepared_path),
        _chimes_dir(),
        _cfg().chimes.max_lock_chime_size,
    )
    if not result.ok or result.saved_path is None:
        raise _RouteError(result.message, HTTPStatus.UNPROCESSABLE_ENTITY)
    return result.saved_path


def _parse_schedule_time(schedule_type: str, form: ImmutableMultiDict[str, str]) -> str:
    if schedule_type == "recurring":
        return _TIME_DEFAULT
    explicit = _trimmed_text(form.get("time_hhmm") or form.get("time"))
    if explicit:
        hour_text, minute_text = explicit.split(":", maxsplit=1)
        return f"{int(hour_text):02d}:{int(minute_text):02d}"
    hour_text = _trimmed_text(form.get("hour"))
    minute_text = _trimmed_text(form.get("minute"))
    am_pm = _trimmed_text(form.get("am_pm")).upper() or "AM"
    if not hour_text or not minute_text:
        raise _RouteError("Time is required", HTTPStatus.BAD_REQUEST)
    hour_12 = int(hour_text)
    minute = int(minute_text)
    if am_pm == "PM" and hour_12 != _HOURS_PER_HALF_DAY:
        hour_24 = hour_12 + _HOURS_PER_HALF_DAY
    elif am_pm == "AM" and hour_12 == _HOURS_PER_HALF_DAY:
        hour_24 = 0
    else:
        hour_24 = hour_12
    return f"{hour_24:02d}:{minute:02d}"


def _schedule_target_from_form(form: ImmutableMultiDict[str, str]) -> tuple[str | None, str | None]:
    chime_filename = _trimmed_text(form.get("chime_filename")) or None
    group_id = _trimmed_text(form.get("group_id")) or None
    if (chime_filename is None) == (group_id is None):
        raise _RouteError(
            "Exactly one of chime_filename or group_id must be provided", HTTPStatus.BAD_REQUEST
        )
    return chime_filename, group_id


def _read_zip_entries(archive_bytes: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        return [
            (Path(info.filename).name, archive.read(info))
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).name
        ]


def _bulk_entries() -> list[tuple[str, bytes]]:
    files = [file for file in request.files.getlist("chime_files") if file.filename]
    if files:
        first_file = files[0]
        if (
            len(files) == 1
            and first_file.filename is not None
            and first_file.filename.lower().endswith(_ZIP_SUFFIX)
        ):
            return _read_zip_entries(first_file.read())
        return [(str(file.filename), file.read()) for file in files if file.filename is not None]
    uploaded_archive = request.files.get("zip_file")
    if (
        uploaded_archive is None
        or uploaded_archive.filename is None
        or not uploaded_archive.filename
    ):
        raise _RouteError("No files selected", HTTPStatus.BAD_REQUEST)
    return _read_zip_entries(uploaded_archive.read())


@lock_chimes_bp.route("/")
def lock_chimes() -> ResponseReturnValue:
    """Render the lock-chimes index page; does not call cache_invalidator."""
    logger.debug("Rendering lock_chimes index")
    scheduler = _get_scheduler()
    manager = _get_group_manager()
    chimes = list_chime_files(_chimes_dir())
    schedules = scheduler.list_schedules()
    groups = manager.list_groups()
    random_config = manager.get_random_config()
    serialized_schedules = [
        {**_serialize_schedule(schedule), **_serialize_schedule_for_edit(schedule)}
        for schedule in schedules
    ]
    serialized_holidays = [
        {
            "name": holiday_name,
            "month": holiday_date.month,
            "day": holiday_date.day,
        }
        for holiday_name, holiday_date in scheduler.get_holidays_with_dates(
            datetime.now(tz=UTC).year
        )
    ]
    return render_template(
        "lock_chimes.html",
        page="media",
        media_tab="chimes",
        active_chime=_active_chime_dict(chimes),
        chime_files=[_serialize_chime(info) for info in chimes],
        schedules=serialized_schedules,
        schedules_json=serialized_schedules,
        holidays=serialized_holidays,
        recurring_intervals=dict(scheduler.get_recurring_intervals()),
        groups=[_serialize_group(group) for group in groups],
        random_config=_serialize_random_config(random_config),
        format_schedule=format_schedule_display,
        format_last_run=format_last_run,
        MAX_LOCK_CHIME_SIZE=_cfg().chimes.max_lock_chime_size,
        MAX_LOCK_CHIME_DURATION=_cfg().chimes.max_lock_chime_duration,
        MIN_LOCK_CHIME_DURATION=_cfg().chimes.min_lock_chime_duration,
        SPEED_RANGE_MIN=_cfg().chimes.speed_range_min,
        SPEED_RANGE_MAX=_cfg().chimes.speed_range_max,
        SPEED_STEP=_cfg().chimes.speed_step,
    )


@lock_chimes_bp.route("/play/active")
def play_active_chime() -> ResponseReturnValue:
    """Stream the active lock chime; does not call cache_invalidator."""
    logger.debug("Streaming active lock chime")
    active_path = _active_chime_path()
    if not active_path.is_file():
        return "Active lock chime not found", HTTPStatus.NOT_FOUND.value
    return send_file(active_path, mimetype=_AUDIO_MIMETYPE)


@lock_chimes_bp.route("/play/<filename>")
def play_lock_chime(filename: str) -> ResponseReturnValue:
    """Stream one library chime by filename; does not call cache_invalidator."""
    logger.debug("Streaming library lock chime %s", filename)
    try:
        safe_name = _safe_wav_name(filename)
    except _RouteError as exc:
        return str(exc), exc.status.value
    file_path = _chimes_dir() / safe_name
    if not file_path.is_file():
        return "File not found", HTTPStatus.NOT_FOUND.value
    return send_file(file_path, mimetype=_AUDIO_MIMETYPE)


@lock_chimes_bp.route("/download/<filename>")
def download_lock_chime(filename: str) -> ResponseReturnValue:
    """Download one library chime by filename; does not call cache_invalidator."""
    logger.debug("Downloading library lock chime %s", filename)
    try:
        safe_name = _safe_wav_name(filename)
    except _RouteError as exc:
        return str(exc), exc.status.value
    file_path = _chimes_dir() / safe_name
    if not file_path.is_file():
        return "File not found", HTTPStatus.NOT_FOUND.value
    return send_file(
        file_path, mimetype=_AUDIO_MIMETYPE, as_attachment=True, download_name=safe_name
    )


@lock_chimes_bp.route("/upload", methods=["POST"])
def upload_lock_chime() -> ResponseReturnValue:
    """Upload one chime; on success schedules cache_invalidator exactly once."""
    logger.info("Uploading one lock chime")
    if "chime_file" not in request.files:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    uploaded_file = request.files["chime_file"]
    if uploaded_file.filename is None or not uploaded_file.filename:
        return _flash_or_json_error("No file selected", HTTPStatus.BAD_REQUEST)
    try:
        extension = Path(uploaded_file.filename).suffix.lower()
        if extension not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise _RouteError("Only WAV and MP3 files are allowed", HTTPStatus.BAD_REQUEST)
        final_filename = f"{Path(uploaded_file.filename).stem}.wav"
        normalize_enabled = _bool_from_value(request.form.get("normalize"))
        target_lufs = int(float(request.form.get("target_lufs", "-16")))
        if normalize_enabled and target_lufs not in _ALLOWED_LUFS_PRESETS:
            raise _RouteError("Invalid volume preset", HTTPStatus.BAD_REQUEST)
        prepared = _prepare_audio_upload(
            uploaded_file,
            final_filename,
            pre_trimmed=_bool_from_value(request.form.get("pre_trimmed")),
            normalize_enabled=normalize_enabled,
            target_lufs=target_lufs,
        )
        saved_path = _upload_prepared_file(prepared, final_filename)
        activated = _bool_from_value(request.form.get("set_as_active"))
        if activated:
            set_active_chime(saved_path.name, _chimes_dir(), _active_chime_path())
    except _RouteError as exc:
        return _flash_or_json_error(str(exc), exc.status)
    except (LockChimeAudioError, LockChimeFileError) as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY)
    return _flash_or_json_success(
        f"Successfully uploaded {final_filename}",
        refresh=ChimeRefresh.for_active_change(changed_active=activated),
    )


@lock_chimes_bp.route("/upload_bulk", methods=["POST"])
def upload_bulk_chimes() -> ResponseReturnValue:
    """Bulk-upload chimes; on success schedules cache_invalidator exactly once."""
    logger.info("Bulk uploading lock chimes")
    try:
        entries = _bulk_entries()
    except (_RouteError, zipfile.BadZipFile) as exc:
        message = str(exc) if isinstance(exc, _RouteError) else "Invalid ZIP archive"
        status = exc.status if isinstance(exc, _RouteError) else HTTPStatus.BAD_REQUEST
        return _flash_or_json_error(message, status)
    results: list[dict[str, object]] = []
    total_uploaded = 0
    for filename, payload in entries:
        if not filename:
            continue
        if Path(filename).suffix.lower() not in _ALLOWED_BULK_EXTENSIONS:
            results.append(
                {
                    "filename": filename,
                    "success": False,
                    "message": "Only WAV files are accepted in bulk upload mode",
                }
            )
            continue
        result = upload_chime_file(
            FileStorage(stream=BytesIO(payload), filename=Path(filename).name),
            _chimes_dir(),
            _cfg().chimes.max_lock_chime_size,
        )
        results.append({"filename": filename, "success": result.ok, "message": result.message})
        if result.ok:
            total_uploaded += 1
    if total_uploaded > 0:
        _get_cache_invalidator().schedule()
    if _is_xhr():
        if total_uploaded > 0:
            return _json_success_payload(
                results=results,
                total_uploaded=total_uploaded,
                summary=f"Successfully uploaded {total_uploaded} of {len(results)} file(s)",
            ), HTTPStatus.OK.value
        return _json_error_payload(
            f"Successfully uploaded {total_uploaded} of {len(results)} file(s)",
        ), HTTPStatus.OK.value
    if total_uploaded > 0:
        flash(f"Successfully uploaded {total_uploaded} chime(s)", "success")
    else:
        flash("All files were rejected. Check file requirements.", "error")
    return redirect(url_for("lock_chimes.lock_chimes"))


@lock_chimes_bp.route("/set/<filename>", methods=["POST"])
def set_as_chime(filename: str) -> ResponseReturnValue:
    """Set one library chime active; on success schedules cache_invalidator exactly once."""
    logger.info("Setting active lock chime to %s", filename)
    try:
        result = set_active_chime(_safe_wav_name(filename), _chimes_dir(), _active_chime_path())
    except _RouteError as exc:
        return _flash_or_json_error(str(exc), exc.status)
    except (LockChimeAudioError, LockChimeFileError, ValueError) as exc:
        return _flash_or_json_error(str(exc), _status_for_service_error(str(exc)))
    return _flash_or_json_success(result.message, refresh=ChimeRefresh.REBIND)


@lock_chimes_bp.route("/delete/<filename>", methods=["POST"])
def delete_lock_chime(filename: str) -> ResponseReturnValue:
    """Delete one library chime; on success schedules cache_invalidator exactly once."""
    logger.info("Deleting lock chime %s", filename)
    try:
        safe_name = _safe_wav_name(filename)
        was_active = any(
            info.name == safe_name and info.is_active for info in list_chime_files(_chimes_dir())
        )
        result = delete_chime_file(safe_name, _chimes_dir(), _active_chime_path())
        if was_active:
            _active_chime_path().unlink(missing_ok=True)
    except _RouteError as exc:
        return _flash_or_json_error(str(exc), exc.status)
    except (LockChimeFileError, ValueError) as exc:
        return _flash_or_json_error(str(exc), _status_for_service_error(str(exc)))
    chime_removed = was_active or result.was_active
    return _flash_or_json_success(
        result.message,
        refresh=ChimeRefresh.for_active_change(changed_active=chime_removed),
        was_active=chime_removed,
        active=None,
    )


@lock_chimes_bp.route("/schedule/add", methods=["POST"])
def add_schedule() -> ResponseReturnValue:
    """Create one schedule; on success schedules cache_invalidator exactly once."""
    logger.info("Adding lock-chime schedule")
    form = request.form
    try:
        schedule_type = _trimmed_text(form.get("schedule_type")) or "weekly"
        chime_filename, group_id = _schedule_target_from_form(form)
        time_hhmm = _parse_schedule_time(schedule_type, form)
        enabled = _bool_from_value(form.get("enabled"), default=True)
        scheduler = _get_scheduler()
        if schedule_type == "weekly":
            result = scheduler.add_weekly(
                tuple(int(day) for day in form.getlist("days")),
                time_hhmm,
                chime=chime_filename,
                group_id=group_id,
            )
        elif schedule_type == "date":
            result = scheduler.add_date(
                int(_trimmed_text(form.get("month"))),
                int(_trimmed_text(form.get("day"))),
                time_hhmm,
                chime=chime_filename,
                group_id=group_id,
            )
        elif schedule_type == "holiday":
            result = scheduler.add_holiday(
                _trimmed_text(form.get("holiday")),
                time_hhmm,
                chime=chime_filename,
                group_id=group_id,
            )
        elif schedule_type == "recurring":
            result = scheduler.add_recurring(
                _trimmed_text(form.get("interval")), chime=chime_filename, group_id=group_id
            )
        else:
            raise _RouteError(f"Invalid schedule type: {schedule_type}", HTTPStatus.BAD_REQUEST)
        if not enabled and result.schedule_id is not None:
            scheduler.set_enabled(result.schedule_id, enabled=False)
    except _RouteError as exc:
        return _flash_or_json_error(str(exc), exc.status)
    except (ChimeScheduleError, ChimeScheduleStateError) as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    return _flash_or_json_success("Schedule created successfully", schedule_id=result.schedule_id)


@lock_chimes_bp.route("/schedule/<schedule_id>/toggle", methods=["POST"])
def toggle_schedule(schedule_id: str) -> ResponseReturnValue:
    """Toggle one schedule; on success schedules cache_invalidator exactly once."""
    logger.info("Toggling schedule %s", schedule_id)
    try:
        scheduler = _get_scheduler()
        schedule = scheduler.get_schedule(schedule_id)
        if schedule is None:
            return _flash_or_json_error("Schedule not found", HTTPStatus.NOT_FOUND)
        result = scheduler.set_enabled(schedule_id, enabled=not schedule.enabled)
        if not result.ok:
            return _flash_or_json_error(result.message, HTTPStatus.NOT_FOUND)
    except (ChimeScheduleError, ChimeScheduleStateError) as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    return _flash_or_json_success(
        "Schedule updated", schedule_id=schedule_id, enabled=not schedule.enabled
    )


@lock_chimes_bp.route("/schedule/<schedule_id>/delete", methods=["POST"])
def delete_schedule(schedule_id: str) -> ResponseReturnValue:
    """Delete one schedule; on success schedules cache_invalidator exactly once."""
    logger.info("Deleting schedule %s", schedule_id)
    try:
        result = _get_scheduler().delete_schedule(schedule_id)
        if not result.ok:
            return _flash_or_json_error(result.message, HTTPStatus.NOT_FOUND)
    except (ChimeScheduleError, ChimeScheduleStateError) as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    return _flash_or_json_success("Schedule deleted", schedule_id=schedule_id)


def _edit_schedule_get(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> ResponseReturnValue:
    return _json_success_payload(schedule=_serialize_schedule_for_edit(schedule))


def _edit_schedule_post(
    scheduler: ChimeScheduler,
    schedule_id: str,
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
) -> ResponseReturnValue:
    form = request.form
    try:
        schedule_type = _trimmed_text(form.get("schedule_type")) or _schedule_type_name(schedule)
        chime_filename, group_id = _schedule_target_from_form(form)
        fields: dict[str, object] = {
            "type": schedule_type,
            "chime": chime_filename,
            "group_id": group_id,
            "enabled": _bool_from_value(form.get("enabled"), default=True),
        }
        if schedule_type == "weekly":
            fields["time_hhmm"] = _parse_schedule_time(schedule_type, form)
            fields["days"] = tuple(int(day) for day in form.getlist("days"))
        elif schedule_type == "date":
            fields["time_hhmm"] = _parse_schedule_time(schedule_type, form)
            fields["month"] = int(_trimmed_text(form.get("month")))
            fields["day"] = int(_trimmed_text(form.get("day")))
        elif schedule_type == "holiday":
            fields["time_hhmm"] = _parse_schedule_time(schedule_type, form)
            fields["holiday_name"] = _trimmed_text(form.get("holiday"))
        elif schedule_type == "recurring":
            fields["interval"] = _trimmed_text(form.get("interval"))
        else:
            raise _RouteError(f"Invalid schedule type: {schedule_type}", HTTPStatus.BAD_REQUEST)
        result = scheduler.update_schedule(schedule_id, **fields)
        if not result.ok:
            return _flash_or_json_error(result.message, HTTPStatus.NOT_FOUND)
    except _RouteError as exc:
        return _flash_or_json_error(str(exc), exc.status)
    except (ChimeScheduleError, ChimeScheduleStateError) as exc:
        return _flash_or_json_error(str(exc), HTTPStatus.BAD_REQUEST)
    return _flash_or_json_success("Schedule updated", schedule_id=schedule_id)


@lock_chimes_bp.route("/schedule/<schedule_id>/edit", methods=["GET", "POST"])
def edit_schedule(schedule_id: str) -> ResponseReturnValue:
    """Get or update one schedule; POST success schedules cache_invalidator exactly once."""
    logger.debug("Editing schedule %s via %s", schedule_id, request.method)
    scheduler = _get_scheduler()
    schedule = scheduler.get_schedule(schedule_id)
    if schedule is None:
        if request.method == "GET":
            return _json_error_payload("Schedule not found"), HTTPStatus.NOT_FOUND.value
        return _flash_or_json_error("Schedule not found", HTTPStatus.NOT_FOUND)
    if request.method == "GET":
        return _edit_schedule_get(schedule)
    return _edit_schedule_post(scheduler, schedule_id, schedule)


@lock_chimes_bp.route("/groups/list", methods=["GET"])
def list_groups() -> ResponseReturnValue:
    """Return group + random-mode JSON; does not call cache_invalidator."""
    logger.debug("Listing chime groups")
    try:
        manager = _get_group_manager()
        return _json_success_payload(
            groups=[_serialize_group(group) for group in manager.list_groups()],
            random_config=_serialize_random_config(manager.get_random_config()),
        )
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST.value
    except Exception:
        logger.exception("Unexpected error listing groups")
        return _json_error_payload("Internal server error"), HTTPStatus.INTERNAL_SERVER_ERROR.value


@lock_chimes_bp.route("/groups/create", methods=["POST"])
def create_group() -> ResponseReturnValue:
    """Create one chime group; on success schedules cache_invalidator exactly once."""
    logger.info("Creating chime group")
    name = _trimmed_text(_request_value("name"))
    try:
        result = _get_group_manager().create_group(name)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST.value
    return _json_mutation_success(result.message, group_id=result.group_id)


@lock_chimes_bp.route("/groups/<group_id>/update", methods=["POST"])
def update_group(group_id: str) -> ResponseReturnValue:
    """Rename one chime group; on success schedules cache_invalidator exactly once."""
    logger.info("Updating chime group %s", group_id)
    name = _trimmed_text(_request_value("name"))
    try:
        result = _get_group_manager().rename_group(group_id, name)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), _group_error_status(str(exc)).value
    return _json_mutation_success(result.message)


@lock_chimes_bp.route("/groups/<group_id>/delete", methods=["POST"])
def delete_group(group_id: str) -> ResponseReturnValue:
    """Delete one chime group; on success schedules cache_invalidator exactly once."""
    logger.info("Deleting chime group %s", group_id)
    try:
        result = _get_group_manager().delete_group(group_id)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), _group_error_status(str(exc)).value
    return _json_mutation_success(result.message)


@lock_chimes_bp.route("/groups/<group_id>/add_chime", methods=["POST"])
def add_chime_to_group(group_id: str) -> ResponseReturnValue:
    """Add one chime to a group; on success schedules cache_invalidator exactly once."""
    logger.info("Adding chime to group %s", group_id)
    chime_filename = _trimmed_text(_request_value("chime_filename"))
    try:
        group = _get_group_manager().add_chime_to_group(group_id, chime_filename)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), _group_error_status(str(exc)).value
    return _json_mutation_success(f"Chime '{chime_filename}' added to group '{group.name}'")


@lock_chimes_bp.route("/groups/<group_id>/remove_chime", methods=["POST"])
def remove_chime_from_group(group_id: str) -> ResponseReturnValue:
    """Remove one chime from a group; on success schedules cache_invalidator exactly once."""
    logger.info("Removing chime from group %s", group_id)
    chime_filename = _trimmed_text(_request_value("chime_filename"))
    try:
        group = _get_group_manager().remove_chime_from_group(group_id, chime_filename)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), _group_error_status(str(exc)).value
    return _json_mutation_success(f"Chime '{chime_filename}' removed from group '{group.name}'")


@lock_chimes_bp.route("/groups/random_mode", methods=["POST"])
def set_random_mode() -> ResponseReturnValue:
    """Update random-mode state; on success schedules cache_invalidator exactly once."""
    logger.info("Updating random mode")
    enabled = _bool_from_value(_request_value("enabled"))
    group_id_text = _trimmed_text(_request_value("group_id"))
    group_id = None if not enabled else group_id_text or None
    try:
        config = _get_group_manager().set_random_mode(enabled=enabled, group_id=group_id)
    except (ChimeGroupError, ChimeGroupStateError) as exc:
        return _json_error_payload(str(exc)), HTTPStatus.BAD_REQUEST.value
    message = _RANDOM_ENABLED_MESSAGE if enabled else _RANDOM_DISABLED_MESSAGE
    return _json_mutation_success(message, random_config=_serialize_random_config(config))


__all__ = ("lock_chimes_bp",)
