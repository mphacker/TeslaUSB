"""Phase 5.1 (#102) — trip distance batching regression tests.

The trip-stat recompute path inside ``_index_video`` previously
fired ``1 + N`` SQL queries (one to list distinct ``video_path``
values, then one per video to fetch its waypoints). On a trip with
50 video clips that's 51 round-trips just to recompute the
distance. The Phase 5.1 fix collapses this to a single
``ORDER BY video_path, id`` query.

This module pins both contracts:

1. **Behavior** — distance is summed per-video (NOT across video
   boundaries — the comment in the source explains why: Tesla
   videos can overlap in time, so a global sort would interleave
   them and produce phantom GPS jumps).

2. **Performance** — the recompute path issues exactly one
   waypoint-fetching query per indexed video, regardless of how
   many distinct ``video_path`` values are attached to the trip.
"""

from __future__ import annotations

import sqlite3

import pytest

from services.mapping_service import (
    _haversine_km,
    _index_video,
    _init_db,
    DEFAULT_THRESHOLDS,
)

# Re-use the synthetic-video fixtures from the main mapping_service
# test module so we don't duplicate the SEI/MP4 builder ladder.
from tests.test_mapping_service import (
    _make_sei_protobuf,
    _make_synthetic_mp4,
)


def _index_clip(conn, tmp_path, filename, samples):
    """Build a synthetic single-video clip from ``samples`` and index it.

    ``samples`` is a list of ``(lat, lon)`` tuples — one waypoint each.
    Filename's timestamp drives trip placement.
    """
    payloads = [
        _make_sei_protobuf(lat=lat, lon=lon, speed=20.0)
        for lat, lon in samples
    ]
    mp4_data = _make_synthetic_mp4(payloads)
    teslacam = tmp_path / "TeslaCam" / "RecentClips"
    teslacam.mkdir(parents=True, exist_ok=True)
    video_file = teslacam / filename
    video_file.write_bytes(mp4_data)
    return _index_video(
        conn, str(video_file), str(tmp_path / "TeslaCam"),
        sample_rate=1, thresholds=DEFAULT_THRESHOLDS,
        trip_gap_minutes=5,
    )


