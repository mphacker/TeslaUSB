//! Server-pushed media-change notifications (the realtime replacement for SPA
//! list polling).
//!
//! `indexd` is the sole writer of the catalog; `webd` only reads it. To let the
//! SPA refresh media lists the instant a change lands — without every browser
//! polling `/api/<category>` on a timer — a background monitor holds ONE
//! dedicated read-only catalog connection and watches SQLite's
//! `PRAGMA data_version`.
//!
//! `data_version` is unchanged for commits made on the *same* connection but
//! increments on every commit by *any other* connection, including a different
//! process. Our monitor connection never writes, so every observed change is an
//! `indexd` commit (a media install/delete that has been applied and indexed).
//! On a change the monitor broadcasts a unit tick; the `/api/media-events` SSE
//! forwards each tick to every connected browser, which then refetches whatever
//! category it is currently viewing.
//!
//! The monitor runs on a dedicated OS thread (rusqlite is synchronous) and
//! holds only a [`Weak`] back-reference to the shared state, so it shuts itself
//! down automatically once the last [`MediaEvents`] handle (and thus the owning
//! `AppState`) is dropped — important for tests, which build and drop many
//! routers.

use std::sync::{Arc, Weak};
use std::time::Duration;

use tokio::sync::broadcast;

use crate::catalog::Catalog;

/// Broadcast backlog for media-change ticks. Every tick means the same thing
/// ("refetch"), so they coalesce; a small buffer is plenty and a lagged
/// subscriber simply treats the gap as one more reason to refetch.
const CHANNEL_CAPACITY: usize = 16;

/// How often the monitor samples `PRAGMA data_version`. 300 ms keeps push
/// latency well under human perception while costing one in-process PRAGMA per
/// sample (no car interaction, no disk write).
const POLL_INTERVAL: Duration = Duration::from_millis(300);

/// Shared state behind a [`MediaEvents`] handle. Its [`Drop`] is the monitor's
/// shutdown signal: the monitor only holds a [`Weak`] to this, so once every
/// clone is gone the next `upgrade()` fails and the thread exits.
struct Inner {
    tx: broadcast::Sender<()>,
}

/// A cloneable handle to the process-wide media-change bus.
#[derive(Clone)]
pub(crate) struct MediaEvents {
    inner: Arc<Inner>,
}

impl MediaEvents {
    /// Start the background `data_version` monitor over a dedicated read-only
    /// catalog connection and return a handle the SSE endpoint subscribes to.
    ///
    /// If the monitor connection cannot be opened, the returned hub simply never
    /// ticks (the SPA falls back to its own slow refresh); `webd` still serves
    /// normally rather than failing to start.
    pub(crate) fn start(catalog: &Catalog) -> Self {
        let (tx, _rx) = broadcast::channel(CHANNEL_CAPACITY);
        let inner = Arc::new(Inner { tx });
        match catalog.connect() {
            Ok(conn) => {
                let weak = Arc::downgrade(&inner);
                if let Err(err) = std::thread::Builder::new()
                    .name("media-events".to_owned())
                    .spawn(move || monitor_loop(&conn, &weak))
                {
                    eprintln!("media-events monitor not started: {err}");
                }
            }
            Err(err) => {
                eprintln!("media-events monitor disabled: cannot open catalog: {err}");
            }
        }
        MediaEvents { inner }
    }

    /// Subscribe to the live media-change tick stream.
    pub(crate) fn subscribe(&self) -> broadcast::Receiver<()> {
        self.inner.tx.subscribe()
    }
}

/// Read `PRAGMA data_version` from a read-only connection.
fn data_version(conn: &rusqlite::Connection) -> rusqlite::Result<i64> {
    conn.query_row("PRAGMA data_version", [], |row| row.get(0))
}

/// Sample `data_version` on each tick and broadcast when it changes. Exits when
/// the last [`MediaEvents`] handle is dropped (the [`Weak`] no longer upgrades).
fn monitor_loop(conn: &rusqlite::Connection, weak: &Weak<Inner>) {
    let mut last = data_version(conn).ok();
    loop {
        std::thread::sleep(POLL_INTERVAL);
        // Hold a strong ref only briefly; never across the sleep, so the handle
        // can drop and signal shutdown.
        let Some(inner) = weak.upgrade() else {
            return;
        };
        match data_version(conn) {
            Ok(v) if last != Some(v) => {
                last = Some(v);
                // A "no subscribers" error is normal (no browser connected).
                let _ = inner.tx.send(());
            }
            // Unchanged, or a transient read error (e.g. a checkpoint race) we
            // skip — the next real change is still caught.
            _ => {}
        }
        drop(inner);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    /// Build a minimal valid catalog file (schema_version table at v1) and
    /// return its path-backed handle, the file path, and the temp dir keeping
    /// it alive.
    fn temp_catalog() -> (Catalog, std::path::PathBuf, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("catalog.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE schema_version(version INTEGER NOT NULL);
             INSERT INTO schema_version(version) VALUES (1);
             CREATE TABLE media(rel_path TEXT);",
        )
        .unwrap();
        let catalog = Catalog::open(&path).unwrap();
        (catalog, path, dir)
    }

    #[test]
    fn ticks_when_another_connection_commits() {
        let (catalog, path, _dir) = temp_catalog();
        let events = MediaEvents::start(&catalog);
        let mut rx = events.subscribe();

        // Give the monitor one sample to establish its baseline data_version.
        std::thread::sleep(Duration::from_millis(450));

        // Commit from a separate writer connection (simulating indexd).
        let writer = Connection::open(&path).unwrap();
        writer
            .execute("INSERT INTO media(rel_path) VALUES ('Music/x.mp3')", [])
            .unwrap();

        // The monitor should observe the data_version bump and broadcast a tick.
        let mut got = false;
        for _ in 0..40 {
            if rx.try_recv().is_ok() {
                got = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(got, "expected a media-changed tick after an external commit");
    }

    #[test]
    fn monitor_thread_exits_when_handle_dropped() {
        let (catalog, _path, _dir) = temp_catalog();
        let events = MediaEvents::start(&catalog);
        let rx = events.subscribe();
        drop(events);
        drop(rx);
        // Nothing to assert deterministically beyond "no panic / no hang"; the
        // Weak upgrade in monitor_loop returns None on the next sample and the
        // thread returns. Give it time to wind down.
        std::thread::sleep(Duration::from_millis(400));
    }
}
