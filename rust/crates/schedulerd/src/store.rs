//! The persisted scheduler state and its CRUD + evaluation operations.
//!
//! `schedulerd` is the single writer of this state, mirroring the project's
//! ownership rule (`gadgetd` owns the write queue, `schedulerd` owns the
//! schedule state). `webd` is a pure proxy that forwards requests here. State is
//! a single versioned JSON document persisted atomically (sibling temp file →
//! fsync → rename → directory fsync), the same durability recipe the gadgetd
//! queue journal uses.

use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use teslausb_core::chime::{CivilTime, Pick, ScheduleKind, resolve_active, resolve_boot};

use crate::model::{
    ChimeGroup, GroupInput, RandomMode, ScheduleInput, StoredSchedule, ValidationError,
};

/// The current on-disk schema version. Bump when the persisted shape changes.
pub const STATE_VERSION: u32 = 1;

/// The whole persisted scheduler document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SchedulerState {
    /// Schema version of this document.
    pub version: u32,
    /// All configured schedules.
    #[serde(default)]
    pub schedules: Vec<StoredSchedule>,
    /// All chime groups.
    #[serde(default)]
    pub groups: Vec<ChimeGroup>,
    /// Random-on-boot configuration.
    #[serde(default)]
    pub random_mode: RandomMode,
    /// Monotonic id counter, so ids never collide across a restart.
    #[serde(default)]
    pub next_seq: u64,
}

impl Default for SchedulerState {
    fn default() -> Self {
        Self {
            version: STATE_VERSION,
            schedules: Vec::new(),
            groups: Vec::new(),
            random_mode: RandomMode::default(),
            next_seq: 1,
        }
    }
}

/// In-memory state owner bound to its on-disk journal path.
#[derive(Debug)]
pub struct Store {
    path: PathBuf,
    state: SchedulerState,
}

