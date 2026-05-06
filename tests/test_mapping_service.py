"""Tests for the mapping/geo-indexer service (mapping_service.py).

Covers: database schema, event detection rules, debouncing, trip queries,
background indexer with synthetic MP4 files.
"""

import json
import os
import struct
import sqlite3
import time
import pytest

from services.mapping_service import (
    _init_db,
    _detect_events,
    _debounce_events,
    _haversine_km,
    _timestamp_from_filename,
    _find_front_camera_videos,
    _index_video,
    boot_catchup_scan,
    canonical_key,
    candidate_db_paths,
    claim_next_queue_item,
    clear_all_queue,
    clear_pending_queue,
    clear_queue,
    complete_queue_item,
    compute_backoff,
    defer_queue_item,
    enqueue_for_indexing,
    enqueue_many_for_indexing,
    get_queue_status,
    index_single_file,
    IndexOutcome,
    IndexResult,
    priority_for_path,
    query_days,
    query_day_routes,
    query_trips,
    query_trip_route,
    query_events,
    get_stats,
    get_driving_stats,
    get_event_chart_data,
    recover_stale_claims,
    release_claim,
    start_daily_stale_scan,
    stop_daily_stale_scan,
    DEFAULT_THRESHOLDS,
    _PARSE_ERROR_MAX_ATTEMPTS,
    _PRIORITY_ARCHIVE,
    _PRIORITY_RECENT,
    _PRIORITY_SENTRY_SAVED,
    _SCHEMA_VERSION,
)
from services.dashcam_pb2 import SeiMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_box(name: str, content: bytes) -> bytes:
    size = 8 + len(content)
    return struct.pack('>I', size) + name.encode('ascii') + content


def _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=25.0,
                       gear=1, autopilot=0, heading=90.0,
                       accel_x=0.0, accel_y=0.0) -> bytes:
    msg = SeiMetadata()
    msg.latitude_deg = lat
    msg.longitude_deg = lon
    msg.heading_deg = heading
    msg.vehicle_speed_mps = speed
    msg.gear_state = gear
    msg.autopilot_state = autopilot
    msg.linear_acceleration_mps2_x = accel_x
    msg.linear_acceleration_mps2_y = accel_y
    msg.brake_applied = False
    msg.steering_wheel_angle = 0.0
    msg.frame_seq_no = 1
    return msg.SerializeToString()


def _make_sei_nal(protobuf_payload: bytes) -> bytes:
    nal_header = bytes([0x06, 0x05, 0x00])
    padding = bytes([0x42, 0x42, 0x42])
    marker = bytes([0x69])
    trailing = bytes([0x80])
    return nal_header + padding + marker + protobuf_payload + trailing


def _make_synthetic_mp4(sei_payloads, timescale=30000, frame_ticks=1001):
    """Build a minimal valid MP4 with SEI NAL units."""
    mdhd_content = struct.pack('>I', 0) + struct.pack('>I', 0) + struct.pack('>I', 0)
    mdhd_content += struct.pack('>I', timescale)
    mdhd_content += struct.pack('>I', frame_ticks * len(sei_payloads))
    mdhd_content += struct.pack('>I', 0)
    mdhd = _make_box('mdhd', mdhd_content)

    stts_content = struct.pack('>I', 0) + struct.pack('>I', 1)
    stts_content += struct.pack('>I', len(sei_payloads)) + struct.pack('>I', frame_ticks)
    stts = _make_box('stts', stts_content)

    avc1_inner = b'\x00' * 78
    avcc_content = bytes([0x01, 0x64, 0x00, 0x1F, 0xFF, 0xE1])
    avcc_content += struct.pack('>H', 4) + b'\x00' * 4
    avcc_content += bytes([0x01]) + struct.pack('>H', 4) + b'\x00' * 4
    avcc = _make_box('avcC', avcc_content)
    avc1 = _make_box('avc1', avc1_inner + avcc)
    stsd = _make_box('stsd', struct.pack('>I', 0) + struct.pack('>I', 1) + avc1)

    stbl = _make_box('stbl', stsd + stts)
    minf = _make_box('minf', stbl)
    mdia = _make_box('mdia', mdhd + minf)
    trak = _make_box('trak', mdia)
    moov = _make_box('moov', trak)

    mdat_content = bytearray()
    for pb in sei_payloads:
        sei_nal = _make_sei_nal(pb)
        mdat_content += struct.pack('>I', len(sei_nal)) + sei_nal
        idr = bytes([0x65, 0x00, 0x00, 0x01])
        mdat_content += struct.pack('>I', len(idr)) + idr

    mdat = _make_box('mdat', bytes(mdat_content))
    ftyp = _make_box('ftyp', b'mp42' + b'\x00' * 4)
    return ftyp + moov + mdat


# ---------------------------------------------------------------------------
# Database Schema Tests
# ---------------------------------------------------------------------------

