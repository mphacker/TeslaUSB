//! Tesla `event.json` parsing for SavedClips/SentryClips events.
//!
//! Pure domain-ish parsing only: callers do the filesystem read,
//! pass bytes here, then hand the typed result to the store layer.

use serde::Deserialize;
use serde::de::{self, Visitor};
use thiserror::Error;

const SECONDS_PER_MINUTE: i64 = 60;
const MINUTES_PER_HOUR: i64 = 60;
const HOURS_PER_DAY: i64 = 24;
const SECONDS_PER_HOUR: i64 = SECONDS_PER_MINUTE * MINUTES_PER_HOUR;
const SECONDS_PER_DAY: i64 = SECONDS_PER_HOUR * HOURS_PER_DAY;
const YEARS_PER_ERA: i64 = 400;
const DAYS_PER_ERA: i64 = 146_097;
const DAYS_TO_UNIX_EPOCH: i64 = 719_468;
const MARCH_BASED_YEAR_OFFSET: i64 = 399;
const MARCH_BASED_MONTH_SHIFT: i64 = 9;
const JAN_FEB_YEAR_SHIFT_MONTH: u32 = 2;
const MARCH_BASED_MONTH_OFFSET: i64 = 3;
const DAYS_PER_NON_LEAP_YEAR: i64 = 365;
const LEAP_4_YEARS: i64 = 4;
const LEAP_100_YEARS: i64 = 100;
const DAYS_PER_5_MONTH_BLOCK: i64 = 153;
const DAYS_PER_5_MONTH_ROUNDING: i64 = 2;
const MONTH_BLOCK_DIVISOR: i64 = 5;
const DATE_PARTS: usize = 3;
const TIME_PARTS: usize = 3;
const MONTH_MIN: u32 = 1;
const MONTH_MAX: u32 = 12;
const DAY_MIN: u32 = 1;
const DAY_MAX: u32 = 31;
const HOUR_MAX: u32 = 23;
const MINUTE_MAX: u32 = 59;
const SECOND_MAX: u32 = 60;
const OFFSET_HH_LEN: usize = 2;
const OFFSET_HHMM_LEN: usize = 4;
/// Length of a Tesla local timestamp prefix `YYYY-MM-DD_HH-MM-SS`.
const TESLA_STAMP_LEN: usize = 19;
/// Civil timezone offsets are quantised to 15-minute steps worldwide.
const TZ_OFFSET_ROUND_SECONDS: i64 = 15 * SECONDS_PER_MINUTE;
/// Widest real civil UTC offset (UTC+14, Line Islands). Anything beyond
/// this magnitude signals a mismatched anchor clip, not a real zone.
const MAX_TZ_OFFSET_SECONDS: i64 = 14 * SECONDS_PER_HOUR;

/// Parsed content of a Tesla `event.json` file.
#[derive(Debug, Clone, PartialEq)]
pub struct ClipEventMetadata {
    /// Best-effort UTC from `event.json` alone: equal to the raw
    /// wall-clock when Tesla omits an offset, or the true UTC when an
    /// explicit offset/`Z` is present.
    pub timestamp_utc: i64,
    /// Raw local wall-clock seconds (the civil time interpreted as if
    /// UTC, with no offset applied). The store layer anchors this
    /// against a clip's SEI `clip_started_utc` to recover the true
    /// UTC instant when `timestamp_has_offset` is false.
    pub timestamp_local_naive: i64,
    /// Whether `event.json` carried an explicit timezone offset (`Z`
    /// or `±HH:MM`). When true, [`Self::timestamp_utc`] is already the
    /// authoritative UTC and must NOT be anchor-corrected.
    pub timestamp_has_offset: bool,
    /// Estimated latitude, or `None` when absent/malformed/0,0.
    pub est_lat: Option<f64>,
    /// Estimated longitude, or `None` when absent/malformed/0,0.
    pub est_lon: Option<f64>,
    /// Tesla reason code, e.g. `user_interaction_honk`.
    pub reason: Option<String>,
    /// City string Tesla included, if any.
    pub city: Option<String>,
    /// Camera string Tesla included, if any.
    pub camera: Option<String>,
}