impl Store {
    /// Load the store from `path`, starting empty if the file is missing or
    /// unreadable (the same fail-soft posture as the gadgetd queue journal).
    #[must_use]
    pub fn load(path: PathBuf) -> Self {
        let state = match std::fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_default(),
            Err(_) => SchedulerState::default(),
        };
        Self { path, state }
    }

    /// Borrow the current state (read-only).
    #[must_use]
    pub fn state(&self) -> &SchedulerState {
        &self.state
    }

    /// All schedules.
    #[must_use]
    pub fn schedules(&self) -> &[StoredSchedule] {
        &self.state.schedules
    }

    /// All groups.
    #[must_use]
    pub fn groups(&self) -> &[ChimeGroup] {
        &self.state.groups
    }

    /// The random-on-boot config.
    #[must_use]
    pub fn random_mode(&self) -> &RandomMode {
        &self.state.random_mode
    }

    fn next_id(&mut self, prefix: &str) -> String {
        let n = self.state.next_seq;
        self.state.next_seq = self.state.next_seq.saturating_add(1);
        format!("{prefix}-{n}")
    }

    /// Add a schedule after validating it. Returns the stored record (with its
    /// new id). Persists on success.
    ///
    /// # Errors
    /// [`StoreError::Validation`] if the input is invalid; [`StoreError::Io`]
    /// if persistence fails (the in-memory state is left unchanged on I/O
    /// failure).
    pub fn add_schedule(&mut self, input: ScheduleInput) -> Result<StoredSchedule, StoreError> {
        let id = self.next_id("sched");
        input.validate(&id)?;
        let record = StoredSchedule { id, input };
        self.state.schedules.push(record.clone());
        self.persist()?;
        Ok(record)
    }

    /// Replace an existing schedule by id. Returns the updated record.
    ///
    /// # Errors
    /// [`StoreError::NotFound`] if no schedule has that id;
    /// [`StoreError::Validation`] if the new input is invalid; [`StoreError::Io`]
    /// on persistence failure.
    pub fn update_schedule(
        &mut self,
        id: &str,
        input: ScheduleInput,
    ) -> Result<StoredSchedule, StoreError> {
        input.validate(id)?;
        let slot = self
            .state
            .schedules
            .iter_mut()
            .find(|s| s.id == id)
            .ok_or(StoreError::NotFound)?;
        slot.input = input;
        let record = slot.clone();
        self.persist()?;
        Ok(record)
    }

    /// Delete a schedule by id. Returns whether one was removed.
    ///
    /// # Errors
    /// [`StoreError::Io`] on persistence failure.
    pub fn delete_schedule(&mut self, id: &str) -> Result<bool, StoreError> {
        let before = self.state.schedules.len();
        self.state.schedules.retain(|s| s.id != id);
        let removed = self.state.schedules.len() != before;
        if removed {
            self.persist()?;
        }
        Ok(removed)
    }

    /// Add a chime group. Member filenames are validated.
    ///
    /// # Errors
    /// [`StoreError::Validation`] for an empty name or an invalid member
    /// filename; [`StoreError::Io`] on persistence failure.
    pub fn add_group(&mut self, input: GroupInput) -> Result<ChimeGroup, StoreError> {
        validate_group(&input)?;
        let id = self.next_id("group");
        let group = ChimeGroup {
            id,
            name: input.name,
            description: input.description,
            chimes: input.chimes,
        };
        self.state.groups.push(group.clone());
        self.persist()?;
        Ok(group)
    }

    /// Replace a group by id.
    ///
    /// # Errors
    /// [`StoreError::NotFound`], [`StoreError::Validation`], or
    /// [`StoreError::Io`].
    pub fn update_group(&mut self, id: &str, input: GroupInput) -> Result<ChimeGroup, StoreError> {
        validate_group(&input)?;
        let slot = self
            .state
            .groups
            .iter_mut()
            .find(|g| g.id == id)
            .ok_or(StoreError::NotFound)?;
        slot.name = input.name;
        slot.description = input.description;
        slot.chimes = input.chimes;
        let group = slot.clone();
        self.persist()?;
        Ok(group)
    }

    /// Delete a group by id. If it was the random-mode group, random mode is
    /// disabled. Returns whether one was removed.
    ///
    /// # Errors
    /// [`StoreError::Io`] on persistence failure.
    pub fn delete_group(&mut self, id: &str) -> Result<bool, StoreError> {
        let before = self.state.groups.len();
        self.state.groups.retain(|g| g.id != id);
        let removed = self.state.groups.len() != before;
        if removed {
            if self.state.random_mode.group_id.as_deref() == Some(id) {
                self.state.random_mode = RandomMode::default();
            }
            self.persist()?;
        }
        Ok(removed)
    }

    /// Set the random-on-boot mode. A referenced group must exist.
    ///
    /// # Errors
    /// [`StoreError::NotFound`] if `enabled` with an unknown group;
    /// [`StoreError::Io`] on persistence failure.
    pub fn set_random_mode(&mut self, mode: RandomMode) -> Result<(), StoreError> {
        if mode.enabled {
            let gid = mode
                .group_id
                .as_deref()
                .ok_or(StoreError::Validation(ValidationError {
                    code: "no_group",
                    message: "random mode requires a group".to_owned(),
                }))?;
            if !self.state.groups.iter().any(|g| g.id == gid) {
                return Err(StoreError::NotFound);
            }
        }
        self.state.random_mode = mode;
        self.persist()
    }

    /// Evaluate which chime should be active at `now`. Invalid stored schedules
    /// are skipped (defence-in-depth — they should never have been stored). See
    /// [`teslausb_core::chime::resolve_active`].
    #[must_use]
    pub fn evaluate(
        &self,
        now: CivilTime,
        active_chime: Option<&str>,
        library: &[String],
    ) -> Option<Pick> {
        let core: Vec<_> = self
            .state
            .schedules
            .iter()
            .filter_map(|s| s.input.validate(&s.id).ok())
            .collect();
        resolve_active(now, &core, active_chime, library)
    }

    /// Evaluate the boot-time chime: schedules (including `OnBoot` recurring)
    /// plus the random-on-boot fallback drawn from the configured group.
    #[must_use]
    pub fn evaluate_boot(
        &self,
        now: CivilTime,
        active_chime: Option<&str>,
        library: &[String],
        boot_seed: u64,
    ) -> Option<Pick> {
        let core: Vec<_> = self
            .state
            .schedules
            .iter()
            .filter_map(|s| s.input.validate(&s.id).ok())
            .collect();
        let members = self.random_members(library);
        resolve_boot(now, &core, active_chime, library, members.as_deref(), boot_seed)
    }

    /// Evaluate the boot-time chime when the system clock is NOT yet trustworthy
    /// (no-RTC device before NTP sync). Time-windowed schedules (weekly/date/
    /// holiday/timed-recurring) are skipped because matching them against a bogus
    /// clock is unsafe; clock-INDEPENDENT behaviors are preserved: `Interval::OnBoot`
    /// recurring schedules (boot-event-driven, eligible at minute 0) and the
    /// random-on-boot fallback.
    #[must_use]
    pub fn evaluate_boot_clockless(
        &self,
        now: CivilTime,
        active_chime: Option<&str>,
        library: &[String],
        boot_seed: u64,
    ) -> Option<Pick> {
        let onboot: Vec<_> = self
            .state
            .schedules
            .iter()
            .filter_map(|s| s.input.validate(&s.id).ok())
            .filter(
                |s| matches!(&s.kind, ScheduleKind::Recurring { interval } if interval.minutes().is_none()),
            )
            .collect();
        let members = self.random_members(library);
        resolve_boot(now, &onboot, active_chime, library, members.as_deref(), boot_seed)
    }

    fn random_members(&self, library: &[String]) -> Option<Vec<String>> {
        if !self.state.random_mode.enabled {
            return None;
        }
        let gid = self.state.random_mode.group_id.as_deref()?;
        let group = self.state.groups.iter().find(|g| g.id == gid)?;
        let members = group
            .chimes
            .iter()
            .filter(|name| library.contains(*name))
            .cloned()
            .collect::<Vec<_>>();
        (!members.is_empty()).then_some(members)
    }

    /// Persist the state atomically: sibling temp file → fsync → rename →
    /// directory fsync.
    ///
    /// # Errors
    /// Propagates the first I/O error; the in-memory state is retained.
    pub fn persist(&self) -> Result<(), StoreError> {
        let parent = self.path.parent().unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)?;
        let tmp = with_tmp_suffix(&self.path);
        let bytes = serde_json::to_vec_pretty(&self.state).map_err(io::Error::other)?;
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, &self.path)?;
        if let Ok(dir) = std::fs::File::open(parent) {
            let _ = dir.sync_all();
        }
        Ok(())
    }
}

