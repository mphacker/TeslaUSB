# ruff: noqa: ANN001  # pytest injects fixtures dynamically in test signatures.
"""Tests for the lock-chimes blueprint."""

from __future__ import annotations

import wave
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import pytest
from teslausb_web.app import create_app
from teslausb_web.blueprints.lock_chimes import (
    _get_group_manager,
    _get_scheduler,
    _PathBackedUpload,
    _schedule_type_name,
    _serialize_schedule,
    _serialize_schedule_for_edit,
)
from teslausb_web.config import ChimesSection, FeaturesSection, PathsSection, WebConfig, WebSection
from teslausb_web.services.chime_scheduler import (
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    WeeklySchedule,
)
from teslausb_web.services.gadget_rebind import RebindResult
from teslausb_web.services.lock_chime_service import LockChimeFileError, ReencodeResult

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient
    from teslausb_web.services.cache_invalidation import CacheInvalidator
    from teslausb_web.services.chime_group_service import ChimeGroupManager
    from teslausb_web.services.chime_scheduler import ChimeScheduler
    from teslausb_web.services.gadget_rebind import GadgetRebinder

_XHR: Final[dict[str, str]] = {"X-Requested-With": "XMLHttpRequest"}
_SAMPLE_FRAMES: Final[int] = 200
_MAX_SIZE_BYTES: Final[int] = 1_048_576


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    backing_root = tmp_path / "backing"
    state_dir = tmp_path / "state"
    (backing_root / "Chimes").mkdir(parents=True)
    state_dir.mkdir()
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(
            backing_root=backing_root,
            state_dir=state_dir,
            cache_invalidate_script=tmp_path / "invalidate.sh",
        ),
        features=FeaturesSection(),
        chimes=ChimesSection(),
        source_path=None,
    )
    flask_app = create_app(cfg)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def invalidator(app: Flask) -> CacheInvalidator:
    return app.extensions["cache_invalidator"]


@pytest.fixture
def rebinder(app: Flask) -> GadgetRebinder:
    return app.extensions["gadget_rebinder"]


@pytest.fixture
def chimes_dir(app: Flask) -> Path:
    cfg = app.config["teslausb_config"]
    path = cfg.paths.media_root / cfg.chimes.chimes_folder
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def active_path(app: Flask) -> Path:
    cfg = app.config["teslausb_config"]
    return cfg.paths.media_root / cfg.chimes.lock_chime_filename


@pytest.fixture
def group_manager(app: Flask) -> ChimeGroupManager:
    return app.extensions["chime_group_manager"]


@pytest.fixture
def scheduler(app: Flask) -> ChimeScheduler:
    return app.extensions["chime_scheduler"]


def _wav_bytes(*, channels: int = 1, sampwidth: int = 2, framerate: int = 44_100) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"\x00" * (_SAMPLE_FRAMES * channels * sampwidth))
    return buffer.getvalue()


def _upload_data(filename: str, payload: bytes) -> dict[str, object]:
    return {"chime_file": (BytesIO(payload), filename)}


def _bulk_data(files: list[tuple[str, bytes]]) -> dict[str, object]:
    return {"chime_files": [(BytesIO(payload), filename) for filename, payload in files]}


def _create_group(group_manager: ChimeGroupManager, name: str = "Holiday") -> str:
    group_id = group_manager.create_group(name).group_id
    assert group_id is not None
    return group_id


def _create_weekly_schedule(scheduler: ChimeScheduler) -> str:
    schedule_id = scheduler.add_weekly((0,), "09:00", chime="weekday.wav").schedule_id
    assert schedule_id is not None
    return schedule_id


def test_path_backed_upload_reads_full_and_partial_payload(tmp_path: Path) -> None:
    source = tmp_path / "payload.wav"
    source.write_bytes(b"abcdef")
    upload = _PathBackedUpload("payload.wav", source)
    assert upload.read() == b"abcdef"
    assert upload.read(3) == b"abc"


def test_get_group_manager_recreates_cached_manager(app: Flask) -> None:
    with app.app_context():
        app.extensions.pop("chime_group_manager", None)
        manager = _get_group_manager()
    assert app.extensions["chime_group_manager"] is manager


def test_get_scheduler_recreates_cached_scheduler(app: Flask) -> None:
    with app.app_context():
        app.extensions.pop("chime_scheduler", None)
        scheduler = _get_scheduler()
    assert app.extensions["chime_scheduler"] is scheduler


