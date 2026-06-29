//! Idempotent ingest of scanned clips + derived trips/events.
//!
//! The scan pipeline calls these entry points as the **sole writer**
//! (contract D1 §1). All upserts are idempotent via `ON CONFLICT` so a
//! re-scan of unchanged media produces no row churn. The split mirrors the
//! v1 worker:
//!
//!   * **Derived state** (`clips`, `angles`, `clip_waypoints`, `trips`,
//!     `trip_points`, `events`) is owned by the scan and is fully
//!     rebuildable from the media.
//!   * **Durable control state** (`archive_items`, `eviction_tombstones`,
//!     `leases`, `prefs`, and the `pinned` / `durable` / `delete_state`
//!     columns) is never touched by the scan ingest path — a rebuild must
//!     preserve it (D1 §5/§6). The explicit archive-registration mutation
//!     is a separate entry point below. Pruning a vanished clip therefore
//!     only `SET NULL`s the `archive_items.clip_id` back-reference (via the
//!     schema FK), never deletes the archive row.
//!
//! Prune semantics mirror `mapping_index_prune.py`: rows for clips whose
//! `canonical_key` is no longer present on the media are removed, and only
//! after a *complete* successful scan (the caller passes the full present
//! key set).

use std::collections::HashSet;

use rusqlite::{Connection, params};
use teslausb_core::sei::tesla::{AutopilotState, Gear};

use crate::db::{DbError, now_epoch_s};
use crate::derive::{DeriveConfig, derive};
use crate::model::{
    ClipEventInput, Derivation, DeriveClip, DeriveWaypoint, DerivedTrip, FolderClass,
};

/// Identity + classification facts for one clip (a group of camera
/// angles). `started_at` is the resolved recording instant (mvhd-first,
/// filename fallback) in UTC epoch seconds.
#[derive(Debug, Clone)]
pub struct ClipFacts {
    /// Dedup key: clip directory + timestamp prefix (camera suffix
    /// stripped), mirroring v1 `mapping_service.canonical_key`.
    pub canonical_key: String,
    /// Resolved recording instant, UTC epoch seconds.
    pub started_at: i64,
    /// Resolved end instant, if known.
    pub ended_at: Option<i64>,
    /// Source partition label (exFAT slot identity).
    pub partition: String,
    /// Source-folder classification.
    pub folder_class: FolderClass,
    /// Clip duration in seconds, if probed.
    pub duration_s: Option<f64>,
}

/// One camera angle (file) belonging to a clip.
#[derive(Debug, Clone)]
pub struct AngleFacts {
    /// Tesla camera label (`front`, `back`, `left_repeater`, …).
    pub camera: String,
    /// Opaque reference the reader can resolve back to the file
    /// (path within the volume).
    pub file_ref: String,
    /// `ro_usb` / `archive` provenance (D1 `angles.view_kind`).
    pub view_kind: String,
    /// Millisecond offset of this angle relative to the clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds, if probed.
    pub duration_s: Option<f64>,
    /// File size in bytes, if known.
    pub size_bytes: Option<i64>,
}

/// One archive-backed angle attached during archive registration.
#[derive(Debug, Clone)]
pub struct ArchiveAngleRegistration {
    /// Camera label (`front`, `back`, `left_repeater`, ...).
    pub camera: String,
    /// Archive-root-relative playable file reference.
    pub file_ref: String,
    /// Milliseconds relative to clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds, when known.
    pub duration_s: Option<f64>,
    /// File size in bytes.
    pub size_bytes: i64,
}

/// One archive item unit attached during archive registration.
#[derive(Debug, Clone)]
pub struct ArchiveUnitRegistration {
    /// Deterministic archive-root-relative item path.
    pub path: String,
    /// Total bytes in the archive item.
    pub size_bytes: i64,
    /// Number of files in the archive item.
    pub file_count: i64,
    /// Wall-clock archive completion epoch seconds.
    pub archived_at: i64,
}

/// Complete archive registration payload written in one DB transaction.
#[derive(Debug, Clone)]
pub struct ArchiveRegistration {
    /// Clip identity key shared with scanner ingest.
    pub canonical_key: String,
    /// Source bucket classification (origin, not promotion target).
    pub folder_class: FolderClass,
    /// Source partition label.
    pub partition: String,
    /// Clip start epoch seconds.
    pub started_at: i64,
    /// Clip end epoch seconds.
    pub ended_at: i64,
    /// Clip duration in seconds when known.
    pub duration_s: Option<f64>,
    /// Archive item metadata.
    pub archive: ArchiveUnitRegistration,
    /// Camera angles to force archive-backed.
    pub angles: Vec<ArchiveAngleRegistration>,
}

/// Upsert a clip by `canonical_key`, returning its DB id. `created_at` is
/// preserved across updates (only set on first insert).
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn upsert_clip(conn: &Connection, facts: &ClipFacts) -> Result<i64, DbError> {
    let now = now_epoch_s();
    let id: i64 = conn.query_row(
        "INSERT INTO clips
             (canonical_key, started_at, ended_at, partition, folder_class,
              is_sentry, duration_s, availability, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'present', ?8, ?8)
         ON CONFLICT(canonical_key) DO UPDATE SET
             started_at   = excluded.started_at,
             ended_at     = excluded.ended_at,
             partition    = excluded.partition,
             folder_class = excluded.folder_class,
             is_sentry    = excluded.is_sentry,
             duration_s   = excluded.duration_s,
             availability = excluded.availability,
             updated_at   = excluded.updated_at
         RETURNING id",
        params![
            facts.canonical_key,
            facts.started_at,
            facts.ended_at,
            facts.partition,
            facts.folder_class.as_db_str(),
            i64::from(facts.folder_class.is_sentry()),
            facts.duration_s,
            now,
        ],
        |r| r.get(0),
    )?;
    Ok(id)
}

/// Ensure a clip row exists WITHOUT overwriting its recording instant.
///
/// Used for non-front angles, whose Tesla-filename timestamp is a weaker
/// recording instant than the front clip's `mvhd`/GPS-derived one. On a
/// fresh row this seeds `started_at`/`folder_class` from the filename
/// facts; on an existing row it touches only `updated_at`, so a
/// previously front-resolved `started_at` is never downgraded. Returns
/// the clip id.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn ensure_clip(conn: &Connection, facts: &ClipFacts) -> Result<i64, DbError> {
    let now = now_epoch_s();
    let id: i64 = conn.query_row(
        "INSERT INTO clips
             (canonical_key, started_at, ended_at, partition, folder_class,
              is_sentry, duration_s, availability, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'present', ?8, ?8)
         ON CONFLICT(canonical_key) DO UPDATE SET
             availability = 'present',
             updated_at   = excluded.updated_at
         RETURNING id",
        params![
            facts.canonical_key,
            facts.started_at,
            facts.ended_at,
            facts.partition,
            facts.folder_class.as_db_str(),
            i64::from(facts.folder_class.is_sentry()),
            facts.duration_s,
            now,
        ],
        |r| r.get(0),
    )?;
    Ok(id)
}