/// Validate a group input: non-empty name and safe member filenames.
fn validate_group(input: &GroupInput) -> Result<(), ValidationError> {
    if input.name.trim().is_empty() {
        return Err(ValidationError {
            code: "empty_name",
            message: "group name is required".to_owned(),
        });
    }
    for c in &input.chimes {
        crate::model::validate_chime_filename(c)?;
    }
    Ok(())
}

/// `path` with a `.tmp` suffix on the file name, for atomic-rename writes.
fn with_tmp_suffix(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    path.with_file_name(name)
}

/// Errors from store operations.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The input failed validation.
    #[error("validation: {0}")]
    Validation(#[from] ValidationError),
    /// No record matched the given id.
    #[error("not found")]
    NotFound,
    /// Persistence failed.
    #[error("io: {0}")]
    Io(#[from] io::Error),
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::*;
    use crate::model::ScheduleType;
    use teslausb_core::chime::weekday_of;

    fn tmp_path(tag: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!("schedstore-{tag}-{nanos}.json"))
    }

    fn weekly(name: &str, file: &str, day: &str, hour: u8) -> ScheduleInput {
        ScheduleInput {
            name: name.to_owned(),
            chime_filename: file.to_owned(),
            schedule_type: ScheduleType::Weekly,
            days: vec![day.to_owned()],
            month: None,
            day: None,
            holiday: None,
            interval: None,
            hour: Some(hour),
            minute: Some(0),
            enabled: true,
        }
    }

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

    #[test]
    fn add_assigns_sequential_ids_and_persists() {
        let path = tmp_path("ids");
        let mut store = Store::load(path.clone());
        let a = store
            .add_schedule(weekly("A", "A.wav", "Monday", 8))
            .unwrap();
        let b = store
            .add_schedule(weekly("B", "B.wav", "Tuesday", 9))
            .unwrap();
        assert_eq!(a.id, "sched-1");
        assert_eq!(b.id, "sched-2");

        // Reload from disk: both survive and the seq counter persists.
        let reloaded = Store::load(path.clone());
        assert_eq!(reloaded.schedules().len(), 2);
        assert_eq!(reloaded.state().next_seq, 3);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn invalid_schedule_is_rejected_and_not_stored() {
        let path = tmp_path("reject");
        let mut store = Store::load(path.clone());
        let mut bad = weekly("X", "X.wav", "Monday", 8);
        bad.days.clear();
        assert!(store.add_schedule(bad).is_err());
        assert_eq!(store.schedules().len(), 0);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn update_and_delete_schedule() {
        let path = tmp_path("upd");
        let mut store = Store::load(path.clone());
        let s = store
            .add_schedule(weekly("A", "A.wav", "Monday", 8))
            .unwrap();
        let updated = store
            .update_schedule(&s.id, weekly("A2", "B.wav", "Friday", 17))
            .unwrap();
        assert_eq!(updated.input.name, "A2");
        assert!(
            store
                .update_schedule("nope", weekly("Z", "Z.wav", "Monday", 0))
                .is_err()
        );
        assert!(store.delete_schedule(&s.id).unwrap());
        assert!(!store.delete_schedule(&s.id).unwrap());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn group_crud_and_random_mode_guard() {
        let path = tmp_path("grp");
        let mut store = Store::load(path.clone());
        let g = store
            .add_group(GroupInput {
                name: "Holidays".to_owned(),
                description: "festive".to_owned(),
                chimes: vec!["Jingle.wav".to_owned()],
            })
            .unwrap();
        // Enabling random mode for an unknown group fails.
        assert!(
            store
                .set_random_mode(RandomMode {
                    enabled: true,
                    group_id: Some("missing".to_owned()),
                })
                .is_err()
        );
        // Enabling for the real group works.
        store
            .set_random_mode(RandomMode {
                enabled: true,
                group_id: Some(g.id.clone()),
            })
            .unwrap();
        // Deleting that group disables random mode.
        assert!(store.delete_group(&g.id).unwrap());
        assert!(!store.random_mode().enabled);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn group_rejects_unsafe_member() {
        let path = tmp_path("grpbad");
        let mut store = Store::load(path.clone());
        assert!(
            store
                .add_group(GroupInput {
                    name: "Bad".to_owned(),
                    description: String::new(),
                    chimes: vec!["../evil.wav".to_owned()],
                })
                .is_err()
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn evaluate_uses_stored_schedules() {
        let path = tmp_path("eval");
        let mut store = Store::load(path.clone());
        // 2026-01-01 is a Thursday.
        store
            .add_schedule(weekly("Morn", "Classic.wav", "Thursday", 9))
            .unwrap();
        let pick = store.evaluate(ct(2026, 1, 1, 9, 30), None, &[]).unwrap();
        assert_eq!(pick.chime_filename, "Classic.wav");
        // Before the trigger time → nothing active.
        assert!(store.evaluate(ct(2026, 1, 1, 8, 0), None, &[]).is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn evaluate_boot_uses_random_group() {
        let path = tmp_path("bootgroup");
        let mut store = Store::load(path.clone());
        let group = store
            .add_group(GroupInput {
                name: "Boot".to_owned(),
                description: String::new(),
                chimes: vec!["G1.wav".to_owned(), "G2.wav".to_owned()],
            })
            .unwrap();
        store
            .set_random_mode(RandomMode {
                enabled: true,
                group_id: Some(group.id.clone()),
            })
            .unwrap();
        let members = vec!["G1.wav".to_owned(), "G2.wav".to_owned()];
        let pick = store.evaluate_boot(ct(2026, 1, 1, 12, 0), None, &members, 7).unwrap();
        assert!(members.contains(&pick.chime_filename));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn evaluate_boot_skips_missing_members() {
        let path = tmp_path("bootmiss");
        let mut store = Store::load(path.clone());
        let group = store
            .add_group(GroupInput {
                name: "Boot".to_owned(),
                description: String::new(),
                chimes: vec!["Missing.wav".to_owned()],
            })
            .unwrap();
        store
            .set_random_mode(RandomMode {
                enabled: true,
                group_id: Some(group.id.clone()),
            })
            .unwrap();
        assert!(store.evaluate_boot(ct(2026, 1, 1, 12, 0), None, &[], 1).is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn evaluate_boot_clockless_skips_time_windowed_schedules() {
        let path = tmp_path("clockless-weekly");
        let mut store = Store::load(path.clone());
        store
            .add_schedule(weekly("Sched", "Sched.wav", "Thursday", 9))
            .unwrap();
        let group = store
            .add_group(GroupInput {
                name: "Boot".to_owned(),
                description: String::new(),
                chimes: vec!["Rand.wav".to_owned()],
            })
            .unwrap();
        store
            .set_random_mode(RandomMode {
                enabled: true,
                group_id: Some(group.id.clone()),
            })
            .unwrap();

        // Thursday 09:30 → the weekly schedule WOULD be eligible, but the clockless
        // path skips time-windowed schedules → falls through to random Rand.wav.
        let pick = store
            .evaluate_boot_clockless(
                ct(2026, 1, 1, 9, 30),
                None,
                &["Sched.wav".to_owned(), "Rand.wav".to_owned()],
                7,
            )
            .unwrap();
        assert_eq!(pick.chime_filename, "Rand.wav");
        assert_ne!(pick.chime_filename, "Sched.wav");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn evaluate_boot_clockless_honors_onboot_recurring_schedule() {
        let path = tmp_path("clockless-onboot");
        let mut store = Store::load(path.clone());
        store
            .add_schedule(ScheduleInput {
                name: "Boot".to_owned(),
                chime_filename: "Boot.wav".to_owned(),
                schedule_type: ScheduleType::Recurring,
                days: vec![],
                month: None,
                day: None,
                holiday: None,
                interval: Some("on_boot".to_owned()),
                hour: None,
                minute: None,
                enabled: true,
            })
            .unwrap();
        let group = store
            .add_group(GroupInput {
                name: "Boot".to_owned(),
                description: String::new(),
                chimes: vec!["Rand.wav".to_owned()],
            })
            .unwrap();
        store
            .set_random_mode(RandomMode {
                enabled: true,
                group_id: Some(group.id.clone()),
            })
            .unwrap();

        // OnBoot recurring schedule is clock-independent → it is honored even on a
        // bogus clock, beating the random fallback.
        let pick = store
            .evaluate_boot_clockless(
                ct(2026, 1, 1, 12, 0),
                None,
                &["Boot.wav".to_owned(), "Rand.wav".to_owned()],
                7,
            )
            .unwrap();
        assert_eq!(pick.chime_filename, "Boot.wav");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn corrupt_journal_starts_empty() {
        let path = tmp_path("corrupt");
        std::fs::write(&path, b"{ not json").unwrap();
        let store = Store::load(path.clone());
        assert_eq!(store.schedules().len(), 0);
        assert_eq!(store.state().version, STATE_VERSION);
        let _ = std::fs::remove_file(path);
    }
}