class TestTripDistanceBatching:
    def test_distance_matches_sum_of_per_video_haversines(self, tmp_path):
        """Pin the semantic invariant: total trip distance is the
        sum of per-video haversine sums, NOT the sum across all
        rows by global timestamp/id order. (Tesla can write
        overlapping videos — a global sort interleaves them and
        creates phantom jumps.)
        """
        db_path = str(tmp_path / "trip_dist.db")
        conn = _init_db(db_path)

        # Two clips, each with 3 waypoints. Each clip's 3 points form
        # a known small geographic line. The total distance must be
        # the sum of each clip's per-video haversine chain.
        clip_a = [
            (37.7749, -122.4194),
            (37.7750, -122.4195),
            (37.7751, -122.4196),
        ]
        clip_b = [
            (37.8000, -122.4000),
            (37.8002, -122.4000),
            (37.8004, -122.4000),
        ]

        _index_clip(conn, tmp_path,
                    "2026-05-13_08-00-00-front.mp4", clip_a)
        _index_clip(conn, tmp_path,
                    "2026-05-13_08-02-00-front.mp4", clip_b)

        # Compute the expected distance using the same haversine
        # primitive the production code uses, summed per-video.
        expected = 0.0
        for clip in (clip_a, clip_b):
            for i in range(1, len(clip)):
                expected += _haversine_km(
                    clip[i - 1][0], clip[i - 1][1],
                    clip[i][0], clip[i][1],
                )

        # There must be exactly one trip (two contiguous clips, gap
        # well under the trip_gap_minutes default).
        trips = conn.execute("SELECT * FROM trips").fetchall()
        assert len(trips) == 1, (
            f"Expected one trip from two contiguous clips, got "
            f"{len(trips)}"
        )

        actual = trips[0]['distance_km']
        # Floating-point — 1 cm tolerance is more than enough.
        assert actual == pytest.approx(expected, abs=1e-5), (
            f"Recomputed distance {actual} km does not match the "
            f"per-video haversine sum {expected} km. The Phase 5.1 "
            f"batching fix must preserve the per-video boundary."
        )
        conn.close()

    def test_does_not_haversine_across_video_boundaries(self, tmp_path):
        """The two test clips are placed ~7 km apart (37.7749 vs
        37.8000). If the batched implementation accidentally
        haversines from the last point of clip A to the first
        point of clip B, the result would inflate by ~7 km — far
        bigger than the per-clip distances combined. Pin that
        boundary protection explicitly.
        """
        db_path = str(tmp_path / "trip_dist_boundary.db")
        conn = _init_db(db_path)

        clip_a = [(37.7749, -122.4194), (37.7750, -122.4195)]
        clip_b = [(37.8000, -122.4000), (37.8001, -122.4000)]

        _index_clip(conn, tmp_path,
                    "2026-05-13_09-00-00-front.mp4", clip_a)
        _index_clip(conn, tmp_path,
                    "2026-05-13_09-01-00-front.mp4", clip_b)

        # Sum of per-clip distances — both clips are tiny (< 50 m
        # each). Total must stay well under 1 km.
        per_clip_sum = 0.0
        for clip in (clip_a, clip_b):
            for i in range(1, len(clip)):
                per_clip_sum += _haversine_km(
                    clip[i - 1][0], clip[i - 1][1],
                    clip[i][0], clip[i][1],
                )

        # Crossing the boundary would add ~3+ km.
        cross_boundary = _haversine_km(
            clip_a[-1][0], clip_a[-1][1],
            clip_b[0][0], clip_b[0][1],
        )
        assert cross_boundary > 2.0, (
            "Test setup invariant: clip_a and clip_b are far apart "
            "so a boundary leak would be obvious."
        )

        trips = conn.execute("SELECT * FROM trips").fetchall()
        assert len(trips) == 1
        actual = trips[0]['distance_km']
        # Strict upper bound: must never exceed the per-clip sum
        # plus a tiny FP fudge. Crossing the boundary would make
        # it >= 5 km.
        assert actual <= per_clip_sum + 1e-5, (
            f"Distance {actual} km exceeds per-clip sum "
            f"{per_clip_sum} km — Phase 5.1 fix is haversine-ing "
            f"across the video boundary, which the original code "
            f"explicitly avoided."
        )
        conn.close()

    def test_recompute_uses_single_waypoint_query(self, tmp_path,
                                                   monkeypatch):
        """The whole point of Phase 5.1 is collapsing 1+N queries
        into 1. Pin that the single-query SQL is in fact issued by
        intercepting ``conn.execute`` calls during a recompute.

        We intercept the connection bound to ``_index_video`` by
        wrapping it AFTER the first clip is fully indexed (so the
        baseline tables, indexes, and trip row exist). Then we
        index a SECOND clip and count the queries fired during the
        recompute phase that touch ``waypoints`` for the trip.

        The legacy code fired ``1 + N`` queries per recompute (1
        DISTINCT video_path query + 1 per video). The Phase 5.1 fix
        fires exactly 1 query of the new shape:
            SELECT video_path, lat, lon FROM waypoints
            WHERE trip_id = ? AND video_path IS NOT NULL
            ORDER BY video_path, id
        """
        db_path = str(tmp_path / "trip_dist_query_count.db")
        conn = _init_db(db_path)

        # First clip — indexed without instrumentation so we don't
        # count the initial setup queries.
        _index_clip(conn, tmp_path,
                    "2026-05-13_10-00-00-front.mp4",
                    [(37.7749, -122.4194), (37.7750, -122.4195)])

        # Use sqlite3's trace callback to record every SQL statement
        # executed against this connection during the second-clip
        # indexing. Unlike monkey-patching ``conn.execute`` (which is
        # read-only on a Connection object), set_trace_callback gives
        # us every statement issued via the connection regardless of
        # which method (.execute, .executemany, fetchone(), etc.)
        # initiated it.
        recorded: list[str] = []

        def trace(sql):
            recorded.append(sql)

        conn.set_trace_callback(trace)
        try:
            _index_clip(conn, tmp_path,
                        "2026-05-13_10-02-00-front.mp4",
                        [(37.7751, -122.4196), (37.7752, -122.4197)])
        finally:
            conn.set_trace_callback(None)

        # Find queries that look like the recompute waypoint SELECT.
        # Match the Phase 5.1 batched shape and the two legacy
        # shapes that the fix replaced. Anchored on "trip_id = ?"
        # to avoid false-matching the unrelated cross-folder dedup
        # query (also "SELECT DISTINCT video_path" but with a
        # ``video_path IN (…)`` filter — not the recompute path).
        new_shape = [
            s for s in recorded
            if 'FROM waypoints' in s
            and 'video_path' in s
            and 'lat' in s
            and 'lon' in s
            and 'ORDER BY video_path' in s
        ]
        legacy_distinct = [
            s for s in recorded
            if 'SELECT DISTINCT video_path FROM waypoints' in s
            and 'trip_id = ?' in s  # exclude unrelated dedup query
        ]
        legacy_per_video = [
            s for s in recorded
            if 'SELECT lat, lon FROM waypoints' in s
            and 'video_path = ?' in s
        ]

        assert len(new_shape) == 1, (
            f"Phase 5.1 fix should issue exactly one batched "
            f"waypoint SELECT per recompute. Saw {len(new_shape)}: "
            f"{new_shape!r}"
        )
        assert not legacy_distinct, (
            f"Legacy 'SELECT DISTINCT video_path' query should have "
            f"been removed by Phase 5.1, but saw: {legacy_distinct!r}"
        )
        assert not legacy_per_video, (
            f"Legacy per-video 'SELECT lat, lon … WHERE video_path = ?' "
            f"queries should have been removed by Phase 5.1, but saw "
            f"{len(legacy_per_video)}: {legacy_per_video[:3]!r}"
        )
        conn.close()

    def test_distance_correct_for_three_video_trip(self, tmp_path):
        """End-to-end with N=3 videos. Verifies the loop's reset
        logic between videos works for more than two segments and
        that the total still equals the sum of per-clip haversines.
        """
        db_path = str(tmp_path / "trip_dist_three.db")
        conn = _init_db(db_path)

        clips = [
            [(37.7749, -122.4194), (37.7750, -122.4195)],
            [(37.7800, -122.4200), (37.7801, -122.4201)],
            [(37.7850, -122.4250), (37.7851, -122.4251)],
        ]
        timestamps = [
            "2026-05-13_11-00-00-front.mp4",
            "2026-05-13_11-01-00-front.mp4",
            "2026-05-13_11-02-00-front.mp4",
        ]

        for ts, clip in zip(timestamps, clips):
            _index_clip(conn, tmp_path, ts, clip)

        expected = 0.0
        for clip in clips:
            for i in range(1, len(clip)):
                expected += _haversine_km(
                    clip[i - 1][0], clip[i - 1][1],
                    clip[i][0], clip[i][1],
                )

        trips = conn.execute("SELECT * FROM trips").fetchall()
        assert len(trips) == 1, (
            f"Three contiguous clips should form one trip, got "
            f"{len(trips)}"
        )
        assert trips[0]['distance_km'] == pytest.approx(expected,
                                                        abs=1e-5)
        conn.close()
