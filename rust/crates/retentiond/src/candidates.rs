//! Read-only archive-candidate inventory from the `indexd` `SQLite` catalog.
//!
//! This is the Phase-1 inventory seam from ADR-0004: no mounted `RecentClips`
//! listing, no exFAT parsing in `retentiond`.

use std::io;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, params};

const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

/// One camera angle eligible for archive copy via the `ReadFile` seam.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateAngle {
    /// Camera token (`front`, `back`, ...).
    pub camera: String,
    /// Volume-root-relative source path (`angles.file_ref`).
    pub file_ref: String,
    /// Milliseconds from clip start.
    pub offset_ms: i64,
    /// Angle duration in seconds when known.
    pub duration_s: Option<i64>,
    /// Source bytes according to indexd.
    pub size_bytes: u64,
}

/// One `RecentClips` clip selected for archiving.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    /// `clips.id`.
    pub clip_id: i64,
    /// Canonical clip key.
    pub canonical_key: String,
    /// Partition label (`slot0`, ...).
    pub partition: String,
    /// Clip start epoch seconds.
    pub started_at: i64,
    /// Clip end epoch seconds.
    pub ended_at: i64,
    /// Clip duration in seconds when known.
    pub duration_s: Option<i64>,
    /// Source exFAT volume serial from the boot sector.
    pub source_volume_serial: u32,
    /// Stable source fingerprint for archive-local dedup marker matching.
    pub source_fingerprint: String,
    /// Live `ro_usb` angles to copy.
    pub angles: Vec<CandidateAngle>,
}

/// Candidate inventory seam consumed by `archive_recent_once`.
pub trait CandidateSource {
    /// List clips that should be archived in this cycle.
    ///
    /// # Errors
    ///
    /// Returns an error when the source inventory cannot be queried.
    fn list_candidates(&self) -> io::Result<Vec<Candidate>>;
}

#[derive(Debug, thiserror::Error)]
enum CandidateError {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

/// Read-only SQLite-backed candidate source.
#[derive(Debug, Clone)]
pub struct SqliteCandidateReader {
    path: Arc<PathBuf>,
}

impl SqliteCandidateReader {
    /// Create a reader over the `indexd` catalog path.
    ///
    /// Performs a startup probe with the same read-only connect pattern used by
    /// `webd`: `READ_ONLY | NO_MUTEX`, `busy_timeout`, and `PRAGMA query_only`.
    ///
    /// # Errors
    ///
    /// Returns an error when the database cannot be opened read-only.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, io::Error> {
        let reader = Self {
            path: Arc::new(path.into()),
        };
        reader.connect().map_err(map_sqlite_error)?;
        Ok(reader)
    }

