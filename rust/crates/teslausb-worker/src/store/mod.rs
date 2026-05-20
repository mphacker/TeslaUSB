//! SQLite-backed waypoint / clip index store.
//!
//! Layer-3 adapter (per the charter layering rule). Wraps
//! `rusqlite::Connection` behind a typed-error API. Schema
//! and rationale: [ADR-0010].
//!
//! [ADR-0010]: ../../../../docs/adr/0010-rusqlite-for-indexer-store.md
//!
//! ## Concurrency model
//!
//! * Single writer (the indexer) + multiple readers (cleanup,
//!   future web). WAL mode is enabled at open so readers do
//!   not block on writer transactions.
//! * The DB API is blocking (rusqlite is synchronous). Callers
//!   running inside a tokio runtime MUST wrap each call in
//!   `tokio::task::spawn_blocking`. This is enforced by
//!   review, not by the type system, because hiding the
//!   blocking call inside an `async fn` is exactly the
//!   anti-pattern the charter rejects.
//!
//! ## Module layout
//!
//! Split per charter §1 god-module ceiling. Each submodule
//! owns one responsibility:
//!
//! * `bucket`     — the [`Bucket`] enum + DB/Tesla-dir mappings.
//! * `types`      — [`ClipRecord`], [`StoreError`], [`Result`].
//! * `schema`     — schema version + migration list (private to
//!   the store layer).
//! * `store_impl` — the [`Store`] struct and all query methods.
//! * `helpers`    — row-mapping and serialization helpers, also
//!   private to the store layer.
//!
//! ## Migration discipline
//!
//! Every schema mutation lives inside `MIGRATIONS` in the
//! `schema` submodule, which is an ordered list of SQL
//! strings. [`Store::open`] reads the current `schema_version`
//! from the `meta` table, then applies every migration with
//! `version > current` in one transaction. Downgrades are
//! refused.

// File-level: "SQLite", "WAL", "FK", "UPSERT" are domain
// terms that read more naturally in prose than as code.
// Matches the carve-out used in the SEI files.
#![allow(clippy::doc_markdown)]

mod bucket;
mod helpers;
mod schema;
mod store_impl;
mod types;

#[cfg(test)]
mod tests;

pub use bucket::Bucket;
pub use schema::CURRENT_SCHEMA_VERSION;
pub use store_impl::Store;
pub use types::{ClipRecord, Result, StoreError};
