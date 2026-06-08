//! Decoder for the cached `trips.polyline` blob.
//!
//! `indexd` caches an RDP-simplified polyline per trip as a self-describing
//! big-endian blob (`indexd::derive::encode_polyline`): a `u32 segment_count`,
//! then per segment a `u32 point_count` followed by `point_count × (f64 lat,
//! f64 lon)`. The format is `indexd`-internal/provisional; `webd` decodes it
//! into nested `[lat, lon]` arrays for the map render path.
//!
//! The blob is trusted (only `indexd` writes it) but decoding is still written
//! defensively: a truncated or corrupt blob yields a decode error rather than a
//! panic or an unbounded allocation, and the caller degrades to an empty
//! polyline instead of failing the whole response.
#![allow(clippy::module_name_repetitions)]

use std::io::{Cursor, Read};

/// Upper bound on segments in one trip's cached polyline. Far above any real
/// trip; guards against a corrupt `segment_count`.
const MAX_SEGMENTS: usize = 10_000;

/// Upper bound on total points across all segments. `indexd` caps rendered
/// points per trip well below this.
const MAX_POINTS: usize = 1_000_000;

/// Bytes per encoded point: two big-endian `f64`s (lat, lon).
const BYTES_PER_POINT: usize = 16;

/// A decoded polyline: a list of segments, each a list of `[lat, lon]` points.
pub(crate) type Polyline = Vec<Vec<[f64; 2]>>;

/// Errors from decoding a polyline blob.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub(crate) enum PolylineError {
    /// The blob ended before the declared structure was fully read.
    #[error("polyline blob is truncated")]
    Truncated,

    /// A declared count exceeded the safety caps (corrupt blob).
    #[error("polyline blob declares an implausible size")]
    TooLarge,

    /// A coordinate was NaN or infinite (cannot be represented in JSON).
    #[error("polyline blob contains a non-finite coordinate")]
    NonFinite,
}

/// Decode a cached polyline blob into nested `[lat, lon]` segments.
///
/// `None` (a `NULL` blob, i.e. a trip with no cached polyline) decodes to an
/// empty list.
pub(crate) fn decode(blob: Option<&[u8]>) -> Result<Polyline, PolylineError> {
    let Some(blob) = blob else {
        return Ok(Vec::new());
    };
    let mut cursor = Cursor::new(blob);
    let total_len = blob.len();

    let segment_count =
        usize::try_from(read_u32(&mut cursor)?).map_err(|_| PolylineError::TooLarge)?;
    if segment_count > MAX_SEGMENTS {
        return Err(PolylineError::TooLarge);
    }

    let mut segments: Polyline = Vec::with_capacity(segment_count);
    let mut total_points: usize = 0;
    for _ in 0..segment_count {
        let point_count =
            usize::try_from(read_u32(&mut cursor)?).map_err(|_| PolylineError::TooLarge)?;
        total_points = total_points
            .checked_add(point_count)
            .ok_or(PolylineError::TooLarge)?;
        if total_points > MAX_POINTS {
            return Err(PolylineError::TooLarge);
        }
        // Reject a count the remaining bytes cannot satisfy *before* allocating,
        // so a lying header cannot force a large reservation.
        let needed = point_count
            .checked_mul(BYTES_PER_POINT)
            .ok_or(PolylineError::TooLarge)?;
        let position = usize::try_from(cursor.position()).map_err(|_| PolylineError::TooLarge)?;
        let remaining = total_len.saturating_sub(position);
        if needed > remaining {
            return Err(PolylineError::Truncated);
        }

        let mut segment: Vec<[f64; 2]> = Vec::with_capacity(point_count);
        for _ in 0..point_count {
            let lat = read_f64(&mut cursor)?;
            let lon = read_f64(&mut cursor)?;
            if !lat.is_finite() || !lon.is_finite() {
                return Err(PolylineError::NonFinite);
            }
            segment.push([lat, lon]);
        }
        segments.push(segment);
    }
    Ok(segments)
}

/// Read a big-endian `u32`, or [`PolylineError::Truncated`] at end of blob.
fn read_u32(cursor: &mut Cursor<&[u8]>) -> Result<u32, PolylineError> {
    let mut buf = [0_u8; 4];
    cursor
        .read_exact(&mut buf)
        .map_err(|_| PolylineError::Truncated)?;
    Ok(u32::from_be_bytes(buf))
}

/// Read a big-endian `f64`, or [`PolylineError::Truncated`] at end of blob.
fn read_f64(cursor: &mut Cursor<&[u8]>) -> Result<f64, PolylineError> {
    let mut buf = [0_u8; 8];
    cursor
        .read_exact(&mut buf)
        .map_err(|_| PolylineError::Truncated)?;
    Ok(f64::from_be_bytes(buf))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]

    use super::{PolylineError, decode};

    /// Encode in the same format as `indexd::derive::encode_polyline` so the
    /// decoder is tested against the real wire shape without depending on
    /// indexd at this layer.
    fn encode(segments: &[Vec<(f64, f64)>]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&(u32::try_from(segments.len()).unwrap()).to_be_bytes());
        for seg in segments {
            out.extend_from_slice(&(u32::try_from(seg.len()).unwrap()).to_be_bytes());
            for &(lat, lon) in seg {
                out.extend_from_slice(&lat.to_be_bytes());
                out.extend_from_slice(&lon.to_be_bytes());
            }
        }
        out
    }

    #[test]
    fn null_blob_is_empty() {
        assert_eq!(decode(None).unwrap(), Vec::<Vec<[f64; 2]>>::new());
    }

    #[test]
    fn round_trips_two_segments() {
        let blob = encode(&[vec![(40.0, -75.0), (41.0, -76.0)], vec![(1.0, 2.0)]]);
        let decoded = decode(Some(&blob)).unwrap();
        assert_eq!(decoded.len(), 2);
        assert_eq!(decoded[0], vec![[40.0, -75.0], [41.0, -76.0]]);
        assert_eq!(decoded[1], vec![[1.0, 2.0]]);
    }

    #[test]
    fn truncated_blob_errors() {
        let mut blob = encode(&[vec![(40.0, -75.0)]]);
        blob.truncate(blob.len() - 3);
        assert_eq!(decode(Some(&blob)), Err(PolylineError::Truncated));
    }

    #[test]
    fn lying_point_count_is_rejected_without_overallocating() {
        // Declares 1 segment of u32::MAX points but carries no point bytes.
        let mut blob = Vec::new();
        blob.extend_from_slice(&1_u32.to_be_bytes());
        blob.extend_from_slice(&u32::MAX.to_be_bytes());
        assert!(matches!(
            decode(Some(&blob)),
            Err(PolylineError::Truncated | PolylineError::TooLarge)
        ));
    }

    #[test]
    fn empty_blob_errors() {
        assert_eq!(decode(Some(&[])), Err(PolylineError::Truncated));
    }
}