@pytest.mark.parametrize(
    ("schedule", "expected_type"),
    [
        (
            WeeklySchedule(
                id="weekly-id",
                days=(0, 2),
                time_hhmm="09:00",
                chime="weekly.wav",
                group_id=None,
                enabled=True,
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
                last_run=None,
            ),
            "weekly",
        ),
        (
            DateSchedule(
                id="date-id",
                month=12,
                day=25,
                time_hhmm="09:00",
                chime="date.wav",
                group_id=None,
                enabled=True,
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
                last_run=None,
            ),
            "date",
        ),
        (
            HolidaySchedule(
                id="holiday-id",
                holiday_name="Christmas Day",
                time_hhmm="09:00",
                chime="holiday.wav",
                group_id=None,
                enabled=True,
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
                last_run=None,
            ),
            "holiday",
        ),
        (
            RecurringSchedule(
                id="recurring-id",
                interval="1hour",
                chime="recurring.wav",
                group_id=None,
                enabled=True,
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
                updated_at=datetime(2024, 1, 1, tzinfo=UTC),
                last_run=None,
            ),
            "recurring",
        ),
    ],
)
def test_serialize_schedule_reports_each_schedule_type(
    schedule: WeeklySchedule | DateSchedule | HolidaySchedule | RecurringSchedule,
    expected_type: str,
) -> None:
    payload = _serialize_schedule(schedule)
    assert payload["schedule_type"] == expected_type
    assert _schedule_type_name(schedule) == expected_type


def test_serialize_schedule_for_edit_supports_recurring() -> None:
    schedule = RecurringSchedule(
        id="recurring-id",
        interval="1hour",
        chime="recurring.wav",
        group_id=None,
        enabled=False,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        last_run=None,
    )
    payload = _serialize_schedule_for_edit(schedule)
    assert payload["schedule_type"] == "recurring"
    assert payload["interval"] == "1hour"
    assert payload["enabled"] is False