/// Errors returned by [`parse_event_json`].
#[derive(Debug, Error)]
pub enum ClipEventParseError {
    /// The JSON document is malformed or has an unexpected root
    /// shape.
    #[error("event.json is not valid Tesla event metadata: {0}")]
    Json(#[from] serde_json::Error),
    /// `timestamp` is required because it is the event's primary
    /// temporal key.
    #[error("event.json missing required timestamp")]
    MissingTimestamp,
    /// `timestamp` existed but was not a parseable ISO timestamp.
    #[error("event.json timestamp {0:?} is not a supported ISO timestamp")]
    InvalidTimestamp(String),
}

/// Parse Tesla `event.json` bytes into typed metadata.
///
/// # Errors
///
/// Returns an error when the JSON is malformed or the required
/// `timestamp` is missing/invalid. Optional fields are kept as
/// `None` when absent or blank; malformed lat/lon simply become
/// `(None, None)` to mirror the v1 Python parser.
pub fn parse_event_json(input: &[u8]) -> Result<ClipEventMetadata, ClipEventParseError> {
    let raw: RawClipEvent = serde_json::from_slice(input)?;
    let timestamp = raw.timestamp.ok_or(ClipEventParseError::MissingTimestamp)?;
    let parsed = parse_timestamp(&timestamp)
        .ok_or_else(|| ClipEventParseError::InvalidTimestamp(timestamp.clone()))?;
    let (est_lat, est_lon) = parse_latlon(raw.est_lat, raw.est_lon);
    Ok(ClipEventMetadata {
        timestamp_utc: parsed.utc,
        timestamp_local_naive: parsed.local_naive,
        timestamp_has_offset: parsed.has_offset,
        est_lat,
        est_lon,
        reason: non_empty(raw.reason),
        city: non_empty(raw.city),
        camera: non_empty(raw.camera),
    })
}

#[derive(Debug, Deserialize)]
struct RawClipEvent {
    timestamp: Option<String>,
    est_lat: Option<JsonCoord>,
    est_lon: Option<JsonCoord>,
    reason: Option<String>,
    city: Option<String>,
    camera: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
enum JsonCoord {
    Number(f64),
    Text(String),
    Unsupported,
}

impl<'de> Deserialize<'de> for JsonCoord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        deserializer.deserialize_any(JsonCoordVisitor)
    }
}

struct JsonCoordVisitor;

impl Visitor<'_> for JsonCoordVisitor {
    type Value = JsonCoord;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a string or finite number coordinate")
    }

    fn visit_bool<E>(self, _value: bool) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(JsonCoord::Unsupported)
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        let number = value.to_string().parse::<f64>().map_err(E::custom)?;
        Ok(JsonCoord::Number(number))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        let number = value.to_string().parse::<f64>().map_err(E::custom)?;
        Ok(JsonCoord::Number(number))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(JsonCoord::Number(value))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(JsonCoord::Text(value.to_string()))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(JsonCoord::Text(value))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Ok(JsonCoord::Unsupported)
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.and_then(|s| {
        let trimmed = s.trim();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed.to_string())
        }
    })
}

fn parse_latlon(lat: Option<JsonCoord>, lon: Option<JsonCoord>) -> (Option<f64>, Option<f64>) {
    let Some(lat) = coord_to_f64(lat) else {
        return (None, None);
    };
    let Some(lon) = coord_to_f64(lon) else {
        return (None, None);
    };
    if lat == 0.0 && lon == 0.0 {
        return (None, None);
    }
    (Some(lat), Some(lon))
}

fn coord_to_f64(value: Option<JsonCoord>) -> Option<f64> {
    let candidate = match value? {
        JsonCoord::Number(n) => n,
        JsonCoord::Text(s) => s.trim().parse::<f64>().ok()?,
        JsonCoord::Unsupported => return None,
    };
    candidate.is_finite().then_some(candidate)
}

