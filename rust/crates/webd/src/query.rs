//! The short read queries over the catalog. Every function takes a borrowed
//! read-only [`Connection`] and binds straight to the as-built `indexd` schema
//! (`indexd::db::migrations`). All queries are read-only; none mutate.

use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::dto::{
    AnalyticsDto, AngleDto, Bbox, ClipDto, DaySummary, DayTripCount, EventDto, EventTypeCount,
    FolderClassStat, InstalledChimeDto, MediaItemDto, PrefDto, SeverityCount, TripDetailDto,
    TripDto, TripPointDto, VideoStats,
};
use crate::polyline;

/// `trips` column list shared by the list/detail queries (column order is
/// relied on by [`map_trip`]).
const TRIP_COLS: &str = "SELECT id, day, started_at, ended_at, \
     bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon, \
     distance_m, point_count, polyline FROM trips";

/// `clips` column list shared by the list/detail queries (column order is
/// relied on by [`map_clip`]).
const CLIP_COLS: &str = "SELECT id, canonical_key, started_at, ended_at, partition, \
     folder_class, is_sentry, duration_s, availability FROM clips";

/// `events` column list (column order is relied on by [`map_event`]).
const EVENT_COLS: &str = "SELECT id, type, severity, t, lat, lon, clip_id, trip_id, \
     front_frame_index, front_frame_offset, description FROM events";

/// Descending keyset cursor state (`(date DESC, id DESC)`).
#[derive(Clone, Copy)]
pub(crate) struct Keyset {
    /// Snapshot pin: every page filters `id <= snap`.
    pub snap: i64,
    /// Last row of the previous page (`date`, `id`), or `None` on the first page.
    pub after: Option<(i64, i64)>,
}

/// The table to snapshot for cursor pagination.
#[derive(Clone, Copy)]
pub(crate) enum SnapshotResource {
    Events,
    Clips,
    Trips,
}

impl SnapshotResource {
    fn table_name(self) -> &'static str {
        match self {
            Self::Events => "events",
            Self::Clips => "clips",
            Self::Trips => "trips",
        }
    }
}

