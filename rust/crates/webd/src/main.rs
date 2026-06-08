//! `webd` binary: opens the read-only catalog, builds the router, and serves it
//! on the configured LAN/AP address.
//!
//! Configuration is via environment (a systemd unit supplies these on the Pi):
//!   * `WEBD_DB`           — path to the `indexd` `SQLite` catalog (required).
//!   * `WEBD_STATIC`       — directory holding the SPA bundle (default `static`).
//!   * `WEBD_BIND`         — `host:port` to bind (default `127.0.0.1:8080`).
//!   * `WEBD_ARCHIVE_ROOT` — archive root jailing streamed/exported files
//!                           (default `/srv/teslausb/archive`).
//!   * `WEBD_CACHE_DIR`    — directory for the zip-export tempfile (default the
//!                           system temp dir; on the Pi point this at `NVMe`
//!                           storage, not tmpfs, so a large export cannot
//!                           exhaust RAM).
//!
//! `WEBD_BIND` MUST be a LAN/AP address, never a public-internet interface
//! (SPEC.md §7, webd.md §3.1). The default `127.0.0.1` is a safe dev default; on
//! the device the unit binds the AP/LAN address explicitly.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;

use webd::{Catalog, MediaConfig, build_router};

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("webd: {err}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let db_path = std::env::var_os("WEBD_DB")
        .map(PathBuf::from)
        .ok_or("WEBD_DB must point at the indexd catalog")?;
    let static_dir =
        std::env::var_os("WEBD_STATIC").map_or_else(|| PathBuf::from("static"), PathBuf::from);
    let bind = std::env::var("WEBD_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_owned());
    let addr: SocketAddr = bind.parse()?;

    let archive_root = std::env::var_os("WEBD_ARCHIVE_ROOT")
        .map_or_else(|| PathBuf::from("/srv/teslausb/archive"), PathBuf::from);
    let cache_dir =
        std::env::var_os("WEBD_CACHE_DIR").map_or_else(std::env::temp_dir, PathBuf::from);
    let media = MediaConfig::new(archive_root, cache_dir);

    let catalog = Catalog::open(db_path)?;
    let app = build_router(catalog, static_dir, media);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    println!("webd listening on http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
