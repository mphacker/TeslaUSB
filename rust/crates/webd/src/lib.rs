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
mod cloud;
mod cloud_creds;
mod daybucket;
mod dto;
mod error;
mod fsck;
mod gadget;
mod health;
mod indexd_client;
mod jobs;
mod lightshows;
mod media;
mod media_events;
mod media_upload;
mod mutation_origin;
mod music;
mod plates;
mod polyline;
mod query;
mod range;
mod read_client;
mod retention;
mod route;
mod scheduler;
mod stats_client;
mod sysinfo;
pub(crate) mod timezone;
mod uploadd_client;
mod wifi;
mod wifi_ap;
mod wifi_mutate;
mod wifid_client;
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
/// Only one media install may stage its (up to 256 MiB) blob at a time, so the
/// pre-stage free-space check is race-free and concurrent uploads can't jointly
/// exhaust the recording-critical /data card.
const MAX_CONCURRENT_INSTALL_STAGES: usize = 1;

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
    upload_sem: Arc<Semaphore>,
    sys: SysHandle,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    wifid: Arc<dyn wifid_client::WifidClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    uploadd: Arc<dyn uploadd_client::UploaddClient>,
    read_client: Arc<dyn read_client::ReadFileClient + Send + Sync>,
    stats_client: Arc<dyn stats_client::VolumeStatsClient + Send + Sync>,
    jobs: jobs::JobHub,
    /// Process-wide media-change bus: a background `data_version` monitor ticks
    /// this whenever `indexd` commits, and the `/api/media-events` SSE forwards
    /// each tick to browsers so media lists refresh in real time (no polling).
    media_events: media_events::MediaEvents,
    wifi_mutation: Arc<tokio::sync::Mutex<()>>,
    cloud_creds_dir: PathBuf,
    cloud_creds_mutation: Arc<tokio::sync::Mutex<()>>,
    onedrive_discoverer: cloud_creds::OnedriveDiscoverer,
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

/// Assemble the router over an explicit `wifid` client — the injection seam used
/// by the Wi-Fi AP handler tests. All other daemon clients default to their
/// platform clients.
#[cfg(test)]
fn router_with_wifid(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    wifid: Arc<dyn wifid_client::WifidClient>,
) -> Router {
    let gadget = default_gadget_client(PathBuf::from("/nonexistent/gadgetd.sock"));
    router_with_all_clients_and_read_client(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler::default_client(default_scheduler_sock()),
        indexd_client::default_client(default_indexd_sock()),
        default_read_client(),
        default_stats_client(),
        wifid,
        default_chime_library_dir(),
    )
}

