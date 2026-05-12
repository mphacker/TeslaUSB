"""Unit tests for the one-shot ``clock_skew_repair`` backfill script."""

from __future__ import annotations

import os
import sqlite3
import struct
from datetime import datetime, timezone

import pytest

from services import clock_skew_repair, mapping_service, sei_parser

# Reuse the synthetic-MP4 helpers from the mvhd test module so the
# fixtures stay aligned with the parser's expectations.
from tests.test_mp4_mvhd import _make_mp4, _mvhd_v0  # noqa: E402


# 2026-05-10 15:57:41 UTC — same anchor as the mvhd tests; matches the
# real Tesla file we verified during investigation.
_TRUE_DT = datetime(2026, 5, 10, 15, 57, 41, tzinfo=timezone.utc)
_TRUE_MP4 = int(_TRUE_DT.timestamp()) + sei_parser._MP4_EPOCH_OFFSET


def _mk_clip(tmp_path, name: str, mvhd_unix_dt: datetime) -> str:
    """Write a synthetic Tesla-named MP4 with a known mvhd time."""
    mp4_t = int(mvhd_unix_dt.timestamp()) + sei_parser._MP4_EPOCH_OFFSET
    p = tmp_path / name
    p.write_bytes(_make_mp4(_mvhd_v0(mp4_t)))
    return str(p)


