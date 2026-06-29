//! The read-only catalog handle.
//!
//! `webd` opens the `indexd` `SQLite` catalog with `SQLITE_OPEN_READ_ONLY`
//! (`indexd` is the sole writer, D1 §1). A [`Catalog`] is a cheap, clonable
//! handle around the database path; each request opens its own short-lived
//! read-only connection inside a blocking task, which sidesteps `Connection`'s
//! `!Sync` and keeps readers off the async runtime threads. WAL allows any
//! number of concurrent readers alongside the live `indexd` writer.
#![allow(clippy::module_name_repetitions)]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

/// The highest catalog schema version this `webd` build understands. Mirrors
/// `indexd`'s `LATEST_VERSION`; a catalog reporting a newer version was written
/// by a newer `indexd` and is refused rather than misread.
const SUPPORTED_SCHEMA_VERSION: i64 = 3;

/// How long a read-only connection waits on a locked database before erroring.
/// WAL readers rarely block, but this is cheap insurance against a checkpoint
/// race.
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

/// Errors from opening or validating the read-only catalog.
#[derive(Debug, thiserror::Error)]
pub enum CatalogError {
    /// The catalog file could not be opened read-only or a probe query failed.
    #[error("cannot open catalog read-only: {0}")]
    Open(#[from] rusqlite::Error),

    /// The catalog schema is newer than this `webd` build supports.
    #[error("catalog schema v{found} is newer than supported v{supported}; refusing to read")]
    SchemaTooNew {
        /// Version found in the catalog.
        found: i64,
        /// Highest version this build supports ([`SUPPORTED_SCHEMA_VERSION`]).
        supported: i64,
    },
}

/// A clonable handle to the read-only catalog at a fixed path.
#[derive(Clone, Debug)]
pub struct Catalog {
    path: Arc<PathBuf>,
}

impl Catalog {
    /// Open and validate the catalog at `path`.
    ///
    /// Performs a one-time startup probe: opens the database read-only and
    /// checks the schema version is not newer than this build supports. Request
    /// handlers then open their own connections via [`Catalog::connect`] without
    /// re-probing.
    ///
    /// # Errors
    ///
    /// Returns [`CatalogError::Open`] if the file cannot be opened read-only or
    /// the probe query fails, or [`CatalogError::SchemaTooNew`] if the catalog
    /// was written by a newer `indexd`.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, CatalogError> {
        let catalog = Self {
            path: Arc::new(path.into()),
        };
        let conn = catalog.connect()?;
        let version = schema_version(&conn)?;
        if version > SUPPORTED_SCHEMA_VERSION {
            return Err(CatalogError::SchemaTooNew {
                found: version,
                supported: SUPPORTED_SCHEMA_VERSION,
            });
        }
        Ok(catalog)
    }

    /// Open a fresh **read-only** connection to the catalog.
    ///
    /// Always call this inside a blocking task — `rusqlite` is synchronous.
    ///
    /// # Errors
    ///
    /// Returns the underlying `rusqlite` error if the database cannot be opened
    /// read-only or a connection pragma fails.
    pub fn connect(&self) -> Result<Connection, rusqlite::Error> {
        let conn = Connection::open_with_flags(
            self.path.as_ref(),
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
        // Defence in depth: reject any statement that would write, on top of
        // the read-only open flag.
        conn.pragma_update(None, "query_only", true)?;
        Ok(conn)
    }
}

/// Read the catalog's current schema version (`MAX(schema_version.version)`),
/// or `0` if the `schema_version` table is absent (an empty/foreign file).
fn schema_version(conn: &Connection) -> Result<i64, rusqlite::Error> {
    let present: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'",
            [],
            |_| Ok(true),
        )
        .or_else(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(other),
        })?;
    if !present {
        return Ok(0);
    }
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version",
        [],
        |row| row.get(0),
    )
}
