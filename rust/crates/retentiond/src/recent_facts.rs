//! Pure `RecentClips` fact gathering from injected directory observations.
//!
//! This module performs no filesystem I/O. It consumes stat-only filename
//! observations, tracks cross-pass stability, and emits complete segment facts
//! suitable for a later copy/register driver.

use std::collections::{BTreeMap, HashMap, HashSet};

/// One observed file directly inside the `RecentClips` source directory.
///
/// Stat-only: NO content hash here (the rolling buffer is large and rewritten
/// continuously; hashing every file every pass is too expensive — the real
/// content hash is computed once at copy time by the `ArchiveStore` read-back).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentFileObservation {
    /// File name only, e.g. `2026-06-19_10-00-00-front.mp4`.
    pub name: String,
    /// File size in bytes.
    pub size: u64,
    /// Modification time in milliseconds.
    pub mtime_ms: i64,
}

/// Lists the direct files in the `RecentClips` source directory for one slot.
///
/// The live filesystem implementation lives in `live.rs` (a later lane) — this
/// module only consumes injected observations so the gatherer is pure and
/// unit-testable.
pub trait RecentDirReader {
    /// List the direct regular files in the `RecentClips` directory for `slot`.
    ///
    /// # Errors
    /// Returns an error when the underlying listing fails.
    fn list(&self, slot: u8) -> std::io::Result<Vec<RecentFileObservation>>;
}

/// One camera angle within a complete `RecentClips` segment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentAngleFact {
    /// Camera token verbatim, e.g. `front`, `left_repeater`.
    pub camera: String,
    /// Source-root-relative path to copy FROM.
    pub src_rel: String,
    /// Source file size in bytes.
    pub size_bytes: u64,
    /// Source file mtime in milliseconds.
    pub mtime_ms: i64,
}

