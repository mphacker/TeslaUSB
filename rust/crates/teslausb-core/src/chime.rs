//! Lock-chime scheduler rule engine — pure, deterministic, I/O-free.
//!
//! Tesla plays a single `LockChime.wav` from the MEDIA partition root when the
//! car locks. The scheduler lets the operator keep a **library** of chimes and
//! define **schedules** that decide which library chime should be the active
//! `LockChime.wav` at any given moment. This module is the irreducible core of
//! that feature: given the wall-clock instant (as a civil [`CivilTime`]), the
//! configured [`Schedule`]s, the current active chime, and the available
//! library, [`resolve_active`] returns the chime that *should* be active now.
//!
//! Everything here is a pure function of its inputs. The clock, the random
//! seed, and the library are all injected, so the engine is exhaustively
//! unit-testable with no time, no filesystem, and no RNG state. The daemon that
//! drives it (a per-minute tick) converts the real clock into a [`CivilTime`],
//! calls [`resolve_active`], and — when the answer differs from what is active
//! — enqueues a gadget mutation to swap `LockChime.wav`. None of that side-effect
//! plumbing lives here.
//!
//! ## Priority
//!
//! When several schedules are eligible at the same instant the most *specific*
//! one wins, in the order **date > holiday > weekly > recurring**. Ties at the
//! same priority are broken by the later trigger time, then by id for total
//! determinism.

use std::collections::BTreeSet;

/// Sentinel `chime_filename` meaning "pick a random library chime, avoiding the
/// one that is already active". Mirrors the v1 `RANDOM` option value.
pub const RANDOM_SENTINEL: &str = "RANDOM";

/// A day of the week. `Monday` is the first variant; the discriminant order is
/// not load-bearing (matching is by value).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Weekday {
    /// Monday.
    Monday,
    /// Tuesday.
    Tuesday,
    /// Wednesday.
    Wednesday,
    /// Thursday.
    Thursday,
    /// Friday.
    Friday,
    /// Saturday.
    Saturday,
    /// Sunday.
    Sunday,
}

impl Weekday {
    /// Map ISO weekday number (1 = Monday … 7 = Sunday) to a [`Weekday`].
    #[must_use]
    pub fn from_iso(n: u8) -> Option<Self> {
        Some(match n {
            1 => Self::Monday,
            2 => Self::Tuesday,
            3 => Self::Wednesday,
            4 => Self::Thursday,
            5 => Self::Friday,
            6 => Self::Saturday,
            7 => Self::Sunday,
            _ => return None,
        })
    }

    /// The English name as used by the v1 form values (`"Monday"`, …).
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::Monday => "Monday",
            Self::Tuesday => "Tuesday",
            Self::Wednesday => "Wednesday",
            Self::Thursday => "Thursday",
            Self::Friday => "Friday",
            Self::Saturday => "Saturday",
            Self::Sunday => "Sunday",
        }
    }

    /// Parse an English weekday name (case-sensitive, as the v1 form emits it).
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "Monday" => Self::Monday,
            "Tuesday" => Self::Tuesday,
            "Wednesday" => Self::Wednesday,
            "Thursday" => Self::Thursday,
            "Friday" => Self::Friday,
            "Saturday" => Self::Saturday,
            "Sunday" => Self::Sunday,
            _ => return None,
        })
    }
}

/// A wall-clock instant decomposed into civil fields, as the driving daemon
/// would compute from the local clock. `weekday` is supplied (not derived) so
/// the engine never has to assume a calendar; [`weekday_of`] is provided for
/// callers that need to compute it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CivilTime {
    /// Gregorian year (e.g. 2026).
    pub year: i32,
    /// 1–12.
    pub month: u8,
    /// 1–31.
    pub day: u8,
    /// The day of week for this date (supplied by the caller).
    pub weekday: Weekday,
    /// 0–23.
    pub hour: u8,
    /// 0–59.
    pub minute: u8,
}

impl CivilTime {
    /// Minutes elapsed since local midnight (0–1439).
    #[must_use]
    pub fn minute_of_day(self) -> u32 {
        u32::from(self.hour) * 60 + u32::from(self.minute)
    }
}

/// Compute the weekday of a Gregorian date using Sakamoto's algorithm. Pure
/// integer arithmetic; valid for any year ≥ 1. Used by the daemon adapter and
/// by holiday math.
#[must_use]
pub fn weekday_of(year: i32, month: u8, day: u8) -> Weekday {
    // t[] offsets per Sakamoto; index by (month-1).
    const T: [i32; 12] = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let mut y = year;
    if month < 3 {
        y -= 1;
    }
    let m = month as usize;
    let idx = m.saturating_sub(1).min(11);
    let t = T.get(idx).copied().unwrap_or(0);
    // 0 = Sunday … 6 = Saturday.
    let dow = (y + y / 4 - y / 100 + y / 400 + t + i32::from(day)).rem_euclid(7);
    match dow {
        0 => Weekday::Sunday,
        1 => Weekday::Monday,
        2 => Weekday::Tuesday,
        3 => Weekday::Wednesday,
        4 => Weekday::Thursday,
        5 => Weekday::Friday,
        _ => Weekday::Saturday,
    }
}