/// Upsert one camera angle for the scan path, keyed `UNIQUE(clip_id, camera)`.
/// If the existing row is already archive-backed, preserve its `view_kind`
/// + `file_ref` (Guard A) while still refreshing offset/duration/size.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn upsert_angle_scan_preserving(
    conn: &Connection,
    clip_id: i64,
    angle: &AngleFacts,
) -> Result<(), DbError> {
    conn.execute(
        "INSERT INTO angles
             (clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT(clip_id, camera) DO UPDATE SET
             file_ref   = CASE
                            WHEN angles.view_kind = 'archive' THEN angles.file_ref
                            ELSE excluded.file_ref
                          END,
             view_kind  = CASE
                            WHEN angles.view_kind = 'archive' THEN angles.view_kind
                            ELSE excluded.view_kind
                          END,
             offset_ms  = excluded.offset_ms,
             duration_s = excluded.duration_s,
             size_bytes = excluded.size_bytes",
        params![
            clip_id,
            angle.camera,
            angle.file_ref,
            angle.view_kind,
            angle.offset_ms,
            angle.duration_s,
            angle.size_bytes,
        ],
    )?;
    Ok(())
}

/// Upsert one camera angle and force archive precedence.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn upsert_angle_force_archive(
    conn: &Connection,
    clip_id: i64,
    angle: &AngleFacts,
) -> Result<(), DbError> {
    conn.execute(
        "INSERT INTO angles
             (clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes)
         VALUES (?1, ?2, ?3, 'archive', ?4, ?5, ?6)
         ON CONFLICT(clip_id, camera) DO UPDATE SET
             file_ref   = excluded.file_ref,
             view_kind  = 'archive',
             offset_ms  = excluded.offset_ms,
             duration_s = excluded.duration_s,
             size_bytes = excluded.size_bytes",
        params![
            clip_id,
            angle.camera,
            angle.file_ref,
            angle.offset_ms,
            angle.duration_s,
            angle.size_bytes,
        ],
    )?;
    Ok(())
}

/// Register one archived clip in a single transaction:
/// ensure clip, upsert archive item (`LIVE`, `durable=0`), link
/// `archive_item_clips`, and force angles to archive.
///
/// # Errors
///
/// Returns [`DbError`] if any statement fails (the transaction rolls back).
pub fn register_archived_clip(
    conn: &mut Connection,
    registration: &ArchiveRegistration,
) -> Result<(i64, i64), DbError> {
    register_clip_with_disposition(conn, registration, RegistrationDisposition::Live)
}

/// Register one quarantined archived clip in a single transaction:
/// ensure clip, upsert archive item (`QUARANTINED`, `durable=0`), and link
/// `archive_item_clips` without promoting angles to archive.
///
/// # Errors
///
/// Returns [`DbError`] if any statement fails (the transaction rolls back).
pub fn register_quarantined_clip(
    conn: &mut Connection,
    registration: &ArchiveRegistration,
) -> Result<(i64, i64), DbError> {
    register_clip_with_disposition(
        conn,
        registration,
        RegistrationDisposition::QuarantineUndecodable,
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RegistrationDisposition {
    Live,
    QuarantineUndecodable,
}

fn register_clip_with_disposition(
    conn: &mut Connection,
    registration: &ArchiveRegistration,
    disposition: RegistrationDisposition,
) -> Result<(i64, i64), DbError> {
    let now = now_epoch_s();
    let tx = conn.transaction()?;
    let clip_id = ensure_clip(
        &tx,
        &ClipFacts {
            canonical_key: registration.canonical_key.clone(),
            started_at: registration.started_at,
            ended_at: Some(registration.ended_at),
            partition: registration.partition.clone(),
            folder_class: registration.folder_class,
            duration_s: registration.duration_s,
        },
    )?;

    let archive_item_id: i64 = tx.query_row(
        "INSERT INTO archive_items
             (folder_class, path, clip_id, size_bytes, file_count, archived_at,
              delete_state, durable, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0, ?8, ?8)
         ON CONFLICT(path) DO UPDATE SET
             size_bytes  = excluded.size_bytes,
             file_count  = excluded.file_count,
             archived_at = excluded.archived_at,
             clip_id     = excluded.clip_id,
             delete_state = CASE
                 WHEN archive_items.delete_state = 'LIVE' AND ?9 = 'QUARANTINED'
                 THEN archive_items.delete_state
                 ELSE excluded.delete_state
             END,
             durable     = 0,
             updated_at  = excluded.updated_at
         RETURNING id",
        params![
            registration.folder_class.as_db_str(),
            registration.archive.path,
            clip_id,
            registration.archive.size_bytes,
            registration.archive.file_count,
            registration.archive.archived_at,
            disposition.delete_state(),
            now,
            disposition.delete_state(),
        ],
        |r| r.get(0),
    )?;

    tx.execute(
        "INSERT OR IGNORE INTO archive_item_clips (archive_item_id, clip_id)
         VALUES (?1, ?2)",
        params![archive_item_id, clip_id],
    )?;

    if disposition == RegistrationDisposition::Live {
        // FU-2 owns explicit finalization-aware un-quarantine. A LIVE register
        // over an existing QUARANTINED row currently resurrects it; this is
        // only reachable if candidate-selection exclusion is bypassed.
        for angle in &registration.angles {
            upsert_angle_force_archive(
                &tx,
                clip_id,
                &AngleFacts {
                    camera: angle.camera.clone(),
                    file_ref: angle.file_ref.clone(),
                    view_kind: "archive".to_owned(),
                    offset_ms: angle.offset_ms,
                    duration_s: angle.duration_s,
                    size_bytes: Some(angle.size_bytes),
                },
            )?;
        }
    }

    tx.commit()?;
    Ok((clip_id, archive_item_id))
}

impl RegistrationDisposition {
    const fn delete_state(self) -> &'static str {
        match self {
            Self::Live => "LIVE",
            Self::QuarantineUndecodable => "QUARANTINED",
        }
    }
}

/// Replace the cached SEI telemetry for a clip (full delete + reinsert).
/// This is pure derived state; the rows carry no control flags.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn replace_clip_waypoints(
    conn: &Connection,
    clip_id: i64,
    waypoints: &[DeriveWaypoint],
) -> Result<(), DbError> {
    conn.execute(
        "DELETE FROM clip_waypoints WHERE clip_id = ?1",
        params![clip_id],
    )?;
    let mut stmt = conn.prepare(
        "INSERT INTO clip_waypoints
             (clip_id, seq, frame_index, offset_ms, t, lat, lon, speed, heading,
              accel_x, accel_y, accel_z, autopilot, gear, has_gps_fix)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
    )?;
    for (seq, wp) in waypoints.iter().enumerate() {
        let seq = i64::try_from(seq).unwrap_or(i64::MAX);
        stmt.execute(params![
            clip_id,
            seq,
            wp.frame_index,
            wp.offset_ms,
            wp.absolute_utc,
            wp.lat,
            wp.lon,
            wp.speed,
            wp.heading,
            wp.accel_x,
            wp.accel_y,
            wp.accel_z,
            wp.autopilot_state.as_db_str(),
            wp.gear.as_db_str(),
            i64::from(wp.has_gps_fix),
        ])?;
    }
    Ok(())
}

