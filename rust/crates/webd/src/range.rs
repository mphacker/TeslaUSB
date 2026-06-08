//! HTTP `Range` request parsing for the video-stream endpoint (Task 5.1b).
//!
//! A small, pure, exhaustively unit-tested parser kept free of any HTTP types
//! so the range math can be tested in isolation. Mirrors the legacy
//! `video_service/_range.py` behaviour with one deliberate, RFC 7233-aligned
//! refinement: an `end` past EOF is **clamped** to `total-1` (a `206`) rather
//! than rejected.
//!
//! Only the single-range forms are supported (`bytes=start-end`, `bytes=start-`,
//! `bytes=-suffix`). Multi-range (`bytes=0-9,20-29`) and any non-`bytes` unit
//! are rejected as unsatisfiable, matching the legacy server which never
//! emitted `multipart/byteranges`.

/// The outcome of evaluating a single `bytes=` range against a known size.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ParsedRange {
    /// A satisfiable range, as **inclusive** byte offsets `[start, end]`.
    Satisfiable {
        /// First byte offset (inclusive).
        start: u64,
        /// Last byte offset (inclusive); always `>= start` and `< size`.
        end: u64,
    },
    /// The header was present but cannot be satisfied (malformed, multi-range,
    /// out of bounds, or against an empty file) — the caller returns `416`.
    Unsatisfiable,
}

/// Parse a single `bytes=` range header value against the resource `size`.
///
/// `value` is the raw header value (e.g. `"bytes=0-1023"`). Returns
/// [`ParsedRange::Unsatisfiable`] for anything malformed or out of bounds so
/// the handler can answer `416 Range Not Satisfiable`. Absence of a `Range`
/// header is handled by the caller (a full-body `200`), not here.
pub(crate) fn parse_byte_range(value: &str, size: u64) -> ParsedRange {
    let Some(rest) = value.trim().strip_prefix("bytes=") else {
        return ParsedRange::Unsatisfiable;
    };
    let rest = rest.trim();
    // Multi-range requests are explicitly unsupported (RFC 7233 §3.1 lets the
    // server ignore them; the legacy server never implemented them).
    if rest.contains(',') {
        return ParsedRange::Unsatisfiable;
    }
    let Some((start_str, end_str)) = rest.split_once('-') else {
        return ParsedRange::Unsatisfiable;
    };
    let start_str = start_str.trim();
    let end_str = end_str.trim();

    // An empty resource cannot satisfy any range; report `416` with `*/0`.
    if size == 0 {
        return ParsedRange::Unsatisfiable;
    }
    let last = size - 1;

    let (start, end) = if start_str.is_empty() {
        // Suffix form `-N`: the final N bytes.
        let Ok(suffix) = end_str.parse::<u64>() else {
            return ParsedRange::Unsatisfiable;
        };
        if suffix == 0 {
            return ParsedRange::Unsatisfiable;
        }
        (size.saturating_sub(suffix), last)
    } else {
        let Ok(start) = start_str.parse::<u64>() else {
            return ParsedRange::Unsatisfiable;
        };
        let end = if end_str.is_empty() {
            last
        } else {
            let Ok(end) = end_str.parse::<u64>() else {
                return ParsedRange::Unsatisfiable;
            };
            // Clamp an end past EOF to the last byte (RFC 7233 §4.1).
            end.min(last)
        };
        (start, end)
    };

    if start >= size || start > end {
        return ParsedRange::Unsatisfiable;
    }
    ParsedRange::Satisfiable { start, end }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::{ParsedRange, parse_byte_range};

    fn sat(value: &str, size: u64) -> (u64, u64) {
        match parse_byte_range(value, size) {
            ParsedRange::Satisfiable { start, end } => (start, end),
            ParsedRange::Unsatisfiable => panic!("expected satisfiable for {value:?}/{size}"),
        }
    }

    fn unsat(value: &str, size: u64) {
        assert_eq!(
            parse_byte_range(value, size),
            ParsedRange::Unsatisfiable,
            "expected unsatisfiable for {value:?}/{size}"
        );
    }

    #[test]
    fn open_ended_covers_whole_file() {
        assert_eq!(sat("bytes=0-", 100), (0, 99));
    }

    #[test]
    fn closed_range_is_inclusive() {
        assert_eq!(sat("bytes=0-99", 100), (0, 99));
        assert_eq!(sat("bytes=10-19", 100), (10, 19));
    }

    #[test]
    fn single_byte_and_zero_zero() {
        assert_eq!(sat("bytes=0-0", 100), (0, 0));
        assert_eq!(sat("bytes=0-0", 1), (0, 0));
    }

    #[test]
    fn mid_to_end() {
        assert_eq!(sat("bytes=50-", 100), (50, 99));
        assert_eq!(sat("bytes=99-", 100), (99, 99));
    }

    #[test]
    fn suffix_returns_tail() {
        assert_eq!(sat("bytes=-10", 100), (90, 99));
        assert_eq!(sat("bytes=-1", 100), (99, 99));
    }

    #[test]
    fn suffix_larger_than_file_clamps_to_whole_file() {
        assert_eq!(sat("bytes=-500", 100), (0, 99));
        assert_eq!(sat("bytes=-100", 100), (0, 99));
    }

    #[test]
    fn end_past_eof_is_clamped() {
        assert_eq!(sat("bytes=0-100000", 100), (0, 99));
        assert_eq!(sat("bytes=90-100000", 100), (90, 99));
    }

    #[test]
    fn whitespace_is_tolerated() {
        assert_eq!(sat(" bytes=0-9 ", 100), (0, 9));
    }

    #[test]
    fn start_past_eof_is_unsatisfiable() {
        unsat("bytes=100-", 100);
        unsat("bytes=100-200", 100);
        unsat("bytes=500-600", 100);
    }

    #[test]
    fn empty_file_is_always_unsatisfiable() {
        unsat("bytes=0-", 0);
        unsat("bytes=0-0", 0);
        unsat("bytes=-5", 0);
    }

    #[test]
    fn zero_suffix_is_unsatisfiable() {
        unsat("bytes=-0", 100);
    }

    #[test]
    fn multi_range_is_unsatisfiable() {
        unsat("bytes=0-9,20-29", 100);
    }

    #[test]
    fn malformed_headers_are_unsatisfiable() {
        unsat("bytes=abc", 100);
        unsat("bytes=abc-def", 100);
        unsat("bytes=-", 100);
        unsat("bytes=--1", 100);
        unsat("bytes=0--1", 100);
        unsat("bytes=", 100);
        unsat("items=0-9", 100);
        unsat("0-9", 100);
    }

    #[test]
    fn reversed_range_is_unsatisfiable() {
        unsat("bytes=50-10", 100);
    }

    #[test]
    fn overflowing_integer_is_unsatisfiable_not_panic() {
        unsat("bytes=0-99999999999999999999999999", 100);
        unsat("bytes=99999999999999999999999999-", 100);
    }
}