    fn connect(&self) -> Result<Connection, rusqlite::Error> {
        let conn = Connection::open_with_flags(
            self.path.as_ref(),
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
        conn.pragma_update(None, "query_only", true)?;
        Ok(conn)
    }

    fn load_candidates(&self) -> Result<Vec<Candidate>, CandidateError> {
        let conn = self.connect()?;
        let mut clip_stmt = conn.prepare(
            "SELECT c.id, c.canonical_key, c.partition,
                    c.started_at,
                    COALESCE(c.ended_at, c.started_at) AS ended_at,
                    c.duration_s
             FROM clips c
             WHERE c.folder_class = 'RecentClips'
               AND c.availability = 'present'
               AND EXISTS (SELECT 1 FROM angles a
                           WHERE a.clip_id = c.id AND a.view_kind = 'ro_usb')
               AND NOT EXISTS (SELECT 1 FROM angles a
                               WHERE a.clip_id = c.id AND a.view_kind = 'archive')
               AND NOT EXISTS (SELECT 1 FROM archive_items ai
                               WHERE ai.clip_id = c.id AND ai.delete_state <> 'DELETED')
               AND NOT EXISTS (SELECT 1 FROM archive_item_clips aic
                               JOIN archive_items ai ON ai.id = aic.archive_item_id
                               WHERE aic.clip_id = c.id AND ai.delete_state <> 'DELETED')
             ORDER BY c.started_at ASC",
        )?;
        let mut angle_stmt = conn.prepare(
            "SELECT camera, file_ref, offset_ms, duration_s, COALESCE(size_bytes, 0)
             FROM angles
             WHERE clip_id = ?1 AND view_kind = 'ro_usb'
             ORDER BY camera ASC, file_ref ASC",
        )?;

        let mut out = Vec::new();
        let clip_rows = clip_stmt.query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, Option<f64>>(5)?,
            ))
        })?;

        for clip_row in clip_rows {
            let (clip_id, canonical_key, partition, started_at, ended_at, duration_raw) = clip_row?;
            let mut angles = Vec::new();
            let angle_rows = angle_stmt.query_map(params![clip_id], |row| {
                Ok(CandidateAngle {
                    camera: row.get(0)?,
                    file_ref: row.get(1)?,
                    offset_ms: row.get(2)?,
                    duration_s: duration_from_real(row.get::<_, Option<f64>>(3)?),
                    size_bytes: i64_to_u64_saturating(row.get::<_, i64>(4)?),
                })
            })?;
            for angle_row in angle_rows {
                angles.push(angle_row?);
            }
            if angles.is_empty() {
                continue;
            }
            out.push(Candidate {
                clip_id,
                canonical_key,
                partition,
                started_at,
                ended_at,
                duration_s: duration_from_real(duration_raw),
                source_volume_serial: 0,
                source_fingerprint: format!("sqlite:{clip_id}"),
                angles,
            });
        }
        Ok(out)
    }
}

impl CandidateSource for SqliteCandidateReader {
    fn list_candidates(&self) -> io::Result<Vec<Candidate>> {
        self.load_candidates()
            .map_err(|err| io::Error::other(err.to_string()))
    }
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn duration_from_real(raw: Option<f64>) -> Option<i64> {
    let value = raw?;
    if !value.is_finite() {
        return None;
    }
    if value >= i64::MAX as f64 {
        return Some(i64::MAX);
    }
    if value <= i64::MIN as f64 {
        return Some(i64::MIN);
    }
    Some(value.round() as i64)
}

fn i64_to_u64_saturating(value: i64) -> u64 {
    if value <= 0 {
        0
    } else {
        u64::try_from(value).unwrap_or(u64::MAX)
    }
}

fn map_sqlite_error(err: rusqlite::Error) -> io::Error {
    io::Error::other(CandidateError::Sqlite(err).to_string())
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines,
    clippy::indexing_slicing
)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use indexd::db;
    use rusqlite::params;