/// Convert a Unix timestamp (seconds since the epoch) plus a fixed local-time
/// offset into a [`CivilTime`]. Pure integer date math (Howard Hinnant's
/// `civil_from_days`); no `chrono`, no syscalls. The driving daemon supplies
/// the local UTC offset in seconds (e.g. `-8*3600` for PST); DST handling is
/// the caller's concern when it computes that offset. The `doe`/`doy`/`yoe`
/// bindings are the canonical names from Hinnant's algorithm, kept verbatim.
#[allow(clippy::similar_names)]
#[must_use]
pub fn civil_from_unix(unix_secs: i64, tz_offset_secs: i32) -> CivilTime {
    let local = unix_secs + i64::from(tz_offset_secs);
    let days = local.div_euclid(86_400);
    let secs_of_day = local.rem_euclid(86_400);
    let hour = u8::try_from(secs_of_day / 3600).unwrap_or(0);
    let minute = u8::try_from((secs_of_day % 3600) / 60).unwrap_or(0);

    // civil_from_days: days since 1970-01-01 -> (year, month, day).
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let year_civil = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month_num = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = year_civil + i64::from(month_num <= 2);

    let year_i32 = i32::try_from(year).unwrap_or(1970);
    let month = u8::try_from(month_num).unwrap_or(1);
    let day_u8 = u8::try_from(day).unwrap_or(1);
    CivilTime {
        year: year_i32,
        month,
        day: day_u8,
        weekday: weekday_of(year_i32, month, day_u8),
        hour,
        minute,
    }
}

/// The fixed set of US holidays the v1 scheduler offered. Each resolves to a
/// `(month, day)` for a given year via [`Holiday::date_in_year`]; floating
/// holidays (nth-weekday rules) and Easter (Computus) are computed, fixed-date
/// holidays are constants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Holiday {
    /// January 1.
    NewYearsDay,
    /// 3rd Monday of January.
    MlkDay,
    /// February 14.
    ValentinesDay,
    /// 3rd Monday of February.
    PresidentsDay,
    /// March 17.
    StPatricksDay,
    /// Easter Sunday (Computus).
    Easter,
    /// 2nd Sunday of May.
    MothersDay,
    /// Last Monday of May.
    MemorialDay,
    /// 3rd Sunday of June.
    FathersDay,
    /// July 4.
    IndependenceDay,
    /// 1st Monday of September.
    LaborDay,
    /// 2nd Monday of October.
    ColumbusDay,
    /// October 31.
    Halloween,
    /// November 11.
    VeteransDay,
    /// 4th Thursday of November.
    Thanksgiving,
    /// December 24.
    ChristmasEve,
    /// December 25.
    ChristmasDay,
    /// December 31.
    NewYearsEve,
}

impl Holiday {
    /// The exact label used by the v1 form `value=` attribute, so the wire
    /// contract round-trips byte-for-byte with the captured baseline.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::NewYearsDay => "New Year's Day",
            Self::MlkDay => "Martin Luther King Jr. Day",
            Self::ValentinesDay => "Valentine's Day",
            Self::PresidentsDay => "Presidents' Day",
            Self::StPatricksDay => "St. Patrick's Day",
            Self::Easter => "Easter",
            Self::MothersDay => "Mother's Day",
            Self::MemorialDay => "Memorial Day",
            Self::FathersDay => "Father's Day",
            Self::IndependenceDay => "Independence Day",
            Self::LaborDay => "Labor Day",
            Self::ColumbusDay => "Columbus Day",
            Self::Halloween => "Halloween",
            Self::VeteransDay => "Veterans Day",
            Self::Thanksgiving => "Thanksgiving",
            Self::ChristmasEve => "Christmas Eve",
            Self::ChristmasDay => "Christmas Day",
            Self::NewYearsEve => "New Year's Eve",
        }
    }

    /// Parse the v1 form label back into a [`Holiday`].
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        ALL_HOLIDAYS.iter().copied().find(|h| h.label() == s)
    }

    /// Resolve the `(month, day)` this holiday falls on in `year`.
    #[must_use]
    pub fn date_in_year(self, year: i32) -> (u8, u8) {
        match self {
            Self::NewYearsDay => (1, 1),
            Self::MlkDay => (1, nth_weekday(year, 1, Weekday::Monday, 3)),
            Self::ValentinesDay => (2, 14),
            Self::PresidentsDay => (2, nth_weekday(year, 2, Weekday::Monday, 3)),
            Self::StPatricksDay => (3, 17),
            Self::Easter => easter(year),
            Self::MothersDay => (5, nth_weekday(year, 5, Weekday::Sunday, 2)),
            Self::MemorialDay => (5, last_weekday(year, 5, Weekday::Monday)),
            Self::FathersDay => (6, nth_weekday(year, 6, Weekday::Sunday, 3)),
            Self::IndependenceDay => (7, 4),
            Self::LaborDay => (9, nth_weekday(year, 9, Weekday::Monday, 1)),
            Self::ColumbusDay => (10, nth_weekday(year, 10, Weekday::Monday, 2)),
            Self::Halloween => (10, 31),
            Self::VeteransDay => (11, 11),
            Self::Thanksgiving => (11, nth_weekday(year, 11, Weekday::Thursday, 4)),
            Self::ChristmasEve => (12, 24),
            Self::ChristmasDay => (12, 25),
            Self::NewYearsEve => (12, 31),
        }
    }
}