/// A complete `RecentClips` segment, ready for copy + register.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentSegmentFact {
    /// Canonical dedup key: `{slot}:{parent}/{timestamp}` (numeric slot).
    pub canonical_key: String,
    /// Partition label: `slot{n}`.
    pub partition: String,
    /// 19-char clip timestamp prefix `YYYY-MM-DD_HH-MM-SS`.
    pub timestamp: String,
    /// Relative capture ordering key derived from timestamp (UTC civil parse).
    pub capture_ms: i64,
    /// Deterministic archive-root-relative destination directory.
    ///
    /// Scheme: `RecentClips/{YYYY-MM-DD}/{YYYY-MM-DD_HH-MM-SS}`.
    pub archive_item_path: String,
    /// Segment angles sorted by camera for deterministic output.
    pub angles: Vec<RecentAngleFact>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedClipName {
    timestamp: String,
    camera: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ObservedAngle {
    name: String,
    camera: String,
    src_rel: String,
    size_bytes: u64,
    mtime_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct SegmentState {
    digest: u128,
    stable_passes: u32,
    emitted: bool,
}

/// Tracks per-segment cross-pass stability for `RecentClips`.
///
/// Semantics: a segment is emitted exactly once per stable digest — the pass
/// where it first reaches the required streak. Subsequent unchanged passes do
/// not re-emit. If the observed group changes (file add/remove or size/mtime
/// change), the streak resets and emission eligibility is reset for the new
/// digest. Per-segment state is auto-pruned when a segment rotates off the
/// source (see [`Self::observe`]); callers do NOT drop state after archiving.
/// [`Self::forget`] exists only as an explicit re-evaluation escape hatch.
#[derive(Debug)]
pub struct RecentFactsGatherer {
    required_stable_passes: u32,
    states: HashMap<String, SegmentState>,
}

impl RecentFactsGatherer {
    /// Create a gatherer requiring at least two stable consecutive passes.
    #[must_use]
    pub fn new(required_stable_passes: u32) -> Self {
        Self {
            required_stable_passes: required_stable_passes.max(2),
            states: HashMap::new(),
        }
    }

    /// Observe one pass and emit segments that became complete in this pass.
    ///
    /// The emitted set is deterministic: segments are sorted by `capture_ms`,
    /// then `canonical_key`; angles are sorted by camera.
    ///
    /// # Errors
    /// Propagates any listing error from `reader.list(slot)`.
    pub fn observe(
        &mut self,
        slot: u8,
        recentclips_dir: &str,
        reader: &dyn RecentDirReader,
    ) -> std::io::Result<Vec<RecentSegmentFact>> {
        let parent = normalize_parent(recentclips_dir);
        let observations = reader.list(slot)?;

        let mut grouped: BTreeMap<String, Vec<ObservedAngle>> = BTreeMap::new();
        for file in observations {
            if !is_safe_filename(&file.name) {
                continue;
            }
            let Some(parsed) = parse_clip_name(&file.name) else {
                continue;
            };
            let Some(camera) = parsed.camera else {
                continue;
            };

            let src_rel = join_rel(&parent, &file.name);
            grouped
                .entry(parsed.timestamp)
                .or_default()
                .push(ObservedAngle {
                    name: file.name,
                    camera,
                    src_rel,
                    size_bytes: file.size,
                    mtime_ms: file.mtime_ms,
                });
        }

        let mut out = Vec::new();
        let mut seen_keys: HashSet<String> = HashSet::new();
        for (timestamp, angles) in grouped {
            if angles.is_empty() {
                continue;
            }

            let canonical_key = format!("{slot}:{parent}/{timestamp}");
            seen_keys.insert(canonical_key.clone());
            let digest = digest_group(&angles);

            let state = self.states.entry(canonical_key.clone()).or_insert(SegmentState {
                digest,
                stable_passes: 0,
                emitted: false,
            });

            if state.digest == digest {
                state.stable_passes = state.stable_passes.saturating_add(1);
            } else {
                state.digest = digest;
                state.stable_passes = 1;
                state.emitted = false;
            }

            if state.emitted || state.stable_passes < self.required_stable_passes {
                continue;
            }

            let Some(capture_ms) = civil_timestamp_to_ms(&timestamp) else {
                continue;
            };
            let Some(archive_item_path) = archive_item_path_for_timestamp(&timestamp) else {
                continue;
            };

            let mut angle_facts: Vec<RecentAngleFact> = angles
                .into_iter()
                .map(|a| RecentAngleFact {
                    camera: a.camera,
                    src_rel: a.src_rel,
                    size_bytes: a.size_bytes,
                    mtime_ms: a.mtime_ms,
                })
                .collect();
            angle_facts.sort_by(|a, b| a.camera.cmp(&b.camera).then(a.src_rel.cmp(&b.src_rel)));

            out.push(RecentSegmentFact {
                canonical_key: canonical_key.clone(),
                partition: partition_label(slot),
                timestamp,
                capture_ms,
                archive_item_path,
                angles: angle_facts,
            });
            state.emitted = true;
        }

        out.sort_by(|a, b| {
            a.capture_ms
                .cmp(&b.capture_ms)
                .then(a.canonical_key.cmp(&b.canonical_key))
        });

        // Bound memory the rotation way: drop per-segment state for this slot's
        // segments that are no longer visible (the car overwrote them). This is
        // the primary memory bound, so the driver never needs to `forget` an
        // archived-but-still-present segment (which would re-emit it). Keys for
        // OTHER slots are preserved (one gatherer may serve multiple slots).
        let slot_prefix = format!("{slot}:");
        self.states
            .retain(|key, _| seen_keys.contains(key) || !key.starts_with(slot_prefix.as_str()));
        Ok(out)
    }

    /// Drop tracking for one segment key — an explicit escape hatch that forces
    /// the key to be re-evaluated on the next observation.
    ///
    /// This is NOT the memory-bounding mechanism: [`Self::observe`] already
    /// prunes segments that rotate off the source. Note that forgetting a
    /// segment whose files are still visible will let it emit again on the next
    /// stable streak, so callers should not `forget` a segment merely because it
    /// was archived (the copy/register path is idempotent regardless).
    pub fn forget(&mut self, canonical_key: &str) {
        self.states.remove(canonical_key);
    }

    /// Number of segment keys currently tracked.
    #[must_use]
    pub fn tracked_len(&self) -> usize {
        self.states.len()
    }
}

const TIMESTAMP_LEN: usize = 19;

fn parse_clip_name(name: &str) -> Option<ParsedClipName> {
    let stem = name.strip_suffix(".mp4")?;
    // `.get(..TIMESTAMP_LEN)` yields `None` if the stem is shorter than the
    // prefix OR if byte `TIMESTAMP_LEN` is not a UTF-8 char boundary (a
    // multibyte filename), so a malformed non-ASCII name is skipped rather
    // than panicking in `split_at`.
    let timestamp = stem.get(..TIMESTAMP_LEN)?;
    if !is_timestamp(timestamp) {
        return None;
    }
    let rest = stem.get(TIMESTAMP_LEN..)?;
    let camera = match rest.strip_prefix('-') {
        Some(cam) if !cam.is_empty() => Some(cam.to_owned()),
        _ if rest.is_empty() => None,
        _ => return None,
    };
    Some(ParsedClipName {
        timestamp: timestamp.to_owned(),
        camera,
    })
}

fn is_timestamp(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.len() != TIMESTAMP_LEN {
        return false;
    }
    for (idx, &ch) in bytes.iter().enumerate() {
        let is_valid = if matches!(idx, 4 | 7 | 13 | 16) {
            ch == b'-'
        } else if idx == 10 {
            ch == b'_'
        } else {
            ch.is_ascii_digit()
        };
        if !is_valid {
            return false;
        }
    }
    true
}

/// Parse `YYYY-MM-DD_HH-MM-SS` as UTC civil time and return epoch milliseconds.
///
/// This is a relative ordering key only; no timezone/DST wall-clock semantics
/// are inferred.
#[must_use]
pub fn civil_timestamp_to_ms(ts: &str) -> Option<i64> {
    if !is_timestamp(ts) {
        return None;
    }
    let bytes = ts.as_bytes();
    let year = parse_digits(bytes, 0, 4)?;
    let month = parse_digits(bytes, 5, 2)?;
    let day = parse_digits(bytes, 8, 2)?;
    let hour = parse_digits(bytes, 11, 2)?;
    let minute = parse_digits(bytes, 14, 2)?;
    let second = parse_digits(bytes, 17, 2)?;

    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    if !(0..=23).contains(&hour) || !(0..=59).contains(&minute) || !(0..=59).contains(&second) {
        return None;
    }
    if day > days_in_month(year, month)? {
        return None;
    }

    let days = days_from_civil(year, month, day);
    let seconds = days
        .checked_mul(86_400)?
        .checked_add(hour.checked_mul(3_600)?)?
        .checked_add(minute.checked_mul(60)?)?
        .checked_add(second)?;
    seconds.checked_mul(1_000)
}

fn parse_digits(bytes: &[u8], start: usize, len: usize) -> Option<i64> {
    let end = start.checked_add(len)?;
    let mut value = 0i64;
    for ch in bytes.get(start..end)? {
        if !ch.is_ascii_digit() {
            return None;
        }
        value = value
            .checked_mul(10)?
            .checked_add(i64::from(ch.saturating_sub(b'0')))?;
    }
    Some(value)
}

fn days_in_month(year: i64, month: i64) -> Option<i64> {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => Some(31),
        4 | 6 | 9 | 11 => Some(30),
        2 => {
            let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
            Some(if leap { 29 } else { 28 })
        }
        _ => None,
    }
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - i64::from(month <= 2);
    let era = if adjusted_year >= 0 {
        adjusted_year
    } else {
        adjusted_year - 399
    } / 400;
    let yoe = adjusted_year - era * 400;
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + day_of_year;
    era * 146_097 + doe - 719_468
}

fn normalize_parent(parent: &str) -> String {
    parent
        .trim_end_matches(['/', '\\'])
        .replace('\\', "/")
}

fn join_rel(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_owned()
    } else {
        format!("{parent}/{name}")
    }
}

