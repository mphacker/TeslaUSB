//! Scan orchestrator — the in-process composition of the
//! `scannerd → indexd` seam.
//!
//! [`run_scan_pass`] is now a thin composition of the two halves the
//! cross-process daemons run over a local socket:
//!
//! ```text
//! producer (scannerd::produce::produce)  raw image → ScanBatch of facts
//! consumer (crate::apply::apply)         facts → SQLite catalog
//! ```
//!
//! Running both in one process is behavior-identical to the daemons
//! running them across the socket — that shared decomposition is what
//! keeps the parity tests meaningful for both paths. All raw byte parsing
//! lives in [`scannerd`] (the least-privilege producer); `indexd` only
//! ingests typed, validated, capped facts and writes the DB
//! (`indexd.md` §1/§3 — "No raw parsing or SEI decoding").
//!
//! ## Daemon loop
//!
//! Stability gating needs ≥2 observations spaced by the quiescence
//! window, so the binary calls [`run_scan_pass`] repeatedly (the tracker
//! and DB persist across passes); a single call is the per-tick unit and
//! is what host tests drive.

use rusqlite::Connection;
use scannerd::produce::produce;
use scannerd::reader::BlockReader;
use scannerd::record::BatchError;
use scannerd::stability::StabilityTracker;

use crate::apply::apply;
use crate::db::DbError;
use crate::derive::DeriveConfig;

/// SEI sample-rate decimation stride. Matches the v1 worker
/// (`worker.toml` `sample_rate = 30`) so the cached waypoint cadence —
/// and therefore the derived events — match production. Re-exported from
/// the producer, which now owns the parse/normalize pipeline.
pub use scannerd::produce::DEFAULT_SEI_SAMPLE_RATE;

/// Errors from a scan pass.
#[derive(Debug, thiserror::Error)]
pub enum ScanError {
    /// A raw-media read/parse failure (MBR, boot sector, FAT chain, ...).
    #[error("scanner error: {0}")]
    Scanner(#[from] scannerd::error::ScannerError),
    /// An inbound batch failed batch-level validation (version / caps).
    #[error("invalid batch: {0}")]
    Batch(#[from] BatchError),
    /// A database error from an ingest/derive step.
    #[error("database error: {0}")]
    Db(#[from] DbError),
}

/// Per-pass tuning. The stability window is owned by the caller's
/// [`StabilityTracker`]; this only carries the SEI cadence and the
/// (Copy) derivation parameters.
#[derive(Debug, Clone, Copy)]
pub struct ScanConfig {
    /// SEI sample-rate decimation stride (see [`DEFAULT_SEI_SAMPLE_RATE`]).
    pub sample_rate: u32,
    /// Derivation thresholds (defaults are the v1 production values).
    pub derive: DeriveConfig,
}

impl Default for ScanConfig {
    fn default() -> Self {
        Self {
            sample_rate: DEFAULT_SEI_SAMPLE_RATE,
            derive: DeriveConfig::default(),
        }
    }
}

/// Diagnostic counts from a scan pass (for logging; carries no control
/// state).
#[derive(Debug, Default, Clone, Copy)]
pub struct ScanReport {
    /// exFAT partitions visited.
    pub partitions: usize,
    /// Total directory entries (files) walked across partitions.
    pub files_seen: usize,
    /// Records reported just-stable this pass.
    pub eligible: usize,
    /// Clip angles upserted (front + other).
    pub clips_upserted: usize,
    /// Front clips whose SEI was walked this pass.
    pub front_clips_walked: usize,
    /// Cached waypoints written this pass.
    pub waypoints: usize,
    /// Clips pruned (vanished from the media).
    pub pruned: usize,
    /// Trips materialized after the rebuild.
    pub trips: usize,
    /// Events materialized after the rebuild (driving + sentry).
    pub events: usize,
    /// Eligible clips that errored during ingest and were skipped (the
    /// pass still commits the rest; see [`run_scan_pass`]).
    pub errors: usize,
}

/// Run one scan + derivation pass.
///
/// `tracker` and `conn` persist across passes (the stability gate needs
/// repeated observations). `now_secs` is the wall-clock time used for
/// the quiescence window.
///
/// The pass is the in-process composition of the producer
/// ([`produce`](scannerd::produce::produce)) and the consumer
/// ([`apply`](crate::apply::apply)); the [`ScanReport`] merges the
/// producer's diagnostic counts with the consumer's DB-outcome counts so
/// the reported numbers are identical to the legacy single-function pass:
///
/// * `clips_upserted = clips_written + unplaceable_clips`
/// * `front_clips_walked = front_walked + unplaceable_front`
/// * `errors = read_errors + record_errors`
///
/// (An *unplaceable* clip — no `mvhd`/GPS instant and an out-of-range
/// filename timestamp — never reached a DB write in the legacy pass but
/// was still counted as upserted/walked; the producer counts it so the
/// merge reproduces that exactly.)
///
/// # Errors
///
/// Returns [`ScanError`] if a raw-media read, batch validation, or a
/// database step fails. Individual unreadable/malformed clips are skipped
/// (not fatal); a malformed partition table or a DB write failure aborts
/// the pass.
pub fn run_scan_pass<R: BlockReader + ?Sized>(
    reader: &R,
    conn: &mut Connection,
    tracker: &mut StabilityTracker,
    now_secs: u64,
    config: ScanConfig,
) -> Result<ScanReport, ScanError> {
    let batch = produce(reader, tracker, now_secs, config.sample_rate)?;
    let applied = apply(conn, &batch, config.derive)?;
    let stats = batch.stats;
    Ok(ScanReport {
        partitions: stats.partitions,
        files_seen: stats.files_seen,
        eligible: stats.eligible,
        clips_upserted: applied.clips_written + stats.unplaceable_clips,
        front_clips_walked: applied.front_walked + stats.unplaceable_front,
        waypoints: applied.waypoints,
        pruned: applied.pruned,
        trips: applied.trips,
        events: applied.events,
        errors: stats.read_errors + applied.record_errors,
    })
}