    use super::{CandidateSource, SqliteCandidateReader};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_db_path() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "retentiond-candidates-{}-{unique}.sqlite3",
            std::process::id()
        ))
    }

    fn seed_clip(
        conn: &rusqlite::Connection,
        id: i64,
        key: &str,
        folder_class: &str,
        availability: &str,
    ) {
        conn.execute(
            "INSERT INTO clips (id, canonical_key, started_at, ended_at, partition, folder_class, is_sentry, duration_s, availability, created_at, updated_at)
             VALUES (?1, ?2, 1000, 1010, 'slot0', ?3, 0, 10.0, ?4, 0, 0)",
            params![id, key, folder_class, availability],
        )
        .expect("insert clip");
    }

    fn seed_angle(
        conn: &rusqlite::Connection,
        id: i64,
        clip_id: i64,
        camera: &str,
        file_ref: &str,
        view_kind: &str,
    ) {
        conn.execute(
            "INSERT INTO angles (id, clip_id, camera, file_ref, view_kind, offset_ms, duration_s, size_bytes)
             VALUES (?1, ?2, ?3, ?4, ?5, 0, 10.0, 100)",
            params![id, clip_id, camera, file_ref, view_kind],
        )
        .expect("insert angle");
    }

    #[test]
    fn sqlite_candidates_select_only_unarchived_recentclips_with_ro_usb_angles() {
        let path = new_db_path();
        {
            let conn = db::open(&path).expect("open fixture db");
            seed_clip(
                &conn,
                1,
                "0:TeslaCam/RecentClips/2026-06-19_10-00-00",
                "RecentClips",
                "present",
            );
            seed_angle(
                &conn,
                10,
                1,
                "front",
                "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
                "ro_usb",
            );
            seed_angle(
                &conn,
                11,
                1,
                "back",
                "TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4",
                "ro_usb",
            );

            seed_clip(
                &conn,
                2,
                "0:TeslaCam/RecentClips/2026-06-19_10-01-00",
                "RecentClips",
                "present",
            );
            seed_angle(
                &conn,
                20,
                2,
                "front",
                "TeslaCam/RecentClips/2026-06-19_10-01-00-front.mp4",
                "ro_usb",
            );
            seed_angle(
                &conn,
                21,
                2,
                "back",
                "archive/RecentClips/2026-06-19_10-01-00-front.mp4",
                "archive",
            );

            seed_clip(
                &conn,
                3,
                "0:TeslaCam/RecentClips/2026-06-19_10-02-00",
                "RecentClips",
                "present",
            );
            seed_angle(
                &conn,
                30,
                3,
                "front",
                "TeslaCam/RecentClips/2026-06-19_10-02-00-front.mp4",
                "ro_usb",
            );
            conn.execute(
                "INSERT INTO archive_items (id, folder_class, path, clip_id, size_bytes, file_count, archived_at, delete_state, created_at, updated_at)
                 VALUES (300, 'RecentClips', 'RecentClips/2026-06-19/2026-06-19_10-02-00', 3, 100, 1, 0, 'LIVE', 0, 0)",
                [],
            )
            .expect("insert archive item");

            seed_clip(
                &conn,
                4,
                "0:TeslaCam/RecentClips/2026-06-19_10-03-00",
                "RecentClips",
                "present",
            );
            seed_angle(
                &conn,
                40,
                4,
                "front",
                "TeslaCam/RecentClips/2026-06-19_10-03-00-front.mp4",
                "ro_usb",
            );
            conn.execute(
                "INSERT INTO archive_items (id, folder_class, path, clip_id, size_bytes, file_count, archived_at, delete_state, created_at, updated_at)
                 VALUES (400, 'RecentClips', 'RecentClips/2026-06-19/2026-06-19_10-03-00', NULL, 100, 1, 0, 'LIVE', 0, 0)",
                [],
            )
            .expect("insert archive item for join");
            conn.execute(
                "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (400, 4)",
                [],
            )
            .expect("insert archive_item_clips row");

            seed_clip(
                &conn,
                5,
                "0:TeslaCam/SentryClips/2026-06-19_10-04-00",
                "SentryClips",
                "present",
            );
            seed_angle(
                &conn,
                50,
                5,
                "front",
                "TeslaCam/SentryClips/2026-06-19_10-04-00-front.mp4",
                "ro_usb",
            );

            seed_clip(
                &conn,
                6,
                "0:TeslaCam/RecentClips/2026-06-19_10-05-00",
                "RecentClips",
                "missing",
            );
            seed_angle(
                &conn,
                60,
                6,
                "front",
                "TeslaCam/RecentClips/2026-06-19_10-05-00-front.mp4",
                "ro_usb",
            );
        }

        let reader = SqliteCandidateReader::open(&path).expect("open read-only candidates");
        let got = reader.list_candidates().expect("query candidates");
        assert_eq!(got.len(), 1);
        let clip = &got[0];
        assert_eq!(clip.clip_id, 1);
        assert_eq!(clip.canonical_key, "0:TeslaCam/RecentClips/2026-06-19_10-00-00");
        assert_eq!(clip.partition, "slot0");
        assert_eq!(clip.started_at, 1000);
        assert_eq!(clip.ended_at, 1010);
        assert_eq!(clip.duration_s, Some(10));
        assert_eq!(clip.angles.len(), 2);
        assert_eq!(clip.angles[0].camera, "back");
        assert_eq!(
            clip.angles[0].file_ref,
            "TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4"
        );
        assert_eq!(clip.angles[1].camera, "front");

        let _ = fs::remove_file(path);
    }
}
