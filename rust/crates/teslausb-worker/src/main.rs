//! `teslausb-worker` — background retention + cloud-sync + indexer.
//!
//! Long-running daemon, separate from `teslafat`. Communicates
//! with `teslafat` over the IPC envelope defined in
//! `teslausb-core`. Owns:
//!
//! * Retention sweeps over `backing_root` (capacity floor +
//!   class-based deletion priority).
//! * Cloud upload queue (single rclone subprocess at a time,
//!   `nice -n 19 ionice -c3`).
//! * Indexer (parses MP4 SEI for GPS / event metadata into the
//!   geodata DB consumed by the web map).
//!
//! ## Phase 0.2 state
//!
//! Placeholder entry point — exits successfully so the empty
//! workspace builds. The real bootstrap and worker loop land in
//! Phase 14 per `docs/00-PLAN.md`.

fn main() {
    // Phase 0.2 placeholder. Replaced by Phase 14.
}