class TestDatabase:
    def test_init_creates_tables(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)

        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        assert 'trips' in tables
        assert 'waypoints' in tables
        assert 'detected_events' in tables
        assert 'indexed_files' in tables
        assert 'schema_version' in tables
        conn.close()

    def test_schema_version_stored(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row['version'] == _SCHEMA_VERSION
        conn.close()

    def test_idempotent_init(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn1 = _init_db(db_path)
        conn1.execute("INSERT INTO trips (start_time) VALUES ('2025-01-01T00:00:00')")
        conn1.commit()
        conn1.close()

        # Second init should not drop data
        conn2 = _init_db(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        assert count == 1
        conn2.close()

    def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == 'wal'
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()


# ---------------------------------------------------------------------------
# Event Detection Tests
# ---------------------------------------------------------------------------

class TestEventDetection:
    def _make_waypoint(self, **overrides):
        defaults = {
            'timestamp': '2025-11-08T08:15:44',
            'lat': 37.7749, 'lon': -122.4194,
            'speed_mps': 25.0,
            'acceleration_x': 0.0, 'acceleration_y': 0.0,
            'autopilot_state': 'NONE',
            'steering_angle': 0.0,
            'gear': 'DRIVE',
            'brake_applied': 0,
            'video_path': 'test.mp4',
            'frame_offset': 0,
        }
        defaults.update(overrides)
        return defaults

    def test_harsh_brake_detected(self):
        wps = [self._make_waypoint(acceleration_x=-5.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert len(events) == 1
        assert events[0]['event_type'] == 'harsh_brake'
        assert events[0]['severity'] == 'warning'

    def test_emergency_brake_detected(self):
        wps = [self._make_waypoint(acceleration_x=-8.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        types = [e['event_type'] for e in events]
        assert 'emergency_brake' in types

    def test_hard_acceleration_detected(self):
        wps = [self._make_waypoint(acceleration_x=4.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert any(e['event_type'] == 'hard_acceleration' for e in events)

    def test_sharp_turn_detected(self):
        wps = [self._make_waypoint(acceleration_y=5.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert any(e['event_type'] == 'sharp_turn' for e in events)

    def test_speeding_detected(self):
        wps = [self._make_waypoint(speed_mps=40.0)]  # ~89 mph
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert any(e['event_type'] == 'speeding' for e in events)

    def test_no_speeding_below_threshold(self):
        wps = [self._make_waypoint(speed_mps=30.0)]  # ~67 mph
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert not any(e['event_type'] == 'speeding' for e in events)

    def test_fsd_disengage_detected(self):
        wps = [
            self._make_waypoint(autopilot_state='AUTOSTEER'),
            self._make_waypoint(autopilot_state='NONE',
                                timestamp='2025-11-08T08:15:45'),
        ]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert any(e['event_type'] == 'fsd_disengage' for e in events)

    def test_fsd_engage_detected(self):
        wps = [
            self._make_waypoint(autopilot_state='NONE'),
            self._make_waypoint(autopilot_state='SELF_DRIVING',
                                timestamp='2025-11-08T08:15:45'),
        ]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert any(e['event_type'] == 'fsd_engage' for e in events)

    def test_no_fsd_event_when_state_unchanged(self):
        wps = [
            self._make_waypoint(autopilot_state='NONE'),
            self._make_waypoint(autopilot_state='NONE'),
        ]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert not any(e['event_type'] in ('fsd_disengage', 'fsd_engage')
                       for e in events)

    def test_normal_driving_no_events(self):
        wps = [self._make_waypoint(acceleration_x=0.5, speed_mps=20.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        assert len(events) == 0

    def test_custom_thresholds(self):
        custom = dict(DEFAULT_THRESHOLDS)
        custom['harsh_brake_threshold'] = -2.0  # More sensitive
        wps = [self._make_waypoint(acceleration_x=-2.5)]
        events = _detect_events(wps, custom, 'test.mp4')
        assert any(e['event_type'] == 'harsh_brake' for e in events)

    def test_event_has_metadata_json(self):
        wps = [self._make_waypoint(acceleration_x=-5.0)]
        events = _detect_events(wps, DEFAULT_THRESHOLDS, 'test.mp4')
        metadata = json.loads(events[0]['metadata'])
        assert 'accel_x' in metadata
        assert 'speed_mps' in metadata


class TestDebounce:
    def test_deduplicates_within_window(self):
        events = [
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:00'},
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:02'},  # 2s later
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:04'},  # 4s later
        ]
        result = _debounce_events(events, window_seconds=5.0)
        assert len(result) == 1

    def test_keeps_events_outside_window(self):
        events = [
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:00'},
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:10'},  # 10s later
        ]
        result = _debounce_events(events, window_seconds=5.0)
        assert len(result) == 2

    def test_different_types_not_debounced(self):
        events = [
            {'event_type': 'harsh_brake', 'timestamp': '2025-01-01T00:00:00'},
            {'event_type': 'sharp_turn', 'timestamp': '2025-01-01T00:00:01'},
        ]
        result = _debounce_events(events, window_seconds=5.0)
        assert len(result) == 2

    def test_empty_list(self):
        assert _debounce_events([], 5.0) == []


# ---------------------------------------------------------------------------
# Utility Function Tests
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_zero_distance(self):
        assert _haversine_km(37.0, -122.0, 37.0, -122.0) == 0.0

    def test_known_distance(self):
        # SF to LA is roughly 559 km
        dist = _haversine_km(37.7749, -122.4194, 34.0522, -118.2437)
        assert 550 < dist < 570

    def test_short_distance(self):
        # ~111 meters (0.001 degrees latitude)
        dist = _haversine_km(37.0, -122.0, 37.001, -122.0)
        assert 0.1 < dist < 0.12


class TestTimestampFromFilename:
    def test_standard_tesla_filename(self):
        ts = _timestamp_from_filename('2025-11-08_08-15-44-front.mp4')
        assert ts == '2025-11-08T08:15:44'

    def test_with_full_path(self):
        ts = _timestamp_from_filename('/mnt/gadget/part1/TeslaCam/RecentClips/2025-11-08_08-15-44-front.mp4')
        assert ts == '2025-11-08T08:15:44'

    def test_invalid_filename(self):
        assert _timestamp_from_filename('random_file.mp4') is None

    def test_short_filename(self):
        assert _timestamp_from_filename('short.mp4') is None


class TestFindFrontCameraVideos:
    def test_finds_recent_clips(self, tmp_path):
        recent = tmp_path / "RecentClips"
        recent.mkdir()
        (recent / "2025-11-08_08-15-44-front.mp4").write_bytes(b'')
        (recent / "2025-11-08_08-15-44-back.mp4").write_bytes(b'')
        (recent / "2025-11-08_08-16-44-front.mp4").write_bytes(b'')

        videos = list(_find_front_camera_videos(str(tmp_path)))
        assert len(videos) == 2
        assert all('-front' in v for v in videos)

    def test_finds_saved_clips(self, tmp_path):
        saved = tmp_path / "SavedClips" / "2025-11-08_08-15-44"
        saved.mkdir(parents=True)
        (saved / "2025-11-08_08-15-44-front.mp4").write_bytes(b'')
        (saved / "2025-11-08_08-15-44-back.mp4").write_bytes(b'')

        videos = list(_find_front_camera_videos(str(tmp_path)))
        assert len(videos) == 1

    def test_empty_directory(self, tmp_path):
        assert list(_find_front_camera_videos(str(tmp_path))) == []


class TestCanonicalKey:
    """The canonical key is the queue/dedup primary key. Two paths share a
    canonical key iff they refer to the same recording."""

    def test_recent_clips_keys_on_basename(self):
        assert canonical_key(
            '/mnt/gadget/part1-ro/TeslaCam/RecentClips/2026-01-01_12-00-00-front.mp4'
        ) == '2026-01-01_12-00-00-front.mp4'

    def test_archived_clips_keys_on_basename(self):
        assert canonical_key(
            '/home/pi/ArchivedClips/2026-01-01_12-00-00-front.mp4'
        ) == '2026-01-01_12-00-00-front.mp4'

    def test_recent_and_archived_collide(self):
        """The whole point: same basename in Recent and Archived → same key."""
        rec = canonical_key('RecentClips/2026-01-01_12-00-00-front.mp4')
        arc = canonical_key('ArchivedClips/2026-01-01_12-00-00-front.mp4')
        assert rec == arc

    def test_saved_clips_keys_include_event_folder(self):
        key = canonical_key(
            '/mnt/gadget/part1-ro/TeslaCam/SavedClips/'
            '2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'
        )
        assert key == 'SavedClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'

    def test_sentry_clips_keys_include_event_folder(self):
        key = canonical_key(
            'SentryClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'
        )
        assert key == 'SentryClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'

    def test_different_events_dont_collide(self):
        """Two SavedClips events must not share a canonical key even if a
        clip basename happens to match (Tesla can use generic timestamps
        within an event)."""
        a = canonical_key(
            'SavedClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'
        )
        b = canonical_key(
            'SavedClips/2026-02-15_09-30-00/2026-01-01_12-00-00-front.mp4'
        )
        assert a != b

    def test_bare_basename_collides_with_recent(self):
        """Legacy DB rows storing just the basename must dedupe with their
        Recent/Archived siblings."""
        bare = canonical_key('2026-01-01_12-00-00-front.mp4')
        rec = canonical_key('RecentClips/2026-01-01_12-00-00-front.mp4')
        assert bare == rec

    def test_handles_windows_separators(self):
        """File paths on Windows / cross-platform tooling may use backslashes."""
        key = canonical_key(
            r'C:\TeslaCam\SentryClips\2026-01-01_12-00-00\2026-01-01_12-00-00-front.mp4'
        )
        assert key == 'SentryClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'


class TestCandidateDbPaths:
    def test_basename_key_expands_to_three_forms(self):
        paths = candidate_db_paths('2026-01-01_12-00-00-front.mp4')
        assert set(paths) == {
            '2026-01-01_12-00-00-front.mp4',
            'RecentClips/2026-01-01_12-00-00-front.mp4',
            'ArchivedClips/2026-01-01_12-00-00-front.mp4',
        }

    def test_event_folder_key_returns_only_itself(self):
        key = 'SavedClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4'
        assert candidate_db_paths(key) == [key]


# ---------------------------------------------------------------------------
# Query API Tests
# ---------------------------------------------------------------------------

class TestQueryAPIs:
    @pytest.fixture
    def db_with_data(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)

        # Insert test trip
        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, start_lat, start_lon,
               end_lat, end_lon, distance_km, duration_seconds, source_folder)
               VALUES (1, '2025-11-08T08:15:44', '2025-11-08T08:25:44',
               37.7749, -122.4194, 37.7850, -122.4100, 1.5, 600, 'RecentClips')"""
        )

        # Insert waypoints
        for i in range(5):
            conn.execute(
                """INSERT INTO waypoints (trip_id, timestamp, lat, lon, speed_mps,
                   autopilot_state, video_path, frame_offset)
                   VALUES (1, ?, ?, ?, 25.0, 'NONE', 'test.mp4', ?)""",
                (f'2025-11-08T08:1{5 + i}:44', 37.7749 + i * 0.001,
                 -122.4194 + i * 0.001, i * 30)
            )

        # Insert events
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
               event_type, severity, description, video_path)
               VALUES (1, '2025-11-08T08:17:44', 37.7769, -122.4174,
               'harsh_brake', 'warning', 'Harsh braking: -5.0 m/s²', 'test.mp4')"""
        )
        conn.commit()
        conn.close()
        return db_path

    def test_query_trips(self, db_with_data):
        trips = query_trips(db_with_data)
        assert len(trips) == 1
        assert trips[0]['source_folder'] == 'RecentClips'
        # Enrichment is now part of the same SQL — make sure it still
        # surfaces the per-trip event/video counts.
        assert trips[0]['event_count'] == 1
        assert trips[0]['video_count'] == 1

    def test_query_trips_zero_counts_when_empty(self, tmp_path):
        # A trip with no events and no waypoints must still come back with
        # numeric counts (not NULL) — the UI sorts/filters on these fields.
        db_path = str(tmp_path / "empty_trip.db")
        conn = _init_db(db_path)
        conn.execute(
            "INSERT INTO trips (id, start_time, end_time, distance_km, "
            "                   source_folder) "
            "VALUES (99, '2025-12-01T00:00:00', '2025-12-01T00:10:00', "
            "        1.0, 'RecentClips')"
        )
        conn.commit()
        conn.close()

        trips = query_trips(db_path)
        assert len(trips) == 1
        assert trips[0]['event_count'] == 0
        assert trips[0]['video_count'] == 0

    def test_query_trips_distinct_video_count(self, tmp_path):
        # Multiple waypoints sharing the same video_path must collapse to
        # ONE video, not N. This is the bug the covering index fixes.
        db_path = str(tmp_path / "distinct.db")
        conn = _init_db(db_path)
        conn.execute(
            "INSERT INTO trips (id, start_time, end_time, distance_km, "
            "                   source_folder) "
            "VALUES (1, '2025-12-01T00:00:00', '2025-12-01T00:10:00', "
            "        1.0, 'RecentClips')"
        )
        # 3 waypoints in same clip + 2 in another + 1 with NULL path
        for i in range(3):
            conn.execute(
                "INSERT INTO waypoints (trip_id, timestamp, lat, lon, "
                "                       video_path) "
                "VALUES (1, ?, 37.0, -122.0, 'RecentClips/clip_a-front.mp4')",
                (f'2025-12-01T00:00:{i:02d}',)
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO waypoints (trip_id, timestamp, lat, lon, "
                "                       video_path) "
                "VALUES (1, ?, 37.0, -122.0, 'RecentClips/clip_b-front.mp4')",
                (f'2025-12-01T00:01:{i:02d}',)
            )
        conn.execute(
            "INSERT INTO waypoints (trip_id, timestamp, lat, lon, "
            "                       video_path) "
            "VALUES (1, '2025-12-01T00:02:00', 37.0, -122.0, NULL)"
        )
        conn.commit()
        conn.close()

        trips = query_trips(db_path)
        assert trips[0]['video_count'] == 2  # NULL excluded; duplicates collapsed

    def test_query_trips_with_date_filter(self, db_with_data):
        trips = query_trips(db_with_data, date_from='2025-11-09')
        assert len(trips) == 0

    def test_query_trip_route(self, db_with_data):
        route = query_trip_route(db_with_data, trip_id=1)
        assert len(route) == 5
        assert 'lat' in route[0]
        assert 'lon' in route[0]

    def test_query_events(self, db_with_data):
        events = query_events(db_with_data)
        assert len(events) == 1
        assert events[0]['event_type'] == 'harsh_brake'

    def test_query_events_filter_type(self, db_with_data):
        events = query_events(db_with_data, event_type='speeding')
        assert len(events) == 0

    def test_get_stats(self, db_with_data):
        stats = get_stats(db_with_data)
        assert stats['trip_count'] == 1
        assert stats['waypoint_count'] == 5
        assert stats['event_count'] == 1
        assert stats['event_breakdown']['harsh_brake'] == 1


# ---------------------------------------------------------------------------
# Day-Based Query Tests (powering the day navigator)
# ---------------------------------------------------------------------------

class TestQueryDays:
    """Aggregate-by-day query that drives the bottom-left day card.

    Day-bucketing is by ``substr(timestamp, 1, 10)`` — NEVER ``date()``
    — because Tesla writes timezone-naive ISO strings and SQLite's
    ``date()`` would silently mis-bucket any row that ever gained a
    Z/offset suffix. Tests cover trip-only days, event-only days,
    mixed days, ordering, sentry events with NULL coords, and the
    min_distance_km filter (which must match /api/trips behaviour).
    """

    def _make_db(self, tmp_path, name='days.db'):
        db_path = str(tmp_path / name)
        conn = _init_db(db_path)
        return db_path, conn

    def _add_trip(self, conn, trip_id, start, end=None, distance_km=2.5):
        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, distance_km,
                                  duration_seconds, source_folder)
               VALUES (?, ?, ?, ?, 600, 'RecentClips')""",
            (trip_id, start, end or start, distance_km),
        )

    def _add_event(self, conn, trip_id, ts, event_type='harsh_brake',
                   lat=37.7749, lon=-122.4194):
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                            event_type, severity, description)
               VALUES (?, ?, ?, ?, ?, 'warning', 'test')""",
            (trip_id, ts, lat, lon, event_type),
        )

    def test_trip_only_day(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:15:00', '2026-05-04T08:25:00',
                       distance_km=3.2)
        conn.commit(); conn.close()

        days = query_days(db_path)
        assert len(days) == 1
        assert days[0]['date'] == '2026-05-04'
        assert days[0]['trip_count'] == 1
        assert days[0]['event_count'] == 0
        assert days[0]['sentry_count'] == 0
        assert days[0]['total_distance_km'] == pytest.approx(3.2)
        assert days[0]['first_start'] == '2026-05-04T08:15:00'
        assert days[0]['last_end'] == '2026-05-04T08:25:00'

    def test_event_only_day_surfaces_in_navigator(self, tmp_path):
        # A weekend at home with sentry events — no trips — must
        # still appear in the day navigator. This is the entire
        # point of the day-based redesign vs. trip-based.
        db_path, conn = self._make_db(tmp_path)
        self._add_event(conn, None, '2026-05-04T22:30:00',
                        event_type='sentry')
        conn.commit(); conn.close()

        days = query_days(db_path)
        assert len(days) == 1
        assert days[0]['date'] == '2026-05-04'
        assert days[0]['trip_count'] == 0
        assert days[0]['event_count'] == 1
        assert days[0]['sentry_count'] == 1
        assert days[0]['first_start'] is None
        assert days[0]['last_end'] is None

    def test_sentry_with_null_coords_still_counted(self, tmp_path):
        # Sentry events sometimes have NULL lat/lon (when SEI parsing
        # extracted no GPS). They must STILL count in event_count and
        # sentry_count so the day card stat reads truthfully — the
        # frontend handles "N events · location not available" UX.
        db_path, conn = self._make_db(tmp_path)
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                            event_type, severity, description)
               VALUES (NULL, '2026-05-04T22:30:00', NULL, NULL,
                       'sentry', 'info', 'no gps')"""
        )
        conn.commit(); conn.close()

        days = query_days(db_path)
        assert days[0]['event_count'] == 1
        assert days[0]['sentry_count'] == 1

    def test_mixed_day_combines_trip_and_event_stats(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:15:00', '2026-05-04T08:25:00',
                       distance_km=3.2)
        self._add_trip(conn, 2, '2026-05-04T17:00:00', '2026-05-04T17:30:00',
                       distance_km=5.5)
        self._add_event(conn, 1, '2026-05-04T08:20:00')
        self._add_event(conn, None, '2026-05-04T22:30:00',
                        event_type='sentry')
        conn.commit(); conn.close()

        days = query_days(db_path)
        assert len(days) == 1
        d = days[0]
        assert d['date'] == '2026-05-04'
        assert d['trip_count'] == 2
        assert d['total_distance_km'] == pytest.approx(8.7)
        assert d['event_count'] == 2
        assert d['sentry_count'] == 1
        assert d['first_start'] == '2026-05-04T08:15:00'
        assert d['last_end'] == '2026-05-04T17:30:00'

    def test_ordering_most_recent_first(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-04-01T08:00:00')
        self._add_trip(conn, 2, '2026-05-04T08:00:00')
        self._add_trip(conn, 3, '2026-04-15T08:00:00')
        conn.commit(); conn.close()

        days = query_days(db_path)
        assert [d['date'] for d in days] == [
            '2026-05-04', '2026-04-15', '2026-04-01'
        ]

    def test_min_distance_filter_excludes_parking_blips(self, tmp_path):
        # Default 50 m hides parking-lot blips. Without this filter
        # the day card would advertise "3 trips" while /api/trips
        # only returns 1 — the day card and the trips list must
        # agree.
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:15:00', distance_km=3.2)
        self._add_trip(conn, 2, '2026-05-04T08:30:00', distance_km=0.005)  # 5 m blip
        self._add_trip(conn, 3, '2026-05-04T09:00:00', distance_km=0.0)
        conn.commit(); conn.close()

        # With default filter (50 m) only the 3.2 km trip counts.
        days = query_days(db_path)
        assert days[0]['trip_count'] == 1
        assert days[0]['total_distance_km'] == pytest.approx(3.2)

        # With min_distance_km=0 every trip counts.
        days = query_days(db_path, min_distance_km=0.0)
        assert days[0]['trip_count'] == 3
        assert days[0]['total_distance_km'] == pytest.approx(3.205)

    def test_limit_clamps_results(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        for i in range(10):
            self._add_trip(conn, i + 1, f'2026-05-{i+1:02d}T08:00:00')
        conn.commit(); conn.close()

        days = query_days(db_path, limit=3)
        assert len(days) == 3
        # Most recent first
        assert days[0]['date'] == '2026-05-10'
        assert days[2]['date'] == '2026-05-08'

    def test_substr_bucketing_handles_naive_iso_timestamps(self, tmp_path):
        # Tesla writes naive ISO strings like '2026-05-04T08:15:00'.
        # substr(...,1,10) yields '2026-05-04' regardless of any future
        # suffix (Z, +offset). This test pins that contract — switching
        # to date() would silently mis-bucket Z-suffixed rows.
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T23:59:59',
                       end='2026-05-05T00:00:01')
        conn.commit(); conn.close()

        days = query_days(db_path)
        # Trip starting on 5/4 belongs to 5/4, even though it ended on 5/5.
        assert len(days) == 1
        assert days[0]['date'] == '2026-05-04'

    def test_empty_db(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        conn.commit(); conn.close()
        assert query_days(db_path) == []


class TestQueryDayRoutes:
    """Per-day route aggregator that drives the multi-trip overlay.

    Tests cover: multi-trip rendering, midnight-spanning trip
    bucketing, waypoint ordering within a trip, archive path
    handling deferred to the blueprint, empty days, and the
    min_distance filter matching ``/api/trips``.
    """

    def _make_db(self, tmp_path, name='day_routes.db'):
        db_path = str(tmp_path / name)
        conn = _init_db(db_path)
        return db_path, conn

    def _add_trip(self, conn, trip_id, start, end=None, distance_km=2.5,
                  source_folder='RecentClips'):
        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, start_lat,
                                  start_lon, end_lat, end_lon, distance_km,
                                  duration_seconds, source_folder)
               VALUES (?, ?, ?, 37.7, -122.4, 37.8, -122.5, ?, 600, ?)""",
            (trip_id, start, end or start, distance_km, source_folder),
        )

    def _add_waypoints(self, conn, trip_id, count, video_path='clip.mp4',
                       start_ts='2026-05-04T08:15:00'):
        for i in range(count):
            conn.execute(
                """INSERT INTO waypoints (trip_id, timestamp, lat, lon,
                                         speed_mps, autopilot_state,
                                         video_path, frame_offset)
                   VALUES (?, ?, ?, ?, 25.0, 'NONE', ?, ?)""",
                (trip_id, start_ts, 37.7 + i * 0.001, -122.4 + i * 0.001,
                 video_path, i * 30),
            )

    def test_multi_trip_day(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.2)
        self._add_waypoints(conn, 1, count=4)
        self._add_trip(conn, 2, '2026-05-04T17:00:00', distance_km=5.5)
        self._add_waypoints(conn, 2, count=3, video_path='clip2.mp4')
        # A trip on a different day must NOT be returned.
        self._add_trip(conn, 3, '2026-05-05T08:00:00')
        self._add_waypoints(conn, 3, count=2, video_path='clip3.mp4')
        conn.commit(); conn.close()

        result = query_day_routes(db_path, '2026-05-04')
        assert len(result['trips']) == 2
        # Ordered by start_time DESC — newest first.
        assert result['trips'][0]['trip_id'] == 2
        assert result['trips'][1]['trip_id'] == 1
        assert len(result['trips'][0]['waypoints']) == 3
        assert len(result['trips'][1]['waypoints']) == 4

    def test_midnight_spanning_trip_belongs_to_start_day(self, tmp_path):
        # User-confirmed bucketing rule: a trip that starts at 23:59:00
        # on 5/4 and ends at 00:30:00 on 5/5 is part of 5/4's day. The
        # day card reads truthfully and the route renders on the right
        # day. The opposite bucket (5/5) must NOT see this trip.
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T23:59:00',
                       end='2026-05-05T00:30:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=2)
        conn.commit(); conn.close()

        same_day = query_day_routes(db_path, '2026-05-04')
        next_day = query_day_routes(db_path, '2026-05-05')
        assert len(same_day['trips']) == 1
        assert same_day['trips'][0]['trip_id'] == 1
        assert next_day['trips'] == []

    def test_waypoints_ordered_by_id(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00')
        # Insert in shuffled order — query must still return ASC by id.
        self._add_waypoints(conn, 1, count=5)
        conn.commit(); conn.close()

        result = query_day_routes(db_path, '2026-05-04')
        ids = [wp['id'] for wp in result['trips'][0]['waypoints']]
        assert ids == sorted(ids)

    def test_min_distance_filter_excludes_parking_blips(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=2)
        self._add_trip(conn, 2, '2026-05-04T09:00:00', distance_km=0.005)
        self._add_waypoints(conn, 2, count=2, video_path='blip.mp4')
        conn.commit(); conn.close()

        # Default filter excludes the 5 m blip.
        result = query_day_routes(db_path, '2026-05-04')
        assert [t['trip_id'] for t in result['trips']] == [1]
        # min_distance_km=0 includes both.
        result = query_day_routes(db_path, '2026-05-04', min_distance_km=0.0)
        assert sorted(t['trip_id'] for t in result['trips']) == [1, 2]

    def test_empty_day_returns_empty_list(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00')
        self._add_waypoints(conn, 1, count=2)
        conn.commit(); conn.close()
        assert query_day_routes(db_path, '2026-04-01') == {'trips': []}

    def test_trips_with_no_waypoints_excluded_by_inner_join(self, tmp_path):
        # A trip row with no waypoints can't render — the INNER JOIN
        # excludes it. The day card's trip_count may legitimately
        # exceed len(result['trips']) on databases with phantom trips.
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00')
        # Trip 1 has zero waypoints.
        self._add_trip(conn, 2, '2026-05-04T09:00:00')
        self._add_waypoints(conn, 2, count=2)
        conn.commit(); conn.close()

        result = query_day_routes(db_path, '2026-05-04')
        assert [t['trip_id'] for t in result['trips']] == [2]


class TestQueryAllRoutesSimplified:
    """Subsampled all-trip overview that powers the All time map view.

    Tests cover: every-trip rendering, subsampling math (first +
    last preserved + stride sample of middle), min_distance filter
    matching /api/trips, ordering, and exclusion of trips that
    can't render a polyline.
    """

    def _make_db(self, tmp_path, name='all_routes.db'):
        db_path = str(tmp_path / name)
        conn = _init_db(db_path)
        return db_path, conn

    def _add_trip(self, conn, trip_id, start, end=None, distance_km=2.5,
                  source_folder='RecentClips'):
        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, start_lat,
                                  start_lon, end_lat, end_lon, distance_km,
                                  duration_seconds, source_folder)
               VALUES (?, ?, ?, 37.7, -122.4, 37.8, -122.5, ?, 600, ?)""",
            (trip_id, start, end or start, distance_km, source_folder),
        )

    def _add_waypoints(self, conn, trip_id, count, video_path='clip.mp4',
                       start_ts='2026-05-04T08:15:00'):
        for i in range(count):
            conn.execute(
                """INSERT INTO waypoints (trip_id, timestamp, lat, lon,
                                         speed_mps, autopilot_state,
                                         video_path, frame_offset)
                   VALUES (?, ?, ?, ?, 25.0, 'NONE', ?, ?)""",
                (trip_id, start_ts, 37.7 + i * 0.001, -122.4 + i * 0.001,
                 video_path, i * 30),
            )

    def test_all_trips_returned_ordered_newest_first(self, tmp_path):
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-03T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=4)
        self._add_trip(conn, 2, '2026-05-04T08:00:00', distance_km=5.0)
        self._add_waypoints(conn, 2, count=4, video_path='c2.mp4')
        self._add_trip(conn, 3, '2026-05-05T08:00:00', distance_km=8.0)
        self._add_waypoints(conn, 3, count=4, video_path='c3.mp4')
        conn.commit(); conn.close()

        trips = query_all_routes_simplified(db_path)
        assert [t['trip_id'] for t in trips] == [3, 2, 1]

    def test_each_trip_carries_its_start_date(self, tmp_path):
        # The client uses ``date`` to drill into the right day on
        # polyline click — must match substr(start_time, 1, 10), the
        # same bucketing rule query_days/query_day_routes use.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T23:59:00',
                       end='2026-05-05T00:30:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=2)
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path)
        assert trips[0]['date'] == '2026-05-04'

    def test_subsampling_keeps_first_and_last(self, tmp_path):
        # First + last waypoints anchor the polyline at the trip's
        # actual endpoints — a stride-only sample could drop one or
        # both. The kept count must also stay close to the cap so the
        # client renders quickly.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        # 200 waypoints at lat = 37.700, 37.701, ..., 37.899
        self._add_waypoints(conn, 1, count=200)
        conn.commit(); conn.close()

        trips = query_all_routes_simplified(db_path, max_points_per_trip=20)
        assert len(trips) == 1
        wps = trips[0]['waypoints']
        # Allow modest overshoot from the stride+endpoint logic.
        assert 18 <= len(wps) <= 25
        # First waypoint is preserved.
        assert wps[0]['lat'] == pytest.approx(37.700)
        # Last waypoint is preserved.
        assert wps[-1]['lat'] == pytest.approx(37.899)

    def test_subsampling_returns_all_when_count_under_cap(self, tmp_path):
        # A short trip (fewer waypoints than the cap) must round-trip
        # every point — subsampling should never drop data when there
        # is no rendering pressure to do so.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=8)
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path, max_points_per_trip=50)
        assert len(trips[0]['waypoints']) == 8

    def test_min_distance_filter_excludes_parking_blips(self, tmp_path):
        # Same default as /api/trips and /api/day/<date>/routes —
        # the All time overlay must not advertise trips other views
        # hide as parking-lot blips.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=4)
        self._add_trip(conn, 2, '2026-05-04T09:00:00', distance_km=0.005)
        self._add_waypoints(conn, 2, count=4, video_path='blip.mp4')
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path)
        assert [t['trip_id'] for t in trips] == [1]
        # min_distance=0 includes both.
        trips = query_all_routes_simplified(db_path, min_distance_km=0.0)
        assert sorted(t['trip_id'] for t in trips) == [1, 2]

    def test_trip_with_only_one_valid_waypoint_excluded(self, tmp_path):
        # Need >= 2 surviving lat/lon pairs to render a polyline;
        # otherwise Leaflet would draw nothing and the JSON payload
        # would just waste bandwidth. The schema enforces NOT NULL on
        # lat/lon so this guard kicks in via the <2-row count path.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=1)
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path)
        assert trips == []

    def test_trips_with_no_waypoints_excluded_by_inner_join(self, tmp_path):
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        # Trip 1 has zero waypoints — must be excluded.
        self._add_trip(conn, 2, '2026-05-04T09:00:00', distance_km=4.0)
        self._add_waypoints(conn, 2, count=3, video_path='c2.mp4')
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path)
        assert [t['trip_id'] for t in trips] == [2]

    def test_empty_db_returns_empty_list(self, tmp_path):
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        conn.commit(); conn.close()
        assert query_all_routes_simplified(db_path) == []

    def test_waypoints_chronological_within_each_trip(self, tmp_path):
        # Polyline rendering depends on the waypoints arriving in
        # the order the trip actually drove them — out-of-order
        # rows would draw a tangled mess.
        from services.mapping_service import query_all_routes_simplified
        db_path, conn = self._make_db(tmp_path)
        self._add_trip(conn, 1, '2026-05-04T08:00:00', distance_km=3.0)
        self._add_waypoints(conn, 1, count=10)
        conn.commit(); conn.close()
        trips = query_all_routes_simplified(db_path, max_points_per_trip=50)
        lats = [w['lat'] for w in trips[0]['waypoints']]
        assert lats == sorted(lats)


class TestQueryEventsWithDate:
    """The single-day filter on /api/events powers the day-based map.

    Must use substr() bucketing (same contract as query_days) and
    must still return events with NULL lat/lon so the day card can
    advertise them, even though the map skips rendering null
    markers client-side.
    """

    def _make_db(self, tmp_path, name='events_date.db'):
        db_path = str(tmp_path / name)
        conn = _init_db(db_path)
        return db_path, conn

    def test_filter_returns_only_matching_day(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        for ts in ('2026-05-03T08:00:00', '2026-05-04T08:00:00',
                   '2026-05-04T20:00:00', '2026-05-05T08:00:00'):
            conn.execute(
                """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                                event_type, severity, description)
                   VALUES (NULL, ?, 37.7, -122.4, 'harsh_brake',
                           'warning', 'test')""",
                (ts,),
            )
        conn.commit(); conn.close()

        events = query_events(db_path, date='2026-05-04')
        assert len(events) == 2
        assert all(e['timestamp'].startswith('2026-05-04') for e in events)

    def test_filter_includes_null_lat_lon_rows(self, tmp_path):
        # Day card stats include null-coord events; the listing
        # endpoint must return them too so the client can show
        # "N events · location not available" guidance.
        db_path, conn = self._make_db(tmp_path)
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                            event_type, severity, description)
               VALUES (NULL, '2026-05-04T22:00:00', NULL, NULL,
                       'sentry', 'info', 'no gps')"""
        )
        conn.commit(); conn.close()

        events = query_events(db_path, date='2026-05-04')
        assert len(events) == 1
        assert events[0]['lat'] is None
        assert events[0]['lon'] is None

    def test_filter_combined_with_event_type(self, tmp_path):
        db_path, conn = self._make_db(tmp_path)
        for evt in ('harsh_brake', 'sentry', 'speeding'):
            conn.execute(
                """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                                event_type, severity, description)
                   VALUES (NULL, '2026-05-04T08:00:00', 37.7, -122.4,
                           ?, 'warning', 'x')""",
                (evt,),
            )
        conn.commit(); conn.close()

        events = query_events(db_path, date='2026-05-04', event_type='sentry')
        assert len(events) == 1
        assert events[0]['event_type'] == 'sentry'

    def test_no_date_returns_all(self, tmp_path):
        # Backwards compat: omitting `date` returns everything.
        db_path, conn = self._make_db(tmp_path)
        for ts in ('2026-05-04T08:00:00', '2026-05-05T08:00:00'):
            conn.execute(
                """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                                                event_type, severity, description)
                   VALUES (NULL, ?, 37.7, -122.4, 'harsh_brake',
                           'warning', 't')""",
                (ts,),
            )
        conn.commit(); conn.close()
        assert len(query_events(db_path)) == 2


# ---------------------------------------------------------------------------
# End-to-End Indexing Tests
# ---------------------------------------------------------------------------

def _unpack(result: IndexResult):
    """Tests historically asserted on ``(waypoint_count, event_count)``;
    keep that shape locally so individual tests stay readable while the
    public API returns the structured :class:`IndexResult`."""
    return result.waypoints, result.events


class TestIndexVideo:
    def test_index_synthetic_video(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)

        # Create synthetic video with GPS data
        payloads = [
            _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=25.0),
            _make_sei_protobuf(lat=37.7750, lon=-122.4195, speed=26.0),
            _make_sei_protobuf(lat=37.7751, lon=-122.4196, speed=27.0),
        ]
        mp4_data = _make_synthetic_mp4(payloads)

        teslacam = tmp_path / "TeslaCam" / "RecentClips"
        teslacam.mkdir(parents=True)
        video_file = teslacam / "2025-11-08_08-15-44-front.mp4"
        video_file.write_bytes(mp4_data)

        wc, ec = _unpack(_index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        ))

        assert wc == 3
        trips = conn.execute("SELECT * FROM trips").fetchall()
        assert len(trips) == 1

        waypoints = conn.execute("SELECT * FROM waypoints").fetchall()
        assert len(waypoints) == 3
        conn.close()

    def test_index_with_events(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)

        payloads = [
            _make_sei_protobuf(lat=37.7749, lon=-122.4194, speed=25.0, accel_x=-6.0),
            _make_sei_protobuf(lat=37.7750, lon=-122.4195, speed=26.0, accel_x=0.0),
        ]
        mp4_data = _make_synthetic_mp4(payloads)

        teslacam = tmp_path / "TeslaCam" / "RecentClips"
        teslacam.mkdir(parents=True)
        video_file = teslacam / "2025-11-08_08-15-44-front.mp4"
        video_file.write_bytes(mp4_data)

        wc, ec = _unpack(_index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        ))

        assert wc == 2
        assert ec >= 1  # Should detect harsh braking

        events = conn.execute("SELECT * FROM detected_events").fetchall()
        assert len(events) >= 1
        assert any(e['event_type'] == 'harsh_brake' for e in events)
        conn.close()

    def test_skip_no_gps_video(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _init_db(db_path)

        # Video with lat=0, lon=0 (no GPS)
        payloads = [_make_sei_protobuf(lat=0.0, lon=0.0)]
        mp4_data = _make_synthetic_mp4(payloads)

        teslacam = tmp_path / "TeslaCam" / "RecentClips"
        teslacam.mkdir(parents=True)
        video_file = teslacam / "2025-11-08_08-15-44-front.mp4"
        video_file.write_bytes(mp4_data)

        result = _index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        )

        assert result.waypoints == 0
        assert result.events == 0
        # Recent-folder no-GPS clips are recorded as NO_GPS_RECORDED so the
        # queue worker can drop the row without flapping retries.
        assert result.outcome == IndexOutcome.NO_GPS_RECORDED
        conn.close()


# ---------------------------------------------------------------------------
# Driving Stats & Event Chart Data Tests
# ---------------------------------------------------------------------------

class TestDrivingStats:
    @pytest.fixture
    def db_with_driving_data(self, tmp_path):
        db_path = str(tmp_path / "stats.db")
        conn = _init_db(db_path)

        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, start_lat, start_lon,
               end_lat, end_lon, distance_km, duration_seconds, source_folder)
               VALUES (1, '2025-11-08T08:15:44', '2025-11-08T08:25:44',
               37.7749, -122.4194, 37.7850, -122.4100, 15.5, 600, 'RecentClips')"""
        )
        conn.execute(
            """INSERT INTO trips (id, start_time, end_time, distance_km, duration_seconds, source_folder)
               VALUES (2, '2025-11-09T10:00:00', '2025-11-09T10:30:00', 25.0, 1800, 'RecentClips')"""
        )

        # Waypoints with mixed autopilot states
        for i in range(10):
            ap = 'AUTOSTEER' if i < 4 else 'NONE'
            conn.execute(
                """INSERT INTO waypoints (trip_id, timestamp, lat, lon, speed_mps,
                   autopilot_state, video_path, frame_offset)
                   VALUES (1, ?, ?, ?, ?, ?, 'test.mp4', ?)""",
                (f'2025-11-08T08:1{5+i}:44', 37.77 + i*0.001, -122.41 + i*0.001,
                 20.0 + i, ap, i*30)
            )

        # Events
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
               event_type, severity, description)
               VALUES (1, '2025-11-08T08:17:44', 37.77, -122.41,
               'harsh_brake', 'warning', 'test')"""
        )
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
               event_type, severity, description)
               VALUES (1, '2025-11-08T08:18:44', 37.77, -122.41,
               'speeding', 'info', 'test')"""
        )
        conn.execute(
            """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
               event_type, severity, description)
               VALUES (1, '2025-11-08T08:19:44', 37.77, -122.41,
               'emergency_brake', 'critical', 'test')"""
        )

        conn.commit()
        conn.close()
        return db_path

    def test_has_data(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        assert stats['has_data'] is True

    def test_trip_count(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        assert stats['trip_count'] == 2

    def test_total_distance(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        assert stats['total_distance_km'] == 40.5  # 15.5 + 25.0

    def test_fsd_usage(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        # 4 out of 10 waypoints are AUTOSTEER = 40%
        assert stats['fsd_usage_pct'] == 40.0

    def test_event_counts(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        assert stats['total_events'] == 3
        assert stats['warning_events'] == 2  # 1 warning + 1 critical

    def test_events_per_100km(self, db_with_driving_data):
        stats = get_driving_stats(db_with_driving_data)
        # 2 warning/critical events / 40.5 km * 100 = ~4.9
        assert 4.0 < stats['events_per_100km'] < 6.0

    def test_empty_db(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        _init_db(db_path)
        stats = get_driving_stats(db_path)
        assert stats['has_data'] is False


class TestEventChartData:
    @pytest.fixture
    def db_with_events(self, tmp_path):
        db_path = str(tmp_path / "charts.db")
        conn = _init_db(db_path)

        # Need a trip for FK constraints
        conn.execute(
            """INSERT INTO trips (id, start_time, distance_km, duration_seconds, source_folder)
               VALUES (1, '2025-11-08T08:10:00', 10.0, 600, 'RecentClips')"""
        )

        # Insert waypoints with FSD data
        for i in range(5):
            ap = 'SELF_DRIVING' if i < 2 else 'NONE'
            conn.execute(
                """INSERT INTO waypoints (trip_id, timestamp, lat, lon, speed_mps,
                   autopilot_state) VALUES (1, ?, 37.0, -122.0, 25.0, ?)""",
                (f'2025-11-08T08:1{i}:00', ap)
            )

        events = [
            ('harsh_brake', 'warning'), ('harsh_brake', 'warning'),
            ('speeding', 'info'), ('emergency_brake', 'critical'),
            ('fsd_disengage', 'warning'),
        ]
        for i, (etype, sev) in enumerate(events):
            conn.execute(
                """INSERT INTO detected_events (trip_id, timestamp, lat, lon,
                   event_type, severity, description)
                   VALUES (1, ?, 37.0, -122.0, ?, ?, 'test')""",
                (f'2025-11-08T08:1{i}:00', etype, sev)
            )

        conn.commit()
        conn.close()
        return db_path

    def test_by_type(self, db_with_events):
        data = get_event_chart_data(db_with_events)
        assert len(data['by_type']['labels']) > 0
        assert sum(data['by_type']['values']) == 5

    def test_by_severity(self, db_with_events):
        data = get_event_chart_data(db_with_events)
        assert len(data['by_severity']['labels']) == 3  # critical, warning, info
        assert len(data['by_severity']['colors']) == 3

    def test_over_time(self, db_with_events):
        data = get_event_chart_data(db_with_events)
        assert 'labels' in data['over_time']
        assert 'values' in data['over_time']

    def test_fsd_timeline(self, db_with_events):
        data = get_event_chart_data(db_with_events)
        assert 'labels' in data['fsd_timeline']
        assert 'fsd' in data['fsd_timeline']
        assert 'manual' in data['fsd_timeline']

    def test_empty_db(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        _init_db(db_path)
        data = get_event_chart_data(db_path)
        assert data['by_type']['labels'] == []
        assert data['by_type']['values'] == []




# ---------------------------------------------------------------------------
# IndexResult Outcome Dispatch Tests
# ---------------------------------------------------------------------------

class TestIndexResultOutcomes:
    """Each non-INDEXED outcome maps to a specific queue dispatch decision.
    These tests pin the contract so the worker can rely on it."""

    def test_terminal_outcomes(self):
        # All of these allow the queue worker to delete the row.
        for outcome in (
            IndexOutcome.INDEXED,
            IndexOutcome.ALREADY_INDEXED,
            IndexOutcome.DUPLICATE_UPGRADED,
            IndexOutcome.NO_GPS_RECORDED,
            IndexOutcome.NOT_FRONT_CAMERA,
            IndexOutcome.FILE_MISSING,
        ):
            assert IndexResult(outcome).terminal, outcome

    def test_non_terminal_outcomes_require_retry(self):
        # The queue must NOT delete these — worker either reschedules
        # (TOO_NEW), backs off (PARSE_ERROR), or releases the claim
        # (DB_BUSY).
        for outcome in (
            IndexOutcome.TOO_NEW,
            IndexOutcome.PARSE_ERROR,
            IndexOutcome.DB_BUSY,
        ):
            assert not IndexResult(outcome).terminal, outcome


class TestIndexSingleFileOutcomes:
    def test_not_front_camera(self, tmp_path):
        # Right basename for a Tesla clip but wrong camera.
        db = str(tmp_path / "geo.db")
        _init_db(db)
        clip = tmp_path / "2025-11-08_08-15-44-back.mp4"
        clip.write_bytes(b'')

        result = index_single_file(str(clip), db, str(tmp_path))
        assert result.outcome == IndexOutcome.NOT_FRONT_CAMERA
        assert result.terminal

    def test_file_missing(self, tmp_path):
        db = str(tmp_path / "geo.db")
        _init_db(db)
        result = index_single_file(
            str(tmp_path / "does-not-exist-front.mp4"),
            db,
            str(tmp_path),
        )
        assert result.outcome == IndexOutcome.FILE_MISSING
        assert result.terminal

    def test_too_new(self, tmp_path):
        # File exists but mtime is now() — Tesla may still be writing.
        db = str(tmp_path / "geo.db")
        _init_db(db)
        clip = tmp_path / "2025-11-08_08-15-44-front.mp4"
        clip.write_bytes(b'')
        result = index_single_file(str(clip), db, str(tmp_path))
        assert result.outcome == IndexOutcome.TOO_NEW
        assert not result.terminal  # worker should retry once mtime ages

    def test_parse_error_caught(self, tmp_path):
        # Old-enough file (>120s) with no MP4 atoms at all → parser raises.
        # Result is reported as PARSE_ERROR so the queue worker can apply
        # exponential backoff instead of looping forever.
        import os as _os
        import time as _time
        db = str(tmp_path / "geo.db")
        _init_db(db)
        clip = tmp_path / "2025-11-08_08-15-44-front.mp4"
        clip.write_bytes(b'not an mp4 file at all')
        # Backdate so the TOO_NEW guard doesn't intercept us.
        old = _time.time() - 600
        _os.utime(str(clip), (old, old))

        result = index_single_file(str(clip), db, str(tmp_path))
        # Could legitimately come back as either NO_GPS_RECORDED (parser
        # found 0 SEI frames) or PARSE_ERROR (parser raised). Both are
        # acceptable terminal-or-retry classifications — the assertion
        # we care about is that we never get an INDEXED with 0 waypoints.
        assert result.outcome in (
            IndexOutcome.NO_GPS_RECORDED,
            IndexOutcome.PARSE_ERROR,
        )
        if result.outcome == IndexOutcome.PARSE_ERROR:
            assert result.error is not None
            assert not result.terminal


# ---------------------------------------------------------------------------
# Phase 2: Indexing queue
# ---------------------------------------------------------------------------


class TestPriorityForPath:
    def test_sentry_clip_is_highest_priority(self):
        path = '/mnt/teslacam/SentryClips/2025-01-01_event/clip-front.mp4'
        assert priority_for_path(path) == _PRIORITY_SENTRY_SAVED

    def test_saved_clip_is_highest_priority(self):
        path = '/mnt/teslacam/SavedClips/event/clip-front.mp4'
        assert priority_for_path(path) == _PRIORITY_SENTRY_SAVED

    def test_archived_clip_lower_than_event(self):
        path = '/mnt/sd/ArchivedClips/2025-01-01/clip-front.mp4'
        assert priority_for_path(path) == _PRIORITY_ARCHIVE
        assert _PRIORITY_ARCHIVE > _PRIORITY_SENTRY_SAVED

    def test_recent_clip_lowest_among_known_folders(self):
        path = '/mnt/teslacam/RecentClips/clip-front.mp4'
        assert priority_for_path(path) == _PRIORITY_RECENT
        assert _PRIORITY_RECENT > _PRIORITY_ARCHIVE

    def test_windows_path_separator_is_normalized(self):
        path = r'D:\TeslaCam\SentryClips\event\clip-front.mp4'
        assert priority_for_path(path) == _PRIORITY_SENTRY_SAVED

    def test_unknown_folder_gets_default(self):
        path = '/some/random/place/clip.mp4'
        assert priority_for_path(path) == 50

    def test_empty_path_gets_default(self):
        assert priority_for_path('') == 50


class TestComputeBackoff:
    def test_first_failure_uses_base_backoff(self):
        # attempts=0 means "no failures yet, computing wait for the first
        # retry". delay = base * 2^0 = base.
        assert compute_backoff(0) == 60.0

    def test_backoff_doubles_each_attempt(self):
        assert compute_backoff(1) == 120.0
        assert compute_backoff(2) == 240.0
        assert compute_backoff(3) == 480.0

    def test_backoff_is_capped(self):
        # 60 * 2^10 = 61440, well past the 3600 cap.
        assert compute_backoff(10) == 3600.0

    def test_negative_attempts_treated_as_zero(self):
        assert compute_backoff(-5) == 60.0


class TestEnqueue:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "queue.db")
        _init_db(db_path)
        return db_path

    def test_enqueue_writes_one_row(self, db):
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/2025-01-01_clip-front.mp4',
            source='watcher',
        )
        with sqlite3.connect(db) as c:
            rows = c.execute("SELECT * FROM indexing_queue").fetchall()
        assert len(rows) == 1

    def test_enqueue_uses_canonical_key(self, db):
        # Same canonical_key for both Recent and Archived versions.
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/2025-01-01_clip-front.mp4',
        )
        assert enqueue_for_indexing(
            db, '/mnt/sd/ArchivedClips/2025-01-01_clip-front.mp4',
        )
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        # One row, deduplicated by canonical_key.
        assert count == 1

    def test_enqueue_lowers_priority_when_more_urgent(self, db):
        # First enqueue at default (50), then upgrade to sentry priority.
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',  # canonical_key = "clip.mp4"
            priority=50,
        )
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
            priority=10,
        )
        with sqlite3.connect(db) as c:
            prio = c.execute(
                "SELECT priority FROM indexing_queue"
            ).fetchone()[0]
        assert prio == 10

    def test_enqueue_does_not_raise_priority(self, db):
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
            priority=10,
        )
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
            priority=50,
        )
        with sqlite3.connect(db) as c:
            prio = c.execute(
                "SELECT priority FROM indexing_queue"
            ).fetchone()[0]
        # MIN(50, 10) = 10 — re-enqueue at lower priority is a no-op.
        assert prio == 10

    def test_enqueue_empty_path_returns_false(self, db):
        assert enqueue_for_indexing(db, '') is False
        assert enqueue_for_indexing(db, None) is False  # type: ignore

    def test_enqueue_does_not_overwrite_claimed_row(self, db):
        # Simulate a worker holding a claim. A new enqueue for the same
        # canonical_key must NOT change file_path or source — that would
        # rip the file out from under the worker.
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
            source='watcher',
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue SET claimed_by='w1', claimed_at=?
                   WHERE canonical_key='clip.mp4'""",
                (time.time(),),
            )
        # Try to "upgrade" the path/source while it's claimed.
        enqueue_for_indexing(
            db, '/mnt/sd/ArchivedClips/clip.mp4',
            source='archive',
        )
        with sqlite3.connect(db) as c:
            row = c.execute(
                """SELECT file_path, source FROM indexing_queue
                   WHERE canonical_key='clip.mp4'"""
            ).fetchone()
        assert row[0] == '/mnt/teslacam/RecentClips/clip.mp4'
        assert row[1] == 'watcher'

    def test_enqueue_with_next_attempt_at_defers_first_claim(self, db):
        # Producers (the archive flow in particular) need to defer the
        # first attempt atomically with the INSERT to avoid racing the
        # worker. Verify the deferral lands on a fresh row.
        future = time.time() + 120
        assert enqueue_for_indexing(
            db, '/mnt/sd/ArchivedClips/clip-front.mp4',
            source='archive',
            next_attempt_at=future,
        ) is True
        with sqlite3.connect(db) as c:
            row = c.execute(
                """SELECT next_attempt_at FROM indexing_queue
                   WHERE canonical_key='clip-front.mp4'"""
            ).fetchone()
        assert abs(row[0] - future) < 0.01

    def test_enqueue_without_next_attempt_at_is_immediate(self, db):
        # The default is "available right now" so the watcher path
        # doesn't need to know about the deferral feature.
        assert enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip-front.mp4',
        ) is True
        with sqlite3.connect(db) as c:
            row = c.execute(
                """SELECT next_attempt_at FROM indexing_queue
                   WHERE canonical_key='clip-front.mp4'"""
            ).fetchone()
        assert row[0] == 0.0


