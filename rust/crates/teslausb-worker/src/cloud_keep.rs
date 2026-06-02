//! "Keep clips until backed up to cloud" filter for the
//! tier-aware cleanup sweep.
//!
//! The web UI exposes a `keep_clips_until_synced` toggle. When
//! on, the SD-card cleanup sweep must NOT delete a clip whose
//! cloud upload is still in flight (status in the cloud archive
//! database is anything other than `synced` or `dead_letter`).
//!
//! State of truth lives in the Python-managed
//! `/var/lib/teslausb/cloud_sync.db`:
//!
//! * `cloud_archive_meta` — a KV table; key
//!   `cloud_archive.keep_clips_until_synced` carries the toggle
//!   value ("1"/"0" via `_write_kv_value`). Default is **on**
//!   when the row is absent — matches `CloudArchiveConfig`
//!   default in `settings.py`.
//! * `cloud_synced_files` — one row per cloud-side file with a
//!   `status` column. Statuses `pending`, `queued`, `uploading`,
//!   plus any other non-terminal value count as in-flight.
//!
//! The Rust worker opens this database in **read-only mode** so a
//! lock contention with the Python side cannot block the sweep
//! and a stray write from this module cannot corrupt cloud
//! archive accounting. The database is assumed to live under
//! `/var/lib/teslausb/cloud_sync.db`; the path is configurable
//! via `Config::cloud_archive_db_path` to keep tests
//! self-contained.
//!
//! If the database file is absent (operator has not configured
//! cloud sync yet) the filter is constructed in the disabled
//! state and the sweep proceeds exactly as it always has.

// Domain terms used throughout this module: "RecentClips",
// "SentryClips", "SavedClips", "TeslaTrackMode", "SQLite".
#![allow(clippy::doc_markdown)]

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};
use thiserror::Error;
use tracing::{debug, warn};

use crate::store::ClipRecord;

/// Canonical cloud-path roots. Mirrors `KNOWN_CLOUD_ROOTS` in
/// `web/teslausb_web/services/cloud_archive/paths.py`.
const KNOWN_CLOUD_ROOTS: &[&str] = &["RecentClips", "SentryClips", "SavedClips", "TeslaTrackMode"];

/// KV key under `cloud_archive_meta.key` carrying the
/// `keep_clips_until_synced` toggle. Mirrors
/// `KV_KEY_KEEP_CLIPS_UNTIL_SYNCED` in `settings.py`.
const KV_KEY_KEEP_CLIPS_UNTIL_SYNCED: &str = "cloud_archive.keep_clips_until_synced";

/// KV key the worker writes back to surface the most recent
/// sweep's kept-unsynced count for the web UI to display.
/// Stored as a stringified integer (matches the rest of the KV
/// values written by the Python side).
const KV_KEY_LAST_KEPT_UNSYNCED: &str = "cloud_archive.last_sweep_kept_unsynced";

/// Failure modes when loading the cloud-sync filter.
#[derive(Debug, Error)]
pub enum KeepFilterError {
    /// SQLite-level failure opening or querying the cloud db.
    #[error("cloud db {path:?}: {source}")]
    Sqlite {
        /// Database path we tried to open.
        path: PathBuf,
        /// Underlying rusqlite error.
        #[source]
        source: rusqlite::Error,
    },
}

/// Snapshot of which uploads are in flight, applied to every
/// candidate the cleanup sweep would otherwise delete.
#[derive(Debug, Clone, Default)]
pub struct KeepFilter {
    /// `true` when the operator has the
    /// `keep_clips_until_synced` toggle ON.
    pub enabled: bool,
    /// Canonical relative paths (e.g. `RecentClips/foo.mp4`,
    /// `SentryClips/2026-05-28_08-19-30/event.json`) whose
    /// cloud-side status is anything other than `synced` /
    /// `dead_letter` — i.e. the upload is still in flight.
    in_flight: HashSet<String>,
}

impl KeepFilter {
    /// Construct the always-disabled filter. Used as a safe
    /// fallback when the cloud db is absent / unreadable.
    #[must_use]
    pub fn disabled() -> Self {
        Self::default()
    }