/// Outcome of parsing a Tesla `event.json` timestamp string.
struct ParsedTimestamp {
    /// Best-effort UTC: raw wall-clock when no offset, true UTC when
    /// an offset/`Z` is present.
    utc: i64,
    /// Raw civil wall-clock seconds with no offset applied.
    local_naive: i64,
    /// Whether an explicit offset (`Z` or `±HH:MM`) was present.
    has_offset: bool,
}

fn parse_timestamp(raw: &str) -> Option<ParsedTimestamp> {
    let trimmed = raw.trim();
    let (date, time_and_offset) = trimmed.split_once('T')?;
    let (time, offset_seconds) = split_utc_offset(time_and_offset)?;
    let date_parts = parse_parts::<DATE_PARTS>(date, '-')?;
    let time_parts = parse_parts::<TIME_PARTS>(time, ':')?;
    let year = i32::try_from(date_parts[0]).ok()?;
    let month = u32::try_from(date_parts[1]).ok()?;
    let day = u32::try_from(date_parts[2]).ok()?;
    let hour = u32::try_from(time_parts[0]).ok()?;
    let minute = u32::try_from(time_parts[1]).ok()?;
    let second = u32::try_from(time_parts[2]).ok()?;
    validate_datetime(month, day, hour, minute, second)?;
    let local_naive = days_from_civil(year, month, day) * SECONDS_PER_DAY
        + i64::from(hour) * SECONDS_PER_HOUR
        + i64::from(minute) * SECONDS_PER_MINUTE
        + i64::from(second);
    Some(ParsedTimestamp {
        utc: local_naive - offset_seconds.unwrap_or(0),
        local_naive,
        has_offset: offset_seconds.is_some(),
    })
}

/// Parse a Tesla local wall-clock stamp `YYYY-MM-DD_HH-MM-SS` (the form
/// used in clip filenames and event-dir names) into naive seconds (the
/// civil time interpreted as if UTC). Not a real UTC instant — used
/// only to derive the local↔UTC offset against a clip's true
/// SEI-derived `clip_started_utc`.
#[must_use]
pub fn parse_tesla_local_naive(stamp: &str) -> Option<i64> {
    let (date, time) = stamp.split_once('_')?;
    let date_parts = parse_parts::<DATE_PARTS>(date, '-')?;
    let time_parts = parse_parts::<TIME_PARTS>(time, '-')?;
    let year = i32::try_from(date_parts[0]).ok()?;
    let month = u32::try_from(date_parts[1]).ok()?;
    let day = u32::try_from(date_parts[2]).ok()?;
    let hour = u32::try_from(time_parts[0]).ok()?;
    let minute = u32::try_from(time_parts[1]).ok()?;
    let second = u32::try_from(time_parts[2]).ok()?;
    validate_datetime(month, day, hour, minute, second)?;
    Some(
        days_from_civil(year, month, day) * SECONDS_PER_DAY
            + i64::from(hour) * SECONDS_PER_HOUR
            + i64::from(minute) * SECONDS_PER_MINUTE
            + i64::from(second),
    )
}

/// Parse the leading Tesla timestamp from a clip file or directory
/// name (`YYYY-MM-DD_HH-MM-SS-front.mp4` etc.) into naive local
/// seconds. Returns `None` when the name is too short or malformed.
#[must_use]
pub fn parse_clip_name_local_naive(name: &str) -> Option<i64> {
    let stamp = name.get(..TESLA_STAMP_LEN)?;
    parse_tesla_local_naive(stamp)
}

/// Local↔UTC offset east of UTC (seconds), rounded to the nearest 15
/// minutes, derived from a clip's local filename stamp and its true
/// SEI `clip_started_utc`. Returns `None` when the implied offset
/// exceeds ±14 h (the range of real civil offsets) — a sign the anchor
/// clip is mismatched, so the caller must not correct.
#[must_use]
pub fn rounded_tz_offset(filename_local_naive: i64, clip_started_utc: i64) -> Option<i64> {
    let raw = filename_local_naive - clip_started_utc;
    let half = TZ_OFFSET_ROUND_SECONDS / 2;
    let units = if raw >= 0 {
        (raw + half) / TZ_OFFSET_ROUND_SECONDS
    } else {
        (raw - half) / TZ_OFFSET_ROUND_SECONDS
    };
    let offset = units * TZ_OFFSET_ROUND_SECONDS;
    (offset.abs() <= MAX_TZ_OFFSET_SECONDS).then_some(offset)
}