/// Every holiday, in the v1 menu order — also the source of truth the SPA menu
/// can render from.
pub const ALL_HOLIDAYS: [Holiday; 18] = [
    Holiday::NewYearsDay,
    Holiday::MlkDay,
    Holiday::ValentinesDay,
    Holiday::PresidentsDay,
    Holiday::StPatricksDay,
    Holiday::Easter,
    Holiday::MothersDay,
    Holiday::MemorialDay,
    Holiday::FathersDay,
    Holiday::IndependenceDay,
    Holiday::LaborDay,
    Holiday::ColumbusDay,
    Holiday::Halloween,
    Holiday::VeteransDay,
    Holiday::Thanksgiving,
    Holiday::ChristmasEve,
    Holiday::ChristmasDay,
    Holiday::NewYearsEve,
];

/// The day-of-month of the `n`-th (`1`-based) `target` weekday in `month`.
/// `n` is clamped so callers can't index out of the month.
fn nth_weekday(year: i32, month: u8, target: Weekday, n: u8) -> u8 {
    let first = weekday_of(year, month, 1);
    // Offset (0–6) from the 1st of the month to the first `target`.
    let offset = (iso(target) + 7 - iso(first)) % 7;
    let day = 1 + offset + (u32::from(n.saturating_sub(1))) * 7;
    let max = days_in_month(year, month);
    u8::try_from(day.min(u32::from(max))).unwrap_or(max)
}

/// The day-of-month of the *last* `target` weekday in `month`.
fn last_weekday(year: i32, month: u8, target: Weekday) -> u8 {
    let last = days_in_month(year, month);
    let last_dow = weekday_of(year, month, last);
    let back = (iso(last_dow) + 7 - iso(target)) % 7;
    last.saturating_sub(u8::try_from(back).unwrap_or(0))
}

/// ISO weekday number 1 (Mon) … 7 (Sun) — internal helper for weekday offset
/// math (kept as `u32` to avoid repeated widening).
fn iso(w: Weekday) -> u32 {
    match w {
        Weekday::Monday => 1,
        Weekday::Tuesday => 2,
        Weekday::Wednesday => 3,
        Weekday::Thursday => 4,
        Weekday::Friday => 5,
        Weekday::Saturday => 6,
        Weekday::Sunday => 7,
    }
}

/// Days in `month` of `year`, accounting for Gregorian leap years.
#[must_use]
pub fn days_in_month(year: i32, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 0,
    }
}

/// Gregorian leap-year test.
#[must_use]
pub fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// Easter Sunday `(month, day)` via the Anonymous Gregorian algorithm
/// (Computus). Valid for the Gregorian calendar. The single-letter bindings
/// mirror the canonical algorithm exactly, so renaming them would obscure the
/// reference rather than clarify it.
#[allow(clippy::many_single_char_names)]
fn easter(year: i32) -> (u8, u8) {
    let a = year % 19;
    let b = year / 100;
    let c = year % 100;
    let d = b / 4;
    let e = b % 4;
    let f = (b + 8) / 25;
    let g = (b - f + 1) / 3;
    let h = (19 * a + b - d - g + 15) % 30;
    let i = c / 4;
    let k = c % 4;
    let l = (32 + 2 * e + 2 * i - h - k) % 7;
    let m = (a + 11 * h + 22 * l) / 451;
    let month = (h + l - 7 * m + 114) / 31;
    let day = ((h + l - 7 * m + 114) % 31) + 1;
    (
        u8::try_from(month).unwrap_or(4),
        u8::try_from(day).unwrap_or(1),
    )
}

/// How often a recurring rotation re-picks a random chime.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Interval {
    /// Re-pick once per boot (driven by a boot event, not the minute tick).
    OnBoot,
    /// Every 15 minutes.
    Min15,
    /// Every 30 minutes.
    Min30,
    /// Every hour.
    Hour1,
    /// Every 2 hours.
    Hour2,
    /// Every 4 hours.
    Hour4,
    /// Every 6 hours.
    Hour6,
    /// Every 12 hours.
    Hour12,
}

impl Interval {
    /// The cadence in minutes, or `None` for [`Interval::OnBoot`] (which is
    /// driven by a boot event, not the minute tick).
    #[must_use]
    pub fn minutes(self) -> Option<u32> {
        Some(match self {
            Self::OnBoot => return None,
            Self::Min15 => 15,
            Self::Min30 => 30,
            Self::Hour1 => 60,
            Self::Hour2 => 120,
            Self::Hour4 => 240,
            Self::Hour6 => 360,
            Self::Hour12 => 720,
        })
    }