/// Remove derived rows for clips whose `canonical_key` is absent from
/// `present_keys`. Returns the number of clips pruned. Durable
/// `archive_items` are preserved (their `clip_id` back-reference is
/// `SET NULL` by the schema FK). Mirrors `mapping_index_prune.py`.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn prune_missing_clips<S: std::hash::BuildHasher>(
    conn: &Connection,
    present_keys: &HashSet<String, S>,
) -> Result<usize, DbError> {
    let stale: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id, canonical_key FROM clips")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        let mut stale = Vec::new();
        for row in rows {
            let (id, key) = row?;
            if !present_keys.contains(&key) && !has_archive_backing(conn, id)? {
                stale.push(id);
            }
        }
        stale
    };
    for id in &stale {
        conn.execute("DELETE FROM clips WHERE id = ?1", params![id])?;
    }
    Ok(stale.len())
}

fn has_archive_backing(conn: &Connection, clip_id: i64) -> Result<bool, DbError> {
    let has_backing = conn.query_row(
        "SELECT EXISTS(
             SELECT 1
               FROM angles
              WHERE clip_id = ?1 AND view_kind = 'archive'
         ) OR EXISTS(
             SELECT 1
               FROM archive_items
              WHERE clip_id = ?1 AND delete_state <> 'DELETED'
         ) OR EXISTS(
             SELECT 1
               FROM archive_item_clips aic
               JOIN archive_items ai ON ai.id = aic.archive_item_id
              WHERE aic.clip_id = ?1
                AND ai.delete_state <> 'DELETED'
         )",
        params![clip_id],
        |r| r.get::<_, bool>(0),
    )?;
    Ok(has_backing)
}

/// One MEDIA-partition (p2) inventory fact: a file the read-only media
/// screens display (the lock chime today). Identity is
/// `(partition, rel_path)`.
#[derive(Debug, Clone)]
pub struct MediaFacts {
    /// Source partition label (`slot1` for MEDIA).
    pub partition: String,
    /// Path relative to the partition root (e.g. `LockChime.wav`).
    pub rel_path: String,
    /// File name (last path component).
    pub name: String,
    /// File size in bytes (never negative).
    pub size_bytes: i64,
    /// Best-effort naive-local `YYYY-MM-DDThh:mm:ss` modification string.
    pub modified: Option<String>,
}

/// Upsert one media-inventory row by `(partition, rel_path)`. Pure derived
/// state — `updated_at` is refreshed every pass.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn upsert_media(conn: &Connection, facts: &MediaFacts) -> Result<(), DbError> {
    let now = now_epoch_s();
    conn.execute(
        "INSERT INTO media_entries
            (partition, rel_path, name, size_bytes, modified, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(partition, rel_path) DO UPDATE SET
            name       = excluded.name,
            size_bytes = excluded.size_bytes,
            modified   = excluded.modified,
            updated_at = excluded.updated_at",
        params![
            facts.partition,
            facts.rel_path,
            facts.name,
            facts.size_bytes,
            facts.modified,
            now,
        ],
    )?;
    Ok(())
}

/// Delete media rows whose `rel_path` is not in `present_paths`. The caller
/// MUST gate this on the producer's `media_inventory` capability AND a
/// `complete` batch, so a media-unaware or torn scan never wipes the table.
///
/// # Errors
///
/// Returns [`DbError`] if a query fails.
pub fn prune_missing_media<S: std::hash::BuildHasher>(
    conn: &Connection,
    present_paths: &HashSet<String, S>,
) -> Result<usize, DbError> {
    let stale: Vec<i64> = {
        let mut stmt = conn.prepare("SELECT id, rel_path FROM media_entries")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
        let mut stale = Vec::new();
        for row in rows {
            let (id, rel_path) = row?;
            if !present_paths.contains(&rel_path) {
                stale.push(id);
            }
        }
        stale
    };
    for id in &stale {
        conn.execute("DELETE FROM media_entries WHERE id = ?1", params![id])?;
    }
    Ok(stale.len())
}

/// One Saved/Sentry event-directory sidecar fact parsed from `event.json`.
/// Identity is the event directory key (`slot:<event-folder-rel-path>`).
#[derive(Debug, Clone)]
pub struct ClipEventFacts {
    /// Sidecar primary key: `slot:<event-folder-rel-path>`.
    pub event_dir_key: String,
    /// Source-folder classification (`SavedClips` / `SentryClips`).
    pub bucket: String,
    /// Front clip `canonical_key` used as FK link target.
    pub primary_canonical_key: String,
    /// Best-effort UTC from `event.json`.
    pub timestamp_utc: i64,
    /// Raw local wall-clock interpreted as naive seconds.
    pub timestamp_local_naive: i64,
    /// Whether the source timestamp carried an explicit offset.
    pub timestamp_has_offset: bool,
    /// Estimated latitude, when present.
    pub est_lat: Option<f64>,
    /// Estimated longitude, when present.
    pub est_lon: Option<f64>,
    /// Tesla event reason, when present.
    pub reason: Option<String>,
    /// Tesla city label, when present.
    pub city: Option<String>,
    /// Tesla camera label, when present.
    pub camera: Option<String>,
}

/// Upsert one clip-event sidecar row by `event_dir_key`. Raw scanner metadata
/// (not derived state) — `updated_at` is refreshed every pass.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn upsert_clip_event(conn: &Connection, facts: &ClipEventFacts) -> Result<(), DbError> {
    let now = now_epoch_s();
    conn.execute(
        "INSERT INTO clip_events
            (event_dir_key, bucket, primary_canonical_key, timestamp_utc,
             timestamp_local_naive, timestamp_has_offset, est_lat, est_lon,
             reason, city, camera, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
         ON CONFLICT(event_dir_key) DO UPDATE SET
            bucket                = excluded.bucket,
            primary_canonical_key = excluded.primary_canonical_key,
            timestamp_utc         = excluded.timestamp_utc,
            timestamp_local_naive = excluded.timestamp_local_naive,
            timestamp_has_offset  = excluded.timestamp_has_offset,
            est_lat               = excluded.est_lat,
            est_lon               = excluded.est_lon,
            reason                = excluded.reason,
            city                  = excluded.city,
            camera                = excluded.camera,
            updated_at            = excluded.updated_at",
        params![
            facts.event_dir_key,
            facts.bucket,
            facts.primary_canonical_key,
            facts.timestamp_utc,
            facts.timestamp_local_naive,
            i64::from(facts.timestamp_has_offset),
            facts.est_lat,
            facts.est_lon,
            facts.reason,
            facts.city,
            facts.camera,
            now,
        ],
    )?;
    Ok(())
}

