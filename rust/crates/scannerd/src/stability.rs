//! Cross-scan stability gate — the heart of scannerd's safety model.
//!
//! A clip is only emitted once it has looked **identical across
//! consecutive scans** for a quiescence interval AND is structurally
//! complete. This is deliberately conservative: reading raw bytes can
//! never *prove* the car won't resume writing a file (Tesla writes
//! metadata, pauses, then resumes), so "stable" is an **operational**
//! judgement, not a guarantee. The gate stacks several necessary
//! conditions to make a false "stable" verdict vanishingly unlikely:
//!
//! 1. `valid_data_length == data_length` — exFAT's authoritative
//!    "fully written" signal; a mid-write file has `VDL < DataLength`.
//! 2. `set_checksum_ok` — the directory entry set is self-consistent.
//! 3. The cheap fingerprint (entry fields: clusters, lengths,
//!    timestamps, flags) is unchanged across `required_stable_scans`
//!    observations spanning at least `quiescence_secs`. This is what
//!    defeats writer-pause aliasing: if the car pauses mid-write and
//!    later resumes, the resumed write changes `VDL`/`DataLength` and
//!    resets the settle window, so a paused-but-unfinished clip can
//!    never look stable.
//!
//! Only after all conditions hold does the caller perform the expensive
//! `mp4_complete` + content-digest + SEI read, then re-verify the
//! fingerprint before emitting (guarding against a change during the
//! heavy read).

use std::collections::HashMap;

use crate::walk::FileRecord;

/// FNV-1a 64-bit offset basis.
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
/// FNV-1a 64-bit prime.
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

/// Tuning for the stability gate. Defaults are conservative
/// placeholders; the real values are captured on hardware (spike 2.4).
#[derive(Debug, Clone, Copy)]
pub struct StabilityConfig {
    /// Consecutive identical observations required before a file is
    /// considered settled (`>= 2`).
    pub required_stable_scans: u32,
    /// Minimum wall-clock seconds the fingerprint must hold steady.
    pub quiescence_secs: u64,
}

impl Default for StabilityConfig {
    fn default() -> Self {
        Self {
            required_stable_scans: 2,
            quiescence_secs: 60,
        }
    }
}

/// Per-file tracking state.
#[derive(Debug, Clone, Copy)]
struct FileState {
    fingerprint: u64,
    first_seen_secs: u64,
    stable_scans: u32,
    emitted: bool,
}

/// Stateful cross-scan tracker. One instance lives for the lifetime of
/// the daemon; `observe` is called once per scan.
#[derive(Debug, Default)]
pub struct StabilityTracker {
    config: StabilityConfig,
    states: HashMap<String, FileState>,
}

impl StabilityTracker {
    /// Create a tracker with the given config.
    #[must_use]
    pub fn new(config: StabilityConfig) -> Self {
        Self {
            config,
            states: HashMap::new(),
        }
    }

    /// Stable identity key for a record (partition + full path).
    fn identity_key(record: &FileRecord) -> String {
        format!("{}:{}", record.partition_slot, record.path)
    }

    /// Observe a full scan's worth of records at `now_secs`, returning
    /// the indices of records that have *just* become eligible to emit
    /// (caller then does the expensive validate-and-emit). A record is
    /// returned at most once per content version.
    pub fn observe(&mut self, records: &[FileRecord], now_secs: u64) -> Vec<usize> {
        let mut eligible = Vec::new();

        for (idx, record) in records.iter().enumerate() {
            let key = Self::identity_key(record);
            let fp = fingerprint(record);

            let state = self.states.entry(key).or_insert(FileState {
                fingerprint: fp,
                first_seen_secs: now_secs,
                stable_scans: 0,
                emitted: false,
            });

            if state.fingerprint == fp {
                state.stable_scans = state.stable_scans.saturating_add(1);
            } else {
                // Content changed — reset the settle window.
                state.fingerprint = fp;
                state.first_seen_secs = now_secs;
                state.stable_scans = 1;
                state.emitted = false;
            }

            if state.emitted {
                continue;
            }
            if is_eligible(record, state, &self.config, now_secs) {
                state.emitted = true;
                eligible.push(idx);
            }
        }

        eligible
    }

