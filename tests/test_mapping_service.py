"""Tests for the mapping/geo-indexer service (mapping_service.py).

Covers: database schema, event detection rules, debouncing, trip queries,
background indexer with synthetic MP4 files.
"""

import json
import os
import struct
import sqlite3
import pytest

from services.mapping_service import (
    _init_db,
    _detect_events,
    _debounce_events,
    _haversine_km,
    _timestamp_from_filename,
    _find_front_camera_videos,
    _index_video,
    query_trips,
    query_trip_route,
    query_events,
    get_stats,
    get_driving_stats,
    get_event_chart_data,
    DEFAULT_THRESHOLDS,
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
# End-to-End Indexing Tests
# ---------------------------------------------------------------------------

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

        wc, ec = _index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        )

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

        wc, ec = _index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        )

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

        wc, ec = _index_video(
            conn, str(video_file), str(tmp_path / "TeslaCam"),
            sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
            trip_gap_minutes=5,
        )

        assert wc == 0
        assert ec == 0
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