/// `GET /api/days`: civil days with driving trips and/or standalone parked
/// pinned events (`trip_id IS NULL`), plus rolled-up counts.
///
/// Days that only have standalone parked pins are returned with `trip_count = 0`.
pub(crate) fn list_days(conn: &Connection) -> Result<Vec<DaySummary>, rusqlite::Error> {
    let sql = "WITH trip_days AS ( \
            SELECT day FROM trips GROUP BY day \
        ), standalone_days AS ( \
            SELECT strftime('%Y-%m-%d', t, 'unixepoch') AS day \
            FROM events \
            WHERE trip_id IS NULL AND lat IS NOT NULL AND lon IS NOT NULL \
            GROUP BY strftime('%Y-%m-%d', t, 'unixepoch') \
        ), all_days AS ( \
            SELECT day FROM trip_days \
            UNION \
            SELECT day FROM standalone_days \
        ), trip_rollup AS ( \
            SELECT day, COUNT(*) AS trip_count, COALESCE(SUM(distance_m), 0.0) AS distance_m \
            FROM trips \
            GROUP BY day \
        ), trip_event_rollup AS ( \
            SELECT t.day AS day, COUNT(*) AS event_count \
            FROM events e \
            JOIN trips t ON e.trip_id = t.id \
            GROUP BY t.day \
        ), standalone_event_rollup AS ( \
            SELECT strftime('%Y-%m-%d', t, 'unixepoch') AS day, COUNT(*) AS event_count \
            FROM events \
            WHERE trip_id IS NULL AND lat IS NOT NULL AND lon IS NOT NULL \
            GROUP BY strftime('%Y-%m-%d', t, 'unixepoch') \
        ) \
        SELECT d.day, \
               COALESCE(tr.trip_count, 0) AS trip_count, \
               COALESCE(tr.distance_m, 0.0) AS distance_m, \
               COALESCE(te.event_count, 0) + COALESCE(se.event_count, 0) AS event_count \
        FROM all_days d \
        LEFT JOIN trip_rollup tr ON tr.day = d.day \
        LEFT JOIN trip_event_rollup te ON te.day = d.day \
        LEFT JOIN standalone_event_rollup se ON se.day = d.day \
        ORDER BY d.day DESC";
    let mut stmt = conn.prepare(sql)?;
    let out = stmt
        .query_map([], |row| {
            Ok(DaySummary {
                day: row.get(0)?,
                trip_count: row.get(1)?,
                distance_m: row.get(2)?,
                event_count: row.get(3)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// `GET /api/trips[?day=]`: trip rows with bbox + decoded cached polyline.
pub(crate) fn list_trips(
    conn: &Connection,
    day: Option<&str>,
) -> Result<Vec<TripDto>, rusqlite::Error> {
    if let Some(day) = day {
        let sql = format!("{TRIP_COLS} WHERE day = ?1 ORDER BY started_at DESC, id DESC");
        let mut stmt = conn.prepare(&sql)?;
        let out = stmt
            .query_map(params![day], map_trip)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(out)
    } else {
        let sql = format!("{TRIP_COLS} ORDER BY started_at DESC, id DESC");
        let mut stmt = conn.prepare(&sql)?;
        let out = stmt
            .query_map([], map_trip)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(out)
    }
}

/// `GET /api/trips/page`: newest-first keyset page over the whole trip catalog.
pub(crate) fn list_trips_page(
    conn: &Connection,
    keyset: Keyset,
    limit: i64,
) -> Result<Vec<TripDto>, rusqlite::Error> {
    let limit_plus_one = limit.saturating_add(1);
    let trips = if let Some((ts, id)) = keyset.after {
        let sql = format!(
            "{TRIP_COLS} WHERE id <= ?1 \
             AND (started_at < ?2 OR (started_at = ?2 AND id < ?3)) \
             ORDER BY started_at DESC, id DESC LIMIT ?4"
        );
        let mut stmt = conn.prepare(&sql)?;
        stmt.query_map(params![keyset.snap, ts, id, limit_plus_one], map_trip)?
            .collect::<Result<Vec<_>, _>>()?
    } else {
        let sql = format!("{TRIP_COLS} WHERE id <= ?1 ORDER BY started_at DESC, id DESC LIMIT ?2");
        let mut stmt = conn.prepare(&sql)?;
        stmt.query_map(params![keyset.snap, limit_plus_one], map_trip)?
            .collect::<Result<Vec<_>, _>>()?
    };
    Ok(trips)
}

/// `SELECT MAX(id)` snapshot anchor for cursor pagination.
pub(crate) fn snapshot_max_id(
    conn: &Connection,
    resource: SnapshotResource,
) -> Result<Option<i64>, rusqlite::Error> {
    let sql = format!("SELECT MAX(id) FROM {}", resource.table_name());
    conn.query_row(&sql, [], |row| row.get(0))
}

/// `GET /api/trips/:id`: a trip plus its `trip_points` (in `seq` order).
/// `None` when no trip has that id.
pub(crate) fn get_trip(
    conn: &Connection,
    id: i64,
) -> Result<Option<TripDetailDto>, rusqlite::Error> {
    let sql = format!("{TRIP_COLS} WHERE id = ?1");
    let mut stmt = conn.prepare(&sql)?;
    let Some(trip) = stmt.query_row(params![id], map_trip).optional()? else {
        return Ok(None);
    };
    let mut pstmt = conn.prepare(
        "SELECT t, lat, lon, speed, heading FROM trip_points \
         WHERE trip_id = ?1 ORDER BY seq ASC",
    )?;
    let points = pstmt
        .query_map(params![id], |row| {
            Ok(TripPointDto {
                t: row.get(0)?,
                lat: row.get(1)?,
                lon: row.get(2)?,
                speed: row.get(3)?,
                heading: row.get(4)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Some(TripDetailDto { trip, points }))
}

/// `GET /api/events/:id`: a single event row.
/// `None` when no event has that id.
pub(crate) fn get_event(conn: &Connection, id: i64) -> Result<Option<EventDto>, rusqlite::Error> {
    let sql = format!("{EVENT_COLS} WHERE id = ?1");
    let mut stmt = conn.prepare(&sql)?;
    stmt.query_row(params![id], map_event).optional()
}

/// `GET /api/events`: newest-first keyset page of event bubbles, optionally
/// filtered to a single `trip`.
pub(crate) fn list_events(
    conn: &Connection,
    keyset: Keyset,
    limit: i64,
    trip: Option<i64>,
) -> Result<Vec<EventDto>, rusqlite::Error> {
    let limit_plus_one = limit.saturating_add(1);
    let out = match (trip, keyset.after) {
        (Some(trip_id), Some((ts, id))) => {
            let sql = format!(
                "{EVENT_COLS} WHERE id <= ?1 AND trip_id = ?2 \
                 AND (t < ?3 OR (t = ?3 AND id < ?4)) \
                 ORDER BY t DESC, id DESC LIMIT ?5"
            );
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, trip_id, ts, id, limit_plus_one], map_event)?
                .collect::<Result<Vec<_>, _>>()?
        }
        (Some(trip_id), None) => {
            let sql =
                format!("{EVENT_COLS} WHERE id <= ?1 AND trip_id = ?2 ORDER BY t DESC, id DESC LIMIT ?3");
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, trip_id, limit_plus_one], map_event)?
                .collect::<Result<Vec<_>, _>>()?
        }
        (None, Some((ts, id))) => {
            let sql = format!(
                "{EVENT_COLS} WHERE id <= ?1 AND (t < ?2 OR (t = ?2 AND id < ?3)) \
                 ORDER BY t DESC, id DESC LIMIT ?4"
            );
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, ts, id, limit_plus_one], map_event)?
                .collect::<Result<Vec<_>, _>>()?
        }
        (None, None) => {
            let sql = format!("{EVENT_COLS} WHERE id <= ?1 ORDER BY t DESC, id DESC LIMIT ?2");
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, limit_plus_one], map_event)?
                .collect::<Result<Vec<_>, _>>()?
        }
    };
    Ok(out)
}