class TestEnqueueMany:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "queue.db")
        _init_db(db_path)
        return db_path

    def test_batch_inserts_all(self, db):
        items = [
            ('/mnt/teslacam/RecentClips/a-front.mp4', None),
            ('/mnt/teslacam/RecentClips/b-front.mp4', None),
            ('/mnt/teslacam/SentryClips/event/c-front.mp4', None),
        ]
        n = enqueue_many_for_indexing(db, items, source='catchup')
        assert n == 3
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 3

    def test_batch_dedups_by_canonical_key(self, db):
        # The Recent and Archived versions share canonical_key — second
        # one collapses into the first.
        items = [
            ('/mnt/teslacam/RecentClips/clip-front.mp4', None),
            ('/mnt/sd/ArchivedClips/clip-front.mp4', None),
        ]
        enqueue_many_for_indexing(db, items)
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 1

    def test_batch_skips_empty(self, db):
        items = [
            ('', None),
            ('/mnt/teslacam/RecentClips/a-front.mp4', None),
        ]
        n = enqueue_many_for_indexing(db, items)
        assert n == 1


class TestClaimQueueItem:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "queue.db")
        _init_db(db_path)
        return db_path

    def test_returns_none_when_empty(self, db):
        assert claim_next_queue_item(db, 'worker-1') is None

    def test_claim_returns_highest_priority_first(self, db):
        # Insert in random order.
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/recent.mp4',
            priority=_PRIORITY_RECENT,
        )
        enqueue_for_indexing(
            db, '/mnt/teslacam/SentryClips/event/sentry-front.mp4',
            priority=_PRIORITY_SENTRY_SAVED,
        )
        enqueue_for_indexing(
            db, '/mnt/sd/ArchivedClips/archive.mp4',
            priority=_PRIORITY_ARCHIVE,
        )
        row = claim_next_queue_item(db, 'worker-1')
        assert row is not None
        assert row['canonical_key'] == 'SentryClips/event/sentry-front.mp4'
        assert row['priority'] == _PRIORITY_SENTRY_SAVED

    def test_claim_marks_row_claimed(self, db):
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        claim_next_queue_item(db, 'worker-X')
        with sqlite3.connect(db) as c:
            row = c.execute(
                "SELECT claimed_by, claimed_at FROM indexing_queue"
            ).fetchone()
        assert row[0] == 'worker-X'
        assert row[1] is not None

    def test_two_concurrent_claims_dont_double_book(self, db, tmp_path):
        # The atomic-claim contract: even with two threads racing, a
        # given canonical_key can only be picked once per release cycle.
        # Enqueue 5 items, spawn 2 worker threads, each claiming as fast
        # as possible. No canonical_key should appear in both workers'
        # results.
        import threading
        for i in range(5):
            enqueue_for_indexing(
                db, f'/mnt/teslacam/RecentClips/clip{i}.mp4',
            )
        results = {'a': [], 'b': []}

        def claim_loop(label):
            for _ in range(10):
                row = claim_next_queue_item(db, label)
                if row is None:
                    break
                results[label].append(row['canonical_key'])

        ta = threading.Thread(target=claim_loop, args=('a',))
        tb = threading.Thread(target=claim_loop, args=('b',))
        ta.start(); tb.start()
        ta.join(timeout=10); tb.join(timeout=10)

        all_claimed = results['a'] + results['b']
        assert len(all_claimed) == 5
        assert len(set(all_claimed)) == 5  # No duplicates.

    def test_claim_skips_future_attempts(self, db):
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        # Defer to 100s in the future.
        defer_queue_item(
            db, 'clip.mp4', time.time() + 100,
        )
        assert claim_next_queue_item(db, 'worker-1') is None

    def test_claim_skips_dead_letter_rows(self, db):
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        # Drive attempts past the cap.
        with sqlite3.connect(db) as c:
            c.execute(
                "UPDATE indexing_queue SET attempts = ?",
                (_PARSE_ERROR_MAX_ATTEMPTS,),
            )
        assert claim_next_queue_item(db, 'worker-1') is None


