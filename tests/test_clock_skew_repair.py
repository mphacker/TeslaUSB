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


class TestEventDedup:
    """Tests for ``_dedupe_detected_events`` — the missing pass that lets
    duplicate ``detected_events`` rows survive a clock-skew repair when the
    indexer was triggered twice on the same physical clip.
    """

    def test_dedupe_keeps_row_with_archived_video_path(self, tmp_path):
        # Repro of the May 10/11 production duplicates: same FSD event,
        # same trip, same timestamp/lat/lon. One row has video_path=NULL
        # (left over from a prior purge), the other has the canonical
        # ``ArchivedClips/...`` path. After dedup, only the ArchivedClips
        # row should remain.
        db = _mk_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trips(id) VALUES (74)")
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(151,74,'2026-05-10T12:06:20.666600',42.901,-83.630,"
            "'fsd_disengage',1.0,'',NULL,0,'{}')"
        )
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(156,74,'2026-05-10T12:06:20.666600',42.901,-83.630,"
            "'fsd_disengage',1.0,'',"
            "'ArchivedClips/2026-05-11_07-58-40-front.mp4',0,'{}')"
        )
        conn.commit()

        deleted = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()

        assert deleted == 1
        rows = conn.execute(
            "SELECT id, video_path FROM detected_events ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 156
        assert rows[0][1] == 'ArchivedClips/2026-05-11_07-58-40-front.mp4'
        conn.close()

    def test_dedupe_prefers_archivedclips_over_recentclips(self, tmp_path):
        # When neither row has NULL video_path, prefer the ArchivedClips
        # path (durable SD-card copy) over RecentClips (Tesla's rolling
        # buffer that gets rotated out within ~1 hour).
        db = _mk_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trips(id) VALUES (74)")
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(1,74,'2026-05-10T12:00:00',42.0,-83.0,"
            "'sharp_turn',1.0,'','RecentClips/x-front.mp4',0,'{}')"
        )
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(2,74,'2026-05-10T12:00:00',42.0,-83.0,"
            "'sharp_turn',1.0,'','ArchivedClips/x-front.mp4',0,'{}')"
        )
        conn.commit()

        deleted = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()

        assert deleted == 1
        survivor = conn.execute(
            "SELECT video_path FROM detected_events"
        ).fetchone()[0]
        assert survivor == 'ArchivedClips/x-front.mp4'
        conn.close()

    def test_dedupe_handles_null_trip_id(self, tmp_path):
        # Sentry/Saved events have trip_id=NULL. SQL ``NULL = NULL`` is
        # false, so the dedup must use COALESCE in the GROUP BY and the
        # subsequent lookup must handle NULL trip_id explicitly.
        db = _mk_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(1,NULL,'2026-05-10T10:00:00',42.0,-83.0,"
            "'sentry',1.0,'',NULL,0,'{}')"
        )
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(2,NULL,'2026-05-10T10:00:00',42.0,-83.0,"
            "'sentry',1.0,'','SentryClips/2026-05-10_10-14-31/x-front.mp4',"
            "0,'{}')"
        )
        conn.commit()

        deleted = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()
        assert deleted == 1
        rows = conn.execute(
            "SELECT id FROM detected_events"
        ).fetchall()
        assert [r[0] for r in rows] == [2]
        conn.close()

    def test_dedupe_keeps_distinct_event_types_at_same_lat_lon(self, tmp_path):
        # fsd_engage immediately followed by fsd_disengage at the same
        # GPS coordinate (driver tap-tap on the stalk) MUST NOT be
        # collapsed into one event — the event_type column is part of
        # the dedup key.
        db = _mk_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trips(id) VALUES (74)")
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(1,74,'2026-05-10T12:00:00',42.0,-83.0,"
            "'fsd_engage',1.0,'','ArchivedClips/x-front.mp4',0,'{}')"
        )
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(2,74,'2026-05-10T12:00:00',42.0,-83.0,"
            "'fsd_disengage',1.0,'','ArchivedClips/x-front.mp4',0,'{}')"
        )
        conn.commit()

        deleted = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()
        assert deleted == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM detected_events"
        ).fetchone()[0]
        assert count == 2
        conn.close()

    def test_dedupe_idempotent(self, tmp_path):
        # Running the dedup twice on already-clean data does nothing.
        db = _mk_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trips(id) VALUES (74)")
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(1,74,'2026-05-10T12:00:00',42.0,-83.0,"
            "'fsd_engage',1.0,'','ArchivedClips/x-front.mp4',0,'{}')"
        )
        conn.commit()

        first = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()
        second = clock_skew_repair._dedupe_detected_events(conn)
        conn.commit()
        assert first == 0
        assert second == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM detected_events"
        ).fetchone()[0]
        assert count == 1
        conn.close()

    def test_repair_removes_event_dups_left_by_re_indexing(self, tmp_path):
        # End-to-end: a clip that was indexed twice (one set of
        # waypoints+events with the wrong day, one set with NULL
        # video_path on the correct day) should end up with exactly one
        # waypoint AND one event after a single repair pass — not one
        # waypoint and two events.
        db = _mk_db(tmp_path)
        clip = _mk_clip(tmp_path, "2026-05-11_07-50-38-front.mp4", _TRUE_DT)
        retimed_ts = datetime.fromtimestamp(_TRUE_DT.timestamp()).isoformat()

        conn = sqlite3.connect(db)
        # Trip 74 — correct day, NULL video_path
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
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(151,74, ?, 40.0,-80.0,'fsd_disengage',1.0,'',"
            "NULL,0,'{}')",
            (retimed_ts,),
        )
        # Trip 75 — wrong day, has video_path
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
        conn.execute(
            "INSERT INTO detected_events(id,trip_id,timestamp,lat,lon,"
            "event_type,severity,description,video_path,frame_offset,metadata)"
            " VALUES(251,75,'2026-05-11T07:50:50',40.0,-80.0,"
            "'fsd_disengage',1.0,'',?,0,'{}')",
            (clip,),
        )
        conn.commit()
        conn.close()

        stats = clock_skew_repair.repair(db, dry_run=False)
        assert stats["events_deduped"] >= 1

        conn = sqlite3.connect(db)
        wp_count = conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        ev_count = conn.execute(
            "SELECT COUNT(*) FROM detected_events"
        ).fetchone()[0]
        # One survivor each — both the waypoint dedup AND the new
        # event dedup ran.
        assert wp_count == 1
        assert ev_count == 1
        # The survivor event carries the real video_path (the NULL
        # row was the loser).
        ev_video = conn.execute(
            "SELECT video_path FROM detected_events"
        ).fetchone()[0]
        assert ev_video == clip
        conn.close()
