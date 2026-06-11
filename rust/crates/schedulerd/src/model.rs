//! Wire + persisted DTOs for the chime scheduler, and their validation into the
//! pure [`teslausb_core::chime`] engine types.
//!
//! These structs are the single representation shared by three surfaces: the
//! versioned JSON state file `schedulerd` owns on disk, the request bodies the
//! `webd` proxy forwards, and the menu data the SPA renders. They use camelCase
//! JSON (the project's API convention) and validate at the boundary —
//! [`ScheduleInput::validate`] is the one place untrusted schedule data is
//! turned into a trusted [`teslausb_core::chime::Schedule`].

use serde::{Deserialize, Serialize};
use teslausb_core::chime::{
    self, ChimeRef, Holiday, Interval, RANDOM_SENTINEL, Schedule, ScheduleKind, Weekday,
    days_in_month,
};

/// A validation failure with a stable machine code and a human message — shaped
/// so `webd` can map it straight onto the `{error:{code,message}}` envelope.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct ValidationError {
    /// Machine-readable code, e.g. `"invalid_schedule_type"`.
    pub code: &'static str,
    /// Human-readable explanation.
    pub message: String,
}

impl ValidationError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

/// The discriminant of a schedule, matching the v1 form `schedule_type` tokens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ScheduleType {
    /// Fire on selected weekdays at a time.
    Weekly,
    /// Fire on a calendar month/day at a time, every year.
    Date,
    /// Fire at 00:00 on a US holiday.
    Holiday,
    /// Rotate a random chime on an interval.
    Recurring,
}

/// A schedule as stored/transferred. Optional fields are populated per
/// [`ScheduleType`]; [`ScheduleInput::validate`] enforces the per-type shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleInput {
    /// Operator-facing display name.
    pub name: String,
    /// The chime filename to activate, or the [`RANDOM_SENTINEL`].
    pub chime_filename: String,
    /// Which kind of schedule this is.
    pub schedule_type: ScheduleType,
    /// Weekday names (for [`ScheduleType::Weekly`]).
    #[serde(default)]
    pub days: Vec<String>,
    /// Calendar month 1–12 (for [`ScheduleType::Date`]).
    #[serde(default)]
    pub month: Option<u8>,
    /// Day-of-month 1–31 (for [`ScheduleType::Date`]).
    #[serde(default)]
    pub day: Option<u8>,
    /// Holiday label (for [`ScheduleType::Holiday`]).
    #[serde(default)]
    pub holiday: Option<String>,
    /// Interval token (for [`ScheduleType::Recurring`]).
    #[serde(default)]
    pub interval: Option<String>,
    /// Trigger hour 0–23 (weekly/date).
    #[serde(default)]
    pub hour: Option<u8>,
    /// Trigger minute 0–59 (weekly/date).
    #[serde(default)]
    pub minute: Option<u8>,
    /// Whether the schedule is active. Defaults to enabled.
    #[serde(default = "default_true")]
    pub enabled: bool,
}

fn default_true() -> bool {
    true
}

/// A persisted schedule: an [`ScheduleInput`] plus the server-assigned id.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StoredSchedule {
    /// Stable unique id, server-assigned (e.g. `"sched-3"`).
    pub id: String,
    /// The schedule definition.
    #[serde(flatten)]
    pub input: ScheduleInput,
}

/// A named group of chimes used for scoped random selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChimeGroup {
    /// Stable unique id, server-assigned (e.g. `"group-1"`).
    pub id: String,
    /// Display name.
    pub name: String,
    /// Optional description.
    #[serde(default)]
    pub description: String,
    /// Member chime filenames.
    #[serde(default)]
    pub chimes: Vec<String>,
}

/// Input body for creating/updating a group (no id).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GroupInput {
    /// Display name.
    pub name: String,
    /// Optional description.
    #[serde(default)]
    pub description: String,
    /// Member chime filenames.
    #[serde(default)]
    pub chimes: Vec<String>,
}

/// "Random on boot" configuration: pick a random chime from a group each boot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct RandomMode {
    /// Whether random-on-boot is enabled.
    pub enabled: bool,
    /// The group to draw from, if any.
    #[serde(default)]
    pub group_id: Option<String>,
}

