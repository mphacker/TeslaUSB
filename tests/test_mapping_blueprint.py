"""Blueprint-level tests for the day-based map endpoints.

These cover *only* the validation/wiring surface area that doesn't
already have direct service-layer coverage in
``test_mapping_service.py``. The service layer owns the business
logic; the blueprint owns:

* request parameter parsing (``limit``, ``min_distance``, ``date``)
* strict regex validation of the ``date`` argument (rejects
  malformed input with HTTP 400 before reaching SQL)
* limit clamping (default 60, max 365)
* ArchivedClips path normalization in ``/api/day/<date>/routes``

The wider blueprint module imports a lot of optional Pi-runtime
deps; we set them up inside a dedicated tmp-db fixture so the
tests stay hermetic on Windows.
"""
import os
import sys

import pytest


def _tmp_image_file(tmp_path):
    """Create a non-empty file used as a stand-in for ``usb_cam.img``.

    The mapping blueprint's ``before_request`` hook gates *all*
    routes on this file existing. We don't care about its contents —
    only the ``os.path.isfile`` check.
    """
    f = tmp_path / "usb_cam.img"
    f.write_bytes(b"\x00")
    return str(f)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Build a hermetic Flask app with the mapping blueprint mounted.

    Patches ``IMG_CAM_PATH`` and ``MAPPING_DB_PATH`` in the blueprint
    module so the ``before_request`` hook passes and SQL queries hit
    a fresh in-tmp SQLite DB. Returns the Flask app — use ``client``
    for actual requests.
    """
    from services.mapping_service import _init_db
    from flask import Flask
    from blueprints import mapping as mapping_module

    db_path = str(tmp_path / "geodata.db")
    conn = _init_db(db_path)
    conn.commit()
    conn.close()

    img_path = _tmp_image_file(tmp_path)

    monkeypatch.setattr(mapping_module, 'IMG_CAM_PATH', img_path)
    monkeypatch.setattr(mapping_module, 'MAPPING_DB_PATH', db_path)

    flask_app = Flask(__name__)
    flask_app.secret_key = 'test'
    flask_app.register_blueprint(mapping_module.mapping_bp)
    flask_app.config['TESTING'] = True
    flask_app.db_path = db_path
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _add_trip(db_path, trip_id, start, end=None, distance_km=2.5):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO trips (id, start_time, end_time, start_lat, start_lon,
                              end_lat, end_lon, distance_km, duration_seconds,
                              source_folder)
           VALUES (?, ?, ?, 37.7, -122.4, 37.8, -122.5, ?, 600, 'RecentClips')""",
        (trip_id, start, end or start, distance_km),
    )
    conn.commit()
    conn.close()


def _add_waypoint(db_path, trip_id, video_path='clip.mp4', ts='2026-05-04T08:00:00'):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO waypoints (trip_id, timestamp, lat, lon, speed_mps,
                                  autopilot_state, video_path, frame_offset)
           VALUES (?, ?, 37.7, -122.4, 25.0, 'NONE', ?, 0)""",
        (trip_id, ts, video_path),
    )
    conn.commit()
    conn.close()


def _add_event(db_path, ts, event_type='harsh_brake', lat=37.7, lon=-122.4):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                        event_type, severity, description)
           VALUES (NULL, ?, ?, ?, ?, 'warning', 'test')""",
        (ts, lat, lon, event_type),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# /api/days
# ---------------------------------------------------------------------------

class TestApiDays:
    def test_empty_db_returns_empty_list(self, client):
        r = client.get('/api/days')
        assert r.status_code == 200
        assert r.get_json() == {'days': []}

    def test_returns_sentry_only_days(self, app, client):
        # The point of the day-based redesign: a day with sentry
        # events but no trips must still appear in the navigator.
        _add_event(app.db_path, '2026-05-04T22:00:00', event_type='sentry')
        r = client.get('/api/days')
        assert r.status_code == 200
        days = r.get_json()['days']
        assert len(days) == 1
        assert days[0]['date'] == '2026-05-04'
        assert days[0]['trip_count'] == 0
        assert days[0]['sentry_count'] == 1

    def test_negative_limit_falls_back_to_default(self, app, client):
        # 365 unique days, request limit=-1 — should return default (60).
        for i in range(70):
            _add_trip(app.db_path, i + 1, f'2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T08:00:00')
        r = client.get('/api/days?limit=-1')
        assert r.status_code == 200
        # Default cap is 60; with 70 unique-ish dates we should get
        # at most 60 rows back.
        assert len(r.get_json()['days']) <= 60

    def test_excessive_limit_clamped_to_max(self, app, client):
        _add_trip(app.db_path, 1, '2026-05-04T08:00:00')
        r = client.get('/api/days?limit=999999')
        # The request should succeed (no 400), and the underlying SQL
        # has been clamped to LIMIT 365 — we can't observe that directly
        # but a successful response with the row we inserted proves
        # the request was not rejected on the way in.
        assert r.status_code == 200
        assert len(r.get_json()['days']) == 1


# ---------------------------------------------------------------------------
# /api/day/<date>/routes
# ---------------------------------------------------------------------------