def _mk_db(tmp_path) -> str:
    """Build a minimal geodata.db with the schema columns the script touches."""
    path = str(tmp_path / "geodata.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trips(
            id INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT,
            start_lat REAL, start_lon REAL,
            end_lat REAL, end_lon REAL,
            distance_km REAL,
            duration_seconds INTEGER,
            source_folder TEXT,
            indexed_at TEXT
        );
        CREATE TABLE waypoints(
            id INTEGER PRIMARY KEY,
            trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
            timestamp TEXT,
            lat REAL, lon REAL,
            heading REAL, speed_mps REAL,
            video_path TEXT,
            frame_offset INTEGER
        );
        CREATE TABLE detected_events(
            id INTEGER PRIMARY KEY,
            trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
            timestamp TEXT,
            lat REAL, lon REAL,
            event_type TEXT, severity REAL, description TEXT,
            video_path TEXT,
            frame_offset INTEGER,
            metadata TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return path


class TestRepairCore:
    def test_dry_run_writes_nothing(self, tmp_path):
        db = _mk_db(tmp_path)
        clip = _mk_clip(tmp_path, "2026-05-11_07-50-38-front.mp4", _TRUE_DT)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(1,'2026-05-11T07:50:38','2026-05-11T07:51:38')"
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(1,1,'2026-05-11T07:50:50',40.0,-80.0,?)",
            (clip,),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=True)
        assert stats["dry_run"] is True
        assert stats["files_retimed"] == 0
        assert stats["waypoints_shifted"] == 0
        assert stats["backup_path"] is None

        # Verify nothing changed on disk
        conn = sqlite3.connect(db)
        ts = conn.execute("SELECT timestamp FROM waypoints WHERE id=1").fetchone()[0]
        conn.close()
        assert ts == "2026-05-11T07:50:50"

    def test_apply_shifts_waypoints_and_events(self, tmp_path):
        db = _mk_db(tmp_path)
        clip = _mk_clip(tmp_path, "2026-05-11_07-50-38-front.mp4", _TRUE_DT)

        # Build a single trip with the wrong-day timestamps so the
        # script has something to shift.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(1,'2026-05-11T07:50:38','2026-05-11T07:51:38')"
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(1,1,'2026-05-11T07:50:50',40.0,-80.0,?)",
            (clip,),
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(2,1,'2026-05-11T07:51:00',40.001,-80.001,?)",
            (clip,),
        )
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata) "
            "VALUES(1,1,'2026-05-11T07:50:55',40.0,-80.0,"
            "'sentry',1.0,'',?,12000,'{}')",
            (clip,),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=False)
        assert stats["files_retimed"] == 1
        assert stats["waypoints_shifted"] == 2
        assert stats["events_shifted"] == 1
        assert stats["backup_path"] is not None
        assert os.path.isfile(stats["backup_path"])

        # Verify timestamps moved to the May 10 timeline
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT timestamp FROM waypoints ORDER BY id"
        ).fetchall()
        for (ts,) in rows:
            assert ts.startswith("2026-05-10"), f"waypoint not retimed: {ts}"
        ev_ts = conn.execute(
            "SELECT timestamp FROM detected_events WHERE id=1"
        ).fetchone()[0]
        assert ev_ts.startswith("2026-05-10")

        # Trip stats should have been recomputed off the new waypoints
        trip = conn.execute(
            "SELECT start_time, end_time FROM trips WHERE id=1"
        ).fetchone()
        assert trip[0].startswith("2026-05-10")
        assert trip[1].startswith("2026-05-10")
        conn.close()

    def test_skips_clip_with_correct_timestamp(self, tmp_path):
        # Filename matches mvhd within the noise floor → no shift, no
        # WARNING, idempotent re-runs are safe.
        db = _mk_db(tmp_path)
        # The filename has the local-rendered minute of _TRUE_DT, so
        # mvhd vs filename will differ by < 60 s.
        local = datetime.fromtimestamp(_TRUE_DT.timestamp())
        fname = local.strftime("%Y-%m-%d_%H-%M-%S") + "-front.mp4"
        clip = _mk_clip(tmp_path, fname, _TRUE_DT)

        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(1,?,?)", (local.isoformat(), local.isoformat()),
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(1,1,?,40.0,-80.0,?)",
            (local.isoformat(), clip),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=False)
        assert stats["files_retimed"] == 0
        assert stats["waypoints_shifted"] == 0

    def test_idempotent_second_run_is_noop(self, tmp_path):
        db = _mk_db(tmp_path)
        clip = _mk_clip(tmp_path, "2026-05-11_07-50-38-front.mp4", _TRUE_DT)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(1,'2026-05-11T07:50:38','2026-05-11T07:51:38')"
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(1,1,'2026-05-11T07:50:50',40.0,-80.0,?)",
            (clip,),
        )
        conn.commit()
        conn.close()

        first = clock_skew_repair.repair(db, dry_run=False)
        second = clock_skew_repair.repair(db, dry_run=False)
        assert first["files_retimed"] == 1
        assert second["files_retimed"] == 0
        assert second["waypoints_shifted"] == 0

    def test_missing_video_file_is_skipped(self, tmp_path):
        db = _mk_db(tmp_path)
        # Reference a clip path that doesn't exist on disk; the
        # script must skip it gracefully (no crash, no shift).
        ghost = str(tmp_path / "rotated_out.mp4")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(1,'2026-05-11T07:50:38','2026-05-11T07:51:38')"
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,video_path) "
            "VALUES(1,1,'2026-05-11T07:50:50',40.0,-80.0,?)",
            (ghost,),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=False)
        assert stats["files_skipped"] == 1
        assert stats["files_retimed"] == 0
        assert stats["waypoints_shifted"] == 0

    def test_cross_trip_waypoint_dedup(self, tmp_path):
        # Reproduces the trip 74 / trip 75 situation: same drive
        # indexed twice, one trip has correct timestamps but NULL
        # video_path, the other has wrong timestamps with valid
        # video_path. After retiming and the cross-trip dedup pass,
        # both trips should collapse into one with the surviving
        # waypoint carrying the video_path from the duplicate.
        db = _mk_db(tmp_path)
        clip = _mk_clip(tmp_path, "2026-05-11_07-50-38-front.mp4", _TRUE_DT)

        # Compute the timestamp where trip 75's waypoint will land
        # after retime, so trip 74 can carry an identical timestamp.
        # The retime maps "current_base" → mvhd-local. With a
        # waypoint at 2026-05-11T07:50:50 and frame_offset=0, the
        # current_base is 2026-05-11T07:50:50; new_base is
        # mvhd-local; so the waypoint lands at mvhd-local + 0.
        retimed_ts = datetime.fromtimestamp(_TRUE_DT.timestamp()).isoformat()

        conn = sqlite3.connect(db)
        # Trip 74 — correct day, no video_path, waypoint already at
        # the post-retime time (this is what an earlier indexing of
        # the now-rotated original clip would have produced).
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(74, ?, ?)", (retimed_ts, retimed_ts),
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,"
            "frame_offset,video_path) "
            "VALUES(101,74, ?, 40.0, -80.0, 0, NULL)",
            (retimed_ts,),
        )
        # Trip 75 — wrong day, has the video_path, frame_offset=0 so
        # current_base equals the waypoint timestamp.
        conn.execute(
            "INSERT INTO trips(id,start_time,end_time) VALUES "
            "(75,'2026-05-11T07:50:50','2026-05-11T07:50:50')"
        )
        conn.execute(
            "INSERT INTO waypoints(id,trip_id,timestamp,lat,lon,"
            "frame_offset,video_path) "
            "VALUES(201,75,'2026-05-11T07:50:50',40.0,-80.0,0,?)",
            (clip,),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=False)
        # The trip-merge pass should collapse the pair into one trip.
        assert stats["files_retimed"] == 1
        assert stats["trips_merged"] >= 1

        conn = sqlite3.connect(db)
        trip_count = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        # One survivor (the lower-id trip 74) with the video_path
        # transferred over.
        assert trip_count == 1
        wp_count = conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        assert wp_count == 1
        wp_video = conn.execute(
            "SELECT video_path FROM waypoints"
        ).fetchone()[0]
        assert wp_video == clip
        conn.close()
