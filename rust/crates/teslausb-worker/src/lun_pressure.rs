//! LUN-aware free-space pressure measurement.
//!
//! Both cleanup paths in this crate (`cleanup.rs` age-based
//! retention, `cleanup_sweep.rs` tier-aware target-free sweep)
//! historically measured "free space" via `statvfs(backing_root)`
//! — i.e. the free space of the SD card's root filesystem. That
//! is the WRONG measurement for Tesla's view of the world.
//!
//! Tesla only sees the bytes the teslafat backend exposes
//! through the synthesised LUN. Its size is fixed at
//! `storage.teslacam_gb * 1 GiB` (see `storage_config.rs` and
//! `teslafat::config::VolumeConfig::volume_size_gb`). The SD
//! card under the hood may be 64 GiB, 256 GiB, or 470 GiB —
//! irrelevant. What matters is `sum(file sizes under
//! backing_root) / lun_size_bytes`.
//!
//! The bug this module fixes was observed live on
//! `cybertruckusb.local` on 2026-05-23: a 470 GB SD card with
//! 176 GB free reported "no pressure" while the LUN-visible
//! backing tree had grown to 266 GiB against a 256 GiB LUN cap,
//! crash-looping `teslafat@0` with `exFAT cluster allocator
//! out of capacity`. Operator had to recover by hand.
//!
//! ## Design rules
//!
//! * `lun_used_bytes(root)` walks `root` recursively and sums
//!   `file.metadata().len()` for every regular file. Symlinks
//!   are followed via `entry.file_type().is_file()`'s default
//!   semantics — Tesla writes real files only, so this is
//!   moot in practice but documented for future readers.
//! * Per-entry I/O errors (a vanished file mid-walk, an
//!   `EACCES` on one directory) are logged and skipped: cleanup
//!   pressure must not fail the supervisor because one stat
//!   raced with an unlink. The function only returns `Err` on
//!   a fatal initial-open failure of `root` itself.
//! * `lun_free_pct` is a pure function with no I/O so the test
//!   suite can exercise every edge (zero-sized LUN, used >
//!   capacity, used == capacity) without filesystem fixtures.
//! * No platform gates. `std::fs::read_dir` and
//!   `Metadata::len` work on Windows and macOS too, so the
//!   dev-workstation tests cover the same code path as the
//!   live Pi.
//!
//! See ADR-0018 for the architectural decision.

// "TeslaCam", "LUN", "exFAT", "SD" and friends are domain
// terms; backticking each one in doc comments adds noise.
// Matches the SEI / cleanup carve-out.
#![allow(clippy::doc_markdown)]

use std::path::Path;

use tracing::warn;

/// One GiB in bytes. Mirrors `teslafat::backend::synth::BYTES_PER_GIB`
/// so the worker's LUN-size arithmetic matches what teslafat
/// actually advertises to the kernel.
pub const BYTES_PER_GIB: u64 = 1 << 30;

/// Convert a TOML `teslacam_gb` value (GiB, u32) into raw bytes.
/// Returns 0 when `teslacam_gb` is 0 — callers MUST treat 0 as
/// "LUN-pressure measurement unavailable" and skip the check.
#[must_use]
pub fn lun_size_bytes(teslacam_gb: u32) -> u64 {
    u64::from(teslacam_gb).saturating_mul(BYTES_PER_GIB)
}

/// Sum the byte sizes of every regular file under `root`,
/// recursively. Returns 0 if `root` does not exist (cleanup is
/// a no-op on a fresh device) or is empty.
///
/// # Errors
///
/// Returns `Err` only when the initial `read_dir(root)` fails
/// with anything other than `NotFound`. Per-entry stat or
/// recursion failures are logged at WARN and skipped so a
/// single inaccessible subtree cannot stall the cleanup loop.
pub fn lun_used_bytes(root: &Path) -> std::io::Result<u64> {
    match std::fs::metadata(root) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e),
    }
    let mut total: u64 = 0;
    walk(root, &mut total)?;
    Ok(total)
}

/// Recursion helper. Iterative recursion is fine here: Tesla
/// only writes 2-3 levels deep under `backing_root`
/// (`TeslaCam/{RecentClips,SavedClips,SentryClips}/<event>/*.mp4`)
/// so the stack depth is bounded by the filesystem itself.
fn walk(dir: &Path, total: &mut u64) -> std::io::Result<()> {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                warn!(dir = %dir.display(), error = %e,
                    "lun_used_bytes: read_dir entry failed; skipping");
                continue;
            }
        };
        let ft = match entry.file_type() {
            Ok(f) => f,
            Err(e) => {
                warn!(path = %entry.path().display(), error = %e,
                    "lun_used_bytes: file_type failed; skipping");
                continue;
            }
        };
        if ft.is_dir() {
            if let Err(e) = walk(&entry.path(), total) {
                warn!(path = %entry.path().display(), error = %e,
                    "lun_used_bytes: recursive walk failed; skipping subtree");
            }
        } else if ft.is_file() {
            match entry.metadata() {
                Ok(md) => *total = total.saturating_add(md.len()),
                Err(e) => warn!(
                    path = %entry.path().display(),
                    error = %e,
                    "lun_used_bytes: metadata failed; skipping file"
                ),
            }
        }
        // Symlinks, sockets, fifos etc. are ignored. Tesla
        // never writes any of those.
    }
    Ok(())
}

