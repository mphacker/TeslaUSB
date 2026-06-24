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

/// `GET /api/days`: civil days with driving trips plus rolled-up counts.
///
/// `event_count` is events linked to trips on that day (trip-less sentry events
/// are not attributed to a day here — see [`DaySummary`]).
pub(crate) fn list_days(conn: &Connection) -> Result<Vec<DaySummary>, rusqlite::Error> {
    let sql = "SELECT t.day, COUNT(*) AS trip_count, \
        COALESCE(SUM(t.distance_m), 0.0) AS distance_m, \
        (SELECT COUNT(*) FROM events e JOIN trips t2 ON e.trip_id = t2.id \
         WHERE t2.day = t.day) AS event_count \
        FROM trips t GROUP BY t.day ORDER BY t.day DESC";
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
    for clip in &mut clips {
        if let Some(set) = angles.remove(&clip.id) {
            clip.angles = set;
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
    use rusqlite::Connection;

    use super::list_chime_library;

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
}
