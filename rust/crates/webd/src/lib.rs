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
//! * Streaming/export serve archive clips from the Pi-side ext4 files first,
//!   with map-playback fallback to non-archive (`ro_usb`/legacy `live`) bytes
//!   via `scannerd` `ReadFile`. The retentiond playback lease, auth, and most
//!   mutations remain out of scope (see `media.rs` for the deferred-lease seam).
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

mod boombox;
mod catalog;
mod chime_enforcer;
mod chime_library;
mod chime_scheduler;
mod chimes;
mod dto;
mod error;
mod gadget;
mod health;
mod indexd_client;
mod jobs;
mod lightshows;
mod media;
mod media_events;
mod media_upload;
mod music;
mod plates;
mod polyline;
mod query;
mod range;
mod read_client;
mod route;
mod scheduler;
mod sysinfo;
pub(crate) mod timezone;
mod wraps;

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

/// Shared handler state: the read-only catalog handle plus the media config,
/// export concurrency limiter, system-probe handle, and the `gadgetd` control
/// client used by the car-delete handoff route. Cheap to clone (`Arc`-backed),
/// so each request can open its own short-lived read-only connection inside a
/// blocking task.
#[derive(Clone)]
struct AppState {
    catalog: Catalog,
    media: MediaConfig,
    export_sem: Arc<Semaphore>,
    sys: SysHandle,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    read_client: Arc<dyn read_client::ReadFileClient + Send + Sync>,
    jobs: jobs::JobHub,
    /// Process-wide media-change bus: a background `data_version` monitor ticks
    /// this whenever `indexd` commits, and the `/api/media-events` SSE forwards
    /// each tick to browsers so media lists refresh in real time (no polling).
    media_events: media_events::MediaEvents,
    /// The `schedulerd`-owned chime library directory (`/data/teslausb/chimes`),
    /// kept for compatibility with the legacy scheduler proxy path.
    #[allow(dead_code)]
    chime_library_dir: PathBuf,
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
/// `gadget_sock` is the `gadgetd` control socket used by the car-delete handoff.
pub fn build_router(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget_sock: PathBuf,
) -> Router {
    router_with_gadget(
        catalog,
        static_dir,
        media,
        default_gadget_client(gadget_sock),
    )
}

/// Construct the platform default `gadgetd` client: a Unix-socket client on the
/// Pi (Linux), and an always-unavailable stub on non-Unix dev hosts.
fn default_gadget_client(gadget_sock: PathBuf) -> Arc<dyn gadget::GadgetClient> {
    #[cfg(unix)]
    {
        Arc::new(gadget::UnixGadgetClient::new(gadget_sock))
    }
    #[cfg(not(unix))]
    {
        let _ = gadget_sock;
        Arc::new(gadget::UnavailableGadgetClient)
    }
}

/// Assemble the router over an explicit `gadgetd` client (the injection seam used
/// by [`build_router`] and the handler tests). The `schedulerd` client defaults
/// to the platform client over the configured socket path.
fn router_with_gadget(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
) -> Router {
    let scheduler = scheduler::default_client(default_scheduler_sock());
    router_with_clients(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        default_chime_library_dir(),
    )
}

/// The default `schedulerd` control-socket path (overridable via
/// `WEBD_SCHEDULERD_SOCK`).
fn default_scheduler_sock() -> PathBuf {
    std::env::var_os("WEBD_SCHEDULERD_SOCK").map_or_else(
        || PathBuf::from("/run/teslausb/schedulerd.sock"),
        PathBuf::from,
    )
}

/// The default `indexd` control-socket path (overridable via
/// `WEBD_INDEXD_SOCK`).
fn default_indexd_sock() -> PathBuf {
    std::env::var_os("WEBD_INDEXD_SOCK")
        .map_or_else(|| PathBuf::from("/run/teslausb/indexd.sock"), PathBuf::from)
}

/// The default `schedulerd` chime-library directory (overridable via
/// `WEBD_CHIME_LIBRARY_DIR`). Must match `schedulerd`'s `SCHEDULERD_LIBRARY_DIR`;
/// `webd` only ever reads it.
fn default_chime_library_dir() -> PathBuf {
    std::env::var_os("WEBD_CHIME_LIBRARY_DIR")
        .map_or_else(|| PathBuf::from("/data/teslausb/chimes"), PathBuf::from)
}

/// Assemble the router over explicit `gadgetd` AND `schedulerd` clients — the
/// injection seam used by the chime-scheduler handler tests.
fn router_with_clients(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    chime_library_dir: PathBuf,
) -> Router {
    let indexd = indexd_client::default_client(default_indexd_sock());
    router_with_all_clients(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        indexd,
        chime_library_dir,
    )
}

fn router_with_all_clients(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    chime_library_dir: PathBuf,
) -> Router {
    router_with_all_clients_and_read_client(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        indexd,
        default_read_client(),
        chime_library_dir,
    )
}

fn default_read_client() -> Arc<dyn read_client::ReadFileClient + Send + Sync> {
    #[cfg(unix)]
    {
        Arc::new(read_client::UnixReadFileClient::new(
            read_client::SCANNERD_READ_SOCKET_PATH,
        ))
    }
    #[cfg(not(unix))]
    {
        Arc::new(read_client::UnavailableReadFileClient)
    }
}

#[allow(clippy::too_many_arguments)]
fn router_with_all_clients_and_read_client(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    read_client: Arc<dyn read_client::ReadFileClient + Send + Sync>,
    chime_library_dir: PathBuf,
) -> Router {
    let sys = SysHandle {
        probe: Arc::new(sysinfo::LinuxProbe),
        paths: Arc::new(sysinfo::SysPaths {
            archive_root: media.archive_root_path(),
            worker_health_file: std::env::var_os("WEBD_WORKER_HEALTH_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/run/teslausb/retentiond.health.json")),
        }),
    };
    let media_events = media_events::MediaEvents::start(&catalog);
    let state = AppState {
        catalog,
        media,
        export_sem: Arc::new(Semaphore::new(MAX_CONCURRENT_EXPORTS)),
        sys,
        gadget,
        scheduler,
        indexd,
        read_client,
        jobs: jobs::JobHub::new(),
        media_events,
        chime_library_dir,
    };
    if std::env::var_os("WEBD_CHIME_ENFORCER").is_some() {
        chime_enforcer::spawn(state.clone());
    }
    route::router(state, static_dir)
}