/// Percent of the LUN that is still free, given the measured
/// `used_bytes` and the configured `lun_size_bytes`.
///
/// Pure function. Saturates to `0.0` when used >= capacity
/// (which is the precise scenario the operator hit — the
/// backing tree overflowed the LUN). Returns `100.0` when the
/// LUN size is 0 (caller asked for a no-op measurement).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn lun_free_pct(used_bytes: u64, lun_size_bytes: u64) -> f64 {
    if lun_size_bytes == 0 {
        return 100.0;
    }
    let free = lun_size_bytes.saturating_sub(used_bytes);
    (free as f64 / lun_size_bytes as f64) * 100.0
}

#[cfg(test)]
mod tests {
    #![allow(clippy::float_cmp, clippy::unwrap_used)]

    use super::*;

    #[test]
    fn lun_size_bytes_multiplies_gib() {
        assert_eq!(lun_size_bytes(1), 1 << 30);
        assert_eq!(lun_size_bytes(256), 256u64 * (1 << 30));
        assert_eq!(lun_size_bytes(0), 0);
    }

    #[test]
    fn lun_free_pct_full_lun_is_zero() {
        let size = lun_size_bytes(256);
        assert_eq!(lun_free_pct(size, size), 0.0);
    }

    #[test]
    fn lun_free_pct_empty_lun_is_hundred() {
        let size = lun_size_bytes(256);
        assert_eq!(lun_free_pct(0, size), 100.0);
    }

    #[test]
    fn lun_free_pct_overfilled_saturates_at_zero() {
        // The exact "266 GiB tree on a 256 GiB LUN" symptom
        // the operator hit on 2026-05-23.
        let size = lun_size_bytes(256);
        let overflow = lun_size_bytes(266);
        assert_eq!(lun_free_pct(overflow, size), 0.0);
    }

    #[test]
    fn lun_free_pct_zero_capacity_is_hundred() {
        // Caller asked for a no-op; we must not divide by 0.
        assert_eq!(lun_free_pct(0, 0), 100.0);
        assert_eq!(lun_free_pct(1024, 0), 100.0);
    }

    #[test]
    fn lun_free_pct_half_full_is_fifty() {
        let size = lun_size_bytes(64);
        assert_eq!(lun_free_pct(size / 2, size), 50.0);
    }

    #[test]
    fn lun_used_bytes_missing_root_is_zero() {
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("nope");
        assert_eq!(lun_used_bytes(&missing).unwrap(), 0);
    }

    #[test]
    fn lun_used_bytes_empty_root_is_zero() {
        let tmp = tempfile::tempdir().unwrap();
        assert_eq!(lun_used_bytes(tmp.path()).unwrap(), 0);
    }

    #[test]
    fn lun_used_bytes_sums_file_sizes_recursively() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        std::fs::write(root.join("a.mp4"), vec![0u8; 1024]).unwrap();
        std::fs::create_dir_all(root.join("TeslaCam/RecentClips/event1")).unwrap();
        std::fs::write(
            root.join("TeslaCam/RecentClips/event1/front.mp4"),
            vec![0u8; 2048],
        )
        .unwrap();
        std::fs::write(
            root.join("TeslaCam/RecentClips/event1/back.mp4"),
            vec![0u8; 4096],
        )
        .unwrap();
        std::fs::create_dir_all(root.join("TeslaCam/SavedClips/event2")).unwrap();
        std::fs::write(
            root.join("TeslaCam/SavedClips/event2/front.mp4"),
            vec![0u8; 8192],
        )
        .unwrap();
        assert_eq!(lun_used_bytes(root).unwrap(), 1024 + 2048 + 4096 + 8192,);
    }

    #[test]
    fn lun_used_bytes_ignores_empty_dirs() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join("a/b/c")).unwrap();
        assert_eq!(lun_used_bytes(tmp.path()).unwrap(), 0);
    }

    #[test]
    fn pressure_scenario_from_2026_05_23_outage() {
        // Reproduces the exact failure: simulate a 256 GiB LUN
        // whose backing tree grew to 257 GiB. `min_free_pct = 10`
        // (worker.toml default on the device). Pressure MUST
        // trigger.
        let size = lun_size_bytes(256);
        let used = lun_size_bytes(257);
        let free = lun_free_pct(used, size);
        let min_free_pct: f64 = 10.0;
        assert!(
            free < min_free_pct,
            "expected pressure (free={free}% < min={min_free_pct}%)",
        );
    }
}
