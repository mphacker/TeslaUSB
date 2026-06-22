//! MP4 playability probe for landed archive angle files.
//!
//! This is a bounded structural check: a landed file is considered playable
//! when it has top-level `ftyp`, `moov`, and `mdat` boxes, and the
//! `moov/trak/mdia/mdhd` chain parses with `timescale > 0`.

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;

use teslausb_core::sei::mp4::{find_box, find_box_path, parse_mdhd};

const MAX_TOP_LEVEL_BOXES: usize = 1024;
const MAX_MOOV_BODY: u64 = 64 * 1024 * 1024;

/// Archive-angle container playability verdict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ArchivePlayability {
    /// Structurally complete MP4 container.
    Playable,
    /// Structurally incomplete/unusable MP4 container.
    Unplayable(UnplayableReason),
}

/// Why a landed archive-angle file is structurally unplayable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnplayableReason {
    /// Missing top-level `ftyp`.
    NoFtyp,
    /// Missing top-level `moov`.
    NoMoov,
    /// `moov` exists but `moov/trak/mdia/mdhd` is missing/unparseable/zero-timescale.
    MoovUnparseable,
    /// Missing top-level `mdat`.
    NoMdat,
    /// Top-level `moov` declares an extent beyond EOF.
    MoovExtentBeyondEof,
    /// A top-level non-`moov` box declares an extent beyond EOF.
    MalformedTopLevelBox,
    /// `moov` body exceeds the configured bounded parse cap.
    MoovTooLarge,
    /// Top-level box walk exceeded scan cap.
    ScanLimit,
}

