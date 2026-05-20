//! `teslafat` library — domain code for the FAT/`exFAT` synthesizer
//! and NBD server.
//!
//! Split out from the binary so:
//!
//! * Per-module unit tests run against a stable public surface
//!   instead of `mod`-private items reachable only from `main.rs`.
//! * The Phase 1.5 transmission loop (and any future Rust callers)
//!   can depend on the protocol helpers without spawning the
//!   process.
//! * `dead_code` lint discipline stays meaningful: the lib's
//!   surface is what consumers can call; the binary's surface is
//!   the CLI.
//!
//! ## Module layout (Phase 1.6 state)
//!
//! * [`config`] — TOML config loader (Phase 1.1) extended with
//!   the `[nbd]` listen-socket schema (Phase 1.6).
//! * [`nbd`] — NBD newstyle handshake (Phase 1.3) +
//!   transmission-phase dispatch loop (Phase 1.5) backed by
//!   [`teslausb_core::backend::BlockBackend`].
//! * [`backend`] — `teslafat`-local [`BlockBackend`] impls.
//!   `backend::zero::ZeroBackend` (Phase 1.6) is the
//!   zero-allocation placeholder retained for smoke tests.
//!   `backend::synth::SynthBackend` (Phase 2.19) is the
//!   production backend: walks `backing_root`, plans a FAT32
//!   or `exFAT` layout, and serves byte-level reads from the
//!   metadata synthesizer + on-demand backing-file overlay.
//! * [`server`] — accept loop that wraps [`nbd::handshake::run`]
//!   in a configurable timeout and hands the established
//!   connection to [`nbd::transmission::run`] (Phase 1.6). The
//!   accept-loop entry point itself is `#[cfg(unix)]`-only since
//!   it binds a Unix-domain socket.
//!
//! [`BlockBackend`]: teslausb_core::backend::BlockBackend

// Charter Pillar 1 carve-out: `unwrap` + bounds-indexing in tests
// are fine since the desired failure mode is a loud panic. Both
// lints stay `deny` in production code.
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::indexing_slicing))]

//! * [`backing_walker`] — Phase 2.15 — `std::fs::read_dir`-driven
//!   walker that produces a [`teslausb_core::fs::backing_tree::BackingTree`]
//!   for the synthesizer to render. I/O half of Phase 2.15; the
//!   type definitions + name validator live in `teslausb-core`.

pub mod backend;
pub mod backing_walker;
pub mod config;
pub mod nbd;
pub mod server;