    /// Load the filter from `cloud_db_path`. Returns the
    /// always-disabled filter (no error) when the file is
    /// missing — operators who have not configured cloud sync
    /// have no upload state to honor.
    ///
    /// `cloud_credentials_path` is the OAuth credentials file
    /// the web app writes when a cloud provider is connected.
    /// When that file is absent the filter is also forced
    /// disabled: with no provider, nothing will ever transition
    /// to `synced` and honoring the toggle would pin the LUN at
    /// 100% full forever.
    ///
    /// # Errors
    ///
    /// Returns `Err` only on a SQLite-level failure (corrupt
    /// db, schema mismatch). Missing-file is non-fatal.
    pub fn load(
        cloud_db_path: &Path,
        cloud_credentials_path: &Path,
    ) -> Result<Self, KeepFilterError> {
        if !cloud_db_path.exists() {
            debug!(
                path = %cloud_db_path.display(),
                "cloud_keep: cloud db absent; filter disabled",
            );
            return Ok(Self::disabled());
        }
        if !cloud_credentials_path.exists() {
            debug!(
                path = %cloud_credentials_path.display(),
                "cloud_keep: no cloud provider connected; filter disabled",
            );
            return Ok(Self::disabled());
        }
        let conn = Connection::open_with_flags(
            cloud_db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| KeepFilterError::Sqlite {
            path: cloud_db_path.to_path_buf(),
            source: e,
        })?;
        let enabled = read_keep_toggle(&conn).map_err(|e| KeepFilterError::Sqlite {
            path: cloud_db_path.to_path_buf(),
            source: e,
        })?;
        if !enabled {
            debug!("cloud_keep: keep_clips_until_synced is OFF; filter disabled");
            return Ok(Self {
                enabled: false,
                in_flight: HashSet::new(),
            });
        }
        let in_flight = read_in_flight_paths(&conn).map_err(|e| KeepFilterError::Sqlite {
            path: cloud_db_path.to_path_buf(),
            source: e,
        })?;
        debug!(
            count = in_flight.len(),
            "cloud_keep: loaded in-flight upload set",
        );
        Ok(Self {
            enabled: true,
            in_flight,
        })
    }

    /// `true` when the sweep must skip this clip because its
    /// upload is still in flight.
    ///
    /// Clips without any waypoint metadata (no SEI, no GPS)
    /// are never kept: they carry no operator-meaningful
    /// telemetry and are not worth pinning storage for, even
    /// mid-upload.
    #[must_use]
    pub fn should_keep(&self, clip: &ClipRecord) -> bool {
        if !self.enabled {
            return false;
        }
        if clip.waypoint_count == 0 {
            return false;
        }
        let Some(canonical) = canonical_cloud_path(&clip.relative_path) else {
            return false;
        };
        self.in_flight.contains(&canonical)
    }

    /// Number of in-flight uploads currently shielding clips.
    /// Used for diagnostics / UI surface, not for control flow.
    #[must_use]
    pub fn in_flight_len(&self) -> usize {
        self.in_flight.len()
    }
}

/// Best-effort write-back of the most recent kept-unsynced
/// count so the web UI can surface "X clips kept pending
/// upload" without opening the same db twice.
///
/// Opens the db in read-write mode for the single UPSERT.
/// Failures are logged at WARN — they MUST NOT abort the sweep.
pub fn record_last_kept_count(cloud_db_path: &Path, count: u32) {
    if !cloud_db_path.exists() {
        return;
    }
    let conn = match Connection::open(cloud_db_path) {
        Ok(c) => c,
        Err(e) => {
            warn!(
                path = %cloud_db_path.display(),
                error = %e,
                "cloud_keep: could not open cloud db to record kept count",
            );
            return;
        }
    };
    if let Err(e) = conn.execute(
        "INSERT INTO cloud_archive_meta (key, value) VALUES (?1, ?2) \
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        rusqlite::params![KV_KEY_LAST_KEPT_UNSYNCED, count.to_string()],
    ) {
        warn!(
            path = %cloud_db_path.display(),
            error = %e,
            "cloud_keep: failed to record kept_unsynced count",
        );
    }
}

fn read_keep_toggle(conn: &Connection) -> Result<bool, rusqlite::Error> {
    let mut stmt = match conn.prepare("SELECT value FROM cloud_archive_meta WHERE key = ?1") {
        Ok(s) => s,
        // Treat "no such table" as the freshly-installed case
        // — the Python migrations have not run yet. Default ON
        // matches CloudArchiveConfig.keep_clips_until_synced.
        Err(e) if is_no_such_table(&e) => return Ok(true),
        Err(e) => return Err(e),
    };
    let value: Option<String> = stmt
        .query_row(rusqlite::params![KV_KEY_KEEP_CLIPS_UNTIL_SYNCED], |row| {
            row.get::<_, Option<String>>(0)
        })
        .optional_or(None)?;
    Ok(match value.as_deref() {
        // The Python side persists booleans as the literal
        // strings "1" / "0" (see `_coerce_bool` /
        // `_write_kv_value`). Anything that does not look
        // explicitly OFF is treated as ON to match the
        // CloudArchiveConfig default.
        Some("0" | "false" | "False" | "no" | "off") => false,
        _ => true,
    })
}

