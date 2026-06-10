//! The `SQLite` store: open + pragmas, the forward-only migrator, and (in
//! sibling modules) the indexd-mediated mutation entry points.
//!
//! `indexd` is the **sole `SQLite` writer** (contract D1 §1, D3 §2): all
//! schema, ingest, lease, delete-state, durability and WAL-checkpoint
//! mutations funnel through here. Readers (webd / retentiond / uploadd)
//! never write directly.

pub mod ingest;
pub mod migrations;
pub mod mutations;

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::Connection;

use self::migrations::{LATEST_VERSION, MIGRATIONS};

/// Errors from opening, migrating, or mutating the index database.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    /// An underlying `rusqlite` / `SQLite` error.
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    /// The database reports a schema version newer than this binary can
    /// produce. Migrations are forward-only; we refuse to open it
    /// read-write rather than risk corrupting a newer schema.
    #[error(
        "database schema v{db_version} is newer than this binary (v{binary_version}); refusing to open"
    )]
    SchemaTooNew {
        /// Version found in the database.
        db_version: i64,
        /// Highest version this binary knows ([`LATEST_VERSION`]).
        binary_version: i64,
    },
}

/// Current wall-clock time as UTC epoch seconds, clamped at 0 for the
/// degenerate pre-epoch case. Used only for bookkeeping columns
/// (`*.created_at`, `schema_version.applied_at`); recording instants come
/// from the media, not this clock.
#[must_use]
pub fn now_epoch_s() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(i64::MAX))
}

/// Open the index database at `path`, apply pragmas (WAL,
/// `synchronous=NORMAL`, `foreign_keys=ON`) and run any pending
/// migrations. The path must be on the Pi-side ext4 filesystem — NEVER
/// inside `disk.img` / the Tesla volume (D1 §1).
///
/// # Errors
///
/// Returns [`DbError`] if the connection cannot be opened, a pragma
/// fails, the schema is newer than this binary, or a migration fails.
pub fn open<P: AsRef<Path>>(path: P) -> Result<Connection, DbError> {
    let mut conn = Connection::open(path)?;
    apply_pragmas(&conn)?;
    apply_migrations(&mut conn)?;
    Ok(conn)
}

/// Open an in-memory database with pragmas + migrations applied. For
/// host tests; WAL silently degrades to `memory` journal mode.
///
/// # Errors
///
/// Returns [`DbError`] on pragma or migration failure.
pub fn open_in_memory() -> Result<Connection, DbError> {
    let mut conn = Connection::open_in_memory()?;
    apply_pragmas(&conn)?;
    apply_migrations(&mut conn)?;
    Ok(conn)
}

/// Apply the connection-scoped pragmas. WAL + `journal_mode` cannot be
/// set inside a transaction, so this runs before [`apply_migrations`].
fn apply_pragmas(conn: &Connection) -> Result<(), DbError> {
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA foreign_keys=ON;",
    )?;
    Ok(())
}

/// The DB's current schema version (`MAX(schema_version.version)`), or `0`
/// if the `schema_version` table does not exist yet (fresh database).
///
/// # Errors
///
/// Returns [`DbError`] if the catalog query fails.
pub fn current_version(conn: &Connection) -> Result<i64, DbError> {
    let table_present: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'",
            [],
            |_| Ok(true),
        )
        .or_else(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(other),
        })?;
    if !table_present {
        return Ok(0);
    }
    let version: i64 = conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version",
        [],
        |r| r.get(0),
    )?;
    Ok(version)
}

/// Run every migration whose version exceeds the DB's current version,
/// in one transaction. Idempotent: a fully-migrated DB applies nothing.
/// Returns the version after migrating.
///
/// # Errors
///
/// Returns [`DbError::SchemaTooNew`] if the DB is ahead of this binary,
/// or [`DbError::Sqlite`] if a migration statement fails (the whole
/// transaction is then rolled back).
pub fn apply_migrations(conn: &mut Connection) -> Result<i64, DbError> {
    let current = current_version(conn)?;
    if current > LATEST_VERSION {
        return Err(DbError::SchemaTooNew {
            db_version: current,
            binary_version: LATEST_VERSION,
        });
    }
    if current == LATEST_VERSION {
        return Ok(current);
    }
    let applied_at = now_epoch_s();
    let tx = conn.transaction()?;
    for migration in MIGRATIONS.iter().filter(|m| m.version > current) {
        tx.execute_batch(migration.sql)?;
        tx.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (?1, ?2, ?3)",
            rusqlite::params![migration.version, applied_at, migration.note],
        )?;
    }
    tx.commit()?;
    Ok(LATEST_VERSION)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::{DbError, LATEST_VERSION, apply_migrations, current_version, open_in_memory};
    use rusqlite::Connection;

    #[test]
    fn fresh_open_is_latest_version() {
        let conn = open_in_memory().unwrap();
        assert_eq!(current_version(&conn).unwrap(), LATEST_VERSION);
    }

    #[test]
    fn foreign_keys_enabled() {
        let conn = open_in_memory().unwrap();
        let fk: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .unwrap();
        assert_eq!(fk, 1);
    }

    #[test]
    fn migration_is_idempotent() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);
        // Re-applying must be a no-op (no duplicate rows / no error).
        assert_eq!(apply_migrations(&mut conn).unwrap(), LATEST_VERSION);
        // One row per migration applied from fresh (v1 + v2).
        let rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(rows, LATEST_VERSION);
    }

    #[test]
    fn schema_newer_than_binary_is_rejected() {
        let mut conn = open_in_memory().unwrap();
        conn.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (?1, 0, 'future')",
            rusqlite::params![LATEST_VERSION + 1],
        )
        .unwrap();
        match apply_migrations(&mut conn) {
            Err(DbError::SchemaTooNew {
                db_version,
                binary_version,
            }) => {
                assert_eq!(db_version, LATEST_VERSION + 1);
                assert_eq!(binary_version, LATEST_VERSION);
            }
            other => panic!("expected SchemaTooNew, got {other:?}"),
        }
    }

    #[test]
    fn all_expected_tables_exist() {
        let conn = open_in_memory().unwrap();
        for table in [
            "schema_version",
            "clips",
            "angles",
            "clip_waypoints",
            "trips",
            "trip_points",
            "events",
            "archive_items",
            "archive_item_clips",
            "eviction_tombstones",
            "leases",
            "prefs",
            "media_entries",
        ] {
            let found: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
                    rusqlite::params![table],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(found, 1, "missing table {table}");
        }
    }
}
