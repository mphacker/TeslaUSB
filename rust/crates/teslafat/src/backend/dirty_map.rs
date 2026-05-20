//! Tracks which bytes of a region the kernel has written, so the
//! synth read path can overlay only the dirty bytes back onto a
//! read (Phase 3.5f).
//!
//! ## Why
//!
//! `SynthBackend` serves reads from an immutable layout computed
//! at daemon startup. The write state machines
//! ([`super::exfat_write::ExfatWriteState`],
//! [`super::fat32_write::Fat32WriteState`]) accumulate kernel
//! writes to the FAT region and to directory clusters in their
//! own in-memory buffers, but those buffers start out zero — the
//! synth's pre-existing FAT entries and pre-existing directory
//! entries are not copied into them.
//!
//! On a subsequent read of a partially-written FAT region or
//! directory cluster, we therefore can **not** blindly copy the
//! write-state buffer over the synth bytes (that would zero out
//! the entries for pre-existing files). We must overlay **only**
//! the bytes the kernel actually wrote.
//!
//! `DirtyByteMap` is the data structure that records, for one
//! region with bytes addressed `[0, region_len)`, the disjoint
//! intervals of bytes that have been written. The write-state
//! machines `mark(start, len)` on every `apply_*_write`; the
//! synth read path calls [`DirtyByteMap::for_each_overlap`] to
//! discover the dirty sub-ranges of the read window.
//!
//! ## Representation
//!
//! Disjoint, sorted intervals stored as a `BTreeMap<usize, usize>`
//! keyed by `start`, value `end`. Intervals are merged on insert
//! whenever they touch or overlap, so iteration cost stays bounded
//! by the number of truly disjoint write extents (rarely > 100
//! for a typical small-file workload).

use std::collections::BTreeMap;

/// Sparse record of "which bytes in `[0, region_len)` are dirty".
///
/// Coordinates are byte offsets inside one region (e.g., the FAT
/// region, or one directory's flat cluster buffer). The map does
/// not know `region_len` — out-of-range writes are the caller's
/// responsibility to clamp (the write-state machines already do).
#[derive(Default, Debug, Clone)]
pub struct DirtyByteMap {
    /// `start -> end_exclusive`. Invariant: intervals are
    /// non-empty (`end > start`), non-overlapping, and
    /// non-adjacent (a new write that touches or overlaps an
    /// existing run is merged at insert time).
    runs: BTreeMap<usize, usize>,
}

impl DirtyByteMap {
    /// Create a fresh map with no dirty bytes.
    #[must_use]
    pub fn new() -> Self {
        Self {
            runs: BTreeMap::new(),
        }
    }

    /// Record `[start, start + len)` as dirty, merging with any
    /// existing runs it touches or overlaps. `len == 0` is a no-op.
    pub fn mark(&mut self, start: usize, len: usize) {
        if len == 0 {
            return;
        }
        let mut new_start = start;
        let mut new_end = start.saturating_add(len);

        // Find any run that overlaps or touches the new range and
        // absorb it. We walk forward from the candidate start.
        let mut to_remove: Vec<usize> = Vec::new();
        // Predecessor: the run with the largest start <= new_start.
        if let Some((&pred_start, &pred_end)) = self.runs.range(..=new_start).next_back() {
            if pred_end >= new_start {
                new_start = pred_start.min(new_start);
                new_end = pred_end.max(new_end);
                to_remove.push(pred_start);
            }
        }
        // Forward sweep: runs whose start is within new range.
        for (&s, &e) in self.runs.range(new_start..) {
            if s > new_end {
                break;
            }
            new_end = new_end.max(e);
            to_remove.push(s);
        }
        for s in to_remove {
            self.runs.remove(&s);
        }
        self.runs.insert(new_start, new_end);
    }

    /// Iterate over the dirty sub-ranges that overlap
    /// `[read_start, read_start + read_len)`. The callback is
    /// invoked with `(dirty_start, dirty_end_exclusive)` clamped
    /// to the read window. Used by the synth read path to copy
    /// only kernel-written bytes back over a synth-produced read.
    pub fn for_each_overlap(
        &self,
        read_start: usize,
        read_len: usize,
        mut f: impl FnMut(usize, usize),
    ) {
        if read_len == 0 {
            return;
        }
        let read_end = read_start.saturating_add(read_len);
        // Predecessor: largest start <= read_start.
        if let Some((&pred_start, &pred_end)) = self.runs.range(..=read_start).next_back() {
            if pred_end > read_start {
                let s = pred_start.max(read_start);
                let e = pred_end.min(read_end);
                if e > s {
                    f(s, e);
                }
            }
        }
        for (&s, &e) in self.runs.range(read_start + 1..) {
            if s >= read_end {
                break;
            }
            let clamped_end = e.min(read_end);
            if clamped_end > s {
                f(s, clamped_end);
            }
        }
    }