class TestCompleteAndRelease:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "queue.db")
        _init_db(db_path)
        enqueue_for_indexing(
            db_path, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        claim_next_queue_item(db_path, 'worker-1')
        return db_path

    def test_complete_deletes_row(self, db):
        assert complete_queue_item(db, 'clip.mp4') is True
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 0

    def test_complete_no_row_returns_false(self, db):
        assert complete_queue_item(db, 'nonexistent.mp4') is False

    def test_release_clears_claim_but_keeps_row(self, db):
        assert release_claim(db, 'clip.mp4') is True
        with sqlite3.connect(db) as c:
            row = c.execute(
                "SELECT claimed_by, claimed_at, attempts FROM indexing_queue"
            ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] == 0  # release does NOT bump attempts

    def test_after_release_can_be_reclaimed(self, db):
        release_claim(db, 'clip.mp4')
        row = claim_next_queue_item(db, 'worker-2')
        assert row is not None
        assert row['canonical_key'] == 'clip.mp4'


class TestDeferQueueItem:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "queue.db")
        _init_db(db_path)
        enqueue_for_indexing(
            db_path, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        claim_next_queue_item(db_path, 'worker-1')
        return db_path

    def test_defer_without_bump_does_not_increment_attempts(self, db):
        future = time.time() + 200
        assert defer_queue_item(
            db, 'clip.mp4', future, bump_attempts=False,
        )
        with sqlite3.connect(db) as c:
            row = c.execute(
                """SELECT attempts, next_attempt_at, claimed_by
                   FROM indexing_queue"""
            ).fetchone()
        assert row[0] == 0
        assert abs(row[1] - future) < 1e-3
        assert row[2] is None

    def test_defer_with_bump_increments_attempts(self, db):
        defer_queue_item(
            db, 'clip.mp4', time.time() + 60,
            bump_attempts=True, last_error='boom',
        )
        with sqlite3.connect(db) as c:
            row = c.execute(
                "SELECT attempts, last_error FROM indexing_queue"
            ).fetchone()
        assert row[0] == 1
        assert row[1] == 'boom'


class TestRecoverStaleClaims:
    def test_releases_old_claim(self, tmp_path):
        db = str(tmp_path / "stale.db")
        _init_db(db)
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        # Manually plant an ancient claim.
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue
                   SET claimed_by='dead-worker', claimed_at=?""",
                (time.time() - 7200,),  # 2 hours ago
            )
        n = recover_stale_claims(db, max_age_seconds=1800)
        assert n == 1
        with sqlite3.connect(db) as c:
            row = c.execute(
                "SELECT claimed_by FROM indexing_queue"
            ).fetchone()
        assert row[0] is None

    def test_keeps_recent_claim(self, tmp_path):
        db = str(tmp_path / "stale.db")
        _init_db(db)
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/clip.mp4',
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue
                   SET claimed_by='active-worker', claimed_at=?""",
                (time.time() - 60,),  # 1 minute ago
            )
        assert recover_stale_claims(db, max_age_seconds=1800) == 0