    /// Number of files currently tracked (for diagnostics).
    #[must_use]
    pub fn tracked_len(&self) -> usize {
        self.states.len()
    }

    /// Re-arm every already-emitted file so the next [`Self::observe`]
    /// re-emits each one that is still eligible — i.e. replays the full
    /// currently-stable set instead of only newly-eligible files.
    ///
    /// This backs the daemon's `resync` path: the consumer (which owns the
    /// durable, rebuildable DB) asks for a replay on first connect or after
    /// an apply failure, recovering a batch that was produced (the tracker
    /// already advanced past it) but never durably committed. It only
    /// clears the `emitted` flag; the fingerprint / settle window are left
    /// intact, so a clip the car is still writing — which was never emitted
    /// — is unaffected and cannot be falsely replayed.
    pub fn arm_resync(&mut self) {
        for state in self.states.values_mut() {
            state.emitted = false;
        }
    }
}

/// Decide eligibility for a single observed record.
fn is_eligible(
    record: &FileRecord,
    state: &FileState,
    config: &StabilityConfig,
    now_secs: u64,
) -> bool {
    // (1) fully written, (2) self-consistent entry set.
    if record.valid_data_length != record.data_length || !record.set_checksum_ok {
        return false;
    }
    // (3) settled across enough scans and long enough. A clip the car
    // is still recording keeps changing its VDL/DataLength, so its
    // window keeps resetting and it never reaches this point.
    let held_secs = now_secs.saturating_sub(state.first_seen_secs);
    state.stable_scans >= config.required_stable_scans && held_secs >= config.quiescence_secs
}

/// Cheap fingerprint over the directory-entry fields that change as a
/// file is written. Excludes the path/name (that is the identity key).
fn fingerprint(record: &FileRecord) -> u64 {
    let mut h = FNV_OFFSET;
    let mut fold = |bytes: &[u8]| {
        for &b in bytes {
            h ^= u64::from(b);
            h = h.wrapping_mul(FNV_PRIME);
        }
    };
    fold(&[record.partition_slot]);
    fold(&record.dir_first_cluster.to_le_bytes());
    fold(&record.first_cluster.to_le_bytes());
    fold(&record.data_length.to_le_bytes());
    fold(&record.valid_data_length.to_le_bytes());
    fold(&[u8::from(record.no_fat_chain)]);
    fold(&[u8::from(record.set_checksum_ok)]);
    fold(&record.timestamps.create_timestamp.to_le_bytes());
    fold(&record.timestamps.modify_timestamp.to_le_bytes());
    fold(&[record.timestamps.modify_10ms]);
    h
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]
mod tests {
    use super::*;
    use teslausb_core::fs::exfat::directory::FileTimestamps;

    fn record(name: &str, vdl: u64, dlen: u64) -> FileRecord {
        FileRecord {
            partition_slot: 0,
            path: format!("TeslaCam/SavedClips/2026-06-01_20-10-53/{name}"),
            name: name.to_owned(),
            name_hash: 0,
            first_cluster: 100,
            data_length: dlen,
            valid_data_length: vdl,
            no_fat_chain: false,
            timestamps: FileTimestamps::default(),
            set_checksum_ok: true,
            dir_first_cluster: 50,
        }
    }

    fn config() -> StabilityConfig {
        StabilityConfig {
            required_stable_scans: 2,
            quiescence_secs: 10,
        }
    }

