//! SEI extraction over already-read clip bytes, reusing the
//! `teslausb_core::sei` pipeline validated end-to-end on real Tesla
//! footage during spike 2.5 (19,345 SEI NALs, 100% decoded, 0 errors).
//!
//! Flow: `find_box(mdat) → AvccIter → SEI NALs → extract_tesla_payload
//! → decode_sei_message`. Pure and host-testable; the binary supplies
//! the bytes (read raw through the cluster chain, never via a mount).

use teslausb_core::sei::mp4::find_box;
use teslausb_core::sei::nal::{AvccIter, NalUnit};
use teslausb_core::sei::payload::extract_tesla_payload;
use teslausb_core::sei::tesla::decode_sei_message;

/// Hard cap on retained SEI samples per clip (a Tesla minute-clip holds
/// ~1300; the cap only guards against a pathological input).
const MAX_SEI_SAMPLES: usize = 100_000;

/// One decoded SEI/HUD sample.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SeiSample {
    /// Monotonic frame sequence number from the SEI message.
    pub frame_seq_no: u64,
    /// Latitude in degrees (valid only if `has_gps_fix`).
    pub latitude_deg: f64,
    /// Longitude in degrees (valid only if `has_gps_fix`).
    pub longitude_deg: f64,
    /// Vehicle speed in m/s.
    pub vehicle_speed_mps: f32,
    /// Whether this sample carried a usable GPS fix.
    pub has_gps_fix: bool,
}

/// Summary of an SEI scan over one clip.
#[derive(Debug, Clone, Default)]
pub struct SeiScan {
    /// Total NAL units iterated across all `mdat` boxes.
    pub nal_total: u64,
    /// SEI NAL units seen.
    pub nal_sei: u64,
    /// SEI messages decoded successfully.
    pub decoded: u64,
    /// SEI payload/protobuf decode failures.
    pub decode_errors: u64,
    /// Samples that carried a GPS fix.
    pub gps_fix_count: u64,
    /// Decoded samples (bounded by [`MAX_SEI_SAMPLES`]).
    pub samples: Vec<SeiSample>,
}

/// Extract SEI samples from clip bytes (bounded by `ValidDataLength`).
#[must_use]
pub fn scan_sei(clip: &[u8]) -> SeiScan {
    let mut scan = SeiScan::default();
    let mut search_from = 0usize;
    while let Ok(mdat) = find_box(clip, search_from, clip.len(), b"mdat") {
        scan_mdat(mdat.body(clip), &mut scan);
        let next = mdat.end.max(search_from + 1);
        if next >= clip.len() {
            break;
        }
        search_from = next;
    }
    scan
}

/// Scan one `mdat` body's NAL stream for Tesla SEI messages.
fn scan_mdat(body: &[u8], scan: &mut SeiScan) {
    for item in AvccIter::new(body) {
        let Ok(nal) = item else {
            // AvccIter stops after a malformed NAL; loop ends naturally.
            break;
        };
        scan.nal_total += 1;
        if nal.nal_type != NalUnit::NAL_TYPE_SEI {
            continue;
        }
        scan.nal_sei += 1;
        let Ok(payload) = extract_tesla_payload(nal.payload) else {
            scan.decode_errors += 1;
            continue;
        };
        let Ok(msg) = decode_sei_message(&payload) else {
            scan.decode_errors += 1;
            continue;
        };
        scan.decoded += 1;
        let has_gps_fix = msg.has_gps_fix();
        if has_gps_fix {
            scan.gps_fix_count += 1;
        }
        if scan.samples.len() < MAX_SEI_SAMPLES {
            scan.samples.push(SeiSample {
                frame_seq_no: msg.frame_seq_no,
                latitude_deg: msg.latitude_deg,
                longitude_deg: msg.longitude_deg,
                vehicle_speed_mps: msg.vehicle_speed_mps,
                has_gps_fix,
            });
        }
    }
}