def test_get_index_renders_template(client) -> None:
    response = client.get("/lock_chimes/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Lock Chimes" in html
    assert "/static/vendor/lamejs/lame.min.js" in html
    assert "/static/js/audio_trimmer.js" in html


def test_get_index_strips_removed_mode_terms(client) -> None:
    response = client.get("/lock_chimes/")
    html = response.data.decode("utf-8").casefold()
    assert "edit mode" not in html
    assert "present mode" not in html
    assert "quick_edit" not in html
    assert "cdn.jsdelivr" not in html


def test_get_index_empty_state_renders_without_errors(client) -> None:
    response = client.get("/lock_chimes/")
    html = response.data.decode("utf-8")
    assert response.status_code == 200
    assert "No active lock chime set." in html
    assert "No chimes found in the Chimes library." in html
    assert "No schedules configured." in html
    assert "No chime groups yet." in html


def test_get_index_full_state_renders_all_sections(
    client,
    chimes_dir: Path,
    active_path: Path,
    group_manager: ChimeGroupManager,
    scheduler: ChimeScheduler,
) -> None:
    for filename in ("alpha.wav", "bravo.wav", "charlie.wav"):
        payload = _wav_bytes()
        (chimes_dir / filename).write_bytes(payload)
    active_path.write_bytes((chimes_dir / "alpha.wav").read_bytes())

    holiday_group = group_manager.create_group("Holiday").group_id
    assert holiday_group is not None
    group_manager.add_chime_to_group(holiday_group, "alpha.wav")
    group_manager.add_chime_to_group(holiday_group, "bravo.wav")

    funny_group = group_manager.create_group("Funny").group_id
    assert funny_group is not None
    group_manager.add_chime_to_group(funny_group, "charlie.wav")
    group_manager.set_random_mode(enabled=True, group_id=holiday_group)

    scheduler.add_weekly((0, 2), "09:00", chime="alpha.wav")
    scheduler.add_date(12, 25, "08:30", chime="bravo.wav")
    scheduler.add_holiday("Christmas Day", "00:00", chime="charlie.wav")
    scheduler.add_recurring("1hour", chime="RANDOM")

    response = client.get("/lock_chimes/")
    html = response.data.decode("utf-8")
    assert response.status_code == 200
    for token in (
        "alpha.wav",
        "bravo.wav",
        "charlie.wav",
        "Holiday",
        "Funny",
        "Christmas Day",
        "Random Chime",
        "Recurring Rotation",
    ):
        assert token in html


def test_get_index_render_contains_no_emoji_icons(client) -> None:
    response = client.get("/lock_chimes/")
    html = response.data.decode("utf-8")
    forbidden_codepoints = {
        0x26A0,
        0x2713,
        0x2714,
        0x2717,
        0x2718,
        0x2716,
        0x1F3B5,
        0x1F514,
        0x1F4CB,
        0x1F4E4,
        0x2702,
        0x1F50A,
        0x1F4DD,
        0x1F3AF,
        0x21BA,
        0x25B6,
        0x2795,
        0x1F4C5,
        0x1F4C6,
        0x1F389,
        0x1F504,
        0x2139,
        0x23F0,
        0x1F4CA,
        0x1F4A1,
        0x270F,
        0x1F5D1,
        0x1F4C1,
        0x25CB,
        0x1F3B2,
        0x2022,
    }
    assert all(ord(char) not in forbidden_codepoints for char in html)


def test_play_active_chime_returns_wav(client, active_path: Path) -> None:
    active_path.write_bytes(_wav_bytes())
    response = client.get("/lock_chimes/play/active")
    assert response.status_code == 200
    assert response.mimetype == "audio/wav"


def test_play_active_chime_returns_404_when_missing(client) -> None:
    response = client.get("/lock_chimes/play/active")
    assert response.status_code == 404


def test_play_lock_chime_rejects_path_traversal(client) -> None:
    response = client.get("/lock_chimes/play/..%5Cevil.wav")
    assert response.status_code == 400


def test_download_lock_chime_sets_attachment_header(client, chimes_dir: Path) -> None:
    (chimes_dir / "ding.wav").write_bytes(_wav_bytes())
    response = client.get("/lock_chimes/download/ding.wav")
    assert response.status_code == 200
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "ding.wav" in response.headers["Content-Disposition"]


def test_upload_happy_path_saves_file_and_schedules_cache(
    client, chimes_dir: Path, invalidator
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/lock_chimes/upload",
            data=_upload_data("new.wav", _wav_bytes()),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert (chimes_dir / "new.wav").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_with_set_as_active_rebinds_gadget(
    client, chimes_dir: Path, active_path: Path, invalidator, rebinder
) -> None:
    data = _upload_data("new.wav", _wav_bytes())
    data["set_as_active"] = "true"
    with (
        patch.object(rebinder, "rebind", return_value=RebindResult(0, "", "")) as rebind_mock,
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post(
            "/lock_chimes/upload",
            data=data,
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert active_path.is_file()
    # Uploading WITH "set as active" changes LockChime.wav, so it must
    # re-enumerate; uploading without it (above) only schedules a soft
    # invalidate.
    rebind_mock.assert_called_once_with()
    schedule_mock.assert_not_called()


def test_upload_returns_400_when_no_file(client) -> None:
    response = client.post("/lock_chimes/upload", data={}, headers=_XHR)
    assert response.status_code == 400
    assert response.get_json()["error"] == "No file selected"


def test_upload_returns_400_for_bad_extension(client) -> None:
    response = client.post(
        "/lock_chimes/upload",
        data=_upload_data("bad.txt", b"nope"),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "Only WAV and MP3 files are allowed" in response.get_json()["error"]


def test_upload_returns_422_for_oversize_file(client) -> None:
    response = client.post(
        "/lock_chimes/upload",
        data=_upload_data("big.wav", b"0" * (_MAX_SIZE_BYTES + 1)),
        headers=_XHR,
        content_type="multipart/form-data",
    )
    assert response.status_code == 422


def test_upload_returns_422_when_reencode_fails(client) -> None:
    with patch(
        "teslausb_web.blueprints.lock_chimes.reencode_wav_for_tesla",
        return_value=ReencodeResult(
            ok=False,
            message="ffmpeg failed",
            strategy=None,
            attempt=None,
            size_mb=None,
        ),
    ):
        response = client.post(
            "/lock_chimes/upload",
            data=_upload_data("song.mp3", b"fake-mp3"),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 422
    assert response.get_json()["error"] == "ffmpeg failed"


def test_upload_bulk_mixed_results_schedules_cache_once(
    client, chimes_dir: Path, invalidator
) -> None:
    files = [
        ("a.wav", _wav_bytes()),
        ("b.wav", _wav_bytes()),
        ("c.wav", _wav_bytes()),
        ("bad.txt", b"bad"),
    ]
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            "/lock_chimes/upload_bulk",
            data=_bulk_data(files),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    body = response.get_json()
    assert response.status_code == 200
    assert body["total_uploaded"] == 3
    assert len(body["results"]) == 4
    assert (chimes_dir / "a.wav").is_file()
    assert (chimes_dir / "b.wav").is_file()
    assert (chimes_dir / "c.wav").is_file()
    schedule_mock.assert_called_once_with()


def test_upload_bulk_returns_400_when_empty(client) -> None:
    response = client.post("/lock_chimes/upload_bulk", data={}, headers=_XHR)
    assert response.status_code == 400


def test_set_as_chime_replaces_active_file_and_rebinds_gadget(
    client,
    chimes_dir: Path,
    active_path: Path,
    invalidator,
    rebinder,
) -> None:
    source_bytes = _wav_bytes()
    (chimes_dir / "select.wav").write_bytes(source_bytes)
    with (
        patch.object(rebinder, "rebind", return_value=RebindResult(0, "", "")) as rebind_mock,
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post("/lock_chimes/set/select.wav", headers=_XHR)
    assert response.status_code == 200
    assert active_path.read_bytes() == source_bytes
    # Activating a chime must trigger a full USB re-enumeration so Tesla
    # re-reads LockChime.wav; the soft cache-invalidate is NOT enough.
    rebind_mock.assert_called_once_with()
    schedule_mock.assert_not_called()


def test_set_as_chime_falls_back_to_soft_invalidate_when_rebind_fails(
    client,
    chimes_dir: Path,
    invalidator,
    rebinder,
) -> None:
    (chimes_dir / "select.wav").write_bytes(_wav_bytes())
    with (
        patch.object(
            rebinder, "rebind", return_value=RebindResult(2, "", "boom")
        ) as rebind_mock,
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post("/lock_chimes/set/select.wav", headers=_XHR)
    assert response.status_code == 200
    rebind_mock.assert_called_once_with()
    # On rebind failure we still do the weaker soft invalidate rather
    # than silently doing nothing.
    schedule_mock.assert_called_once_with()


def test_set_as_chime_rejects_traversal(client) -> None:
    response = client.post("/lock_chimes/set/..%5Cevil.wav", headers=_XHR)
    assert response.status_code == 400


def test_set_as_chime_returns_404_for_missing_file(client) -> None:
    response = client.post("/lock_chimes/set/missing.wav", headers=_XHR)
    assert response.status_code == 404


def test_delete_lock_chime_removes_file_and_active_copy(
    client,
    chimes_dir: Path,
    active_path: Path,
    invalidator,
    rebinder,
) -> None:
    payload = _wav_bytes()
    library_file = chimes_dir / "delete.wav"
    library_file.write_bytes(payload)
    active_path.write_bytes(payload)
    with (
        patch.object(rebinder, "rebind", return_value=RebindResult(0, "", "")) as rebind_mock,
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post("/lock_chimes/delete/delete.wav", headers=_XHR)
    body = response.get_json()
    assert response.status_code == 200
    assert body["was_active"] is True
    assert not library_file.exists()
    assert not active_path.exists()
    # Removing the ACTIVE chime also changes LockChime.wav, so it too
    # must re-enumerate the gadget.
    rebind_mock.assert_called_once_with()
    schedule_mock.assert_not_called()


def test_delete_inactive_lock_chime_schedules_cache_without_rebind(
    client,
    chimes_dir: Path,
    invalidator,
    rebinder,
) -> None:
    chimes_dir.joinpath("spare.wav").write_bytes(_wav_bytes())
    with (
        patch.object(rebinder, "rebind") as rebind_mock,
        patch.object(invalidator, "schedule") as schedule_mock,
    ):
        response = client.post("/lock_chimes/delete/spare.wav", headers=_XHR)
    assert response.status_code == 200
    # Deleting a non-active library chime does not touch LockChime.wav,
    # so a soft invalidate is sufficient and no rebind should fire.
    rebind_mock.assert_not_called()
    schedule_mock.assert_called_once_with()


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {
                "schedule_type": "weekly",
                "days": ["0"],
                "hour": "9",
                "minute": "00",
                "am_pm": "AM",
                "chime_filename": "a.wav",
            },
            WeeklySchedule,
        ),
        (
            {
                "schedule_type": "date",
                "month": "12",
                "day": "25",
                "hour": "9",
                "minute": "00",
                "am_pm": "AM",
                "chime_filename": "a.wav",
            },
            None,
        ),
        (
            {
                "schedule_type": "holiday",
                "holiday": "Christmas Day",
                "hour": "9",
                "minute": "00",
                "am_pm": "AM",
                "chime_filename": "a.wav",
            },
            None,
        ),
        ({"schedule_type": "recurring", "interval": "1hour", "chime_filename": "a.wav"}, None),
    ],
)
def test_add_schedule_happy_paths(
    client, scheduler: ChimeScheduler, invalidator, payload, expected_type
) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/lock_chimes/schedule/add", data=payload, headers=_XHR)
    assert response.status_code == 200
    schedule_mock.assert_called_once_with()
    assert len(scheduler.list_schedules()) == 1
    if expected_type is not None:
        assert isinstance(scheduler.list_schedules()[0], expected_type)


def test_add_schedule_returns_400_when_time_missing(client) -> None:
    response = client.post(
        "/lock_chimes/schedule/add",
        data={"schedule_type": "weekly", "days": ["0"], "chime_filename": "a.wav"},
        headers=_XHR,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Time is required"


def test_add_schedule_returns_400_when_both_chime_and_group_provided(client) -> None:
    response = client.post(
        "/lock_chimes/schedule/add",
        data={
            "schedule_type": "weekly",
            "days": ["0"],
            "hour": "9",
            "minute": "00",
            "am_pm": "AM",
            "chime_filename": "a.wav",
            "group_id": "group-1",
        },
        headers=_XHR,
    )
    assert response.status_code == 400


def test_add_schedule_returns_400_when_target_missing(client) -> None:
    response = client.post(
        "/lock_chimes/schedule/add",
        data={"schedule_type": "weekly", "days": ["0"], "hour": "9", "minute": "00", "am_pm": "AM"},
        headers=_XHR,
    )
    assert response.status_code == 400


def test_toggle_schedule_happy_path(client, scheduler: ChimeScheduler, invalidator) -> None:
    schedule_id = _create_weekly_schedule(scheduler)
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(f"/lock_chimes/schedule/{schedule_id}/toggle", headers=_XHR)
    assert response.status_code == 200
    assert scheduler.get_schedule(schedule_id).enabled is False
    schedule_mock.assert_called_once_with()


def test_toggle_schedule_returns_404_for_missing_id(client) -> None:
    response = client.post("/lock_chimes/schedule/missing/toggle", headers=_XHR)
    assert response.status_code == 404


def test_delete_schedule_happy_path(client, scheduler: ChimeScheduler, invalidator) -> None:
    schedule_id = _create_weekly_schedule(scheduler)
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(f"/lock_chimes/schedule/{schedule_id}/delete", headers=_XHR)
    assert response.status_code == 200
    assert scheduler.get_schedule(schedule_id) is None
    schedule_mock.assert_called_once_with()


def test_delete_schedule_returns_404_for_missing_id(client) -> None:
    response = client.post("/lock_chimes/schedule/missing/delete", headers=_XHR)
    assert response.status_code == 404


def test_edit_schedule_get_returns_schedule_json(client, scheduler: ChimeScheduler) -> None:
    schedule_id = _create_weekly_schedule(scheduler)
    response = client.get(f"/lock_chimes/schedule/{schedule_id}/edit")
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["schedule"]["schedule_type"] == "weekly"


def test_edit_schedule_post_updates_schedule(
    client, scheduler: ChimeScheduler, invalidator
) -> None:
    schedule_id = _create_weekly_schedule(scheduler)
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            f"/lock_chimes/schedule/{schedule_id}/edit",
            data={
                "schedule_type": "weekly",
                "days": ["1", "2"],
                "hour": "10",
                "minute": "15",
                "am_pm": "AM",
                "chime_filename": "updated.wav",
                "enabled": "true",
            },
            headers=_XHR,
        )
    updated = scheduler.get_schedule(schedule_id)
    assert response.status_code == 200
    assert isinstance(updated, WeeklySchedule)
    assert updated.days == (1, 2)
    assert updated.time_hhmm == "10:15"
    assert updated.chime == "updated.wav"
    schedule_mock.assert_called_once_with()


def test_edit_schedule_returns_404_for_missing_id(client) -> None:
    response = client.get("/lock_chimes/schedule/missing/edit")
    assert response.status_code == 404


def test_groups_list_returns_all_groups(client, group_manager: ChimeGroupManager) -> None:
    _create_group(group_manager, "Holiday")
    response = client.get("/lock_chimes/groups/list")
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert len(body["groups"]) == 1


def test_create_group_validation_rejects_empty_name(client) -> None:
    response = client.post("/lock_chimes/groups/create", json={"name": ""})
    assert response.status_code == 400


def test_create_group_rejects_duplicates(client) -> None:
    client.post("/lock_chimes/groups/create", json={"name": "Holiday"})
    response = client.post("/lock_chimes/groups/create", json={"name": "Holiday"})
    assert response.status_code == 400


def test_create_group_success_schedules_cache(client, invalidator) -> None:
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post("/lock_chimes/groups/create", json={"name": "Holiday"})
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["group_id"]
    schedule_mock.assert_called_once_with()


def test_update_group_returns_404_for_missing_id(client) -> None:
    response = client.post("/lock_chimes/groups/missing/update", json={"name": "Renamed"})
    assert response.status_code == 404


def test_delete_group_returns_404_for_missing_id(client) -> None:
    response = client.post("/lock_chimes/groups/missing/delete")
    assert response.status_code == 404


def test_add_chime_to_group_success_and_duplicate_rejected(
    client,
    group_manager: ChimeGroupManager,
    invalidator,
) -> None:
    group_id = _create_group(group_manager)
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            f"/lock_chimes/groups/{group_id}/add_chime",
            json={"chime_filename": "a.wav"},
        )
    assert response.status_code == 200
    schedule_mock.assert_called_once_with()
    duplicate = client.post(
        f"/lock_chimes/groups/{group_id}/add_chime",
        json={"chime_filename": "a.wav"},
    )
    assert duplicate.status_code == 400


def test_remove_chime_from_group_success_and_repeat_is_400(
    client,
    group_manager: ChimeGroupManager,
    invalidator,
) -> None:
    group_id = _create_group(group_manager)
    group_manager.add_chime_to_group(group_id, "a.wav")
    with patch.object(invalidator, "schedule") as schedule_mock:
        response = client.post(
            f"/lock_chimes/groups/{group_id}/remove_chime",
            json={"chime_filename": "a.wav"},
        )
    assert response.status_code == 200
    schedule_mock.assert_called_once_with()
    repeat = client.post(
        f"/lock_chimes/groups/{group_id}/remove_chime",
        json={"chime_filename": "a.wav"},
    )
    assert repeat.status_code == 400


def test_random_mode_enable_and_disable(
    client, group_manager: ChimeGroupManager, invalidator
) -> None:
    group_id = _create_group(group_manager)
    group_manager.add_chime_to_group(group_id, "a.wav")
    with patch.object(invalidator, "schedule") as schedule_mock:
        enable = client.post(
            "/lock_chimes/groups/random_mode", json={"enabled": True, "group_id": group_id}
        )
    assert enable.status_code == 200
    assert enable.get_json()["random_config"]["enabled"] is True
    schedule_mock.assert_called_once_with()
    with patch.object(invalidator, "schedule") as schedule_mock:
        disable = client.post("/lock_chimes/groups/random_mode", json={"enabled": False})
    assert disable.status_code == 200
    assert disable.get_json()["random_config"]["enabled"] is False
    schedule_mock.assert_called_once_with()


def test_random_mode_rejects_unknown_group(client) -> None:
    response = client.post(
        "/lock_chimes/groups/random_mode",
        json={"enabled": True, "group_id": "missing"},
    )
    assert response.status_code == 400


def test_upload_translates_service_exception_to_422(client) -> None:
    with patch(
        "teslausb_web.blueprints.lock_chimes.upload_chime_file",
        side_effect=LockChimeFileError("disk full"),
    ):
        response = client.post(
            "/lock_chimes/upload",
            data=_upload_data("new.wav", _wav_bytes()),
            headers=_XHR,
            content_type="multipart/form-data",
        )
    assert response.status_code == 422


def test_schedule_toggle_translates_service_exception_to_400(
    client,
    scheduler: ChimeScheduler,
) -> None:
    from teslausb_web.services.chime_scheduler import ChimeScheduleError

    schedule_id = _create_weekly_schedule(scheduler)
    with patch.object(scheduler, "set_enabled", side_effect=ChimeScheduleError("boom")):
        response = client.post(f"/lock_chimes/schedule/{schedule_id}/toggle", headers=_XHR)
    assert response.status_code == 400
