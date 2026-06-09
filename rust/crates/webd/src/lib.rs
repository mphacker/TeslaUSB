//! `webd` — the disposable axum/tokio web backend (contract **D2**,
//! [`docs/specs/webd.md`]).
//!
//! This crate began as the **Task 5.1a** read-only slice and now also carries
//! the **Task 5.1b** archive-clip streaming + export endpoints (range-request
//! mp4 streaming, single-file download, and zip export). The static-SPA host is
//! still a placeholder bundle (the real bundle arrives in Task 5.2).
//!
//! ## Boundaries (SPEC.md §2, §7; webd.md §4)
//!
//! * The catalog is opened **read-only** (`SQLITE_OPEN_READ_ONLY`). `indexd`
//!   is the sole `SQLite` writer; `webd` never writes.
//! * `webd` **never parses video / SEI** — every datum comes from the catalog.
//! * Streaming/export serve **`archive`-view angles only** (Pi-side ext4
//!   files), jailed under the configured archive root. The retentiond playback
//!   lease, `ro_usb` live streaming, auth, mutations, and SSE are **not** in
//!   this slice (see `media.rs` for the deferred-lease seam).
//! * The HTTP listener must bind the **LAN/AP interface only** (the binary
//!   takes the bind address from configuration; never the public internet).
//!
//! ## Layering
//!
//! * [`catalog`] — the read-only connection factory + reader-side schema-version
//!   guard.
//! * `dto` / `polyline` / `query` — the (crate-private) DTOs, the cached
//!   `trips.polyline` blob decoder, and the short read queries.
//! * `error` — the uniform `{"error": {...}}` envelope.
//! * [`build_router`] — assembles the API routes + the SPA host into one
//!   `axum::Router`, the single public entry point used by the binary and the
//!   handler tests.

mod catalog;
mod dto;
mod error;
mod health;
mod media;
mod polyline;
mod query;
mod range;
mod route;
mod sysinfo;

#[cfg(test)]
mod tests;

pub use catalog::{Catalog, CatalogError};
pub use media::MediaConfig;

use std::path::PathBuf;
use std::sync::Arc;

use axum::Router;
use tokio::sync::Semaphore;

/// Maximum number of clip-zip exports built concurrently. Bounds blocking-pool
/// and cache-filesystem pressure from a burst of `GET /api/clips/{id}/export`.
const MAX_CONCURRENT_EXPORTS: usize = 2;

/// Shared handler state: the read-only catalog handle plus the media config and
/// export concurrency limiter. Cheap to clone (`Arc`-backed), so each request
/// can open its own short-lived read-only connection inside a blocking task.
#[derive(Clone)]
struct AppState {
    catalog: Catalog,
    media: MediaConfig,
    export_sem: Arc<Semaphore>,
    sys: SysHandle,
}

/// The read-only system-probe handle carried in [`AppState`] for the
/// device-status endpoints. Cloning is cheap (`Arc`-backed). It carries no
/// service clients — only a kernel-fact probe and the paths to probe — so the
/// web backend stays a read-only observer (`webd.md` §4).
#[derive(Clone)]
struct SysHandle {
    probe: Arc<dyn sysinfo::SystemProbe>,
    paths: Arc<sysinfo::SysPaths>,
}

/// Build the full `webd` application router: the `/api/*` read endpoints plus
/// the static-SPA host with SPA-fallback routing.
///
/// `static_dir` is the directory holding the SPA bundle (an `index.html` plus
/// hashed assets). Any non-API path that does not match a file falls back to
/// `index.html` so client-side routes resolve. Unknown `/api/*` paths return
/// the JSON error envelope (never the SPA shell). `media` supplies the archive
/// root (the jail for streamed/exported files) and the zip-export cache dir.
pub fn build_router(catalog: Catalog, static_dir: PathBuf, media: MediaConfig) -> Router {
    let sys = SysHandle {
        probe: Arc::new(sysinfo::LinuxProbe),
        paths: Arc::new(sysinfo::SysPaths {
            archive_root: media.archive_root_path(),
        }),
    };
    let state = AppState {
        catalog,
        media,
        export_sem: Arc::new(Semaphore::new(MAX_CONCURRENT_EXPORTS)),
        sys,
    };
    route::router(state, static_dir)
}
