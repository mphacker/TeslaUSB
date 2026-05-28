//! `teslausb-worker` library — modules consumed by both the
//! daemon binary and the unit-test suite.
//!
//! Split out from the binary so per-module unit tests run against
//! a stable public surface instead of `mod`-private items reachable
//! only from `main.rs`. Same pattern as `teslafat`.
//!
//! ## Module layout (Phase 4b.1z state)
//!
//! * [`sei`] — Tesla SEI walker. Reads an MP4 file into RAM,
//!   uses [`teslausb_core::sei`] primitives to find the H.264
//!   elementary stream, walk NAL units, decode SEI envelopes,
//!   and yield per-frame [`teslausb_core::sei::tesla::SeiMessage`]
//!   waypoints with frame-accurate timestamps. This is the I/O
//!   adapter for the pure-logic SEI primitives in `teslausb-core`.
//!
//! Later phases will add `indexer`, `cleanup`, and `supervisor`
//! modules per `docs/00-PLAN.md`.

// Charter Pillar 1 carve-out: `unwrap` + bounds-indexing in tests
// are fine since the desired failure mode is a loud panic. Both
// lints stay `deny` in production code. Matches the `teslafat`
// pattern.
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::indexing_slicing))]

pub mod cleanup;
pub mod cleanup_sweep;
pub mod cloud_keep;
pub mod config;
pub mod indexer;
pub mod lun_pressure;
pub mod mapping_overrides;
pub mod materializer;
pub mod sei;
pub mod storage_config;
pub mod store;
pub mod supervisor;
pub mod watcher;