fn partition_label(slot: u8) -> String {
    format!("slot{slot}")
}

fn archive_item_path_for_timestamp(timestamp: &str) -> Option<String> {
    let date = timestamp.get(0..10)?;
    Some(format!("RecentClips/{date}/{timestamp}"))
}

fn digest_group(angles: &[ObservedAngle]) -> u128 {
    const OFFSET: u128 = 0x6c62_272e_07bb_0142_62b8_2175_6295_c58d;
    const PRIME: u128 = 0x0000_0000_0100_0000_0000_0000_0000_013b;

    let mut ordered: Vec<&ObservedAngle> = angles.iter().collect();
    ordered.sort_by(|a, b| {
        a.name
            .cmp(&b.name)
            .then(a.size_bytes.cmp(&b.size_bytes))
            .then(a.mtime_ms.cmp(&b.mtime_ms))
    });

    let mut hash = OFFSET;
    let mut fold = |bytes: &[u8]| {
        for &byte in bytes {
            hash ^= u128::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
    };

    for angle in ordered {
        fold(angle.name.as_bytes());
        fold(&[0xff]);
        fold(&angle.size_bytes.to_le_bytes());
        fold(&angle.mtime_ms.to_le_bytes());
        fold(&[0xfe]);
    }
    hash
}

fn is_safe_filename(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.contains('\\')
        && !name.as_bytes().contains(&0)
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use std::{cell::RefCell, io};

    use super::{
        RecentDirReader, RecentFactsGatherer, RecentFileObservation, civil_timestamp_to_ms,
    };

    #[derive(Default)]
    struct FakeReader {
        files: RefCell<Vec<RecentFileObservation>>,
    }

    impl FakeReader {
        fn set_files(&self, files: Vec<RecentFileObservation>) {
            *self.files.borrow_mut() = files;
        }
    }

    impl RecentDirReader for FakeReader {
        fn list(&self, _slot: u8) -> io::Result<Vec<RecentFileObservation>> {
            Ok(self.files.borrow().clone())
        }
    }

    fn obs(name: &str, size: u64, mtime_ms: i64) -> RecentFileObservation {
        RecentFileObservation {
            name: name.to_owned(),
            size,
            mtime_ms,
        }
    }

    #[test]
    fn canonical_key_and_partition_match_scannerd_format() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(vec![
            obs("2026-06-19_10-00-00-front.mp4", 10, 1),
            obs("2026-06-19_10-00-00-back.mp4", 20, 1),
        ]);

        let first = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert!(first.is_empty());

        let second = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(second.len(), 1);
        let fact = &second[0];
        assert_eq!(
            fact.canonical_key,
            "0:TeslaCam/RecentClips/2026-06-19_10-00-00"
        );
        assert_eq!(fact.partition, "slot0");
        assert_eq!(
            fact.archive_item_path,
            "RecentClips/2026-06-19/2026-06-19_10-00-00"
        );
    }

    #[test]
    fn variable_camera_sets_are_supported() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(vec![
            obs("2026-06-19_10-00-00-front.mp4", 10, 1),
            obs("2026-06-19_10-00-00-back.mp4", 11, 1),
            obs("2026-06-19_10-00-00-left_repeater.mp4", 12, 1),
            obs("2026-06-19_10-01-00-front.mp4", 20, 1),
            obs("2026-06-19_10-01-00-back.mp4", 21, 1),
            obs("2026-06-19_10-01-00-left_repeater.mp4", 22, 1),
            obs("2026-06-19_10-01-00-right_repeater.mp4", 23, 1),
            obs("2026-06-19_10-01-00-left_pillar.mp4", 24, 1),
            obs("2026-06-19_10-01-00-right_pillar.mp4", 25, 1),
        ]);

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let facts = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(facts.len(), 2);
        assert_eq!(facts[0].angles.len(), 3);
        assert_eq!(facts[1].angles.len(), 6);
    }

    #[test]
    fn segment_becomes_complete_only_on_second_steady_pass() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(vec![obs("2026-06-19_10-00-00-front.mp4", 10, 1)]);

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let second = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(second.len(), 1);
    }

    #[test]
    fn still_writing_and_late_arrival_reset_streak() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();

        reader.set_files(vec![obs("2026-06-19_10-00-00-front.mp4", 10, 1)]);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        reader.set_files(vec![obs("2026-06-19_10-00-00-front.mp4", 11, 1)]);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        reader.set_files(vec![obs("2026-06-19_10-00-00-front.mp4", 12, 1)]);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        let stable = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(stable.len(), 1);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        reader.set_files(vec![
            obs("2026-06-19_10-00-00-front.mp4", 12, 1),
            obs("2026-06-19_10-00-00-back.mp4", 8, 1),
        ]);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let after_late_arrival = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(after_late_arrival.len(), 1);
        assert_eq!(after_late_arrival[0].angles.len(), 2);
    }

    #[test]
    fn complete_segment_emits_once_and_forget_allows_reemit_after_reappearance() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        let files = vec![obs("2026-06-19_10-00-00-front.mp4", 10, 1)];
        reader.set_files(files.clone());

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let emitted = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(emitted.len(), 1);
        let key = emitted[0].canonical_key.clone();

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        gatherer.forget(&key);
        reader.set_files(Vec::new());
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        reader.set_files(files);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let re_emitted = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(re_emitted.len(), 1);
    }

    #[test]
    fn invalid_and_malicious_names_are_skipped() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        let nul_name = "2026-06-19_10-00-00-back.mp4\0evil";
        reader.set_files(vec![
            obs("2026-06-19_10-00-00-front.mp4", 10, 1),
            obs("notes.txt", 10, 1),
            obs("2026-06-19_10-00-00.mp4", 10, 1),
            obs("2026-06-19_10-00-00-.mp4", 10, 1),
            obs("2026-13-99_77-88-99-side.mp4", 10, 1),
            obs("2026-06-19_10-00-00/front.mp4", 10, 1),
            obs("2026-06-19_10-00-00\\front.mp4", 10, 1),
            obs("..", 1, 1),
            obs(".", 1, 1),
            obs(nul_name, 10, 1),
        ]);

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let facts = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].angles.len(), 1);
        assert_eq!(facts[0].angles[0].camera, "front");
    }

    #[test]
    fn civil_timestamp_ordering_and_validation() {
        let a = civil_timestamp_to_ms("2026-06-19_10-00-00").unwrap();
        let b = civil_timestamp_to_ms("2026-06-19_10-00-01").unwrap();
        let c = civil_timestamp_to_ms("2026-06-19_10-01-00").unwrap();
        let d = civil_timestamp_to_ms("2026-06-20_00-00-00").unwrap();
        assert!(a < b && b < c && c < d);

        assert!(civil_timestamp_to_ms("2026-06-19T10-00-00").is_none());
        assert!(civil_timestamp_to_ms("2026-02-30_00-00-00").is_none());
        assert!(civil_timestamp_to_ms("2026-13-01_00-00-00").is_none());
        assert!(civil_timestamp_to_ms("not-a-timestamp").is_none());
    }

    #[test]
    fn output_order_is_deterministic_and_angles_are_sorted() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(vec![
            obs("2026-06-19_10-01-00-right_repeater.mp4", 1, 1),
            obs("2026-06-19_10-01-00-front.mp4", 1, 1),
            obs("2026-06-19_10-01-00-back.mp4", 1, 1),
            obs("2026-06-19_10-00-00-left_repeater.mp4", 1, 1),
            obs("2026-06-19_10-00-00-front.mp4", 1, 1),
            obs("2026-06-19_10-00-00-back.mp4", 1, 1),
        ]);

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let facts = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(facts.len(), 2);
        assert_eq!(facts[0].timestamp, "2026-06-19_10-00-00");
        assert_eq!(facts[1].timestamp, "2026-06-19_10-01-00");

        let cams: Vec<&str> = facts[1].angles.iter().map(|a| a.camera.as_str()).collect();
        assert_eq!(cams, vec!["back", "front", "right_repeater"]);
    }

    #[test]
    fn non_ascii_filename_is_skipped_without_panic() {
        // 10 × 'é' is a 20-byte stem; byte 19 falls inside a multibyte char, so
        // a naive `split_at(19)` would panic. It must be skipped instead.
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        reader.set_files(vec![
            obs("éééééééééé.mp4", 10, 1),
            obs("2026-06-19_10-00-00-front.mp4", 10, 1),
        ]);

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        let facts = gatherer.observe(0, "TeslaCam/RecentClips", &reader).unwrap();
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].angles.len(), 1);
    }

    #[test]
    fn absent_segment_is_pruned_and_reemits_on_rotation_back() {
        let mut gatherer = RecentFactsGatherer::new(2);
        let reader = FakeReader::default();
        let files = vec![obs("2026-06-19_10-00-00-front.mp4", 10, 1)];
        reader.set_files(files.clone());

        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        assert_eq!(
            gatherer
                .observe(0, "TeslaCam/RecentClips", &reader)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(gatherer.tracked_len(), 1);

        // Still present on later passes → never re-emits (no `forget` needed).
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());

        // Rotates off the car → state is auto-pruned (memory bounded).
        reader.set_files(Vec::new());
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        assert_eq!(gatherer.tracked_len(), 0);

        // Reappears (defensive) → treated as new and re-emits after the streak.
        reader.set_files(files);
        assert!(gatherer
            .observe(0, "TeslaCam/RecentClips", &reader)
            .unwrap()
            .is_empty());
        assert_eq!(
            gatherer
                .observe(0, "TeslaCam/RecentClips", &reader)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn prune_is_scoped_to_the_observed_slot() {
        // One gatherer serving two slots: observing slot 1 must not drop slot 0's
        // tracked state.
        let mut gatherer = RecentFactsGatherer::new(2);
        let slot0 = FakeReader::default();
        slot0.set_files(vec![obs("2026-06-19_10-00-00-front.mp4", 10, 1)]);
        let slot1 = FakeReader::default();
        slot1.set_files(vec![obs("2026-06-19_11-00-00-front.mp4", 10, 1)]);

        gatherer.observe(0, "TeslaCam/RecentClips", &slot0).unwrap();
        gatherer.observe(1, "TeslaCam/RecentClips", &slot1).unwrap();
        assert_eq!(gatherer.tracked_len(), 2);

        // Re-observe only slot 1 → slot 0's state is retained.
        gatherer.observe(1, "TeslaCam/RecentClips", &slot1).unwrap();
        assert_eq!(gatherer.tracked_len(), 2);
    }
}