#[cfg(test)]
fn router_with_gadget_and_probe(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    probe: Arc<dyn sysinfo::SystemProbe>,
) -> Router {
    router_with_all_clients_and_read_client_and_probe(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler::default_client(default_scheduler_sock()),
        indexd_client::default_client(default_indexd_sock()),
        default_read_client(),
        default_stats_client(),
        wifid_client::default_client(default_wifid_sock()),
        default_chime_library_dir(),
        probe,
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

/// The default `wifid` control-socket path (overridable via `WEBD_WIFID_SOCK`).
fn default_wifid_sock() -> PathBuf {
    std::env::var_os("WEBD_WIFID_SOCK")
        .map_or_else(|| PathBuf::from("/run/teslausb/wifid.sock"), PathBuf::from)
}

/// The default `uploadd` control-socket path (overridable via
/// `WEBD_UPLOADD_SOCK`).
fn default_uploadd_sock() -> PathBuf {
    std::env::var_os("WEBD_UPLOADD_SOCK").map_or_else(
        || PathBuf::from("/run/teslausb/uploadd.sock"),
        PathBuf::from,
    )
}

/// The default `schedulerd` chime-library directory (overridable via
/// `WEBD_CHIME_LIBRARY_DIR`). Must match `schedulerd`'s `SCHEDULERD_LIBRARY_DIR`;
/// `webd` only ever reads it.
fn default_chime_library_dir() -> PathBuf {
    std::env::var_os("WEBD_CHIME_LIBRARY_DIR")
        .map_or_else(|| PathBuf::from("/data/teslausb/chimes"), PathBuf::from)
}

/// The default cloud-credential directory (overridable via
/// `WEBD_CLOUD_CREDS_DIR`).
fn default_cloud_creds_dir() -> PathBuf {
    std::env::var_os("WEBD_CLOUD_CREDS_DIR")
        .map_or_else(|| PathBuf::from("/var/lib/teslausb"), PathBuf::from)
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
        default_stats_client(),
        wifid_client::default_client(default_wifid_sock()),
        chime_library_dir,
    )
}

#[cfg(test)]
fn router_with_all_clients_and_uploadd(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    uploadd: Arc<dyn uploadd_client::UploaddClient>,
    chime_library_dir: PathBuf,
) -> Router {
    router_with_all_clients_and_read_client_and_probe_and_cloud_creds(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        indexd,
        uploadd,
        default_read_client(),
        default_stats_client(),
        wifid_client::default_client(default_wifid_sock()),
        chime_library_dir,
        default_cloud_creds_dir(),
        Arc::new(sysinfo::LinuxProbe),
        cloud_creds::default_onedrive_discoverer(),
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

fn default_stats_client() -> Arc<dyn stats_client::VolumeStatsClient + Send + Sync> {
    #[cfg(unix)]
    {
        Arc::new(stats_client::UnixVolumeStatsClient::new(
            stats_client::SCANNERD_STAT_SOCKET_PATH,
        ))
    }
    #[cfg(not(unix))]
    {
        Arc::new(stats_client::UnavailableVolumeStatsClient)
    }
}

#[allow(clippy::too_many_arguments)]
fn router_with_all_clients_and_read_client_and_probe(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    read_client: Arc<dyn read_client::ReadFileClient + Send + Sync>,
    stats_client: Arc<dyn stats_client::VolumeStatsClient + Send + Sync>,
    wifid: Arc<dyn wifid_client::WifidClient>,
    chime_library_dir: PathBuf,
    probe: Arc<dyn sysinfo::SystemProbe>,
) -> Router {
    router_with_all_clients_and_read_client_and_probe_and_cloud_creds(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        indexd,
        uploadd_client::default_client(default_uploadd_sock()),
        read_client,
        stats_client,
        wifid,
        chime_library_dir,
        default_cloud_creds_dir(),
        probe,
        cloud_creds::default_onedrive_discoverer(),
    )
}

#[allow(clippy::too_many_arguments)]
fn router_with_all_clients_and_read_client_and_probe_and_cloud_creds(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    gadget: Arc<dyn gadget::GadgetClient>,
    scheduler: Arc<dyn scheduler::SchedulerClient>,
    indexd: Arc<dyn indexd_client::IndexdClient>,
    uploadd: Arc<dyn uploadd_client::UploaddClient>,
    read_client: Arc<dyn read_client::ReadFileClient + Send + Sync>,
    stats_client: Arc<dyn stats_client::VolumeStatsClient + Send + Sync>,
    wifid: Arc<dyn wifid_client::WifidClient>,
    chime_library_dir: PathBuf,
    cloud_creds_dir: PathBuf,
    probe: Arc<dyn sysinfo::SystemProbe>,
    onedrive_discoverer: cloud_creds::OnedriveDiscoverer,
) -> Router {
    let sys = SysHandle {
        probe,
        paths: Arc::new(sysinfo::SysPaths {
            archive_root: media.archive_root_path(),
            worker_health_file: std::env::var_os("WEBD_WORKER_HEALTH_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/run/teslausb/retentiond.health.json")),
            indexer_health_file: std::env::var_os("WEBD_INDEXER_HEALTH_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/run/teslausb/indexd.health.json")),
            governor_status_file: std::env::var_os("WEBD_GOVERNOR_STATUS_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/run/teslausb/retentiond.governor.json")),
            fsck_status_file: std::env::var_os("WEBD_FSCK_STATUS_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/data/teslausb/fsck_status.json")),
            fsck_history_file: std::env::var_os("WEBD_FSCK_HISTORY_FILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/data/teslausb/fsck_history.json")),
            media_ro_mount: std::env::var_os("WEBD_MEDIA_RO_MOUNT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/run/teslausb/media-ro")),
        }),
    };
    let media_events = media_events::MediaEvents::start(&catalog);
    let state = AppState {
        catalog,
        media,
        export_sem: Arc::new(Semaphore::new(MAX_CONCURRENT_EXPORTS)),
        upload_sem: Arc::new(Semaphore::new(MAX_CONCURRENT_INSTALL_STAGES)),
        sys,
        gadget,
        scheduler,
        wifid,
        indexd,
        uploadd,
        read_client,
        stats_client,
        jobs: jobs::JobHub::new(),
        media_events,
        wifi_mutation: Arc::new(tokio::sync::Mutex::new(())),
        cloud_creds_dir,
        cloud_creds_mutation: Arc::new(tokio::sync::Mutex::new(())),
        onedrive_discoverer,
        chime_library_dir,
    };
    if std::env::var_os("WEBD_CHIME_ENFORCER").is_some() {
        chime_enforcer::spawn(state.clone());
    }
    route::router(state, static_dir)
}

#[cfg(test)]
fn router_with_cloud_creds_dir_and_onedrive_discoverer(
    catalog: Catalog,
    static_dir: PathBuf,
    media: MediaConfig,
    cloud_creds_dir: PathBuf,
    onedrive_discoverer: cloud_creds::OnedriveDiscoverer,
) -> Router {
    let gadget = default_gadget_client(PathBuf::from("/nonexistent/gadgetd.sock"));
    router_with_all_clients_and_read_client_and_probe_and_cloud_creds(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler::default_client(default_scheduler_sock()),
        indexd_client::default_client(default_indexd_sock()),
        uploadd_client::default_client(default_uploadd_sock()),
        default_read_client(),
        default_stats_client(),
        wifid_client::default_client(default_wifid_sock()),
        default_chime_library_dir(),
        cloud_creds_dir,
        Arc::new(sysinfo::LinuxProbe),
        onedrive_discoverer,
    )
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
    stats_client: Arc<dyn stats_client::VolumeStatsClient + Send + Sync>,
    wifid: Arc<dyn wifid_client::WifidClient>,
    chime_library_dir: PathBuf,
) -> Router {
    router_with_all_clients_and_read_client_and_probe(
        catalog,
        static_dir,
        media,
        gadget,
        scheduler,
        indexd,
        read_client,
        stats_client,
        wifid,
        chime_library_dir,
        Arc::new(sysinfo::LinuxProbe),
    )
}
