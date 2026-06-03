//! Shared UTC calendar decomposition used by the FAT32 and `exFAT`
//! directory-entry timestamp encoders.
//!
//! Both filesystem families pack a wall-clock timestamp into
//! DOS-style date/time bit fields. The only common primitive they
//! need is "given a [`SystemTime`], break it into UTC civil fields".
//! Keeping that in one place avoids duplicating Howard Hinnant's
//! `civil_from_days` algorithm in two synthesizers.

use std::time::SystemTime;

/// Broken-down UTC calendar fields derived from a [`SystemTime`].
///
/// `month` is `1..=12`, `day` is `1..=31`, `hour` is `0..=23`,
/// `minute`/`second` are `0..=59`, and `subsec_nanos` is the
/// fractional-second remainder in nanoseconds (`0..1_000_000_000`).
/// `year` is the full proleptic Gregorian year.
pub(crate) struct CivilDateTime {
    /// Full proleptic Gregorian year (e.g. `2026`).
    pub year: i64,
    /// Month of year, `1..=12`.
    pub month: u8,
    /// Day of month, `1..=31`.
    pub day: u8,
    /// Hour of day, `0..=23`.
    pub hour: u8,
    /// Minute of hour, `0..=59`.
    pub minute: u8,
    /// Second of minute, `0..=59`.
    pub second: u8,
    /// Sub-second remainder in nanoseconds, `0..1_000_000_000`.
    pub subsec_nanos: u32,
}

impl CivilDateTime {
    /// Decompose `time` (interpreted as UTC) into civil calendar
    /// fields.
    ///
    /// Returns `None` for instants before the Unix epoch, which the
    /// DOS-derived FAT/`exFAT` date range cannot represent; callers
    /// fall back to their respective epoch sentinel rather than
    /// failing, because a synthesized directory entry must always be
    /// emittable.
    #[allow(clippy::cast_possible_truncation, clippy::cast_possible_wrap)]
    pub(crate) fn from_system_time(time: SystemTime) -> Option<Self> {
        let dur = time.duration_since(SystemTime::UNIX_EPOCH).ok()?;
        let secs = dur.as_secs();
        let days = (secs / 86_400) as i64;
        let secs_of_day = (secs % 86_400) as u32;
        let (year, month, day) = civil_from_days(days);
        // secs_of_day < 86_400, so each component fits a u8.
        let hour = (secs_of_day / 3_600) as u8;
        let minute = ((secs_of_day % 3_600) / 60) as u8;
        let second = (secs_of_day % 60) as u8;
        Some(Self {
            year,
            month,
            day,
            hour,
            minute,
            second,
            subsec_nanos: dur.subsec_nanos(),
        })
    }
}

/// Convert a count of days since the Unix epoch (1970-01-01) into a
/// `(year, month, day)` civil date in the proleptic Gregorian
/// calendar (UTC).
///
/// Algorithm: Howard Hinnant's `civil_from_days`
/// (<https://howardhinnant.github.io/date_algorithms.html>). `month`
/// is in `1..=12` and `day` in `1..=31`, so both fit a `u8` without
/// truncation.
#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    clippy::similar_names
)]
pub(crate) fn civil_from_days(z: i64) -> (i64, u8, u8) {
    let z = z + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = (doy - (153 * mp + 2) / 5 + 1) as u8; // [1, 31]
    let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u8; // [1, 12]
    (y + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used)]

    use super::*;
    use std::time::Duration;

    #[test]
    fn decomposes_a_modern_utc_timestamp() {
        // 2021-08-15T13:10:20Z + 250 ms.
        let t = SystemTime::UNIX_EPOCH + Duration::new(1_629_033_020, 250_000_000);
        let c = CivilDateTime::from_system_time(t).expect("post-epoch");
        assert_eq!(
            (c.year, c.month, c.day, c.hour, c.minute, c.second),
            (2021, 8, 15, 13, 10, 20)
        );
        assert_eq!(c.subsec_nanos, 250_000_000);
    }

    #[test]
    fn rejects_pre_epoch_instant() {
        let pre = SystemTime::UNIX_EPOCH - Duration::from_secs(1);
        assert!(CivilDateTime::from_system_time(pre).is_none());
    }

    #[test]
    fn civil_from_days_handles_the_unix_epoch_day() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
    }
}
