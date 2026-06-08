//! The short read queries over the catalog. Every function takes a borrowed
//! read-only [`Connection`] and binds straight to the as-built `indexd` schema
//! (`indexd::db::migrations`). All queries are read-only; none mutate.

use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::dto::{
    AnalyticsDto, AngleDto, Bbox, ClipDto, DaySummary, DayTripCount, EventDto, EventTypeCount,
    PrefDto, TripDetailDto, TripDto, TripPointDto,
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

/// `GET /api/events`: cursor page of event bubbles (`id > after`, ascending),
/// optionally filtered to a single `trip`.
pub(crate) fn list_events(
    conn: &Connection,
    after: i64,
    limit: i64,
    trip: Option<i64>,
) -> Result<Vec<EventDto>, rusqlite::Error> {
    if let Some(trip) = trip {
        let sql = format!("{EVENT_COLS} WHERE id > ?1 AND trip_id = ?2 ORDER BY id ASC LIMIT ?3");
        let mut stmt = conn.prepare(&sql)?;
        let out = stmt
            .query_map(params![after, trip, limit], map_event)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(out)
    } else {
        let sql = format!("{EVENT_COLS} WHERE id > ?1 ORDER BY id ASC LIMIT ?2");
        let mut stmt = conn.prepare(&sql)?;
        let out = stmt
            .query_map(params![after, limit], map_event)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(out)
    }
}

/// `GET /api/clips`: cursor page of clips (`id > after`, ascending), optionally
/// filtered by `folder_class`, each with its camera angles.
pub(crate) fn list_clips(
    conn: &Connection,
    after: i64,
    limit: i64,
    folder_class: Option<&str>,
) -> Result<Vec<ClipDto>, rusqlite::Error> {
    let mut clips: Vec<ClipDto> = if let Some(fc) = folder_class {
        let sql =
            format!("{CLIP_COLS} WHERE id > ?1 AND folder_class = ?2 ORDER BY id ASC LIMIT ?3");
        let mut stmt = conn.prepare(&sql)?;
        stmt.query_map(params![after, fc, limit], map_clip)?
            .collect::<Result<Vec<_>, _>>()?
    } else {
        let sql = format!("{CLIP_COLS} WHERE id > ?1 ORDER BY id ASC LIMIT ?2");
        let mut stmt = conn.prepare(&sql)?;
        stmt.query_map(params![after, limit], map_clip)?
            .collect::<Result<Vec<_>, _>>()?
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

    Ok(AnalyticsDto {
        total_trips,
        total_distance_m,
        total_events,
        events_by_type,
        trips_by_day,
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