class TestQueueStatus:
    def test_status_on_empty_queue(self, tmp_path):
        db = str(tmp_path / "q.db")
        _init_db(db)
        st = get_queue_status(db)
        assert st['queue_depth'] == 0
        assert st['claimed_count'] == 0
        assert st['dead_letter_count'] == 0
        assert st['next_ready_at'] is None

    def test_status_reflects_state(self, tmp_path):
        db = str(tmp_path / "q.db")
        _init_db(db)
        # Three pending, one claimed, one dead-lettered.
        for i in range(3):
            enqueue_for_indexing(
                db, f'/mnt/teslacam/RecentClips/p{i}.mp4',
            )
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/claimed.mp4',
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue
                   SET claimed_by='w', claimed_at=?
                   WHERE canonical_key='claimed.mp4'""",
                (time.time(),),
            )
        enqueue_for_indexing(
            db, '/mnt/teslacam/RecentClips/dead.mp4',
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """UPDATE indexing_queue SET attempts=?
                   WHERE canonical_key='dead.mp4'""",
                (_PARSE_ERROR_MAX_ATTEMPTS,),
            )
        st = get_queue_status(db)
        assert st['queue_depth'] == 3
        assert st['claimed_count'] == 1
        assert st['dead_letter_count'] == 1
        assert st['next_ready_at'] is not None


class TestClearQueue:
    def test_removes_everything(self, tmp_path):
        db = str(tmp_path / "q.db")
        _init_db(db)
        for i in range(5):
            enqueue_for_indexing(
                db, f'/mnt/teslacam/RecentClips/c{i}.mp4',
            )
        n = clear_queue(db)
        assert n == 5
        with sqlite3.connect(db) as c:
            assert c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0] == 0

    def test_clear_pending_preserves_claimed_rows(self, tmp_path):
        # /api/index/cancel must keep the in-flight file's claim row
        # intact so the worker can finish the file without its
        # owner-guarded complete failing on a vanished row.
        db = str(tmp_path / "q.db")
        _init_db(db)
        for i in range(3):
            enqueue_for_indexing(
                db, f'/mnt/teslacam/RecentClips/c{i}-front.mp4',
            )
        # Claim one — simulates the worker mid-file.
        claimed = claim_next_queue_item(db, worker_id='wk-1')
        assert claimed is not None

        n = clear_pending_queue(db)
        # Two pending unclaimed rows removed; the claimed row stays.
        assert n == 2

        with sqlite3.connect(db) as c:
            rows = c.execute(
                "SELECT canonical_key, claimed_by FROM indexing_queue"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == claimed['canonical_key']
        assert rows[0][1] == 'wk-1'

    def test_clear_all_removes_claimed_rows_too(self, tmp_path):
        # The advanced-rebuild path uses clear_all_queue (after pausing
        # the worker) to wipe everything.
        db = str(tmp_path / "q.db")
        _init_db(db)
        for i in range(3):
            enqueue_for_indexing(
                db, f'/mnt/teslacam/RecentClips/c{i}-front.mp4',
            )
        claim_next_queue_item(db, worker_id='wk-1')

        n = clear_all_queue(db)
        assert n == 3
        with sqlite3.connect(db) as c:
            assert c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0] == 0


class TestPurgeDeletedVideos:
    """Targeted-purge regression tests.

    The targeted purge flow runs from the file-watcher's delete
    callback whenever Tesla rotates a clip out of RecentClips. Older
    versions used ``LIKE '%basename%'`` to delete waypoints, which
    erased ArchivedClips geodata for clips that had a same-basename
    rotated copy in RecentClips. These tests pin the safe behavior:

      - skip purge entirely when a surviving on-disk copy exists
      - exact-match candidate relative paths instead of basename LIKE
    """

    def _seed(self, db, *, waypoint_video_path, indexed_abs_path,
              file_size=1024, file_mtime=100.0):
        from services.mapping_service import purge_deleted_videos  # noqa: F401
        with sqlite3.connect(db) as c:
            c.execute(
                "INSERT INTO trips (start_time, indexed_at) "
                "VALUES ('2025-01-01T00:00:00', '2025-01-01T00:00:00')"
            )
            trip_id = c.execute(
                "SELECT id FROM trips ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            c.execute(
                "INSERT INTO waypoints "
                "(trip_id, timestamp, lat, lon, video_path) "
                "VALUES (?, '2025-01-01T00:00:01', 37.0, -122.0, ?)",
                (trip_id, waypoint_video_path),
            )
            c.execute(
                "INSERT INTO indexed_files "
                "(file_path, file_size, file_mtime, indexed_at, "
                " waypoint_count, event_count) "
                "VALUES (?, ?, ?, '2025-01-01T00:00:00', 1, 0)",
                (indexed_abs_path, file_size, file_mtime),
            )
            c.commit()
        return trip_id

    def test_purge_skips_when_archive_copy_exists(self, tmp_path):
        # Reproduces BLOCKING bug: Tesla rotates RecentClips/foo-front
        # while ArchivedClips/foo-front (same basename) still exists.
        # The waypoint MUST survive — it's tied to the archived copy.
        from services.mapping_service import purge_deleted_videos
        db = str(tmp_path / "geo.db")
        _init_db(db)

        recent_path = str(tmp_path / "TeslaCam" / "RecentClips" /
                           "2025-01-01_00-foo-front.mp4")
        archive_path = str(tmp_path / "ArchivedClips" /
                            "2025-01-01_00-foo-front.mp4")

        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        # Surviving archive copy
        with open(archive_path, "wb") as f:
            f.write(b"hello")
        # No file at recent_path — Tesla deleted it. The watcher fires
        # purge_deleted_videos with the recent_path.

        # The waypoint reflects the archived copy (post-archive rewrite)
        self._seed(
            db,
            waypoint_video_path='ArchivedClips/'
                                  '2025-01-01_00-foo-front.mp4',
            indexed_abs_path=archive_path,
        )

        # Patch ARCHIVE_DIR so the surviving-copy check finds it.
        import config as _cfg
        old_dir = getattr(_cfg, 'ARCHIVE_DIR', None)
        old_en = getattr(_cfg, 'ARCHIVE_ENABLED', None)
        _cfg.ARCHIVE_DIR = str(tmp_path / "ArchivedClips")
        _cfg.ARCHIVE_ENABLED = True
        try:
            result = purge_deleted_videos(
                db, deleted_paths=[recent_path],
            )
        finally:
            if old_dir is not None:
                _cfg.ARCHIVE_DIR = old_dir
            if old_en is not None:
                _cfg.ARCHIVE_ENABLED = old_en

        # Nothing purged — surviving copy detected.
        assert result['purged_waypoints'] == 0
        assert result['purged_files'] == 0

        with sqlite3.connect(db) as c:
            wp_count = c.execute(
                "SELECT COUNT(*) FROM waypoints"
            ).fetchone()[0]
            file_count = c.execute(
                "SELECT COUNT(*) FROM indexed_files"
            ).fetchone()[0]
        assert wp_count == 1
        assert file_count == 1

    def test_purge_exact_matches_when_no_surviving_copy(self, tmp_path):
        # Counterpart: with no surviving copy on disk, the targeted
        # purge SHOULD remove the matching waypoints/indexed_files
        # rows — using exact-match against canonical candidate paths,
        # not a basename LIKE.
        from services.mapping_service import purge_deleted_videos
        db = str(tmp_path / "geo.db")
        _init_db(db)

        recent_path = str(tmp_path / "TeslaCam" / "RecentClips" /
                           "2025-01-01_00-bar-front.mp4")
        # No file written anywhere — both Recent and Archived missing.

        self._seed(
            db,
            waypoint_video_path='RecentClips/'
                                  '2025-01-01_00-bar-front.mp4',
            indexed_abs_path=recent_path,
        )

        import config as _cfg
        old_dir = getattr(_cfg, 'ARCHIVE_DIR', None)
        old_en = getattr(_cfg, 'ARCHIVE_ENABLED', None)
        _cfg.ARCHIVE_DIR = str(tmp_path / "ArchivedClips")  # nonexistent
        _cfg.ARCHIVE_ENABLED = True
        try:
            result = purge_deleted_videos(
                db, deleted_paths=[recent_path],
            )
        finally:
            if old_dir is not None:
                _cfg.ARCHIVE_DIR = old_dir
            if old_en is not None:
                _cfg.ARCHIVE_ENABLED = old_en

        assert result['purged_files'] == 1
        assert result['purged_waypoints'] == 1

        with sqlite3.connect(db) as c:
            assert c.execute(
                "SELECT COUNT(*) FROM waypoints"
            ).fetchone()[0] == 0
            assert c.execute(
                "SELECT COUNT(*) FROM indexed_files"
            ).fetchone()[0] == 0

    def test_purge_does_not_substring_match_unrelated_basename(
        self, tmp_path,
    ):
        # A clip named "front.mp4" must not erase waypoints for
        # unrelated clips like "front-cam-extra.mp4". Older basename-
        # LIKE matching would have done so — the new candidate-path
        # exact match prevents it.
        from services.mapping_service import purge_deleted_videos
        db = str(tmp_path / "geo.db")
        _init_db(db)

        # Waypoint for an UNRELATED clip — substring of victim basename
        unrelated_path = str(tmp_path / "TeslaCam" / "RecentClips" /
                              "2025-01-01_00-extra-front.mp4")
        self._seed(
            db,
            waypoint_video_path='RecentClips/'
                                  '2025-01-01_00-extra-front.mp4',
            indexed_abs_path=unrelated_path,
        )

        # Purge a file with a DIFFERENT basename. The unrelated row
        # must not be touched.
        victim_path = str(tmp_path / "TeslaCam" / "RecentClips" /
                           "2025-01-01_00-front.mp4")
        result = purge_deleted_videos(
            db, deleted_paths=[victim_path],
        )

        assert result['purged_waypoints'] == 0
        with sqlite3.connect(db) as c:
            assert c.execute(
                "SELECT COUNT(*) FROM waypoints"
            ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Phase 3: Boot catch-up scan
# ---------------------------------------------------------------------------


class TestBootCatchupScan:
    def _make_teslacam(self, root, files):
        """Create a fake TeslaCam tree with the given relative paths."""
        for rel in files:
            full = root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b'')
        return str(root)

    def test_no_files_returns_zero_counts(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        tc = self._make_teslacam(tmp_path / "TeslaCam", [])
        result = boot_catchup_scan(db, tc)
        assert result == {'scanned': 0, 'already_indexed': 0, 'enqueued': 0}

    def test_enqueues_orphan_clips(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        tc = self._make_teslacam(tmp_path / "TeslaCam", [
            'RecentClips/2025-11-08_08-15-44-front.mp4',
            'SavedClips/2025-11-08_evt/2025-11-08_08-15-44-front.mp4',
            'SentryClips/2025-11-08_evt2/2025-11-08_08-20-00-front.mp4',
        ])
        result = boot_catchup_scan(db, tc)
        # SavedClips and SentryClips entries enqueue (event-folder keys);
        # RecentClips also enqueues (basename key). Total = 3 distinct
        # canonical keys. (RecentClips and SavedClips share basename
        # 2025-11-08_08-15-44-front.mp4 but DIFFERENT canonical_key
        # because SavedClips is event-folder-keyed.)
        assert result['scanned'] >= 3
        assert result['enqueued'] == 3
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 3

    def test_skips_already_indexed_clips(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        tc = self._make_teslacam(tmp_path / "TeslaCam", [
            'RecentClips/2025-11-08_08-15-44-front.mp4',
        ])
        # Pre-populate indexed_files with the same canonical_key.
        full_path = os.path.join(
            tc, 'RecentClips', '2025-11-08_08-15-44-front.mp4'
        )
        with sqlite3.connect(db) as c:
            c.execute(
                """INSERT INTO indexed_files
                   (file_path, file_size, file_mtime, indexed_at,
                    waypoint_count, event_count)
                   VALUES (?, 0, 0, '2025-01-01', 5, 0)""",
                (full_path,),
            )
        result = boot_catchup_scan(db, tc)
        assert result['scanned'] >= 1
        assert result['already_indexed'] >= 1
        assert result['enqueued'] == 0

    def test_skips_already_queued_clips(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        tc = self._make_teslacam(tmp_path / "TeslaCam", [
            'RecentClips/2025-11-08_08-15-44-front.mp4',
        ])
        full_path = os.path.join(
            tc, 'RecentClips', '2025-11-08_08-15-44-front.mp4'
        )
        # Pre-queue (e.g. from a watcher event during the scan).
        enqueue_for_indexing(db, full_path)
        # Catch-up must not double-enqueue.
        result = boot_catchup_scan(db, tc)
        assert result['enqueued'] == 0
        with sqlite3.connect(db) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM indexing_queue"
            ).fetchone()[0]
        assert count == 1

    def test_recent_and_archived_dedup_by_canonical_key(self, tmp_path):
        # If a clip exists in both RecentClips and ArchivedClips, the
        # catch-up should enqueue only once (canonical_key dedup).
        db = str(tmp_path / "g.db")
        _init_db(db)
        # ArchivedClips lives outside TeslaCam but _find_front_camera_videos
        # picks it up via config.ARCHIVE_DIR. Without setting that env
        # we just test the in-memory dedup against duplicate yields.
        tc = self._make_teslacam(tmp_path / "TeslaCam", [
            'RecentClips/dup-front.mp4',
        ])
        # Fake a second yield by also placing the file in a SavedClips
        # event folder under the same basename — this WILL get a
        # different canonical_key (event/saved keys are unique). So
        # this verifies *that's* the case (no false dedup).
        os.makedirs(os.path.join(tc, 'SavedClips', 'evt'), exist_ok=True)
        with open(
            os.path.join(tc, 'SavedClips', 'evt', 'dup-front.mp4'), 'wb'
        ) as f:
            f.write(b'')
        result = boot_catchup_scan(db, tc)
        # Two distinct canonical keys: bare 'dup-front.mp4' (Recent)
        # and 'SavedClips/evt/dup-front.mp4' (Saved).
        assert result['enqueued'] == 2


# ---------------------------------------------------------------------------
# Phase 5: Daily stale scan
# ---------------------------------------------------------------------------


class TestDailyStaleScan:
    def test_start_returns_true_first_time_false_second(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        try:
            assert start_daily_stale_scan(db, lambda: None) is True
            # Idempotent — second call should not start another thread.
            assert start_daily_stale_scan(db, lambda: None) is False
        finally:
            stop_daily_stale_scan(timeout=2.0)

    def test_stop_terminates_thread(self, tmp_path):
        db = str(tmp_path / "g.db")
        _init_db(db)
        start_daily_stale_scan(db, lambda: None)
        # Cleanly stop within a reasonable time.
        assert stop_daily_stale_scan(timeout=5.0) is True

    def test_stop_when_not_running_is_safe(self, tmp_path):
        # Should be idempotent even when nothing is running.
        assert stop_daily_stale_scan(timeout=1.0) is True
        assert stop_daily_stale_scan(timeout=1.0) is True