    /// The v1 form `value=` token.
    #[must_use]
    pub fn token(self) -> &'static str {
        match self {
            Self::OnBoot => "on_boot",
            Self::Min15 => "15min",
            Self::Min30 => "30min",
            Self::Hour1 => "1hour",
            Self::Hour2 => "2hour",
            Self::Hour4 => "4hour",
            Self::Hour6 => "6hour",
            Self::Hour12 => "12hour",
        }
    }

    /// Parse the v1 form token.
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "on_boot" => Self::OnBoot,
            "15min" => Self::Min15,
            "30min" => Self::Min30,
            "1hour" => Self::Hour1,
            "2hour" => Self::Hour2,
            "4hour" => Self::Hour4,
            "6hour" => Self::Hour6,
            "12hour" => Self::Hour12,
            _ => return None,
        })
    }
}

/// Which chime a schedule activates: a specific library file, or a random pick.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChimeRef {
    /// A specific library filename, e.g. `"Classic.wav"`.
    Specific(String),
    /// Pick a random library chime, avoiding the currently-active one.
    Random,
}

impl ChimeRef {
    /// Build from a wire `chime_filename`: the [`RANDOM_SENTINEL`] maps to
    /// [`ChimeRef::Random`], anything else to [`ChimeRef::Specific`].
    #[must_use]
    pub fn from_wire(filename: &str) -> Self {
        if filename == RANDOM_SENTINEL {
            Self::Random
        } else {
            Self::Specific(filename.to_owned())
        }
    }
}

/// The trigger rule of a schedule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleKind {
    /// Fire at `hour:minute` on each selected weekday.
    Weekly {
        /// The weekdays this schedule fires on.
        days: BTreeSet<Weekday>,
        /// Trigger hour (0–23).
        hour: u8,
        /// Trigger minute (0–59).
        minute: u8,
    },
    /// Fire at `hour:minute` every year on `month`/`day`.
    Date {
        /// Trigger month (1–12).
        month: u8,
        /// Trigger day-of-month (1–31).
        day: u8,
        /// Trigger hour (0–23).
        hour: u8,
        /// Trigger minute (0–59).
        minute: u8,
    },
    /// Fire at 00:00 on the holiday (each year).
    Holiday {
        /// The holiday this schedule fires on.
        holiday: Holiday,
    },
    /// Rotate a random chime on the given cadence. Always treated as random.
    Recurring {
        /// The rotation cadence.
        interval: Interval,
    },
}

impl ScheduleKind {
    /// Specificity rank for tie-breaking — higher wins (date > holiday > weekly
    /// > recurring).
    fn priority(&self) -> u8 {
        match self {
            Self::Date { .. } => 3,
            Self::Holiday { .. } => 2,
            Self::Weekly { .. } => 1,
            Self::Recurring { .. } => 0,
        }
    }
}

/// A single configured schedule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    /// Stable unique identifier.
    pub id: String,
    /// Operator-facing display name.
    pub name: String,
    /// The chime to activate. A [`ScheduleKind::Recurring`] is always random
    /// regardless of this field (v1 behavior), but it is preserved for editing.
    pub chime: ChimeRef,
    /// The trigger rule.
    pub kind: ScheduleKind,
    /// Whether the schedule participates in resolution.
    pub enabled: bool,
}

/// The engine's verdict: the chime that should be active now, and which
/// schedule decided it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pick {
    /// The id of the schedule that decided the active chime.
    pub schedule_id: String,
    /// The display name of the deciding schedule.
    pub schedule_name: String,
    /// The resolved concrete library filename (never the `RANDOM` sentinel).
    pub chime_filename: String,
}

/// Resolve which chime should be active at `now`.
///
/// Evaluates every enabled schedule that has already triggered today (its
/// trigger minute ≤ `now`), and returns the most recently-triggered one,
/// breaking ties by priority (date > holiday > weekly > recurring) then id. A
/// `RANDOM` chime — or any recurring schedule — resolves to a concrete library
/// file chosen deterministically from `library` (excluding `active_chime` when
/// possible) so the same instant always yields the same pick.
///
/// Returns `None` when no schedule has triggered yet today (the caller should
/// leave the active chime unchanged) or when a winning random schedule has no
/// usable library candidate.
#[must_use]
pub fn resolve_active(
    now: CivilTime,
    schedules: &[Schedule],
    active_chime: Option<&str>,
    library: &[String],
) -> Option<Pick> {
    let now_min = now.minute_of_day();

    let mut best: Option<(&Schedule, u32, u32)> = None; // (schedule, trigger_min, boundary)
    for sched in schedules.iter().filter(|s| s.enabled) {
        let Some((trigger, boundary)) = trigger_today(&sched.kind, now) else {
            continue;
        };
        if trigger > now_min {
            continue;
        }
        let candidate = (sched, trigger, boundary);
        best = Some(match best {
            None => candidate,
            Some(cur) => pick_winner(cur, candidate),
        });
    }

    let (winner, _trigger, boundary) = best?;
    let chime_filename = resolve_chime(winner, boundary, active_chime, library)?;
    Some(Pick {
        schedule_id: winner.id.clone(),
        schedule_name: winner.name.clone(),
        chime_filename,
    })
}