/// Delete clip-event rows whose `event_dir_key` is not in `present_keys`. The
/// caller MUST gate this on the producer's `clip_events_inventory` capability
/// AND a `complete` batch, so a clip-event-unaware or torn scan never wipes
/// the sidecar table.
///
/// # Errors
///
/// Returns [`DbError`] if a query fails.
pub fn prune_missing_clip_events<S: std::hash::BuildHasher>(
    conn: &Connection,
    present_keys: &HashSet<String, S>,
) -> Result<usize, DbError> {
    let stale: Vec<String> = {
        let mut stmt = conn.prepare("SELECT event_dir_key FROM clip_events")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        let mut stale = Vec::new();
        for row in rows {
            let event_dir_key = row?;
            if !present_keys.contains(&event_dir_key) {
                stale.push(event_dir_key);
            }
        }
        stale
    };
    for event_dir_key in &stale {
        conn.execute(
            "DELETE FROM clip_events WHERE event_dir_key = ?1",
            params![event_dir_key],
        )?;
    }
    Ok(stale.len())
}

/// Load the front-camera clips with cached waypoints, ready for
/// derivation. A clip qualifies iff it has `clip_waypoints` rows (only
/// front clips are walked). Ordered `(started_at, id)` to match the
/// materializer's `ORDER BY` so clustering is deterministic.
///
/// # Errors
///
/// Returns [`DbError`] if a query fails.
pub fn load_derive_clips(conn: &Connection) -> Result<Vec<DeriveClip>, DbError> {
    let clip_rows: Vec<(i64, i64, String, String)> = {
        let mut stmt = conn.prepare(
            "SELECT c.id, c.started_at, c.folder_class, c.canonical_key
               FROM clips c
              WHERE EXISTS (SELECT 1 FROM clip_waypoints w WHERE w.clip_id = c.id)
              ORDER BY c.started_at ASC, c.id ASC",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        out
    };

    let mut clips = Vec::with_capacity(clip_rows.len());
    for (clip_id, started_at, folder_class, canonical_key) in clip_rows {
        let waypoints = load_waypoints(conn, clip_id)?;
        let gps_waypoint_count = waypoints.iter().filter(|w| w.has_gps_fix).count();
        clips.push(DeriveClip {
            clip_id,
            canonical_key,
            clip_started_utc: started_at,
            folder_class: FolderClass::from_db_str(&folder_class),
            gps_waypoint_count: i64::try_from(gps_waypoint_count).unwrap_or(i64::MAX),
            waypoints,
        });
    }
    Ok(clips)
}

/// Load event.json sidecar rows, resolved against clips for derivation.
///
/// # Errors
///
/// Returns [`DbError`] if a query fails.
pub fn load_clip_events(conn: &Connection) -> Result<Vec<ClipEventInput>, DbError> {
    let mut stmt = conn.prepare(
        "SELECT ce.event_dir_key, ce.bucket, ce.timestamp_utc, ce.timestamp_local_naive,
                ce.timestamp_has_offset, ce.est_lat, ce.est_lon, ce.reason, ce.city, ce.camera,
                c.id, c.started_at,
                (c.id IS NOT NULL AND EXISTS(SELECT 1 FROM clip_waypoints w WHERE w.clip_id = c.id)) AS trusted
           FROM clip_events ce
           LEFT JOIN clips c ON c.canonical_key = ce.primary_canonical_key
          ORDER BY ce.timestamp_utc ASC, ce.event_dir_key ASC",
    )?;
    let rows = stmt.query_map([], |r| {
        let bucket_str: String = r.get(1)?;
        Ok(ClipEventInput {
            event_dir_key: r.get(0)?,
            bucket: FolderClass::from_db_str(&bucket_str),
            primary_clip_id: r.get(10)?,
            primary_started_utc: r.get(11)?,
            primary_started_trusted: r.get::<_, i64>(12)? != 0,
            est_lat: r.get(5)?,
            est_lon: r.get(6)?,
            reason: r.get(7)?,
            city: r.get(8)?,
            camera: r.get(9)?,
            timestamp_utc: r.get(2)?,
            timestamp_local_naive: r.get(3)?,
            timestamp_has_offset: r.get::<_, i64>(4)? != 0,
        })
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// Load one clip's cached waypoints in `seq` order.
fn load_waypoints(conn: &Connection, clip_id: i64) -> Result<Vec<DeriveWaypoint>, DbError> {
    let mut stmt = conn.prepare(
        "SELECT frame_index, offset_ms, t, lat, lon, speed, heading,
                accel_x, accel_y, accel_z, autopilot, gear, has_gps_fix
           FROM clip_waypoints
          WHERE clip_id = ?1
          ORDER BY seq ASC",
    )?;
    let rows = stmt.query_map(params![clip_id], |r| {
        Ok(DeriveWaypoint {
            frame_index: r.get(0)?,
            offset_ms: r.get(1)?,
            absolute_utc: r.get(2)?,
            lat: r.get(3)?,
            lon: r.get(4)?,
            speed: r.get::<_, Option<f64>>(5)?.unwrap_or(0.0),
            heading: r.get::<_, Option<f64>>(6)?.unwrap_or(0.0),
            accel_x: r.get(7)?,
            accel_y: r.get(8)?,
            accel_z: r.get(9)?,
            autopilot_state: autopilot_from_db_str(r.get::<_, Option<String>>(10)?.as_deref()),
            gear: gear_from_db_str(r.get::<_, Option<String>>(11)?.as_deref()),
            has_gps_fix: r.get::<_, i64>(12)? != 0,
        })
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// Map the persisted `autopilot` string back to a state. Unknown / NULL
/// values decode to [`AutopilotState::None`] (never an engaged state, so
/// event parity is preserved).
fn autopilot_from_db_str(s: Option<&str>) -> AutopilotState {
    match s {
        Some("SELF_DRIVING") => AutopilotState::SelfDriving,
        Some("AUTOSTEER") => AutopilotState::Autosteer,
        Some("TACC") => AutopilotState::Tacc,
        _ => AutopilotState::None,
    }
}

/// Map the persisted `gear` string back to a gear. Unknown / NULL decode
/// to [`Gear::Park`] (the proto3 default).
fn gear_from_db_str(s: Option<&str>) -> Gear {
    match s {
        Some("DRIVE") => Gear::Drive,
        Some("REVERSE") => Gear::Reverse,
        Some("NEUTRAL") => Gear::Neutral,
        _ => Gear::Park,
    }
}

/// Replace ALL derived trips/events with `derivation`. Deletes in
/// FK-safe order (events first — they only `SET NULL` on trip delete and
/// would otherwise survive), then reinserts. Caller supplies the
/// transaction.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn rebuild_derived(conn: &Connection, derivation: &Derivation) -> Result<(), DbError> {
    conn.execute("DELETE FROM events", [])?;
    conn.execute("DELETE FROM trip_points", [])?;
    conn.execute("DELETE FROM trips", [])?;

    for trip in &derivation.trips {
        let trip_id = insert_trip(conn, trip)?;
        insert_trip_points(conn, trip_id, trip)?;
        for event in &trip.events {
            insert_event(conn, Some(trip_id), event)?;
        }
    }
    for event in &derivation.sentry_events {
        insert_event(conn, None, event)?;
    }
    Ok(())
}

/// Insert one trip, returning its DB id.
fn insert_trip(conn: &Connection, trip: &DerivedTrip) -> Result<i64, DbError> {
    let now = now_epoch_s();
    let point_count = i64::try_from(trip.points.len()).unwrap_or(i64::MAX);
    conn.execute(
        "INSERT INTO trips
             (day, started_at, ended_at, bbox_min_lat, bbox_min_lon,
              bbox_max_lat, bbox_max_lon, distance_m, point_count, polyline,
              created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?11)",
        params![
            trip.day,
            trip.started_at,
            trip.ended_at,
            trip.bbox_min_lat,
            trip.bbox_min_lon,
            trip.bbox_max_lat,
            trip.bbox_max_lon,
            trip.distance_m,
            point_count,
            trip.polyline,
            now,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

/// Insert a trip's durable polyline points.
fn insert_trip_points(conn: &Connection, trip_id: i64, trip: &DerivedTrip) -> Result<(), DbError> {
    let mut stmt = conn.prepare(
        "INSERT INTO trip_points (trip_id, seq, t, lat, lon, speed, heading)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
    )?;
    for (seq, p) in trip.points.iter().enumerate() {
        let seq = i64::try_from(seq).unwrap_or(i64::MAX);
        stmt.execute(params![trip_id, seq, p.t, p.lat, p.lon, p.speed, p.heading])?;
    }
    Ok(())
}

/// Insert one derived event under an optional trip.
fn insert_event(
    conn: &Connection,
    trip_id: Option<i64>,
    event: &crate::model::DerivedEvent,
) -> Result<(), DbError> {
    let now = now_epoch_s();
    conn.execute(
        "INSERT INTO events
             (trip_id, clip_id, type, severity, t, lat, lon,
              front_frame_offset, front_frame_index, description, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        params![
            trip_id,
            event.clip_id,
            event.event_type.as_db_str(),
            event.severity().ordinal(),
            event.t,
            event.lat,
            event.lon,
            event.front_frame_offset_ms,
            event.front_frame_index,
            event.description,
            now,
        ],
    )?;
    Ok(())
}

/// Re-derive all trips/events from the cached waypoints and replace the
/// derived tables, atomically. The reusable rebuild entry point for the
/// scan pipeline and for later on-demand rebuilds (retentiond / webd).
///
/// # Errors
///
/// Returns [`DbError`] if loading, deriving, or writing fails.
pub fn rebuild_all_from_db(
    conn: &mut Connection,
    config: DeriveConfig,
) -> Result<Derivation, DbError> {
    let clips = load_derive_clips(conn)?;
    let clip_events = load_clip_events(conn)?;
    let derivation = derive(&clips, &clip_events, config);
    let tx = conn.transaction()?;
    rebuild_derived(&tx, &derivation)?;
    tx.commit()?;
    Ok(derivation)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::indexing_slicing,
        clippy::float_cmp,
        clippy::cast_precision_loss,
        clippy::cast_possible_truncation
    )]

    use std::collections::HashSet;

    use teslausb_core::sei::tesla::{AutopilotState, Gear};

    use super::{
        AngleFacts, ArchiveAngleRegistration, ArchiveRegistration, ArchiveUnitRegistration,
        ClipEventFacts, ClipFacts, load_clip_events, load_derive_clips, prune_missing_clips,
        rebuild_all_from_db, register_archived_clip, register_quarantined_clip,
        replace_clip_waypoints, upsert_angle_force_archive, upsert_angle_scan_preserving,
        upsert_clip, upsert_clip_event,
    };
    use crate::db::open_in_memory;
    use crate::derive::DeriveConfig;
    use crate::model::{DeriveWaypoint, FolderClass};

    fn clip_facts(key: &str, started: i64, class: FolderClass) -> ClipFacts {
        ClipFacts {
            canonical_key: key.to_owned(),
            started_at: started,
            ended_at: Some(started + 60),
            partition: "p1".to_owned(),
            folder_class: class,
            duration_s: Some(60.0),
        }
    }

    fn wp(frame: i64, offset_ms: f64, lat: f64, lon: f64, speed: f64) -> DeriveWaypoint {
        DeriveWaypoint {
            frame_index: frame,
            offset_ms,
            absolute_utc: Some(1_700_000_000 + (offset_ms / 1000.0) as i64),
            lat,
            lon,
            speed,
            heading: 90.0,
            accel_x: Some(0.0),
            accel_y: Some(0.0),
            accel_z: Some(0.0),
            autopilot_state: AutopilotState::None,
            gear: Gear::Drive,
            has_gps_fix: !(lat == 0.0 && lon == 0.0),
        }
    }

    fn archive_registration(key: &str, archive_path: &str, file_ref: &str) -> ArchiveRegistration {
        ArchiveRegistration {
            canonical_key: key.to_owned(),
            folder_class: FolderClass::RecentClips,
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60.0),
            archive: ArchiveUnitRegistration {
                path: archive_path.to_owned(),
                size_bytes: 4096,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngleRegistration {
                camera: "front".to_owned(),
                file_ref: file_ref.to_owned(),
                offset_ms: 0,
                duration_s: Some(60.0),
                size_bytes: 1024,
            }],
        }
    }

    #[test]
    fn upsert_clip_is_idempotent_by_canonical_key() {
        let conn = open_in_memory().unwrap();
        let id1 = upsert_clip(&conn, &clip_facts("k1", 1000, FolderClass::SavedClips)).unwrap();
        // Re-upsert with changed facts -> same row, updated fields.
        let mut f = clip_facts("k1", 2000, FolderClass::SavedClips);
        f.duration_s = Some(120.0);
        let id2 = upsert_clip(&conn, &f).unwrap();
        assert_eq!(id1, id2);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM clips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let started: i64 = conn
            .query_row("SELECT started_at FROM clips WHERE id = ?1", [id1], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(started, 2000);
    }

    #[test]
    fn upsert_angle_unique_per_camera() {
        let conn = open_in_memory().unwrap();
        let id = upsert_clip(&conn, &clip_facts("k1", 1000, FolderClass::SavedClips)).unwrap();
        let angle = AngleFacts {
            camera: "front".to_owned(),
            file_ref: "a.mp4".to_owned(),
            view_kind: "ro_usb".to_owned(),
            offset_ms: 0,
            duration_s: Some(60.0),
            size_bytes: Some(100),
        };
        upsert_angle_scan_preserving(&conn, id, &angle).unwrap();
        let mut a2 = angle.clone();
        a2.file_ref = "b.mp4".to_owned();
        upsert_angle_scan_preserving(&conn, id, &a2).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM angles", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let file_ref: String = conn
            .query_row("SELECT file_ref FROM angles", [], |r| r.get(0))
            .unwrap();
        assert_eq!(file_ref, "b.mp4");
    }

    #[test]
    fn replace_waypoints_overwrites() {
        let conn = open_in_memory().unwrap();
        let id = upsert_clip(&conn, &clip_facts("k1", 1000, FolderClass::SavedClips)).unwrap();
        replace_clip_waypoints(
            &conn,
            id,
            &[wp(0, 0.0, 1.0, 2.0, 5.0), wp(1, 33.0, 1.1, 2.1, 6.0)],
        )
        .unwrap();
        replace_clip_waypoints(&conn, id, &[wp(0, 0.0, 1.0, 2.0, 5.0)]).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM clip_waypoints WHERE clip_id = ?1",
                [id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn load_derive_clips_includes_canonical_key() {
        let conn = open_in_memory().unwrap();
        let key = "slot0:TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let id = upsert_clip(&conn, &clip_facts(key, 1000, FolderClass::SavedClips)).unwrap();
        replace_clip_waypoints(&conn, id, &[wp(0, 0.0, 1.0, 2.0, 5.0)]).unwrap();

        let clips = load_derive_clips(&conn).unwrap();
        assert_eq!(clips.len(), 1);
        assert_eq!(clips[0].canonical_key, key);
    }

    #[test]
    fn load_clip_events_resolves_primary_clip_and_trust_flag() {
        let conn = open_in_memory().unwrap();
        let trusted_key = "slot0:TeslaCam/SentryClips/2026-06-01_20-10-04/2026-06-01_20-10-04";
        let trusted_id = upsert_clip(
            &conn,
            &clip_facts(trusted_key, 1_700_000_000, FolderClass::SentryClips),
        )
        .unwrap();
        replace_clip_waypoints(&conn, trusted_id, &[wp(0, 0.0, 1.0, 2.0, 5.0)]).unwrap();
        upsert_clip_event(
            &conn,
            &ClipEventFacts {
                event_dir_key: "slot0:TeslaCam/SentryClips/2026-06-01_20-10-04".to_owned(),
                bucket: FolderClass::SentryClips.as_db_str().to_owned(),
                primary_canonical_key: trusted_key.to_owned(),
                timestamp_utc: 1_700_000_100,
                timestamp_local_naive: 1_700_010_900,
                timestamp_has_offset: true,
                est_lat: Some(42.1),
                est_lon: Some(-83.1),
                reason: Some("sentry".to_owned()),
                city: Some("Detroit".to_owned()),
                camera: Some("front".to_owned()),
            },
        )
        .unwrap();

        upsert_clip_event(
            &conn,
            &ClipEventFacts {
                event_dir_key: "slot0:TeslaCam/SavedClips/2026-06-02_00-00-00".to_owned(),
                bucket: FolderClass::SavedClips.as_db_str().to_owned(),
                primary_canonical_key:
                    "slot0:TeslaCam/SavedClips/2026-06-02_00-00-00/2026-06-02_00-00-00".to_owned(),
                timestamp_utc: 1_700_000_200,
                timestamp_local_naive: 1_700_010_200,
                timestamp_has_offset: false,
                est_lat: None,
                est_lon: None,
                reason: None,
                city: None,
                camera: None,
            },
        )
        .unwrap();

        let events = load_clip_events(&conn).unwrap();
        assert_eq!(events.len(), 2);
        let trusted = events
            .iter()
            .find(|e| e.event_dir_key == "slot0:TeslaCam/SentryClips/2026-06-01_20-10-04")
            .unwrap();
        assert_eq!(trusted.primary_clip_id, Some(trusted_id));
        assert_eq!(trusted.primary_started_utc, Some(1_700_000_000));
        assert!(trusted.primary_started_trusted);
        assert!(trusted.timestamp_has_offset);

        let unresolved = events
            .iter()
            .find(|e| e.event_dir_key == "slot0:TeslaCam/SavedClips/2026-06-02_00-00-00")
            .unwrap();
        assert_eq!(unresolved.primary_clip_id, None);
        assert_eq!(unresolved.primary_started_utc, None);
        assert!(!unresolved.primary_started_trusted);
        assert!(!unresolved.timestamp_has_offset);
    }

    #[test]
    fn register_archived_clip_writes_rows_and_returns_ids() {
        let mut conn = open_in_memory().unwrap();
        let registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-a",
            "archive/2026-06-19/clip-a",
            "archive/2026-06-19/clip-a/front.mp4",
        );
        let (clip_id, archive_item_id) = register_archived_clip(&mut conn, &registration).unwrap();
        assert!(clip_id > 0);
        assert!(archive_item_id > 0);

        let (durable, delete_state, linked_clip): (i64, String, i64) = conn
            .query_row(
                "SELECT durable, delete_state, clip_id FROM archive_items WHERE id = ?1",
                [archive_item_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!(durable, 0);
        assert_eq!(delete_state, "LIVE");
        assert_eq!(linked_clip, clip_id);

        let links: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM archive_item_clips WHERE archive_item_id = ?1 AND clip_id = ?2",
                [archive_item_id, clip_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(links, 1);

        let (view_kind, file_ref): (String, String) = conn
            .query_row(
                "SELECT view_kind, file_ref FROM angles WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(view_kind, "archive");
        assert_eq!(file_ref, "archive/2026-06-19/clip-a/front.mp4");
    }

    #[test]
    fn guard_a_scan_preserves_archive_and_force_archive_overrides() {
        let conn = open_in_memory().unwrap();
        let clip_id = upsert_clip(
            &conn,
            &clip_facts(
                "slot0:TeslaCam/RecentClips/2026-06-19/clip-guard-a",
                1000,
                FolderClass::RecentClips,
            ),
        )
        .unwrap();

        let archive = AngleFacts {
            camera: "front".to_owned(),
            file_ref: "archive/front.mp4".to_owned(),
            view_kind: "archive".to_owned(),
            offset_ms: 0,
            duration_s: Some(60.0),
            size_bytes: Some(200),
        };
        upsert_angle_force_archive(&conn, clip_id, &archive).unwrap();

        let scan = AngleFacts {
            camera: "front".to_owned(),
            file_ref: "TeslaCam/RecentClips/front.mp4".to_owned(),
            view_kind: "ro_usb".to_owned(),
            offset_ms: 500,
            duration_s: Some(59.0),
            size_bytes: Some(190),
        };
        upsert_angle_scan_preserving(&conn, clip_id, &scan).unwrap();
        let (view_kind, file_ref, offset_ms): (String, String, i64) = conn
            .query_row(
                "SELECT view_kind, file_ref, offset_ms FROM angles
                  WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!(view_kind, "archive");
        assert_eq!(file_ref, "archive/front.mp4");
        assert_eq!(offset_ms, 500);

        let forced = AngleFacts {
            camera: "front".to_owned(),
            file_ref: "archive/front-v2.mp4".to_owned(),
            view_kind: "archive".to_owned(),
            offset_ms: 750,
            duration_s: Some(58.0),
            size_bytes: Some(180),
        };
        upsert_angle_force_archive(&conn, clip_id, &forced).unwrap();
        let (view_kind2, file_ref2): (String, String) = conn
            .query_row(
                "SELECT view_kind, file_ref FROM angles
                  WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(view_kind2, "archive");
        assert_eq!(file_ref2, "archive/front-v2.mp4");
    }

    #[test]
    fn guard_b_prune_skips_archived_clip_and_prunes_unbacked_ro_usb() {
        let mut conn = open_in_memory().unwrap();
        let archived_registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-archived",
            "archive/2026-06-19/clip-archived",
            "archive/2026-06-19/clip-archived/front.mp4",
        );
        let (archived_clip_id, _) =
            register_archived_clip(&mut conn, &archived_registration).unwrap();

        let ro_usb_clip_id = upsert_clip(
            &conn,
            &clip_facts(
                "slot0:TeslaCam/RecentClips/2026-06-19/clip-ro-usb",
                2000,
                FolderClass::RecentClips,
            ),
        )
        .unwrap();
        upsert_angle_scan_preserving(
            &conn,
            ro_usb_clip_id,
            &AngleFacts {
                camera: "front".to_owned(),
                file_ref: "TeslaCam/RecentClips/clip-ro-usb/front.mp4".to_owned(),
                view_kind: "ro_usb".to_owned(),
                offset_ms: 0,
                duration_s: Some(60.0),
                size_bytes: Some(1000),
            },
        )
        .unwrap();

        let present: HashSet<String> = HashSet::new();
        let pruned = prune_missing_clips(&conn, &present).unwrap();
        assert_eq!(pruned, 1);

        let archived_exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM clips WHERE id = ?1",
                [archived_clip_id],
                |r| r.get(0),
            )
            .unwrap();
        let ro_usb_exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM clips WHERE id = ?1",
                [ro_usb_clip_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(archived_exists, 1);
        assert_eq!(ro_usb_exists, 0);
    }

    #[test]
    fn register_archived_clip_is_idempotent() {
        let mut conn = open_in_memory().unwrap();
        let registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-idempotent",
            "archive/2026-06-19/clip-idempotent",
            "archive/2026-06-19/clip-idempotent/front.mp4",
        );
        let first = register_archived_clip(&mut conn, &registration).unwrap();
        let second = register_archived_clip(&mut conn, &registration).unwrap();
        assert_eq!(first, second);

        for table in ["clips", "angles", "archive_items", "archive_item_clips"] {
            let count: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
                .unwrap();
            assert_eq!(count, 1, "expected one row in {table}");
        }
    }

    #[test]
    fn register_quarantined_clip_writes_quarantine_without_angle_promotion() {
        let mut conn = open_in_memory().unwrap();
        let registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-quarantine",
            "archive/2026-06-19/clip-quarantine",
            "archive/2026-06-19/clip-quarantine/front.mp4",
        );
        let clip_id = upsert_clip(
            &conn,
            &clip_facts(
                "slot0:TeslaCam/RecentClips/2026-06-19/clip-quarantine",
                1_718_805_600,
                FolderClass::RecentClips,
            ),
        )
        .unwrap();
        upsert_angle_scan_preserving(
            &conn,
            clip_id,
            &AngleFacts {
                camera: "front".to_owned(),
                file_ref: "TeslaCam/RecentClips/clip-quarantine/front.mp4".to_owned(),
                view_kind: "ro_usb".to_owned(),
                offset_ms: 0,
                duration_s: Some(60.0),
                size_bytes: Some(1024),
            },
        )
        .unwrap();

        let (got_clip_id, archive_item_id) =
            register_quarantined_clip(&mut conn, &registration).unwrap();
        assert_eq!(got_clip_id, clip_id);

        let (delete_state, durable): (String, i64) = conn
            .query_row(
                "SELECT delete_state, durable FROM archive_items WHERE id = ?1",
                [archive_item_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(delete_state, "QUARANTINED");
        assert_eq!(durable, 0);

        let links: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM archive_item_clips WHERE archive_item_id = ?1 AND clip_id = ?2",
                [archive_item_id, clip_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(links, 1);

        let (view_kind, file_ref): (String, String) = conn
            .query_row(
                "SELECT view_kind, file_ref FROM angles WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(view_kind, "ro_usb");
        assert_eq!(file_ref, "TeslaCam/RecentClips/clip-quarantine/front.mp4");
    }

    #[test]
    fn register_quarantined_clip_is_idempotent() {
        let mut conn = open_in_memory().unwrap();
        let registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-quarantine-idempotent",
            "archive/2026-06-19/clip-quarantine-idempotent",
            "archive/2026-06-19/clip-quarantine-idempotent/front.mp4",
        );
        let clip_id = upsert_clip(
            &conn,
            &clip_facts(
                "slot0:TeslaCam/RecentClips/2026-06-19/clip-quarantine-idempotent",
                1_718_805_600,
                FolderClass::RecentClips,
            ),
        )
        .unwrap();
        upsert_angle_scan_preserving(
            &conn,
            clip_id,
            &AngleFacts {
                camera: "front".to_owned(),
                file_ref: "TeslaCam/RecentClips/clip-quarantine-idempotent/front.mp4".to_owned(),
                view_kind: "ro_usb".to_owned(),
                offset_ms: 0,
                duration_s: Some(60.0),
                size_bytes: Some(1024),
            },
        )
        .unwrap();

        let first = register_quarantined_clip(&mut conn, &registration).unwrap();
        let second = register_quarantined_clip(&mut conn, &registration).unwrap();
        assert_eq!(first, second);

        let (delete_state, durable): (String, i64) = conn
            .query_row(
                "SELECT delete_state, durable FROM archive_items WHERE id = ?1",
                [first.1],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(delete_state, "QUARANTINED");
        assert_eq!(durable, 0);

        let (view_kind, file_ref): (String, String) = conn
            .query_row(
                "SELECT view_kind, file_ref FROM angles WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(view_kind, "ro_usb");
        assert_eq!(
            file_ref,
            "TeslaCam/RecentClips/clip-quarantine-idempotent/front.mp4"
        );

        let clips: i64 = conn
            .query_row("SELECT COUNT(*) FROM clips", [], |r| r.get(0))
            .unwrap();
        let angles: i64 = conn
            .query_row("SELECT COUNT(*) FROM angles", [], |r| r.get(0))
            .unwrap();
        let archive_items: i64 = conn
            .query_row("SELECT COUNT(*) FROM archive_items", [], |r| r.get(0))
            .unwrap();
        let links: i64 = conn
            .query_row("SELECT COUNT(*) FROM archive_item_clips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(clips, 1);
        assert_eq!(angles, 1);
        assert_eq!(archive_items, 1);
        assert_eq!(links, 1);
    }

    #[test]
    fn register_quarantined_clip_does_not_relabel_existing_live_row() {
        let mut conn = open_in_memory().unwrap();
        let registration = archive_registration(
            "slot0:TeslaCam/RecentClips/2026-06-19/clip-live-kept",
            "archive/2026-06-19/clip-live-kept",
            "archive/2026-06-19/clip-live-kept/front.mp4",
        );

        let (clip_id, archive_item_id) = register_archived_clip(&mut conn, &registration).unwrap();
        let (_, same_archive_item_id) =
            register_quarantined_clip(&mut conn, &registration).unwrap();
        assert_eq!(same_archive_item_id, archive_item_id);

        let delete_state: String = conn
            .query_row(
                "SELECT delete_state FROM archive_items WHERE id = ?1",
                [archive_item_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(delete_state, "LIVE");

        let (view_kind, file_ref): (String, String) = conn
            .query_row(
                "SELECT view_kind, file_ref FROM angles WHERE clip_id = ?1 AND camera = 'front'",
                [clip_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(view_kind, "archive");
        assert_eq!(file_ref, "archive/2026-06-19/clip-live-kept/front.mp4");
    }

    #[test]
    fn prune_preserves_clip_with_live_archive_item_backing() {
        let conn = open_in_memory().unwrap();
        let keep = upsert_clip(&conn, &clip_facts("keep", 1000, FolderClass::SavedClips)).unwrap();
        let gone = upsert_clip(&conn, &clip_facts("gone", 2000, FolderClass::SavedClips)).unwrap();
        // A live archive item referencing the clip that will vanish from
        // `present_keys`; Guard B keeps the clip.
        conn.execute(
            "INSERT INTO archive_items (folder_class, path, clip_id, archived_at, created_at, updated_at)
             VALUES ('SavedClips', '/arch/gone', ?1, 0, 0, 0)",
            [gone],
        )
        .unwrap();

        let mut present = HashSet::new();
        present.insert("keep".to_owned());
        let pruned = prune_missing_clips(&conn, &present).unwrap();
        assert_eq!(pruned, 0);

        let clips: i64 = conn
            .query_row("SELECT COUNT(*) FROM clips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(clips, 2);
        let _ = (keep, gone);
        // Archive item remains linked.
        let (arch_count, clip_id): (i64, Option<i64>) = conn
            .query_row(
                "SELECT COUNT(*), MAX(clip_id) FROM archive_items WHERE path = '/arch/gone'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(arch_count, 1);
        assert_eq!(clip_id, Some(gone));
    }

    #[test]
    fn rebuild_derives_a_moving_trip_and_is_replaceable() {
        let mut conn = open_in_memory().unwrap();
        let id = upsert_clip(
            &conn,
            &clip_facts("k1", 1_700_000_000, FolderClass::SavedClips),
        )
        .unwrap();
        // ~0.5 km of movement so the min-distance gate passes.
        let waypoints: Vec<DeriveWaypoint> = (0..6_i64)
            .map(|i| {
                let f = i as f64;
                wp(i, f * 1000.0, 1.0 + f * 0.001, 2.0, 10.0)
            })
            .collect();
        replace_clip_waypoints(&conn, id, &waypoints).unwrap();

        let clips = load_derive_clips(&conn).unwrap();
        assert_eq!(clips.len(), 1);
        assert_eq!(clips[0].clip_id, id);
        assert_eq!(clips[0].waypoints.len(), 6);

        let d1 = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        assert_eq!(d1.trips.len(), 1);
        let trips1: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips1, 1);
        let points1: i64 = conn
            .query_row("SELECT COUNT(*) FROM trip_points", [], |r| r.get(0))
            .unwrap();
        assert!(points1 >= 2);

        // Rebuild again -> no duplication (derived tables fully replaced).
        let _ = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        let trips2: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips2, 1);
    }

    #[test]
    fn cleared_waypoints_remove_prior_trip() {
        // Finding-2 regression: when a previously-indexed front clip is
        // re-walked but yields no telemetry, scan.rs clears its cached
        // waypoints. The next rebuild MUST NOT retain a phantom trip from
        // the stale cache.
        let mut conn = open_in_memory().unwrap();
        let id = upsert_clip(
            &conn,
            &clip_facts("k1", 1_700_000_000, FolderClass::SavedClips),
        )
        .unwrap();
        let waypoints: Vec<DeriveWaypoint> = (0..6_i64)
            .map(|i| {
                let f = i as f64;
                wp(i, f * 1000.0, 1.0 + f * 0.001, 2.0, 10.0)
            })
            .collect();
        replace_clip_waypoints(&conn, id, &waypoints).unwrap();
        let d1 = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        assert_eq!(d1.trips.len(), 1);

        // Re-walk yields nothing -> cache cleared.
        replace_clip_waypoints(&conn, id, &[]).unwrap();
        let d2 = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        assert_eq!(d2.trips.len(), 0);
        let trips: i64 = conn
            .query_row("SELECT COUNT(*) FROM trips", [], |r| r.get(0))
            .unwrap();
        assert_eq!(trips, 0);
        let points: i64 = conn
            .query_row("SELECT COUNT(*) FROM trip_points", [], |r| r.get(0))
            .unwrap();
        assert_eq!(points, 0);
    }

    #[test]
    fn prune_cascades_waypoints_and_derived_events_parity() {
        // Parity with `test_mapping_index_prune.py`
        // (`test_prune_deleted_clips_removes_clip_and_cascaded_rows`):
        // pruning a vanished clip removes the clip, its waypoints
        // (FK CASCADE), and — after the rebuild — its derived events.
        let mut conn = open_in_memory().unwrap();
        let id = upsert_clip(
            &conn,
            &clip_facts("gone", 1_700_000_000, FolderClass::SentryClips),
        )
        .unwrap();
        // A stationary sentry clip: one no-GPS-fix waypoint -> a sentry
        // event (sentry emits only when gps_waypoint_count == 0).
        replace_clip_waypoints(&conn, id, &[wp(0, 0.0, 0.0, 0.0, 0.0)]).unwrap();
        let d1 = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        assert_eq!(d1.sentry_events.len(), 1);
        let events1: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(events1, 1);

        // Clip vanished from the media -> not in the present set.
        let present: HashSet<String> = HashSet::new();
        let pruned = prune_missing_clips(&conn, &present).unwrap();
        assert_eq!(pruned, 1);

        // Clip + waypoints gone immediately (CASCADE).
        let clips: i64 = conn
            .query_row("SELECT COUNT(*) FROM clips", [], |r| r.get(0))
            .unwrap();
        let wps: i64 = conn
            .query_row("SELECT COUNT(*) FROM clip_waypoints", [], |r| r.get(0))
            .unwrap();
        assert_eq!(clips, 0);
        assert_eq!(wps, 0);

        // Derived events disappear on the next rebuild (no source clip).
        let d2 = rebuild_all_from_db(&mut conn, DeriveConfig::default()).unwrap();
        assert_eq!(d2.sentry_events.len(), 0);
        let events2: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(events2, 0);
    }
}