    #[test]
    fn emits_only_after_settling() {
        let mut t = StabilityTracker::new(config());
        let recs = vec![record("2026-06-01_20-10-04-front.mp4", 1000, 1000)];
        // First scan: seen once, not yet stable.
        assert!(t.observe(&recs, 0).is_empty());
        // Second scan, same content, but quiescence not yet met.
        assert!(t.observe(&recs, 5).is_empty());
        // Third scan past the quiescence window → eligible.
        assert_eq!(t.observe(&recs, 20), vec![0]);
        // Not emitted again.
        assert!(t.observe(&recs, 30).is_empty());
    }

    #[test]
    fn mid_write_vdl_lt_datalen_never_emits() {
        let mut t = StabilityTracker::new(config());
        let recs = vec![record("2026-06-01_20-10-04-front.mp4", 500, 1000)];
        for clk in [0, 20, 40, 60] {
            assert!(t.observe(&recs, clk).is_empty());
        }
    }

    #[test]
    fn pause_resume_resets_window() {
        // Tesla writes some data, pauses (looks steady briefly), then
        // resumes — the resumed write changes DataLength and must reset
        // the settle window so the unfinished clip never emits early.
        let mut t = StabilityTracker::new(config());
        let paused = vec![record("2026-06-01_20-10-04-front.mp4", 1000, 1000)];
        let resumed = vec![record("2026-06-01_20-10-04-front.mp4", 2000, 2000)];
        assert!(t.observe(&paused, 0).is_empty());
        assert!(t.observe(&paused, 5).is_empty());
        // Resume before quiescence elapsed → window resets.
        assert!(t.observe(&resumed, 8).is_empty());
        assert!(t.observe(&resumed, 12).is_empty()); // held only 4s
        // Now it truly settles.
        assert_eq!(t.observe(&resumed, 20), vec![0]);
    }

    #[test]
    fn growth_resets_settle_window() {
        let mut t = StabilityTracker::new(config());
        let small = vec![record("2026-06-01_20-10-04-front.mp4", 1000, 1000)];
        let grown = vec![record("2026-06-01_20-10-04-front.mp4", 2000, 2000)];
        t.observe(&small, 0);
        t.observe(&small, 20); // would be eligible, but then it grows…
        // Re-fetch: the file grew between the eligible scan; emulate a
        // fresh tracker timeline where growth happens before emit.
        let mut t2 = StabilityTracker::new(config());
        assert!(t2.observe(&small, 0).is_empty());
        assert!(t2.observe(&grown, 5).is_empty()); // changed → reset
        assert!(t2.observe(&grown, 12).is_empty()); // only 1 stable scan since reset window/quiescence
        assert_eq!(t2.observe(&grown, 20), vec![0]);
    }

    #[test]
    fn arm_resync_replays_currently_stable_clips() {
        let mut t = StabilityTracker::new(config());
        let recs = vec![record("2026-06-01_20-10-04-front.mp4", 1000, 1000)];
        assert!(t.observe(&recs, 0).is_empty());
        assert_eq!(t.observe(&recs, 20), vec![0]); // emitted once
        assert!(t.observe(&recs, 30).is_empty()); // not re-emitted
        // A resync re-arms the emitted flag, so the next observe replays it.
        t.arm_resync();
        assert_eq!(t.observe(&recs, 40), vec![0]);
        // And only once more — the flag is set again after the replay.
        assert!(t.observe(&recs, 50).is_empty());
    }

    #[test]
    fn arm_resync_does_not_emit_an_unsettled_clip() {
        let mut t = StabilityTracker::new(config());
        // Mid-write clip (VDL < DataLength) is never eligible.
        let recs = vec![record("2026-06-01_20-10-04-front.mp4", 500, 1000)];
        assert!(t.observe(&recs, 0).is_empty());
        assert!(t.observe(&recs, 20).is_empty());
        t.arm_resync();
        // Re-arming must not conjure an emit for a clip that never settled.
        assert!(t.observe(&recs, 40).is_empty());
    }