/// Resolve the chime that should be active at device boot.
///
/// Same evaluation as [`resolve_active`], except that `Interval::OnBoot`
/// recurring schedules are treated as triggered at boot (trigger minute 0,
/// boundary 0). If no schedule wins and `random_members` provides candidates,
/// return the lowest-priority random-on-boot default seeded by `boot_seed`.
#[must_use]
pub fn resolve_boot(
    now: CivilTime,
    schedules: &[Schedule],
    active_chime: Option<&str>,
    library: &[String],
    random_members: Option<&[String]>,
    boot_seed: u64,
) -> Option<Pick> {
    let now_min = now.minute_of_day();
    let mut best: Option<(&Schedule, u32, u32)> = None;
    for sched in schedules.iter().filter(|s| s.enabled) {
        let Some((trigger, boundary)) = trigger_today_boot(&sched.kind, now) else {
            continue;
        };
        if trigger > now_min {
            continue;
        }
        let candidate = (sched, trigger, boundary);
        best = Some(match best {
            None => candidate,
            Some(cur) => pick_winner(cur, candidate),
        });
    }

    if let Some((winner, _trigger, boundary)) = best {
        let chime_filename = resolve_chime(winner, boundary, active_chime, library)?;
        return Some(Pick {
            schedule_id: winner.id.clone(),
            schedule_name: winner.name.clone(),
            chime_filename,
        });
    }

    let members = random_members.filter(|m| !m.is_empty())?;
    let pool: Vec<&String> = members
        .iter()
        .filter(|f| Some(f.as_str()) != active_chime)
        .collect();
    let pool = if pool.is_empty() {
        members.iter().collect::<Vec<_>>()
    } else {
        pool
    };
    let seed = seed_for("random-on-boot", 0)
        .wrapping_mul(0x0000_0100_0000_01b3)
        .wrapping_add(boot_seed);
    let len = u64::try_from(pool.len()).unwrap_or(1).max(1);
    let idx = usize::try_from(seed % len).unwrap_or(0);
    let chime_filename = (*pool.get(idx)?).clone();
    Some(Pick {
        schedule_id: "random-on-boot".to_owned(),
        schedule_name: "Random on boot".to_owned(),
        chime_filename,
    })
}