/// Probe a landed destination file for structural MP4 completeness.
///
/// This function is memory-bounded: it walks top-level boxes via headers and
/// reads only the `moov` body when present (bounded by [`MAX_MOOV_BODY`]).
///
/// # Errors
///
/// Returns [`io::Error`] only for genuine I/O failures (open/seek/read/metadata).
/// Structural incompleteness is reported as [`ArchivePlayability::Unplayable`].
pub fn probe_file_playability(path: &Path) -> io::Result<ArchivePlayability> {
    let mut file = File::open(path)?;
    let file_len = file.metadata()?.len();

    let mut has_ftyp = false;
    let mut has_moov = false;
    let mut moov_parseable = false;
    let mut has_mdat = false;

    let mut pos = 0_u64;
    let mut boxes_walked = 0_usize;
    while pos.saturating_add(8) <= file_len {
        boxes_walked = boxes_walked.saturating_add(1);
        if boxes_walked > MAX_TOP_LEVEL_BOXES {
            return Ok(ArchivePlayability::Unplayable(UnplayableReason::ScanLimit));
        }

        let header = match read_top_level_box_header(&mut file, pos, file_len)? {
            HeaderScan::End => break,
            HeaderScan::Malformed => {
                return Ok(ArchivePlayability::Unplayable(
                    UnplayableReason::MalformedTopLevelBox,
                ));
            }
            HeaderScan::Header(header) => header,
        };

        if header.box_end > file_len {
            return Ok(ArchivePlayability::Unplayable(
                if header.box_type == *b"moov" {
                    UnplayableReason::MoovExtentBeyondEof
                } else {
                    UnplayableReason::MalformedTopLevelBox
                },
            ));
        }

        if header.box_type == *b"ftyp" {
            has_ftyp = true;
        } else if header.box_type == *b"mdat" {
            has_mdat = true;
        } else if header.box_type == *b"moov" {
            has_moov = true;
            match probe_moov_parseable(&mut file, pos, header.header_size, header.box_size)? {
                MoovProbeResult::Parseable => {
                    moov_parseable = true;
                }
                MoovProbeResult::Unparseable => {}
                MoovProbeResult::TooLarge => {
                    return Ok(ArchivePlayability::Unplayable(
                        UnplayableReason::MoovTooLarge,
                    ));
                }
            }
        }

        pos = header.box_end;
    }

    if !has_ftyp {
        return Ok(ArchivePlayability::Unplayable(UnplayableReason::NoFtyp));
    }
    if !has_moov {
        return Ok(ArchivePlayability::Unplayable(UnplayableReason::NoMoov));
    }
    if !moov_parseable {
        return Ok(ArchivePlayability::Unplayable(
            UnplayableReason::MoovUnparseable,
        ));
    }
    if !has_mdat {
        return Ok(ArchivePlayability::Unplayable(UnplayableReason::NoMdat));
    }

    Ok(ArchivePlayability::Playable)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct TopLevelBoxHeader {
    box_type: [u8; 4],
    header_size: u64,
    box_size: u64,
    box_end: u64,
}

enum HeaderScan {
    Header(TopLevelBoxHeader),
    Malformed,
    End,
}

fn read_top_level_box_header(
    file: &mut File,
    pos: u64,
    file_len: u64,
) -> io::Result<HeaderScan> {
    if pos.saturating_add(8) > file_len {
        return Ok(HeaderScan::End);
    }

    file.seek(SeekFrom::Start(pos))?;
    let mut header = [0_u8; 8];
    file.read_exact(&mut header)?;

    let size_u32 = u32::from_be_bytes([header[0], header[1], header[2], header[3]]);
    let box_type = [header[4], header[5], header[6], header[7]];

    let (box_size, header_size) = match size_u32 {
        0 => (file_len.saturating_sub(pos), 8_u64),
        1 => {
            if pos.saturating_add(16) > file_len {
                return Ok(HeaderScan::Malformed);
            }
            let mut ext = [0_u8; 8];
            file.read_exact(&mut ext)?;
            (u64::from_be_bytes(ext), 16_u64)
        }
        n => (u64::from(n), 8_u64),
    };

    if box_size < header_size {
        return Ok(HeaderScan::Malformed);
    }

    let Some(box_end) = pos.checked_add(box_size) else {
        return Ok(HeaderScan::Malformed);
    };
    Ok(HeaderScan::Header(TopLevelBoxHeader {
        box_type,
        header_size,
        box_size,
        box_end,
    }))
}

fn probe_moov_parseable(
    file: &mut File,
    box_start: u64,
    header_size: u64,
    box_size: u64,
) -> io::Result<MoovProbeResult> {
    let body_len = box_size.saturating_sub(header_size);
    if body_len > MAX_MOOV_BODY {
        return Ok(MoovProbeResult::TooLarge);
    }
    let Ok(body_len_usize) = usize::try_from(body_len) else {
        return Ok(MoovProbeResult::TooLarge);
    };
    let body_offset = box_start
        .checked_add(header_size)
        .ok_or_else(|| io::Error::other("moov body offset overflow"))?;

    file.seek(SeekFrom::Start(body_offset))?;
    let mut moov_body = vec![0_u8; body_len_usize];
    file.read_exact(&mut moov_body)?;

    if find_box(&moov_body, 0, moov_body.len(), b"trak").is_err() {
        return Ok(MoovProbeResult::Unparseable);
    }
    let Ok(mdhd_ref) = find_box_path(&moov_body, &[*b"trak", *b"mdia", *b"mdhd"]) else {
        return Ok(MoovProbeResult::Unparseable);
    };
    let Ok(mdhd) = parse_mdhd(mdhd_ref.body(&moov_body)) else {
        return Ok(MoovProbeResult::Unparseable);
    };
    if mdhd.timescale > 0 {
        Ok(MoovProbeResult::Parseable)
    } else {
        Ok(MoovProbeResult::Unparseable)
    }
}

enum MoovProbeResult {
    Parseable,
    Unparseable,
    TooLarge,
}

#[cfg(test)]
#[allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::{ArchivePlayability, UnplayableReason, probe_file_playability};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!("retentiond-probe-{}-{unique}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn write_probe_file(bytes: &[u8]) -> PathBuf {
        let dir = new_temp_dir();
        let path = dir.join("clip.mp4");
        fs::write(&path, bytes).expect("write probe file");
        path
    }

    fn box32(name: [u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).expect("box size");
        let mut out = Vec::with_capacity(8 + body.len());
        out.extend_from_slice(&size.to_be_bytes());
        out.extend_from_slice(&name);
        out.extend_from_slice(body);
        out
    }

    fn box64(name: [u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u64::try_from(16 + body.len()).expect("box size");
        let mut out = Vec::with_capacity(16 + body.len());
        out.extend_from_slice(&1_u32.to_be_bytes());
        out.extend_from_slice(&name);
        out.extend_from_slice(&size.to_be_bytes());
        out.extend_from_slice(body);
        out
    }

    fn mdhd_body(timescale: u32, duration: u32) -> Vec<u8> {
        let mut body = vec![0_u8; 4];
        body.extend_from_slice(&0_u32.to_be_bytes());
        body.extend_from_slice(&0_u32.to_be_bytes());
        body.extend_from_slice(&timescale.to_be_bytes());
        body.extend_from_slice(&duration.to_be_bytes());
        body.extend_from_slice(&[0_u8; 4]);
        body
    }

    fn valid_moov() -> Vec<u8> {
        let mdhd = box32(*b"mdhd", &mdhd_body(30_000, 90_000));
        let mdia = box32(*b"mdia", &mdhd);
        let trak = box32(*b"trak", &mdia);
        box32(*b"moov", &trak)
    }

    fn invalid_moov_without_mdhd() -> Vec<u8> {
        let mdia = box32(*b"mdia", b"");
        let trak = box32(*b"trak", &mdia);
        box32(*b"moov", &trak)
    }

    fn fixed_stub_no_moov_49192() -> Vec<u8> {
        let mut ftyp_body = vec![0_u8; 24];
        ftyp_body[0..4].copy_from_slice(b"isom");
        let ftyp = box32(*b"ftyp", &ftyp_body);
        let mdat_body = vec![0_u8; 49_152];
        let mdat = box32(*b"mdat", &mdat_body);
        let mut out = Vec::with_capacity(49_192);
        out.extend_from_slice(&ftyp);
        out.extend_from_slice(&mdat);
        assert_eq!(out.len(), 49_192);
        out
    }

    #[test]
    fn ftyp_plus_mdat_stub_without_moov_is_unplayable_no_moov() {
        let path = write_probe_file(&fixed_stub_no_moov_49192());
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::NoMoov)
        );
    }

    #[test]
    fn valid_ftyp_moov_mdat_is_playable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 32]));
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(verdict, ArchivePlayability::Playable);
    }

    #[test]
    fn moov_at_end_is_playable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 64]));
        bytes.extend_from_slice(&valid_moov());
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(verdict, ArchivePlayability::Playable);
    }

    #[test]
    fn moov_present_but_unparseable_mdhd_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&invalid_moov_without_mdhd());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 32]));
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::MoovUnparseable)
        );
    }

    #[test]
    fn handles_64bit_header_boxes() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box64(*b"free", &[1_u8, 2, 3]));
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 32]));
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(verdict, ArchivePlayability::Playable);
    }

    #[test]
    fn moov_declared_beyond_eof_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&100_u32.to_be_bytes());
        bytes.extend_from_slice(b"moov");
        bytes.extend_from_slice(&[0_u8; 4]);
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::MoovExtentBeyondEof)
        );
    }

    #[test]
    fn trailing_non_moov_box_declared_beyond_eof_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        bytes.extend_from_slice(&u32::MAX.to_be_bytes());
        bytes.extend_from_slice(b"free");
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::MalformedTopLevelBox)
        );
    }

    #[test]
    fn trailing_truncated_64bit_header_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        bytes.extend_from_slice(&1_u32.to_be_bytes());
        bytes.extend_from_slice(b"free");
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::MalformedTopLevelBox)
        );
    }

    #[test]
    fn trailing_box_size_less_than_header_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        bytes.extend_from_slice(&3_u32.to_be_bytes());
        bytes.extend_from_slice(b"free");
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::MalformedTopLevelBox)
        );
    }

    #[test]
    fn trailing_under_header_slack_bytes_remain_playable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        bytes.extend_from_slice(&[0_u8; 7]);
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(verdict, ArchivePlayability::Playable);
    }

    #[test]
    fn missing_ftyp_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&valid_moov());
        bytes.extend_from_slice(&box32(*b"mdat", &[0_u8; 8]));
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::NoFtyp)
        );
    }

    #[test]
    fn missing_mdat_is_unplayable() {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&box32(*b"ftyp", b"isom"));
        bytes.extend_from_slice(&valid_moov());
        let path = write_probe_file(&bytes);
        let verdict = probe_file_playability(&path).expect("probe");
        assert_eq!(
            verdict,
            ArchivePlayability::Unplayable(UnplayableReason::NoMdat)
        );
    }
}