/// Validate a chime filename used by a schedule or group: a single-segment
/// `*.wav` name with no path traversal. The `RANDOM` sentinel is handled by the
/// caller and is not a filename. Mirrors the fail-closed posture of the media
/// upload validators.
///
/// # Errors
/// Returns a [`ValidationError`] when the name is empty, too long, not `.wav`,
/// or contains a path separator / `..`.
pub fn validate_chime_filename(name: &str) -> Result<(), ValidationError> {
    if name.is_empty() {
        return Err(ValidationError::new(
            "empty_filename",
            "chime filename is empty",
        ));
    }
    if name.len() > 100 {
        return Err(ValidationError::new(
            "filename_too_long",
            "chime filename exceeds 100 characters",
        ));
    }
    if name.contains('/') || name.contains('\\') || name.contains("..") {
        return Err(ValidationError::new(
            "unsafe_filename",
            "chime filename must be a single path segment",
        ));
    }
    let is_wav = std::path::Path::new(name)
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("wav"));
    if !is_wav {
        return Err(ValidationError::new(
            "not_wav",
            "chime filename must end in .wav",
        ));
    }
    Ok(())
}

impl ScheduleInput {
    /// Validate this input and lower it into the pure engine [`Schedule`] under
    /// the given `id`. The single boundary where untrusted schedule data
    /// becomes a trusted engine type.
    ///
    /// # Errors
    /// Returns a [`ValidationError`] for any out-of-range or type-mismatched
    /// field (bad weekday, missing date parts, unknown holiday/interval, …).
    pub fn validate(&self, id: &str) -> Result<Schedule, ValidationError> {
        if self.name.trim().is_empty() {
            return Err(ValidationError::new(
                "empty_name",
                "schedule name is required",
            ));
        }
        let chime = if self.chime_filename == RANDOM_SENTINEL {
            ChimeRef::Random
        } else {
            validate_chime_filename(&self.chime_filename)?;
            ChimeRef::Specific(self.chime_filename.clone())
        };

        let kind = match self.schedule_type {
            ScheduleType::Weekly => {
                let (hour, minute) = self.require_time()?;
                if self.days.is_empty() {
                    return Err(ValidationError::new(
                        "no_days",
                        "select at least one day for a weekly schedule",
                    ));
                }
                let mut days = std::collections::BTreeSet::new();
                for d in &self.days {
                    let wd = Weekday::parse(d).ok_or_else(|| {
                        ValidationError::new("bad_weekday", format!("unknown weekday `{d}`"))
                    })?;
                    days.insert(wd);
                }
                ScheduleKind::Weekly { days, hour, minute }
            }
            ScheduleType::Date => {
                let (hour, minute) = self.require_time()?;
                let month = self
                    .month
                    .filter(|m| (1..=12).contains(m))
                    .ok_or_else(|| ValidationError::new("bad_month", "month must be 1–12"))?;
                let day = self.day.ok_or_else(|| {
                    ValidationError::new("bad_day", "day is required for a date schedule")
                })?;
                // Allow Feb 29 by checking against a leap year.
                let max = days_in_month(2024, month);
                if day < 1 || day > max {
                    return Err(ValidationError::new(
                        "bad_day",
                        format!("day must be 1–{max} for month {month}"),
                    ));
                }
                ScheduleKind::Date {
                    month,
                    day,
                    hour,
                    minute,
                }
            }
            ScheduleType::Holiday => {
                let label = self.holiday.as_deref().unwrap_or_default();
                let holiday = Holiday::parse(label).ok_or_else(|| {
                    ValidationError::new("bad_holiday", format!("unknown holiday `{label}`"))
                })?;
                ScheduleKind::Holiday { holiday }
            }
            ScheduleType::Recurring => {
                let tok = self.interval.as_deref().unwrap_or_default();
                let interval = Interval::parse(tok).ok_or_else(|| {
                    ValidationError::new("bad_interval", format!("unknown interval `{tok}`"))
                })?;
                ScheduleKind::Recurring { interval }
            }
        };

        Ok(Schedule {
            id: id.to_owned(),
            name: self.name.clone(),
            chime,
            kind,
            enabled: self.enabled,
        })
    }

    /// Validate and extract the `(hour, minute)` time fields (weekly/date).
    fn require_time(&self) -> Result<(u8, u8), ValidationError> {
        let hour = self
            .hour
            .filter(|h| *h <= 23)
            .ok_or_else(|| ValidationError::new("bad_hour", "hour must be 0–23"))?;
        let minute = self
            .minute
            .filter(|m| *m <= 59)
            .ok_or_else(|| ValidationError::new("bad_minute", "minute must be 0–59"))?;
        Ok((hour, minute))
    }
}

/// The menu metadata the SPA needs to render the scheduler form, derived from
/// the engine's source-of-truth lists so the two never drift.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SchedulerMenus {
    /// Holiday labels, in menu order.
    pub holidays: Vec<String>,
    /// Interval tokens, in menu order.
    pub intervals: Vec<String>,
    /// Weekday names, Monday-first.
    pub weekdays: Vec<String>,
}