fn read_in_flight_paths(conn: &Connection) -> Result<HashSet<String>, rusqlite::Error> {
    let mut stmt = match conn.prepare(
        "SELECT file_path FROM cloud_synced_files \
         WHERE status NOT IN ('synced', 'dead_letter')",
    ) {
        Ok(s) => s,
        Err(e) if is_no_such_table(&e) => return Ok(HashSet::new()),
        Err(e) => return Err(e),
    };
    let mut out = HashSet::new();
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let raw: String = row.get(0)?;
        if let Some(canonical) = canonical_str(&raw) {
            out.insert(canonical);
        }
    }
    Ok(out)
}

fn is_no_such_table(err: &rusqlite::Error) -> bool {
    matches!(err, rusqlite::Error::SqliteFailure(_, Some(msg)) if msg.contains("no such table"))
}

/// Map a worker-side `relative_path` (relative to
/// `backing_root`, so typically `TeslaCam/RecentClips/...`)
/// onto the canonical cloud form used by `cloud_synced_files`
/// (e.g. `RecentClips/...`). Mirrors `canonical_cloud_path` in
/// `web/teslausb_web/services/cloud_archive/paths.py`.
#[must_use]
pub fn canonical_cloud_path(relative: &Path) -> Option<String> {
    let s = relative.to_string_lossy();
    canonical_str(&s)
}

fn canonical_str(raw: &str) -> Option<String> {
    if raw.is_empty() {
        return None;
    }
    let normalized = raw.replace('\\', "/");
    // Reject traversal so a corrupted row cannot smuggle
    // `..` into the comparison set.
    if normalized.split('/').any(|seg| seg == "..") {
        return None;
    }
    for root in KNOWN_CLOUD_ROOTS {
        let marker = format!("/{root}/");
        if let Some(idx) = normalized.find(&marker) {
            return Some(normalized[idx + 1..].trim_end_matches('/').to_string());
        }
        if normalized == *root || normalized.starts_with(&format!("{root}/")) {
            return Some(normalized.trim_end_matches('/').to_string());
        }
    }
    None
}

// Tiny rusqlite ergonomics shim: `OptionalExt` would let us
// write `.optional()?`, but the version in tree exports it
// under `rusqlite::OptionalExtension`. Reach for it via a
// local helper trait so the call site reads cleanly even when
// the row decoder returns `Option<String>` itself.
trait OptionalOr<T> {
    fn optional_or(self, default: T) -> Result<T, rusqlite::Error>;
}