/// Choose the winning schedule between two eligible candidates: later trigger
/// wins; on a tie, higher priority; on a further tie, lexicographically larger
/// id (total order → deterministic).
fn pick_winner<'a>(
    a: (&'a Schedule, u32, u32),
    b: (&'a Schedule, u32, u32),
) -> (&'a Schedule, u32, u32) {
    let key = |c: &(&Schedule, u32, u32)| (c.1, c.0.kind.priority(), c.0.id.clone());
    if key(&b) > key(&a) { b } else { a }
}

/// The trigger minute-of-day for boot evaluation, where `OnBoot` recurring
/// schedules are eligible at trigger minute 0.
fn trigger_today_boot(kind: &ScheduleKind, now: CivilTime) -> Option<(u32, u32)> {
    match kind {
        ScheduleKind::Recurring { interval } if interval.minutes().is_none() => Some((0, 0)),
        _ => trigger_today(kind, now),
    }
}

/// The trigger minute-of-day for `kind` *today* (if it applies today), plus a
/// "boundary" discriminator that makes recurring random picks change once per
/// interval window. Returns `None` when the schedule does not apply today.
fn trigger_today(kind: &ScheduleKind, now: CivilTime) -> Option<(u32, u32)> {
    match kind {
        ScheduleKind::Weekly { days, hour, minute } => {
            if !days.contains(&now.weekday) {
                return None;
            }
            Some((u32::from(*hour) * 60 + u32::from(*minute), 0))
        }
        ScheduleKind::Date {
            month,
            day,
            hour,
            minute,
        } => {
            if now.month != *month || now.day != *day {
                return None;
            }
            Some((u32::from(*hour) * 60 + u32::from(*minute), 0))
        }
        ScheduleKind::Holiday { holiday } => {
            if holiday.date_in_year(now.year) != (now.month, now.day) {
                return None;
            }
            Some((0, 0))
        }
        ScheduleKind::Recurring { interval } => {
            let step = interval.minutes()?; // OnBoot is not minute-driven.
            if step == 0 {
                return None;
            }
            let boundary = now.minute_of_day() / step;
            Some((boundary * step, boundary))
        }
    }
}

/// Resolve a schedule's [`ChimeRef`] to a concrete library filename. Recurring
/// schedules and the `Random` ref pick deterministically from `library`,
/// avoiding `active_chime` when another candidate exists. The pick is seeded by
/// the schedule id, the calendar day, and the recurring `boundary`, so it is
/// stable within its window but rotates across windows.
fn resolve_chime(
    sched: &Schedule,
    boundary: u32,
    active_chime: Option<&str>,
    library: &[String],
) -> Option<String> {
    let is_random = matches!(sched.kind, ScheduleKind::Recurring { .. })
        || matches!(sched.chime, ChimeRef::Random);

    if !is_random {
        if let ChimeRef::Specific(name) = &sched.chime {
            // A specific pick is honored even if it is not (yet) in the library
            // listing — the library is advisory here, the gadget swap validates.
            return Some(name.clone());
        }
    }

    if library.is_empty() {
        return None;
    }
    // Candidate pool excludes the active chime when that leaves ≥1 option.
    let pool: Vec<&String> = library
        .iter()
        .filter(|f| Some(f.as_str()) != active_chime)
        .collect();
    let pool = if pool.is_empty() {
        library.iter().collect::<Vec<_>>()
    } else {
        pool
    };
    let seed = seed_for(&sched.id, boundary);
    let len = u64::try_from(pool.len()).unwrap_or(1).max(1);
    let idx = usize::try_from(seed % len).unwrap_or(0);
    pool.get(idx).map(|s| (*s).clone())
}

/// A small deterministic seed from a schedule id + boundary, via FNV-1a over
/// the id mixed with the boundary. No RNG crate needed; the modulo in
/// [`resolve_chime`] turns it into an index.
fn seed_for(id: &str, boundary: u32) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in id.as_bytes() {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    for b in boundary.to_le_bytes() {
        hash ^= u64::from(b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::*;

    fn ct(year: i32, month: u8, day: u8, hour: u8, minute: u8) -> CivilTime {
        CivilTime {
            year,
            month,
            day,
            weekday: weekday_of(year, month, day),
            hour,
            minute,
        }
    }

    fn weekly(id: &str, file: &str, days: &[Weekday], hour: u8, minute: u8) -> Schedule {
        Schedule {
            id: id.to_owned(),
            name: id.to_owned(),
            chime: ChimeRef::from_wire(file),
            kind: ScheduleKind::Weekly {
                days: days.iter().copied().collect(),
                hour,
                minute,
            },
            enabled: true,
        }
    }

    #[test]
    fn weekday_of_known_dates() {
        // 2026-01-01 is a Thursday; 2026-07-04 a Saturday; 2000-01-01 a Saturday.
        assert_eq!(weekday_of(2026, 1, 1), Weekday::Thursday);
        assert_eq!(weekday_of(2026, 7, 4), Weekday::Saturday);
        assert_eq!(weekday_of(2000, 1, 1), Weekday::Saturday);
    }

    #[test]
    fn fixed_holidays() {
        assert_eq!(Holiday::NewYearsDay.date_in_year(2026), (1, 1));
        assert_eq!(Holiday::IndependenceDay.date_in_year(2026), (7, 4));
        assert_eq!(Holiday::ChristmasDay.date_in_year(2026), (12, 25));
        assert_eq!(Holiday::Halloween.date_in_year(2026), (10, 31));
    }

    #[test]
    fn floating_holidays_2026() {
        // Cross-checked against the v1 captured baseline labels.
        assert_eq!(Holiday::MlkDay.date_in_year(2026), (1, 19));
        assert_eq!(Holiday::PresidentsDay.date_in_year(2026), (2, 16));
        assert_eq!(Holiday::MothersDay.date_in_year(2026), (5, 10));
        assert_eq!(Holiday::MemorialDay.date_in_year(2026), (5, 25));
        assert_eq!(Holiday::FathersDay.date_in_year(2026), (6, 21));
        assert_eq!(Holiday::LaborDay.date_in_year(2026), (9, 7));
        assert_eq!(Holiday::ColumbusDay.date_in_year(2026), (10, 12));
        assert_eq!(Holiday::Thanksgiving.date_in_year(2026), (11, 26));
    }

    #[test]
    fn easter_computus() {
        // Known Easter Sundays.
        assert_eq!(Holiday::Easter.date_in_year(2026), (4, 5));
        assert_eq!(Holiday::Easter.date_in_year(2024), (3, 31));
        assert_eq!(Holiday::Easter.date_in_year(2025), (4, 20));
    }

    #[test]
    fn holiday_labels_round_trip() {
        for h in ALL_HOLIDAYS {
            assert_eq!(Holiday::parse(h.label()), Some(h));
        }
    }

    #[test]
    fn no_schedule_triggered_yet_returns_none() {
        let s = weekly("morning", "Classic.wav", &[Weekday::Thursday], 9, 0);
        // 2026-01-01 is Thursday; before 09:00 nothing has fired.
        let pick = resolve_active(ct(2026, 1, 1, 8, 59), &[s], None, &[]);
        assert!(pick.is_none());
    }

    #[test]
    fn weekly_triggers_on_its_day_after_time() {
        let s = weekly("morning", "Classic.wav", &[Weekday::Thursday], 9, 0);
        let pick = resolve_active(ct(2026, 1, 1, 9, 0), &[s], None, &[]).unwrap();
        assert_eq!(pick.chime_filename, "Classic.wav");
        assert_eq!(pick.schedule_id, "morning");
    }

    #[test]
    fn weekly_does_not_trigger_on_other_days() {
        let s = weekly("morning", "Classic.wav", &[Weekday::Monday], 9, 0);
        // Thursday — not a configured day.
        assert!(resolve_active(ct(2026, 1, 1, 23, 0), &[s], None, &[]).is_none());
    }

    #[test]
    fn later_trigger_wins_same_priority() {
        let early = weekly("early", "A.wav", &[Weekday::Thursday], 8, 0);
        let late = weekly("late", "B.wav", &[Weekday::Thursday], 17, 0);
        let pick = resolve_active(ct(2026, 1, 1, 18, 0), &[early, late], None, &[]).unwrap();
        assert_eq!(pick.chime_filename, "B.wav");
    }

    #[test]
    fn priority_breaks_trigger_tie() {
        // Both trigger at midnight; a Date schedule outranks a Holiday one.
        let holiday = Schedule {
            id: "hol".to_owned(),
            name: "hol".to_owned(),
            chime: ChimeRef::from_wire("Hol.wav"),
            kind: ScheduleKind::Holiday {
                holiday: Holiday::NewYearsDay,
            },
            enabled: true,
        };
        let date = Schedule {
            id: "date".to_owned(),
            name: "date".to_owned(),
            chime: ChimeRef::from_wire("Date.wav"),
            kind: ScheduleKind::Date {
                month: 1,
                day: 1,
                hour: 0,
                minute: 0,
            },
            enabled: true,
        };
        let pick = resolve_active(ct(2026, 1, 1, 12, 0), &[holiday, date], None, &[]).unwrap();
        assert_eq!(pick.chime_filename, "Date.wav");
    }

    #[test]
    fn disabled_schedule_is_ignored() {
        let mut s = weekly("morning", "Classic.wav", &[Weekday::Thursday], 9, 0);
        s.enabled = false;
        assert!(resolve_active(ct(2026, 1, 1, 12, 0), &[s], None, &[]).is_none());
    }

    #[test]
    fn random_avoids_active_chime() {
        let s = weekly("rand", RANDOM_SENTINEL, &[Weekday::Thursday], 0, 0);
        let lib = vec!["A.wav".to_owned(), "B.wav".to_owned()];
        // With "A.wav" active, a 2-item library must resolve to "B.wav".
        let pick = resolve_active(ct(2026, 1, 1, 12, 0), &[s], Some("A.wav"), &lib).unwrap();
        assert_eq!(pick.chime_filename, "B.wav");
    }

    #[test]
    fn random_single_library_falls_back_to_only_chime() {
        let s = weekly("rand", RANDOM_SENTINEL, &[Weekday::Thursday], 0, 0);
        let lib = vec!["Only.wav".to_owned()];
        let pick = resolve_active(ct(2026, 1, 1, 12, 0), &[s], Some("Only.wav"), &lib).unwrap();
        assert_eq!(pick.chime_filename, "Only.wav");
    }

    #[test]
    fn random_empty_library_yields_none() {
        let s = weekly("rand", RANDOM_SENTINEL, &[Weekday::Thursday], 0, 0);
        assert!(resolve_active(ct(2026, 1, 1, 12, 0), &[s], None, &[]).is_none());
    }

    #[test]
    fn recurring_rotates_on_interval_boundary() {
        let s = Schedule {
            id: "rot".to_owned(),
            name: "rot".to_owned(),
            chime: ChimeRef::Random,
            kind: ScheduleKind::Recurring {
                interval: Interval::Hour1,
            },
            enabled: true,
        };
        let lib = vec!["A.wav".to_owned(), "B.wav".to_owned(), "C.wav".to_owned()];
        // Same hour window → identical pick across minutes.
        let p1 = resolve_active(ct(2026, 1, 1, 10, 5), &[s.clone()], None, &lib).unwrap();
        let p2 = resolve_active(ct(2026, 1, 1, 10, 55), &[s.clone()], None, &lib).unwrap();
        assert_eq!(p1.chime_filename, p2.chime_filename);
        // A later window may differ; at minimum it stays a valid library member.
        let p3 = resolve_active(ct(2026, 1, 1, 13, 5), &[s], None, &lib).unwrap();
        assert!(lib.contains(&p3.chime_filename));
    }

    #[test]
    fn recurring_on_boot_is_not_minute_driven() {
        let s = Schedule {
            id: "boot".to_owned(),
            name: "boot".to_owned(),
            chime: ChimeRef::Random,
            kind: ScheduleKind::Recurring {
                interval: Interval::OnBoot,
            },
            enabled: true,
        };
        let lib = vec!["A.wav".to_owned()];
        assert!(resolve_active(ct(2026, 1, 1, 12, 0), &[s], None, &lib).is_none());
    }

    #[test]
    fn resolve_boot_fires_on_boot_recurring() {
        let s = Schedule {
            id: "boot".to_owned(),
            name: "boot".to_owned(),
            chime: ChimeRef::Random,
            kind: ScheduleKind::Recurring {
                interval: Interval::OnBoot,
            },
            enabled: true,
        };
        let lib = vec!["A.wav".to_owned(), "B.wav".to_owned()];
        let pick = resolve_boot(ct(2026, 1, 1, 12, 0), &[s], None, &lib, None, 0).unwrap();
        assert!(lib.contains(&pick.chime_filename));
    }

    #[test]
    fn resolve_boot_random_default_when_no_schedule() {
        let members = vec!["X.wav".to_owned(), "Y.wav".to_owned()];
        let pick = resolve_boot(ct(2026, 1, 1, 12, 0), &[], None, &[], Some(&members), 7).unwrap();
        assert_eq!(pick.schedule_id, "random-on-boot");
        assert!(members.contains(&pick.chime_filename));
    }

    #[test]
    fn resolve_boot_schedule_beats_random_default() {
        let sched = weekly("weekly", "Specific.wav", &[Weekday::Thursday], 12, 0);
        let members = vec!["X.wav".to_owned(), "Y.wav".to_owned()];
        let pick = resolve_boot(ct(2026, 1, 1, 12, 0), &[sched], None, &[], Some(&members), 9).unwrap();
        assert_eq!(pick.chime_filename, "Specific.wav");
    }

    #[test]
    fn resolve_boot_random_excludes_active() {
        let members = vec!["A.wav".to_owned(), "B.wav".to_owned()];
        let pick = resolve_boot(ct(2026, 1, 1, 12, 0), &[], Some("A.wav"), &members, Some(&members), 1).unwrap();
        assert_eq!(pick.chime_filename, "B.wav");
    }

    #[test]
    fn resolve_boot_none_when_nothing() {
        assert!(resolve_boot(ct(2026, 1, 1, 12, 0), &[], None, &[], None, 0).is_none());
    }

    #[test]
    fn resolve_boot_seed_is_stable() {
        let members = vec!["A.wav".to_owned(), "B.wav".to_owned()];
        let a = resolve_boot(ct(2026, 1, 1, 12, 0), &[], None, &members, Some(&members), 7).unwrap();
        let b = resolve_boot(ct(2026, 1, 1, 12, 0), &[], None, &members, Some(&members), 7).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn resolve_active_random_is_stable_without_active_exclusion() {
        // Regression: the chime enforcer must pass `active_chime=None` on each
        // tick. With a constant seed (same id/day/boundary) a RANDOM schedule
        // then resolves the SAME file every call, so the enforcer dedupes to a
        // no-op (no per-minute gadgetd handoff). Feeding the previous pick back
        // as `active_chime` would instead flip the pick every tick.
        let s = weekly("rnd", "RANDOM", &[Weekday::Thursday], 8, 0);
        let lib = vec!["A.wav".to_owned(), "B.wav".to_owned(), "C.wav".to_owned()];
        let now = ct(2026, 1, 1, 12, 0);
        let first = resolve_active(now, std::slice::from_ref(&s), None, &lib).unwrap();
        let second = resolve_active(now, std::slice::from_ref(&s), None, &lib).unwrap();
        assert_eq!(first.chime_filename, second.chime_filename);
    }

    #[test]
    fn interval_tokens_round_trip() {
        for tok in [
            "on_boot", "15min", "30min", "1hour", "2hour", "4hour", "6hour", "12hour",
        ] {
            assert_eq!(Interval::parse(tok).unwrap().token(), tok);
        }
    }

    #[test]
    fn nth_and_last_weekday_bounds() {
        // Feb 2026: 3rd Monday = 16; last Monday of May 2026 = 25.
        assert_eq!(nth_weekday(2026, 2, Weekday::Monday, 3), 16);
        assert_eq!(last_weekday(2026, 5, Weekday::Monday), 25);
    }

    #[test]
    fn civil_from_unix_epoch_and_offsets() {
        // 1970-01-01T00:00:00Z is a Thursday.
        let t = civil_from_unix(0, 0);
        assert_eq!((t.year, t.month, t.day), (1970, 1, 1));
        assert_eq!(t.weekday, Weekday::Thursday);
        assert_eq!((t.hour, t.minute), (0, 0));

        // 2026-01-01T12:34:00Z.
        // 56 years from 1970 incl. 14 leap days: compute the known instant.
        let secs = 1_767_270_840_i64; // 2026-01-01T12:34:00Z
        let t = civil_from_unix(secs, 0);
        assert_eq!((t.year, t.month, t.day), (2026, 1, 1));
        assert_eq!((t.hour, t.minute), (12, 34));
        assert_eq!(t.weekday, Weekday::Thursday);

        // A negative tz offset can roll the civil date back across midnight.
        let t = civil_from_unix(secs, -13 * 3600);
        assert_eq!((t.year, t.month, t.day), (2025, 12, 31));
    }
}
