//! Tesla clip filename → UTC epoch seconds.
//!
//! This is the **fallback** recording-instant resolver used when a front
//! clip's MP4 `mvhd`/GPS instant is unavailable (the Pi has no RTC). It
//! lives in `scannerd` — the process that owns raw parsing — so that the
//! producer can resolve a clip's `started_at` without depending on
//! `indexd` (which would be a dependency cycle, since `indexd` already
//! depends on `scannerd`). `indexd::derive::epoch_from_tesla_timestamp`
//! delegates here so there is a single implementation.
//!
//! The filename is in the car's LOCAL timezone, so this fallback carries
//! the documented clock-skew (`indexd.md` clock-skew rule); the
//! `mvhd`/GPS instant is always preferred when present.

/// Epoch seconds (UTC) for a `YYYY-MM-DD_HH-MM-SS` Tesla clip filename
/// timestamp.
///
/// Returns `None` if the string is not a parseable Tesla timestamp (wrong
/// length, non-numeric fields, or an out-of-range component). Note this is
/// a *stricter* check than `clip::parse_clip_name`'s structural
/// `is_timestamp` (which only validates digit/separator positions): a
/// structurally valid but semantically impossible stamp such as
/// `2026-13-99_...` parses as a clip name yet returns `None` here.
#[must_use]
pub fn epoch_from_tesla_timestamp(ts: &str) -> Option<i64> {
    // Expected shape: "YYYY-MM-DD_HH-MM-SS" (19 chars).
    let bytes = ts.as_bytes();
    if bytes.len() != 19 {
        return None;
    }
    let year: i64 = ts.get(0..4)?.parse().ok()?;
    let month: i64 = ts.get(5..7)?.parse().ok()?;
    let day: i64 = ts.get(8..10)?.parse().ok()?;
    let hour: i64 = ts.get(11..13)?.parse().ok()?;
    let minute: i64 = ts.get(14..16)?.parse().ok()?;
    let second: i64 = ts.get(17..19)?.parse().ok()?;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || !(0..=23).contains(&hour)
        || !(0..=59).contains(&minute)
        || !(0..=60).contains(&second)
    {
        return None;
    }
    let days = days_from_civil(year, month, day);
    Some(days * 86_400 + hour * 3_600 + minute * 60 + second)
}

/// Convert `(year, month, day)` to days-since-Unix-epoch
/// (Howard Hinnant's `days_from_civil`).
// `era`/`yoe`/`doy`/`doe` are the canonical names from the published
// algorithm; renaming them for clippy would obscure the citation.
#[allow(clippy::similar_names)]
#[must_use]
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::epoch_from_tesla_timestamp;

    #[test]
    fn parses_known_timestamp() {
        // 2021-01-01_00-00-00 UTC == 1_609_459_200.
        assert_eq!(
            epoch_from_tesla_timestamp("2021-01-01_00-00-00"),
            Some(1_609_459_200)
        );
    }

    #[test]
    fn epoch_is_monotonic_in_seconds() {
        let a = epoch_from_tesla_timestamp("2026-06-01_20-10-04").unwrap();
        let b = epoch_from_tesla_timestamp("2026-06-01_20-10-05").unwrap();
        assert_eq!(b - a, 1);
    }

    #[test]
    fn rejects_wrong_length() {
        assert_eq!(epoch_from_tesla_timestamp("2026-06-01"), None);
    }

    #[test]
    fn rejects_out_of_range_components() {
        // Structurally valid (digits + separators) but impossible.
        assert_eq!(epoch_from_tesla_timestamp("2026-13-01_20-10-04"), None);
        assert_eq!(epoch_from_tesla_timestamp("2026-06-32_20-10-04"), None);
        assert_eq!(epoch_from_tesla_timestamp("2026-06-01_24-10-04"), None);
    }

    #[test]
    fn rejects_non_numeric() {
        assert_eq!(epoch_from_tesla_timestamp("20x6-06-01_20-10-04"), None);
    }
}