impl<T> OptionalOr<T> for Result<T, rusqlite::Error> {
    fn optional_or(self, default: T) -> Result<T, rusqlite::Error> {
        match self {
            Ok(v) => Ok(v),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(default),
            Err(e) => Err(e),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::*;
    use crate::store::Bucket;

    fn mk_clip(relative_path: &str, waypoint_count: u32) -> ClipRecord {
        ClipRecord {
            id: 1,
            relative_path: PathBuf::from(relative_path),
            bucket: Bucket::Recent,
            clip_started_utc: None,
            indexed_at_utc: 0,
            waypoint_count,
            gps_waypoint_count: 0,
        }
    }

    fn open_seeded_db(toggle: Option<&str>, in_flight: &[(&str, &str)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cloud_sync.db");
        // Touch a placeholder credentials file so the loader does
        // not short-circuit to disabled. Tests that want the
        // disabled-without-provider behavior should call
        // `load(...)` with a missing path explicitly.
        let creds = dir.path().join("cloud_oauth_credentials.json");
        std::fs::write(&creds, "{}").unwrap();
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE cloud_archive_meta (key TEXT PRIMARY KEY, value TEXT); \
             CREATE TABLE cloud_synced_files (file_path TEXT PRIMARY KEY, status TEXT);",
        )
        .unwrap();
        if let Some(v) = toggle {
            conn.execute(
                "INSERT INTO cloud_archive_meta (key, value) VALUES (?1, ?2)",
                rusqlite::params![KV_KEY_KEEP_CLIPS_UNTIL_SYNCED, v],
            )
            .unwrap();
        }
        for (p, s) in in_flight {
            conn.execute(
                "INSERT INTO cloud_synced_files (file_path, status) VALUES (?1, ?2)",
                rusqlite::params![p, s],
            )
            .unwrap();
        }
        dir
    }

    fn creds_path(dir: &tempfile::TempDir) -> PathBuf {
        dir.path().join("cloud_oauth_credentials.json")
    }

    #[test]
    fn load_returns_disabled_when_db_missing() {
        let f = KeepFilter::load(
            Path::new("/nonexistent/cloud_sync.db"),
            Path::new("/nonexistent/creds.json"),
        )
        .unwrap();
        assert!(!f.enabled);
        assert_eq!(f.in_flight_len(), 0);
        assert!(!f.should_keep(&mk_clip("TeslaCam/RecentClips/x.mp4", 5)));
    }

    #[test]
    fn load_returns_disabled_when_credentials_missing() {
        let dir = open_seeded_db(Some("1"), &[("RecentClips/x.mp4", "pending")]);
        // Delete the placeholder credentials file the helper drops in.
        std::fs::remove_file(creds_path(&dir)).unwrap();
        let f = KeepFilter::load(&dir.path().join("cloud_sync.db"), &creds_path(&dir)).unwrap();
        assert!(!f.enabled);
        assert!(!f.should_keep(&mk_clip("TeslaCam/RecentClips/x.mp4", 5)));
    }

    #[test]
    fn load_defaults_enabled_when_toggle_absent() {
        let dir = open_seeded_db(None, &[("RecentClips/x.mp4", "pending")]);
        let f = KeepFilter::load(&dir.path().join("cloud_sync.db"), &creds_path(&dir)).unwrap();
        assert!(f.enabled);
        assert_eq!(f.in_flight_len(), 1);
    }

    #[test]
    fn load_respects_off_toggle() {
        let dir = open_seeded_db(Some("0"), &[("RecentClips/x.mp4", "pending")]);
        let f = KeepFilter::load(&dir.path().join("cloud_sync.db"), &creds_path(&dir)).unwrap();
        assert!(!f.enabled);
        assert!(!f.should_keep(&mk_clip("TeslaCam/RecentClips/x.mp4", 5)));
    }

    #[test]
    fn should_keep_matches_via_canonical_form() {
        let dir = open_seeded_db(
            Some("1"),
            &[
                ("RecentClips/in_flight.mp4", "pending"),
                ("SentryClips/2026-05-28_event/front.mp4", "uploading"),
                ("SavedClips/synced.mp4", "synced"), // filtered out
            ],
        );
        let f = KeepFilter::load(&dir.path().join("cloud_sync.db"), &creds_path(&dir)).unwrap();
        assert!(f.enabled);
        assert_eq!(f.in_flight_len(), 2);
        // Worker rows include the TeslaCam/ prefix.
        assert!(f.should_keep(&mk_clip("TeslaCam/RecentClips/in_flight.mp4", 3)));
        assert!(f.should_keep(&mk_clip(
            "TeslaCam/SentryClips/2026-05-28_event/front.mp4",
            7,
        )));
        assert!(!f.should_keep(&mk_clip("TeslaCam/SavedClips/synced.mp4", 9)));
        assert!(!f.should_keep(&mk_clip("TeslaCam/RecentClips/never_uploaded.mp4", 1)));
    }

    #[test]
    fn should_keep_skips_clip_without_waypoints() {
        // An in-flight clip with no SEI/GPS waypoints is not
        // worth pinning storage for, even mid-upload — the
        // operator only cares about clips with telemetry.
        let dir = open_seeded_db(Some("1"), &[("RecentClips/in_flight.mp4", "pending")]);
        let f = KeepFilter::load(&dir.path().join("cloud_sync.db"), &creds_path(&dir)).unwrap();
        assert!(f.enabled);
        assert!(!f.should_keep(&mk_clip("TeslaCam/RecentClips/in_flight.mp4", 0)));
        // Same path but with waypoints is kept.
        assert!(f.should_keep(&mk_clip("TeslaCam/RecentClips/in_flight.mp4", 1)));
    }

    #[test]
    fn canonical_strips_backing_prefix() {
        assert_eq!(
            canonical_cloud_path(Path::new("TeslaCam/RecentClips/x.mp4")).as_deref(),
            Some("RecentClips/x.mp4"),
        );
        assert_eq!(
            canonical_cloud_path(Path::new("deeply/nested/RecentClips/2026-05-28/y.mp4"))
                .as_deref(),
            Some("RecentClips/2026-05-28/y.mp4"),
        );
        assert_eq!(
            canonical_cloud_path(Path::new("RecentClips/z.mp4")).as_deref(),
            Some("RecentClips/z.mp4"),
        );
    }

    #[test]
    fn canonical_rejects_unknown_root() {
        assert!(canonical_cloud_path(Path::new("Foo/Bar/x.mp4")).is_none());
    }

    #[test]
    fn canonical_rejects_traversal() {
        assert!(canonical_str("RecentClips/../etc/passwd").is_none());
    }

    #[test]
    fn record_last_kept_count_upserts_kv() {
        let dir = open_seeded_db(Some("1"), &[]);
        let path = dir.path().join("cloud_sync.db");
        record_last_kept_count(&path, 7);
        record_last_kept_count(&path, 11);
        let conn = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .unwrap();
        let v: String = conn
            .query_row(
                "SELECT value FROM cloud_archive_meta WHERE key = ?1",
                rusqlite::params![KV_KEY_LAST_KEPT_UNSYNCED],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(v, "11");
    }
}