/// Splits the post-`T` portion into the bare `HH:MM:SS` time and the
/// timezone offset in seconds east of UTC (`None` when Tesla omits the
/// offset entirely — the common case, where the time is local
/// wall-clock). Accepts a trailing `Z` (→ `Some(0)`), `±HH`, `±HHMM`,
/// or `±HH:MM`.
fn split_utc_offset(time_and_offset: &str) -> Option<(&str, Option<i64>)> {
    if let Some(bare) = time_and_offset.strip_suffix('Z') {
        return Some((bare, Some(0)));
    }
    let Some(sign_index) = time_and_offset.rfind(['+', '-']) else {
        return Some((time_and_offset, None));
    };
    let (time, offset) = time_and_offset.split_at(sign_index);
    let east_of_utc = offset.starts_with('+');
    let magnitude = parse_offset_magnitude(&offset[1..])?;
    Some((time, Some(if east_of_utc { magnitude } else { -magnitude })))
}

/// Parses an unsigned timezone offset (`HH`, `HHMM`, or `HH:MM`) into
/// seconds.
fn parse_offset_magnitude(magnitude: &str) -> Option<i64> {
    let digits: String = magnitude.chars().filter(|c| *c != ':').collect();
    let (hours_str, minutes_str) = match digits.len() {
        OFFSET_HH_LEN => (&digits[..OFFSET_HH_LEN], None),
        OFFSET_HHMM_LEN => (&digits[..OFFSET_HH_LEN], Some(&digits[OFFSET_HH_LEN..])),
        _ => return None,
    };
    let hours: u32 = hours_str.parse().ok()?;
    let minutes: u32 = match minutes_str {
        Some(value) => value.parse().ok()?,
        None => 0,
    };
    if hours > HOUR_MAX || minutes > MINUTE_MAX {
        return None;
    }
    Some(i64::from(hours) * SECONDS_PER_HOUR + i64::from(minutes) * SECONDS_PER_MINUTE)
}

fn parse_parts<const N: usize>(raw: &str, delimiter: char) -> Option<[i64; N]> {
    let mut out = [0_i64; N];
    let mut count = 0_usize;
    for part in raw.split(delimiter) {
        let slot = out.get_mut(count)?;
        if part.is_empty() {
            return None;
        }
        *slot = part.parse::<i64>().ok()?;
        count += 1;
    }
    (count == N).then_some(out)
}