    /// Number of disjoint dirty runs (for tests / diagnostics).
    #[must_use]
    pub fn run_count(&self) -> usize {
        self.runs.len()
    }

    /// Sum of all dirty bytes (for tests / diagnostics).
    #[must_use]
    pub fn dirty_byte_count(&self) -> usize {
        self.runs.iter().map(|(s, e)| e.saturating_sub(*s)).sum()
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::panic, clippy::unwrap_used)]
mod tests {
    use super::*;

    fn collect_overlaps(map: &DirtyByteMap, start: usize, len: usize) -> Vec<(usize, usize)> {
        let mut out = Vec::new();
        map.for_each_overlap(start, len, |s, e| out.push((s, e)));
        out
    }

    #[test]
    fn empty_map_has_no_overlaps() {
        let m = DirtyByteMap::new();
        assert_eq!(m.run_count(), 0);
        assert_eq!(collect_overlaps(&m, 0, 1024), vec![]);
    }

    #[test]
    fn zero_length_mark_is_noop() {
        let mut m = DirtyByteMap::new();
        m.mark(100, 0);
        assert_eq!(m.run_count(), 0);
    }

    #[test]
    fn single_mark_appears_in_overlap_query() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 5);
        assert_eq!(m.run_count(), 1);
        assert_eq!(m.dirty_byte_count(), 5);
        assert_eq!(collect_overlaps(&m, 0, 100), vec![(10, 15)]);
    }

    #[test]
    fn disjoint_marks_stay_disjoint() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 5);
        m.mark(100, 10);
        assert_eq!(m.run_count(), 2);
        assert_eq!(collect_overlaps(&m, 0, 200), vec![(10, 15), (100, 110)]);
    }

    #[test]
    fn touching_marks_merge() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 5); // [10, 15)
        m.mark(15, 5); // [15, 20)  — touches at 15
        assert_eq!(m.run_count(), 1);
        assert_eq!(collect_overlaps(&m, 0, 100), vec![(10, 20)]);
    }

    #[test]
    fn overlapping_marks_merge() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 10); // [10, 20)
        m.mark(15, 10); // [15, 25)
        assert_eq!(m.run_count(), 1);
        assert_eq!(collect_overlaps(&m, 0, 100), vec![(10, 25)]);
    }

    #[test]
    fn new_mark_can_swallow_many_runs() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 5);
        m.mark(20, 5);
        m.mark(30, 5);
        m.mark(40, 5);
        assert_eq!(m.run_count(), 4);
        // Span across all four.
        m.mark(10, 40); // [10, 50)
        assert_eq!(m.run_count(), 1);
        assert_eq!(collect_overlaps(&m, 0, 100), vec![(10, 50)]);
    }

    #[test]
    fn predecessor_overlap_extends_existing_run() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 5); // [10, 15)
        // New write starts at 12, ends at 30 — predecessor [10, 15)
        // is fully absorbed.
        m.mark(12, 18); // [12, 30)
        assert_eq!(m.run_count(), 1);
        assert_eq!(collect_overlaps(&m, 0, 100), vec![(10, 30)]);
    }

    #[test]
    fn for_each_overlap_clamps_to_read_window() {
        let mut m = DirtyByteMap::new();
        m.mark(0, 1000); // [0, 1000)
        // Read window [100, 200) — overlap should be clamped.
        assert_eq!(collect_overlaps(&m, 100, 100), vec![(100, 200)]);
    }

    #[test]
    fn for_each_overlap_skips_non_overlapping_runs() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 10);
        m.mark(100, 10);
        m.mark(200, 10);
        // Read window only covers middle run.
        assert_eq!(collect_overlaps(&m, 50, 100), vec![(100, 110)]);
    }

    #[test]
    fn for_each_overlap_emits_multiple_runs_partially() {
        let mut m = DirtyByteMap::new();
        m.mark(10, 10); // [10, 20)
        m.mark(50, 10); // [50, 60)
        m.mark(90, 10); // [90, 100)
        // Window [15, 95) clips both ends.
        assert_eq!(
            collect_overlaps(&m, 15, 80),
            vec![(15, 20), (50, 60), (90, 95)]
        );
    }

    #[test]
    fn many_small_writes_then_one_big_consolidates() {
        let mut m = DirtyByteMap::new();
        // Simulate kernel writing FAT entries one at a time
        // (4 bytes each at adjacent offsets).
        for i in 0..100 {
            m.mark(i * 4, 4);
        }
        assert_eq!(m.run_count(), 1);
        assert_eq!(m.dirty_byte_count(), 400);
        assert_eq!(collect_overlaps(&m, 0, 1000), vec![(0, 400)]);
    }
}