/// `GET /api/events?day=YYYY-MM-DD`: standalone parked pinned events
/// (`trip_id IS NULL`, with present lat/lon) for one civil day.
pub(crate) fn list_standalone_day_events(
    conn: &Connection,
    day: &str,
    limit: i64,
) -> Result<Vec<EventDto>, rusqlite::Error> {
    let sql = format!(
        "{EVENT_COLS} WHERE trip_id IS NULL AND lat IS NOT NULL AND lon IS NOT NULL \
         AND strftime('%Y-%m-%d', t, 'unixepoch') = ?1 \
         ORDER BY t DESC, id DESC LIMIT ?2"
    );
    let mut stmt = conn.prepare(&sql)?;
    stmt.query_map(params![day, limit], map_event)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/clips`: newest-first keyset page of clips, optionally
/// filtered by `folder_class`, each with its camera angles.
pub(crate) fn list_clips(
    conn: &Connection,
    keyset: Keyset,
    limit: i64,
    folder_class: Option<&str>,
) -> Result<Vec<ClipDto>, rusqlite::Error> {
    let limit_plus_one = limit.saturating_add(1);
    let mut clips: Vec<ClipDto> = match (folder_class, keyset.after) {
        (Some(folder_class), Some((ts, id))) => {
            let sql = format!(
                "{CLIP_COLS} WHERE id <= ?1 AND folder_class = ?2 \
                 AND (started_at < ?3 OR (started_at = ?3 AND id < ?4)) \
                 ORDER BY started_at DESC, id DESC LIMIT ?5"
            );
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(
                params![keyset.snap, folder_class, ts, id, limit_plus_one],
                map_clip,
            )?
            .collect::<Result<Vec<_>, _>>()?
        }
        (Some(folder_class), None) => {
            let sql = format!(
                "{CLIP_COLS} WHERE id <= ?1 AND folder_class = ?2 ORDER BY started_at DESC, id DESC LIMIT ?3"
            );
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, folder_class, limit_plus_one], map_clip)?
                .collect::<Result<Vec<_>, _>>()?
        }
        (None, Some((ts, id))) => {
            let sql = format!(
                "{CLIP_COLS} WHERE id <= ?1 \
                 AND (started_at < ?2 OR (started_at = ?2 AND id < ?3)) \
                 ORDER BY started_at DESC, id DESC LIMIT ?4"
            );
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, ts, id, limit_plus_one], map_clip)?
                .collect::<Result<Vec<_>, _>>()?
        }
        (None, None) => {
            let sql = format!("{CLIP_COLS} WHERE id <= ?1 ORDER BY started_at DESC, id DESC LIMIT ?2");
            let mut stmt = conn.prepare(&sql)?;
            stmt.query_map(params![keyset.snap, limit_plus_one], map_clip)?
                .collect::<Result<Vec<_>, _>>()?
        }
    };

    let ids: Vec<i64> = clips.iter().map(|clip| clip.id).collect();
    let mut angles = angles_for_clips(conn, &ids)?;
    let mut waypoints = waypoints_for_clips(conn, &ids)?;
    for clip in &mut clips {
        if let Some(set) = angles.remove(&clip.id) {
            clip.angles = set;
        }
        if let Some((lat, lon)) = waypoints.remove(&clip.id) {
            clip.lat = Some(lat);
            clip.lon = Some(lon);
        }
    }
    Ok(clips)
}

/// `GET /api/clips/:id`: a single clip plus its angles. `None` when missing.
pub(crate) fn get_clip(conn: &Connection, id: i64) -> Result<Option<ClipDto>, rusqlite::Error> {
    let sql = format!("{CLIP_COLS} WHERE id = ?1");
    let mut stmt = conn.prepare(&sql)?;
    let Some(mut clip) = stmt.query_row(params![id], map_clip).optional()? else {
        return Ok(None);
    };
    let mut angles = angles_for_clips(conn, &[id])?;
    clip.angles = angles.remove(&id).unwrap_or_default();
    let mut waypoints = waypoints_for_clips(conn, &[id])?;
    if let Some((lat, lon)) = waypoints.remove(&id) {
        clip.lat = Some(lat);
        clip.lon = Some(lon);
    }
    Ok(Some(clip))
}

/// `GET /api/analytics`: basic read-only aggregates over events/trips.
pub(crate) fn analytics(conn: &Connection) -> Result<AnalyticsDto, rusqlite::Error> {
    let total_trips: i64 = conn.query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))?;
    let total_distance_m: f64 = conn.query_row(
        "SELECT COALESCE(SUM(distance_m), 0.0) FROM trips",
        [],
        |r| r.get(0),
    )?;
    let total_events: i64 = conn.query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))?;

    let mut by_type =
        conn.prepare("SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY type ASC")?;
    let events_by_type = by_type
        .query_map([], |row| {
            Ok(EventTypeCount {
                event_type: row.get(0)?,
                count: row.get(1)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    let mut by_day = conn.prepare(
        "SELECT day, COUNT(*), COALESCE(SUM(distance_m), 0.0) \
         FROM trips GROUP BY day ORDER BY day DESC",
    )?;
    let trips_by_day = by_day
        .query_map([], |row| {
            Ok(DayTripCount {
                day: row.get(0)?,
                count: row.get(1)?,
                distance_m: row.get(2)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    let total_drive_time_s: i64 = conn.query_row(
        "SELECT COALESCE(SUM(ended_at - started_at), 0) FROM trips",
        [],
        |r| r.get(0),
    )?;
    let warning_event_count: i64 =
        conn.query_row("SELECT COUNT(*) FROM events WHERE severity >= 2", [], |r| {
            r.get(0)
        })?;
    // `AVG`/`MAX` over an all-NULL (or empty) column yield SQL NULL → `None`.
    let (avg_speed_mps, max_speed_mps): (Option<f64>, Option<f64>) = conn.query_row(
        "SELECT AVG(speed), MAX(speed) FROM trip_points WHERE speed IS NOT NULL",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;

    let mut by_sev = conn
        .prepare("SELECT severity, COUNT(*) FROM events GROUP BY severity ORDER BY severity ASC")?;
    let events_by_severity = by_sev
        .query_map([], |row| {
            Ok(SeverityCount {
                severity: row.get(0)?,
                count: row.get(1)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    let video_stats = video_stats(conn)?;

    Ok(AnalyticsDto {
        total_trips,
        total_distance_m,
        total_events,
        events_by_type,
        trips_by_day,
        total_drive_time_s,
        warning_event_count,
        avg_speed_mps,
        max_speed_mps,
        events_by_severity,
        video_stats,
    })
}

/// Footage aggregates over `clips` ⋈ `angles` — totals plus a per-folder-class
/// breakdown, all derived from indexed `size_bytes` (read-only; no filesystem
/// walk). A clip with no angle rows still counts toward `total_clips` and its
/// folder class, contributing zero files/bytes (LEFT JOIN; `COUNT(a.id)` and
/// `SUM(a.size_bytes)` ignore the NULL angle).
fn video_stats(conn: &Connection) -> Result<VideoStats, rusqlite::Error> {
    let (total_clips, total_files, total_bytes): (i64, i64, i64) = conn.query_row(
        "SELECT COUNT(DISTINCT c.id), COUNT(a.id), COALESCE(SUM(a.size_bytes), 0) \
         FROM clips c LEFT JOIN angles a ON a.clip_id = c.id",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;

    let mut by_class = conn.prepare(
        "SELECT c.folder_class, COUNT(DISTINCT c.id), COUNT(a.id), \
                COALESCE(SUM(a.size_bytes), 0) \
         FROM clips c LEFT JOIN angles a ON a.clip_id = c.id \
         GROUP BY c.folder_class ORDER BY c.folder_class ASC",
    )?;
    let by_folder_class = by_class
        .query_map([], |row| {
            Ok(FolderClassStat {
                folder_class: row.get(0)?,
                clip_count: row.get(1)?,
                file_count: row.get(2)?,
                size_bytes: row.get(3)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    Ok(VideoStats {
        total_clips,
        total_files,
        total_bytes,
        by_folder_class,
    })
}

/// `GET /api/settings`: raw `prefs` rows, ordered by key.
pub(crate) fn list_settings(conn: &Connection) -> Result<Vec<PrefDto>, rusqlite::Error> {
    let mut stmt = conn.prepare("SELECT key, value FROM prefs ORDER BY key ASC")?;
    let out = stmt
        .query_map([], |row| {
            Ok(PrefDto {
                key: row.get(0)?,
                value: row.get(1)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// `GET /api/chimes`: the single installed lock chime, read from the
/// `media_entries` catalog (`indexd` v2). Returns `None` when no chime row
/// exists OR the catalog predates the media inventory (the `media_entries`
/// table is absent) — the latter degrades to "no chime" rather than a 500 so
/// a `webd` running ahead of an `indexd` migration still answers cleanly.
///
/// The `(partition, rel_path)` filter mirrors the producer convention:
/// scannerd labels the MEDIA (p2) partition `slot1` and writes the lock chime
/// at the fixed root path `LockChime.wav`.
pub(crate) fn installed_chime(
    conn: &Connection,
) -> Result<Option<InstalledChimeDto>, rusqlite::Error> {
    // p2 MEDIA partition label + fixed lock-chime root path (scannerd
    // `partition_label(1)` / `LOCK_CHIME_REL_PATH`).
    const MEDIA_PARTITION: &str = "slot1";
    const LOCK_CHIME_REL_PATH: &str = "LockChime.wav";

    let table_present: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_entries'",
            [],
            |_| Ok(true),
        )
        .optional()?
        .unwrap_or(false);
    if !table_present {
        return Ok(None);
    }

    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = ?1 AND rel_path = ?2",
    )?;
    stmt.query_row(params![MEDIA_PARTITION, LOCK_CHIME_REL_PATH], |row| {
        Ok(InstalledChimeDto {
            name: row.get(0)?,
            rel_path: row.get(1)?,
            size_bytes: row.get(2)?,
            modified: row.get(3)?,
        })
    })
    .optional()
}

/// Resolve one camera angle's byte source for the stream/download endpoints.
///
/// Returns `(file_ref, view_kind)` for the `(clip_id, camera)` angle, or `None`
/// when no such angle exists. The path resolution + archive-root jail is done
/// by the caller; `file_ref` is **never** placed in any browser-facing DTO.
pub(crate) fn angle_source(
    conn: &Connection,
    clip_id: i64,
    camera: &str,
) -> Result<Option<(String, String)>, rusqlite::Error> {
    let mut stmt =
        conn.prepare("SELECT file_ref, view_kind FROM angles WHERE clip_id = ?1 AND camera = ?2")?;
    stmt.query_row(params![clip_id, camera], |row| {
        Ok((row.get(0)?, row.get(1)?))
    })
    .optional()
}

/// Resolve one non-archive angle source for `(clip_id, camera)`.
///
/// `indexd` currently writes `'ro_usb'` for live car-volume angles
/// (`indexd/src/apply.rs::view_kind_for`), but old catalogs may contain the
/// legacy `'live'` value (see the DTO note). Treat any non-`archive` value as
/// the live source for map-playback fallback.
///
/// Catalog membership is the stable-list gate, and `media.rs` adds a first-read
/// guard. The catalog records `angles.size_bytes` from the file's
/// `valid_data_length` at ingest, and a stable file has
/// `valid_data_length == data_length`. On the first read the probe's served byte
/// count (`readable_size` = current `valid_data_length`) AND file allocation
/// (`ClipIdentity.total_size` = current `data_length`) must both equal that
/// catalog size; otherwise the path was recreated/changed since ingest and
/// streaming fails closed with `410` instead of serving wrong/partial bytes. The
/// per-request `ClipIdentity` fence still covers any mid-stream identity change.
///
/// `size_bytes` is nullable. A `NULL` (or non-positive) size means the catalog
/// cannot describe a stable size, so the angle is treated as unverifiable and the
/// handler fails closed (`404`) rather than serving unverified bytes. Real
/// `indexd`-ingested non-archive angles always carry a positive size.
pub(crate) fn non_archive_angle_source(
    conn: &Connection,
    clip_id: i64,
    camera: &str,
) -> Result<Option<(String, Option<i64>)>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT file_ref, size_bytes FROM angles \
         WHERE clip_id = ?1 AND camera = ?2 AND view_kind <> 'archive'",
    )?;
    stmt.query_row(params![clip_id, camera], |row| {
        Ok((row.get(0)?, row.get(1)?))
    })
        .optional()
}

/// List the archive-view angles of a clip for the zip-export endpoint, as
/// `(camera, file_ref)` pairs ordered by `camera`.
///
/// Only `view_kind = 'archive'` angles are returned: those are the durable
/// Pi-side ext4 files that are safe to read. An empty vec means the clip has no
/// exportable angles (or does not exist) — the caller answers `404`.
pub(crate) fn list_archive_angles(
    conn: &Connection,
    clip_id: i64,
) -> Result<Vec<(String, String)>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT camera, file_ref FROM angles \
         WHERE clip_id = ?1 AND view_kind = 'archive' ORDER BY camera ASC",
    )?;
    let out = stmt
        .query_map(params![clip_id], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// List the **`ro_usb`-view** angles of a clip for the car-delete handoff, as
/// `(camera, file_ref)` pairs ordered by `camera`.
///
/// Only `view_kind = 'ro_usb'` angles are returned: those are the live files on
/// the car-visible USB volume that a `gadgetd` eject-handoff can delete. An
/// empty vec means the clip has no car-visible files (or does not exist) — the
/// caller refuses the delete. `file_ref` is volume-root-relative and is **never**
/// placed in any browser-facing DTO; it is handed only to `gadgetd`.
pub(crate) fn list_ro_usb_angles(
    conn: &Connection,
    clip_id: i64,
) -> Result<Vec<(String, String)>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT camera, file_ref FROM angles \
         WHERE clip_id = ?1 AND view_kind = 'ro_usb' ORDER BY camera ASC",
    )?;
    let out = stmt
        .query_map(params![clip_id], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(out)
}

/// Fetch all angles for a set of clip ids in one query, grouped by clip id and
/// ordered by `camera` for deterministic output.
fn angles_for_clips(
    conn: &Connection,
    ids: &[i64],
) -> Result<std::collections::HashMap<i64, Vec<AngleDto>>, rusqlite::Error> {
    use std::collections::HashMap;

    let mut map: HashMap<i64, Vec<AngleDto>> = HashMap::new();
    if ids.is_empty() {
        return Ok(map);
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let sql = format!(
        "SELECT clip_id, camera, view_kind, offset_ms, duration_s, size_bytes \
         FROM angles WHERE clip_id IN ({placeholders}) ORDER BY clip_id ASC, camera ASC"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(ids.iter()), |row| {
        Ok((
            row.get::<_, i64>(0)?,
            AngleDto {
                camera: row.get(1)?,
                view_kind: row.get(2)?,
                offset_ms: row.get(3)?,
                duration_s: row.get(4)?,
                size_bytes: row.get(5)?,
            },
        ))
    })?;
    for row in rows {
        let (clip_id, angle) = row?;
        map.entry(clip_id).or_default().push(angle);
    }
    Ok(map)
}

/// Fetch a representative waypoint for each clip id in one query.
///
/// The representative point is the first waypoint whose `has_gps_fix = 1`,
/// ordered by `seq ASC`.
fn waypoints_for_clips(
    conn: &Connection,
    ids: &[i64],
) -> Result<std::collections::HashMap<i64, (f64, f64)>, rusqlite::Error> {
    use std::collections::HashMap;

    let mut map: HashMap<i64, (f64, f64)> = HashMap::new();
    if ids.is_empty() {
        return Ok(map);
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let sql = format!(
        "SELECT clip_id, lat, lon FROM clip_waypoints \
         WHERE clip_id IN ({placeholders}) AND has_gps_fix = 1 \
         ORDER BY clip_id ASC, seq ASC"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(ids.iter()), |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, f64>(1)?,
            row.get::<_, f64>(2)?,
        ))
    })?;
    for row in rows {
        let (clip_id, lat, lon) = row?;
        map.entry(clip_id).or_insert((lat, lon));
    }
    Ok(map)
}

/// Map a `TRIP_COLS` row to a [`TripDto`], decoding the polyline blob and
/// collapsing the four bbox columns into an optional [`Bbox`].
fn map_trip(row: &Row<'_>) -> Result<TripDto, rusqlite::Error> {
    let min_lat: Option<f64> = row.get(4)?;
    let min_lon: Option<f64> = row.get(5)?;
    let max_lat: Option<f64> = row.get(6)?;
    let max_lon: Option<f64> = row.get(7)?;
    let bbox = match (min_lat, min_lon, max_lat, max_lon) {
        (Some(min_lat), Some(min_lon), Some(max_lat), Some(max_lon)) => Some(Bbox {
            min_lat,
            min_lon,
            max_lat,
            max_lon,
        }),
        _ => None,
    };
    let blob: Option<Vec<u8>> = row.get(10)?;
    let polyline = polyline::decode(blob.as_deref()).unwrap_or_default();
    Ok(TripDto {
        id: row.get(0)?,
        day: row.get(1)?,
        started_at: row.get(2)?,
        ended_at: row.get(3)?,
        bbox,
        distance_m: row.get(8)?,
        point_count: row.get(9)?,
        polyline,
    })
}

/// Map a `CLIP_COLS` row to a [`ClipDto`] with an empty angle set (filled by
/// the caller).
fn map_clip(row: &Row<'_>) -> Result<ClipDto, rusqlite::Error> {
    Ok(ClipDto {
        id: row.get(0)?,
        canonical_key: row.get(1)?,
        started_at: row.get(2)?,
        ended_at: row.get(3)?,
        partition: row.get(4)?,
        folder_class: row.get(5)?,
        is_sentry: row.get(6)?,
        duration_s: row.get(7)?,
        availability: row.get(8)?,
        lat: None,
        lon: None,
        angles: Vec::new(),
    })
}

/// Map an `EVENT_COLS` row to an [`EventDto`].
fn map_event(row: &Row<'_>) -> Result<EventDto, rusqlite::Error> {
    Ok(EventDto {
        id: row.get(0)?,
        event_type: row.get(1)?,
        severity: row.get(2)?,
        t: row.get(3)?,
        lat: row.get(4)?,
        lon: row.get(5)?,
        clip_id: row.get(6)?,
        trip_id: row.get(7)?,
        front_frame_index: row.get(8)?,
        front_frame_offset_ms: row.get(9)?,
        description: row.get(10)?,
    })
}

// ── Toybox media category list queries ─────────────────────────────────────
//
// All five share the same probe + LIKE pattern:
//   1. Check `sqlite_master` so an old DB without the table degrades to `Ok([])`.
//   2. Query `media_entries WHERE partition = 'slot1' AND rel_path LIKE '<prefix>%'`.
//   3. Return a `Vec<MediaItemDto>` (never `Err` on "table missing" — only real I/O
//      errors propagate).
//
// LightShows targets the `LightShow/` subtree; Wraps targets the separate
// root-level `Wraps/` subtree. The two never overlap on disk.

/// Return `true` iff the `media_entries` table exists in this connection's DB.
fn media_entries_present(conn: &Connection) -> Result<bool, rusqlite::Error> {
    conn.query_row(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_entries'",
        [],
        |_| Ok(true),
    )
    .optional()
    .map(|v| v.unwrap_or(false))
}

/// Map a `media_entries` row (name, `rel_path`, `size_bytes`, modified) to a
/// [`MediaItemDto`].
fn map_media_item(row: &Row<'_>) -> Result<MediaItemDto, rusqlite::Error> {
    Ok(MediaItemDto {
        name: row.get(0)?,
        rel_path: row.get(1)?,
        size_bytes: row.get(2)?,
        modified: row.get(3)?,
    })
}

/// `GET /api/boombox` — files under `Boombox/` on p2.
pub(crate) fn list_boombox(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' AND rel_path LIKE 'Boombox/%' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/music` — files under `Music/` on p2 (any depth).
pub(crate) fn list_music(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' AND rel_path LIKE 'Music/%' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/lightshows` — files under `LightShow/` on p2. Wraps live in the
/// separate root-level `Wraps/` folder ([`list_wraps`]) and never appear here.
pub(crate) fn list_lightshows(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' \
           AND rel_path LIKE 'LightShow/%' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/plates` — files under `LicensePlate/` on p2.
pub(crate) fn list_plates(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' AND rel_path LIKE 'LicensePlate/%' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/chimes/library` — files under the root-level `Chimes/` folder on p2.
pub(crate) fn list_chime_library(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' \
           AND rel_path LIKE 'Chimes/%' \
           AND rel_path NOT LIKE 'Chimes/%/%' \
           AND lower(rel_path) LIKE '%.wav' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

/// `GET /api/wraps` — files under the root-level `Wraps/` folder on p2.
pub(crate) fn list_wraps(conn: &Connection) -> Result<Vec<MediaItemDto>, rusqlite::Error> {
    if !media_entries_present(conn)? {
        return Ok(vec![]);
    }
    let mut stmt = conn.prepare(
        "SELECT name, rel_path, size_bytes, modified FROM media_entries \
         WHERE partition = 'slot1' AND rel_path LIKE 'Wraps/%' \
         ORDER BY rel_path ASC",
    )?;
    stmt.query_map([], map_media_item)?
        .collect::<Result<Vec<_>, _>>()
}

#[cfg(test)]
mod tests {
    use rusqlite::{Connection, params};

    use super::{
        Keyset, SnapshotResource, get_clip, list_chime_library, list_clips, list_days,
        list_standalone_day_events, snapshot_max_id,
    };

    fn seed_media_rows(conn: &Connection, rows: &[(&str, &str, &str, i64)]) {
        for (partition, rel_path, name, size_bytes) in rows {
            conn.execute(
                "INSERT INTO media_entries (partition, rel_path, name, size_bytes, modified, updated_at) \
                 VALUES (?1, ?2, ?3, ?4, 0, 0)",
                (*partition, *rel_path, *name, *size_bytes),
            )
            .unwrap();
        }
    }

    fn test_conn() -> Connection {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        conn
    }

    fn insert_clip(conn: &Connection, id: i64, started_at: i64) {
        conn.execute(
            "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, \
             is_sentry, duration_s, availability, created_at, updated_at) \
             VALUES (?1, ?2, ?3, ?4, 'slot0', 'SavedClips', 0, 60.0, 'present', 0, 0)",
            params![id, format!("clip-{id}"), started_at, started_at + 60],
        )
        .unwrap();
    }

    fn insert_waypoint(
        conn: &Connection,
        clip_id: i64,
        seq: i64,
        lat: f64,
        lon: f64,
        has_gps_fix: bool,
    ) {
        conn.execute(
            "INSERT INTO clip_waypoints
                (clip_id, seq, frame_index, offset_ms, t, lat, lon, speed, heading,
                 accel_x, accel_y, accel_z, autopilot, gear, has_gps_fix)
             VALUES
                (?1, ?2, ?3, ?4, NULL, ?5, ?6, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?7)",
            params![clip_id, seq, seq, seq as f64, lat, lon, i64::from(has_gps_fix)],
        )
        .unwrap();
    }

    fn insert_trip(conn: &Connection, id: i64, day: &str, started_at: i64, distance_m: f64) {
        conn.execute(
            "INSERT INTO trips (id, day, started_at, ended_at, distance_m, point_count, created_at, updated_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, 0, 0, 0)",
            params![id, day, started_at, started_at + 60, distance_m],
        )
        .unwrap();
    }

    #[test]
    fn list_chime_library_returns_only_chimes_rows() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = Connection::open(tmp.path()).unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        indexd::db::apply_migrations(&mut conn).unwrap();
        seed_media_rows(
            &conn,
            &[
                ("slot1", "Chimes/a.wav", "a.wav", 10),
                ("slot1", "Chimes/b.wav", "b.wav", 20),
                ("slot1", "Chimes/sub/c.wav", "c.wav", 30),
                ("slot1", "Chimes/readme.txt", "readme.txt", 40),
                ("slot1", "LockChime.wav", "LockChime.wav", 50),
                ("slot1", "Wraps/x.png", "x.png", 60),
            ],
        );

        let items = list_chime_library(&conn).unwrap();

        assert_eq!(items.len(), 2);
        assert_eq!(items[0].rel_path, "Chimes/a.wav");
        assert_eq!(items[1].rel_path, "Chimes/b.wav");
        assert_eq!(
            items
                .iter()
                .map(|item| item.name.as_str())
                .collect::<Vec<_>>(),
            vec!["a.wav", "b.wav"]
        );
        assert!(items.iter().all(|item| item.rel_path.starts_with("Chimes/") && item.rel_path.matches('/').count() == 1));
    }

    #[test]
    fn list_clips_uses_first_fixed_waypoint_by_seq_for_representative_lat_lon() {
        let conn = test_conn();
        insert_clip(&conn, 1, 1_000);
        insert_waypoint(&conn, 1, 30, 30.0, -30.0, true);
        insert_waypoint(&conn, 1, 10, 10.0, -10.0, true);
        insert_waypoint(&conn, 1, 20, 20.0, -20.0, true);

        let snap = snapshot_max_id(&conn, SnapshotResource::Clips)
            .unwrap()
            .unwrap();
        let clips = list_clips(&conn, Keyset { snap, after: None }, 10, None).unwrap();
        assert_eq!(clips.len(), 1);
        assert_eq!(clips[0].lat, Some(10.0));
        assert_eq!(clips[0].lon, Some(-10.0));
    }

    #[test]
    fn get_clip_ignores_non_fixed_waypoint_before_fixed_representative_point() {
        let conn = test_conn();
        insert_clip(&conn, 2, 2_000);
        insert_waypoint(&conn, 2, 1, 1.0, -1.0, false);
        insert_waypoint(&conn, 2, 2, 2.0, -2.0, true);
        insert_waypoint(&conn, 2, 3, 3.0, -3.0, true);

        let clip = get_clip(&conn, 2).unwrap().unwrap();
        assert_eq!(clip.lat, Some(2.0));
        assert_eq!(clip.lon, Some(-2.0));
    }

    #[test]
    fn get_clip_returns_none_lat_lon_when_only_non_fixed_waypoints_exist() {
        let conn = test_conn();
        insert_clip(&conn, 3, 3_000);
        insert_waypoint(&conn, 3, 1, 40.0, -40.0, false);
        insert_waypoint(&conn, 3, 2, 41.0, -41.0, false);

        let clip = get_clip(&conn, 3).unwrap().unwrap();
        assert_eq!(clip.lat, None);
        assert_eq!(clip.lon, None);
    }

    #[test]
    fn get_clip_returns_none_lat_lon_when_clip_has_no_waypoints() {
        let conn = test_conn();
        insert_clip(&conn, 4, 4_000);

        let clip = get_clip(&conn, 4).unwrap().unwrap();
        assert_eq!(clip.lat, None);
        assert_eq!(clip.lon, None);
    }

    #[test]
    fn list_days_includes_purely_parked_day_with_zero_trips() {
        let conn = test_conn();
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL, NULL, ?7, ?8)",
            params![1, "saved", 1, 86_400, 47.0, -122.0, "parked pin", 86_400],
        )
        .unwrap();

        let days = list_days(&conn).unwrap();
        let day = days.iter().find(|item| item.day == "1970-01-02").unwrap();
        assert_eq!(day.trip_count, 0);
        assert_eq!(day.event_count, 1);
    }

    #[test]
    fn list_days_sums_trip_linked_and_standalone_event_counts() {
        let conn = test_conn();
        insert_trip(&conn, 1, "1970-01-03", 172_800, 1234.5);
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, ?7, NULL, NULL, ?8, ?9)",
            params![2, "hard_brake", 2, 172_900, 47.1, -122.1, 1, "trip-linked", 172_900],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL, NULL, ?7, ?8)",
            params![3, "sentry", 1, 172_950, 47.2, -122.2, "standalone", 172_950],
        )
        .unwrap();

        let days = list_days(&conn).unwrap();
        let day = days.iter().find(|item| item.day == "1970-01-03").unwrap();
        assert_eq!(day.trip_count, 1);
        assert_eq!(day.event_count, 2);
    }

    #[test]
    fn list_standalone_day_events_filters_to_standalone_pinned_rows() {
        let conn = test_conn();
        insert_trip(&conn, 2, "1970-01-04", 259_200, 10.0);
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL, NULL, ?7, ?8)",
            params![10, "saved", 1, 259_210, 47.3, -122.3, "include", 259_210],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, NULL, ?5, NULL, NULL, NULL, NULL, ?6, ?7)",
            params![11, "saved", 1, 259_220, -122.4, "missing lat", 259_220],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, ?7, NULL, NULL, ?8, ?9)",
            params![12, "hard_accel", 2, 259_230, 47.4, -122.4, 2, "trip-linked", 259_230],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO events \
                (id, type, severity, t, lat, lon, clip_id, trip_id, front_frame_index, front_frame_offset, description, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL, NULL, ?7, ?8)",
            params![13, "sentry", 1, 345_600, 47.5, -122.5, "other day", 345_600],
        )
        .unwrap();

        let events = list_standalone_day_events(&conn, "1970-01-04", 5_000).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].id, 10);
    }
}