fn validate_datetime(month: u32, day: u32, hour: u32, minute: u32, second: u32) -> Option<()> {
    let valid_date = (MONTH_MIN..=MONTH_MAX).contains(&month) && (DAY_MIN..=DAY_MAX).contains(&day);
    let valid_time = hour <= HOUR_MAX && minute <= MINUTE_MAX && second <= SECOND_MAX;
    (valid_date && valid_time).then_some(())
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let year = i64::from(year) - i64::from(month <= JAN_FEB_YEAR_SHIFT_MONTH);
    let era = if year >= 0 {
        year
    } else {
        year - MARCH_BASED_YEAR_OFFSET
    } / YEARS_PER_ERA;
    let year_of_era = year - era * YEARS_PER_ERA;
    let month_i = i64::from(month);
    let march_based_month = month_i
        + if month_i > i64::from(JAN_FEB_YEAR_SHIFT_MONTH) {
            -MARCH_BASED_MONTH_OFFSET
        } else {
            MARCH_BASED_MONTH_SHIFT
        };
    let day_of_year = (DAYS_PER_5_MONTH_BLOCK * march_based_month + DAYS_PER_5_MONTH_ROUNDING)
        / MONTH_BLOCK_DIVISOR
        + i64::from(day)
        - 1;
    let day_of_era = year_of_era * DAYS_PER_NON_LEAP_YEAR + year_of_era / LEAP_4_YEARS
        - year_of_era / LEAP_100_YEARS
        + day_of_year;
    era * DAYS_PER_ERA + day_of_era - DAYS_TO_UNIX_EPOCH
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used, clippy::float_cmp)]

    use super::*;

    #[test]
    fn parses_string_latlon_and_metadata() {
        let parsed = parse_event_json(
            br#"{
                "timestamp":"2026-06-01T20:10:35",
                "est_lat":"42.5414",
                "est_lon":"-83.1234",
                "reason":"user_interaction_honk",
                "city":"Detroit",
                "camera":"front"
            }"#,
        )
        .unwrap();
        assert_eq!(parsed.timestamp_utc, 1_780_344_635);
        assert_eq!(parsed.est_lat, Some(42.5414));
        assert_eq!(parsed.est_lon, Some(-83.1234));
        assert_eq!(parsed.reason.as_deref(), Some("user_interaction_honk"));
        assert_eq!(parsed.city.as_deref(), Some("Detroit"));
        assert_eq!(parsed.camera.as_deref(), Some("front"));
    }

    #[test]
    fn rejects_null_island_coordinates() {
        let parsed =
            parse_event_json(br#"{"timestamp":"2026-06-01T20:10:35","est_lat":"0","est_lon":"0"}"#)
                .unwrap();
        assert_eq!((parsed.est_lat, parsed.est_lon), (None, None));
    }

    #[test]
    fn rejects_missing_or_malformed_coordinates_without_dropping_row() {
        for json in [
            br#"{"timestamp":"2026-06-01T20:10:35"}"#.as_slice(),
            br#"{"timestamp":"2026-06-01T20:10:35","est_lat":"abc","est_lon":"1.0"}"#,
            br#"{"timestamp":"2026-06-01T20:10:35","est_lat":"nan","est_lon":"1.0"}"#,
        ] {
            let parsed = parse_event_json(json).unwrap();
            assert_eq!((parsed.est_lat, parsed.est_lon), (None, None));
        }
    }

    #[test]
    fn missing_timestamp_is_an_error() {
        let err = parse_event_json(br#"{"reason":"sentry"}"#).unwrap_err();
        assert!(matches!(err, ClipEventParseError::MissingTimestamp));
    }

    #[test]
    fn timestamp_z_suffix_and_no_offset_are_both_utc() {
        assert_eq!(
            parse_timestamp("2026-06-01T20:10:35").map(|p| p.utc),
            Some(1_780_344_635)
        );
        assert_eq!(
            parse_timestamp("2026-06-01T20:10:35Z").map(|p| p.utc),
            Some(1_780_344_635)
        );
    }

    #[test]
    fn no_offset_records_raw_local_wall_clock() {
        let parsed = parse_timestamp("2026-06-01T20:10:35").unwrap();
        assert!(!parsed.has_offset);
        assert_eq!(parsed.local_naive, 1_780_344_635);
        assert_eq!(parsed.utc, parsed.local_naive);
    }

    #[test]
    fn explicit_offset_keeps_local_naive_separate_from_utc() {
        // 16:10:35 at -04:00 is 20:10:35 UTC; local_naive is the raw 16:10:35.
        let parsed = parse_timestamp("2026-06-01T16:10:35-04:00").unwrap();
        assert!(parsed.has_offset);
        assert_eq!(parsed.utc, 1_780_344_635);
        assert_eq!(parsed.local_naive, 1_780_344_635 - 4 * 3600);
    }

    #[test]
    fn timestamp_negative_offset_converts_to_utc() {
        // 16:10:35 at -04:00 is 20:10:35 UTC.
        assert_eq!(
            parse_timestamp("2026-06-01T16:10:35-04:00").map(|p| p.utc),
            Some(1_780_344_635)
        );
    }

    #[test]
    fn timestamp_positive_half_hour_offset_converts_to_utc() {
        // 01:40:35 on 06-02 at +05:30 is 20:10:35 UTC on 06-01.
        assert_eq!(
            parse_timestamp("2026-06-02T01:40:35+05:30").map(|p| p.utc),
            Some(1_780_344_635)
        );
    }

    #[test]
    fn timestamp_compact_offset_matches_colon_offset() {
        assert_eq!(
            parse_timestamp("2026-06-01T16:10:35-0400").map(|p| p.utc),
            parse_timestamp("2026-06-01T16:10:35-04:00").map(|p| p.utc),
        );
        assert_eq!(
            parse_timestamp("2026-06-01T16:10:35-04").map(|p| p.utc),
            Some(1_780_344_635)
        );
    }

    #[test]
    fn timestamp_rejects_malformed_offset() {
        assert!(parse_timestamp("2026-06-01T20:10:35+9").is_none());
        assert!(parse_timestamp("2026-06-01T20:10:35+99:99").is_none());
    }

    #[test]
    fn parses_clip_name_local_naive_from_front_segment() {
        // 2026-06-01_20-07-54 as wall-clock-interpreted-UTC.
        let stamp = parse_tesla_local_naive("2026-06-01_20-07-54").unwrap();
        assert_eq!(parse_clip_name_local_naive("2026-06-01_20-07-54-front.mp4"), Some(stamp));
        assert_eq!(stamp, 1_780_344_474);
    }

    #[test]
    fn clip_name_too_short_or_malformed_returns_none() {
        assert_eq!(parse_clip_name_local_naive("2026-06-01"), None);
        assert_eq!(parse_clip_name_local_naive("not-a-timestamp----x"), None);
    }

    #[test]
    fn rounded_tz_offset_recovers_edt_minus_four_hours() {
        // Filename wall-clock 2026-06-01_20-07-54 with true SEI start
        // 2026-06-02 00:07:03 UTC implies UTC-4 (EDT), i.e. -14400 s.
        let file_local = parse_tesla_local_naive("2026-06-01_20-07-54").unwrap();
        let clip_started_utc = 1_780_358_823; // 2026-06-02T00:07:03Z
        assert_eq!(rounded_tz_offset(file_local, clip_started_utc), Some(-4 * 3600));
    }

    #[test]
    fn rounded_tz_offset_quantises_to_fifteen_minutes() {
        // A ragged 3:52:10 gap rounds to the nearest real civil offset.
        let clip_started_utc = 1_780_358_823;
        let file_local = clip_started_utc + 3 * 3600 + 52 * 60 + 10;
        assert_eq!(rounded_tz_offset(file_local, clip_started_utc), Some(3 * 3600 + 45 * 60));
    }

    #[test]
    fn rounded_tz_offset_rejects_implausible_anchor() {
        // A 20-hour gap is not a real civil offset → no correction.
        let clip_started_utc = 1_780_358_823;
        let file_local = clip_started_utc + 20 * 3600;
        assert_eq!(rounded_tz_offset(file_local, clip_started_utc), None);
    }

    #[test]
    fn corrected_honk_lands_in_the_same_utc_day_as_its_drive() {
        // The operator's June-1 honk: event.json wall-clock 20:10:35 with
        // its drive's true clip start 2026-06-02T00:07:03Z. Correcting by
        // the anchored -4h offset moves the honk to 2026-06-02T00:10:35Z —
        // the same UTC day as trip 2150, so the route renders with the pin.
        let event = parse_event_json(br#"{"timestamp":"2026-06-01T20:10:35"}"#).unwrap();
        let file_local = parse_tesla_local_naive("2026-06-01_20-07-54").unwrap();
        let clip_started_utc = 1_780_358_823;
        let offset = rounded_tz_offset(file_local, clip_started_utc).unwrap();
        let corrected = event.timestamp_local_naive - offset;
        assert_eq!(corrected, 1_780_359_035); // 2026-06-02T00:10:35Z
    }
}
