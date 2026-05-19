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
//! ## Module layout (Phase 1.5 state)
//!
//! * [`config`] — TOML config loader (Phase 1.1).
//! * [`nbd`] — NBD newstyle handshake (Phase 1.3) +
//!   transmission-phase dispatch loop (Phase 1.5) backed by
//!   [`teslausb_core::backend::BlockBackend`].

// Charter Pillar 1 carve-out: `unwrap` + bounds-indexing in tests
// are fine since the desired failure mode is a loud panic. Both
// lints stay `deny` in production code.
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::indexing_slicing))]

pub mod config;
pub mod nbd;
