//! `webd` — the disposable axum/tokio web backend (contract **D2**,
//! [`docs/specs/webd.md`]).
//!
//! This crate is the **Task 5.1a** slice: a **read-only** REST API over the
//! `indexd` `SQLite` catalog plus the static-SPA host plumbing (a placeholder
//! bundle; the real bundle arrives in Task 5.2).
//!
//! ## Boundaries (SPEC.md §2, §7; webd.md §4)
//!
//! * The catalog is opened **read-only** (`SQLITE_OPEN_READ_ONLY`). `indexd`
//!   is the sole `SQLite` writer; `webd` never writes.
//! * `webd` **never parses video / SEI** — every datum comes from the catalog.
//! * No mutations, no streaming/range/export, no leases, no auth, and no SSE
//!   live in this slice (later 5.1b/5.1c slices).
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
mod polyline;
mod query;
mod route;

#[cfg(test)]
mod tests;

pub use catalog::{Catalog, CatalogError};

use std::path::PathBuf;

use axum::Router;

/// Shared handler state: the read-only catalog handle. Cheap to clone (an
/// `Arc`-backed path), so each request can open its own short-lived read-only
/// connection inside a blocking task.
#[derive(Clone)]
struct AppState {
    catalog: Catalog,
}

/// Build the full `webd` application router: the `/api/*` read endpoints plus
/// the static-SPA host with SPA-fallback routing.
///
/// `static_dir` is the directory holding the SPA bundle (an `index.html` plus
/// hashed assets). Any non-API path that does not match a file falls back to
/// `index.html` so client-side routes resolve. Unknown `/api/*` paths return
/// the JSON error envelope (never the SPA shell).
pub fn build_router(catalog: Catalog, static_dir: PathBuf) -> Router {
    let state = AppState { catalog };
    route::router(state, static_dir)
}