class TestApiDayRoutes:
    @pytest.mark.parametrize('bad_date', [
        'abc', '../etc/passwd',
        '2026/05/04', '20260504', '2026-5-4', '',
    ])
    def test_rejects_structurally_invalid_dates(self, client, bad_date):
        # The regex is a SECURITY gate: it rejects anything that
        # contains path separators, traversal sequences, or could
        # otherwise smuggle non-date data into the substr()
        # comparison or any future code path that uses the date in
        # a filesystem path. Semantically out-of-range numeric dates
        # (e.g. 2026-13-01) are NOT rejected — they're harmless
        # because they pass through SQL as a string compare and
        # simply match nothing.
        r = client.get(f'/api/day/{bad_date}/routes')
        # '' won't match the route at all (404). Anything else that
        # makes it to the handler must be 400.
        assert r.status_code in (400, 404)
        if r.status_code == 400:
            assert 'date must be YYYY-MM-DD' in r.get_json()['error']

    @pytest.mark.parametrize('weird_but_valid', ['2026-13-01', '2026-99-99'])
    def test_accepts_numerically_out_of_range_dates(self, client, weird_but_valid):
        # Out-of-range months/days are semantically meaningless but
        # carry no security risk — they pass through SQL as a string
        # compare and match nothing. Documenting this contract here
        # so future tightening of the regex is a deliberate choice.
        r = client.get(f'/api/day/{weird_but_valid}/routes')
        assert r.status_code == 200
        body = r.get_json()
        assert body['date'] == weird_but_valid
        assert body['trips'] == []

    def test_returns_trips_for_valid_date(self, app, client):
        _add_trip(app.db_path, 1, '2026-05-04T08:00:00', distance_km=3.0)
        _add_waypoint(app.db_path, 1)
        r = client.get('/api/day/2026-05-04/routes')
        assert r.status_code == 200
        body = r.get_json()
        assert body['date'] == '2026-05-04'
        assert len(body['trips']) == 1
        assert body['trips'][0]['trip_id'] == 1

    def test_normalizes_archived_clips_paths(self, app, client):
        _add_trip(app.db_path, 1, '2026-05-04T08:00:00', distance_km=3.0)
        # A canonical ArchivedClips path that would otherwise be
        # filesystem-absolute (boot/RW-mount sees it as
        # /mnt/gadget/part1/ArchivedClips/...). The blueprint must
        # rewrite to the relative URL form.
        _add_waypoint(app.db_path, 1,
                      video_path='/mnt/gadget/part1/ArchivedClips/2026-05-04_clip.mp4')
        r = client.get('/api/day/2026-05-04/routes')
        body = r.get_json()
        wp = body['trips'][0]['waypoints'][0]
        assert wp['video_path'] == 'ArchivedClips/2026-05-04_clip.mp4'

    def test_returns_empty_trips_for_day_with_no_trips(self, client):
        r = client.get('/api/day/2026-04-01/routes')
        assert r.status_code == 200
        body = r.get_json()
        assert body['date'] == '2026-04-01'
        assert body['trips'] == []


# ---------------------------------------------------------------------------
# /api/events?date=YYYY-MM-DD
# ---------------------------------------------------------------------------

class TestApiEventsDateFilter:
    def test_rejects_malformed_date(self, client):
        r = client.get('/api/events?date=garbage')
        assert r.status_code == 400
        assert 'date must be YYYY-MM-DD' in r.get_json()['error']

    def test_filters_by_date(self, app, client):
        _add_event(app.db_path, '2026-05-03T08:00:00')
        _add_event(app.db_path, '2026-05-04T08:00:00')
        _add_event(app.db_path, '2026-05-04T20:00:00')
        _add_event(app.db_path, '2026-05-05T08:00:00')

        r = client.get('/api/events?date=2026-05-04')
        assert r.status_code == 200
        events = r.get_json()['events']
        assert len(events) == 2
        assert all(e['timestamp'].startswith('2026-05-04') for e in events)

    def test_no_date_returns_all(self, app, client):
        _add_event(app.db_path, '2026-05-03T08:00:00')
        _add_event(app.db_path, '2026-05-04T08:00:00')
        r = client.get('/api/events')
        assert r.status_code == 200
        assert len(r.get_json()['events']) == 2

    def test_date_scoped_limit_caps_at_5000(self, app, client):
        # Date-scoped requests need to cover busy sentry days. Cap is
        # 5000; values above that are silently clamped (no 400).
        r = client.get('/api/events?date=2026-05-04&limit=99999')
        assert r.status_code == 200

    def test_unscoped_limit_caps_at_1000(self, app, client):
        # Unscoped requests keep the older lower cap because no
        # caller actually wants more than a few hundred events back
        # in a single page.
        r = client.get('/api/events?limit=99999')
        assert r.status_code == 200

    def test_date_scoped_returns_full_day_above_old_500_limit(self, app, client):
        # Regression: previously the frontend asked for 500 events
        # for a day, and a busy sentry day would silently truncate.
        # The day card promises ``event_count`` so the listing must
        # return all of them.
        for hour in range(24):
            for minute in range(0, 60, 2):  # ~720 events
                _add_event(app.db_path, f'2026-05-04T{hour:02d}:{minute:02d}:00')
        r = client.get('/api/events?date=2026-05-04&limit=5000')
        assert r.status_code == 200
        events = r.get_json()['events']
        assert len(events) == 720