    /// Adversarial record with explicit completeness/consistency fields.
    fn record_full(vdl: u64, dlen: u64, checksum_ok: bool, modify_10ms: u8) -> FileRecord {
        FileRecord {
            partition_slot: 0,
            path: "TeslaCam/SavedClips/2026-06-01_20-10-53/front.mp4".to_owned(),
            name: "front.mp4".to_owned(),
            name_hash: 0,
            first_cluster: 100,
            data_length: dlen,
            valid_data_length: vdl,
            no_fat_chain: false,
            timestamps: FileTimestamps {
                modify_10ms,
                ..FileTimestamps::default()
            },
            set_checksum_ok: checksum_ok,
            dir_first_cluster: 50,
        }
    }

    /// Property fuzz: across thousands of randomized write timelines — grow,
    /// stall mid-write (`VDL` < `DataLength`), finalize, flip checksum, touch
    /// the modify clock, advance the wall clock arbitrarily — the gate must
    /// NEVER emit a record that is not fully written + self-consistent, never
    /// emit before the quiescence floor of the *current* fingerprint streak,
    /// never emit on the scan a change is observed, and never double-emit
    /// within a streak. A deterministic LCG keeps any failure reproducible.
    #[test]
    fn fuzz_gate_never_false_stable() {
        let mut rng: u64 = 0x1234_5678_9abc_def1;
        let next = |r: &mut u64| -> u32 {
            *r = r
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            (*r >> 33) as u32
        };
        let cfg = StabilityConfig {
            required_stable_scans: 2,
            quiescence_secs: 30,
        };

        for _trial in 0..3000 {
            let mut t = StabilityTracker::new(cfg);
            let mut last_fp: Option<u64> = None;
            let mut streak_start: u64 = 0;
            let mut emitted_for_fp = false;
            let mut clock: u64 = 0;
            let mut dlen: u64 = 1000;
            let mut vdl: u64 = 1000;
            let mut checksum_ok = true;
            let mut modify_10ms: u8 = 0;

            for _scan in 0..24 {
                clock += u64::from(next(&mut rng) % 40);
                match next(&mut rng) % 6 {
                    0 => {
                        dlen += 1 + u64::from(next(&mut rng) % 500);
                        vdl = dlen; // grow, fully written
                    }
                    1 => {
                        dlen += 1 + u64::from(next(&mut rng) % 500);
                        vdl = dlen.saturating_sub(1 + u64::from(next(&mut rng) % 200)); // mid-write
                    }
                    2 => vdl = dlen, // finalize a stalled write
                    3 => checksum_ok = next(&mut rng) % 2 == 0,
                    4 => modify_10ms = (next(&mut rng) % 100) as u8,
                    _ => {} // no change this scan
                }

                let recs = vec![record_full(vdl, dlen, checksum_ok, modify_10ms)];
                let fp = fingerprint(&recs[0]);
                let fp_changed = last_fp != Some(fp);
                if fp_changed {
                    last_fp = Some(fp);
                    streak_start = clock;
                    emitted_for_fp = false;
                }

                let out = t.observe(&recs, clock);
                if fp_changed {
                    assert!(out.is_empty(), "emitted on the scan content changed");
                }
                if let Some(&idx) = out.first() {
                    let r = &recs[idx];
                    assert!(
                        r.valid_data_length == r.data_length && r.set_checksum_ok,
                        "FALSE-STABLE: emitted an incomplete/inconsistent record \
                         (vdl={}, dlen={}, checksum_ok={})",
                        r.valid_data_length,
                        r.data_length,
                        r.set_checksum_ok,
                    );
                    assert!(
                        clock - streak_start >= cfg.quiescence_secs,
                        "emitted before quiescence floor (held {}s < {}s)",
                        clock - streak_start,
                        cfg.quiescence_secs,
                    );
                    assert!(!emitted_for_fp, "double-emit within one fingerprint streak");
                    emitted_for_fp = true;
                }
            }
        }
    }
}