impl SchedulerMenus {
    /// Build the menus from the engine constants.
    #[must_use]
    pub fn build() -> Self {
        Self {
            holidays: chime::ALL_HOLIDAYS
                .iter()
                .map(|h| h.label().to_owned())
                .collect(),
            intervals: [
                Interval::OnBoot,
                Interval::Min15,
                Interval::Min30,
                Interval::Hour1,
                Interval::Hour2,
                Interval::Hour4,
                Interval::Hour6,
                Interval::Hour12,
            ]
            .iter()
            .map(|i| i.token().to_owned())
            .collect(),
            weekdays: [
                Weekday::Monday,
                Weekday::Tuesday,
                Weekday::Wednesday,
                Weekday::Thursday,
                Weekday::Friday,
                Weekday::Saturday,
                Weekday::Sunday,
            ]
            .iter()
            .map(|w| w.name().to_owned())
            .collect(),
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::*;

    fn weekly_input() -> ScheduleInput {
        ScheduleInput {
            name: "Morning".to_owned(),
            chime_filename: "Classic.wav".to_owned(),
            schedule_type: ScheduleType::Weekly,
            days: vec!["Monday".to_owned(), "Friday".to_owned()],
            month: None,
            day: None,
            holiday: None,
            interval: None,
            hour: Some(9),
            minute: Some(0),
            enabled: true,
        }
    }

    #[test]
    fn weekly_validates_to_core() {
        let s = weekly_input().validate("sched-1").unwrap();
        assert_eq!(s.id, "sched-1");
        assert!(matches!(s.kind, ScheduleKind::Weekly { .. }));
    }

    #[test]
    fn weekly_rejects_empty_days() {
        let mut i = weekly_input();
        i.days.clear();
        assert_eq!(i.validate("x").unwrap_err().code, "no_days");
    }

    #[test]
    fn weekly_rejects_bad_weekday() {
        let mut i = weekly_input();
        i.days = vec!["Funday".to_owned()];
        assert_eq!(i.validate("x").unwrap_err().code, "bad_weekday");
    }

    #[test]
    fn date_allows_feb_29() {
        let mut i = weekly_input();
        i.schedule_type = ScheduleType::Date;
        i.month = Some(2);
        i.day = Some(29);
        assert!(i.validate("x").is_ok());
    }

    #[test]
    fn date_rejects_feb_30() {
        let mut i = weekly_input();
        i.schedule_type = ScheduleType::Date;
        i.month = Some(2);
        i.day = Some(30);
        assert_eq!(i.validate("x").unwrap_err().code, "bad_day");
    }

    #[test]
    fn holiday_validates() {
        let mut i = weekly_input();
        i.schedule_type = ScheduleType::Holiday;
        i.holiday = Some("Christmas Day".to_owned());
        assert!(matches!(
            i.validate("x").unwrap().kind,
            ScheduleKind::Holiday { .. }
        ));
    }

    #[test]
    fn recurring_validates_random_interval() {
        let mut i = weekly_input();
        i.schedule_type = ScheduleType::Recurring;
        i.interval = Some("1hour".to_owned());
        assert!(matches!(
            i.validate("x").unwrap().kind,
            ScheduleKind::Recurring { .. }
        ));
    }

    #[test]
    fn random_sentinel_is_accepted() {
        let mut i = weekly_input();
        i.chime_filename = RANDOM_SENTINEL.to_owned();
        assert_eq!(i.validate("x").unwrap().chime, ChimeRef::Random);
    }

    #[test]
    fn rejects_filename_traversal() {
        assert_eq!(
            validate_chime_filename("../evil.wav").unwrap_err().code,
            "unsafe_filename"
        );
        assert_eq!(
            validate_chime_filename("sub/dir.wav").unwrap_err().code,
            "unsafe_filename"
        );
        assert_eq!(
            validate_chime_filename("notaudio.txt").unwrap_err().code,
            "not_wav"
        );
    }

    #[test]
    fn menus_match_engine_lists() {
        let m = SchedulerMenus::build();
        assert_eq!(m.holidays.len(), 18);
        assert_eq!(m.intervals.len(), 8);
        assert_eq!(m.weekdays.first().map(String::as_str), Some("Monday"));
    }

    #[test]
    fn schedule_input_round_trips_json() {
        let i = weekly_input();
        let json = serde_json::to_string(&i).unwrap();
        assert!(json.contains("chimeFilename"));
        assert!(json.contains("scheduleType"));
        let back: ScheduleInput = serde_json::from_str(&json).unwrap();
        assert_eq!(back, i);
    }
}
