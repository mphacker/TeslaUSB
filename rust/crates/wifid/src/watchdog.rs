//! Liveness watchdog + recovery escalation (`wifid.md` §2.4, `SPEC.md` §2
//! invariant 4) — **pure**, host-tested.
//!
//! When the BCM43436 wedges (the documented SDIO-deadlock class), recovery is
//! **strictly escalating and write-path-safe**:
//!
//! 1. **Chip reset first, always.** Reload `brcmfmac` (`rmmod`/`modprobe`). The
//!    watchdog judges success by *observed chip health after a verify window* —
//!    **never** by the `rmmod`/`modprobe` exit status, so a "command succeeded
//!    but chip still wedged" outcome cannot mask a real failure and stall
//!    escalation.
//! 2. **Pi reboot only as a last resort, only when USB is idle.** After
//!    `max_chip_resets_before_reboot` resets have each failed to restore health,
//!    a reboot is *considered* — and permitted only when gadgetd's
//!    write-heartbeat proves the car is not writing. This is the single
//!    sanctioned non-gadgetd reboot. Any doubt (absent / stale / future /
//!    wrong-boot heartbeat, or `usb_state != Idle`) **fails safe to no reboot**.

use serde::{Deserialize, Serialize};

use crate::config::WatchdogConfig;

/// The chip's liveness signal, judged by the executor (driver responsive, not
/// in a persistent SDIO error). `healthy = false` means "looks wedged".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ChipObservation {
    /// The chip/driver is responding normally.
    pub(crate) healthy: bool,
}

/// USB write-path state as reported by gadgetd's status RPC.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum UsbState {
    /// Host is enumerated but no recent writes (safe to reboot if idle long
    /// enough).
    Idle,
    /// Host is actively writing — **never** reboot.
    Writing,
    /// State could not be determined — treated as unsafe (never reboot).
    Unknown,
}

/// A reading of gadgetd's write-heartbeat. All timestamps are boot-scoped
/// monotonic milliseconds shared with gadgetd (same boot ⇒ comparable); there
/// is no RTC. `boot_id` lets `wifid` reject a reading carried over from a
/// previous boot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct WriteHeartbeat {
    /// Boot identity gadgetd stamped the reading with. Must equal `wifid`'s own
    /// boot id or the reading is rejected.
    pub(crate) boot_id: u64,
    /// When gadgetd produced this reading (monotonic ms). Used for freshness.
    pub(crate) produced_mono_ms: i64,
    /// Monotonic ms of the last observed host write. USB is "idle for the
    /// grace" when `now - last_write_mono_ms >= reboot_idle_grace`.
    pub(crate) last_write_mono_ms: i64,
    /// Coarse USB state.
    pub(crate) usb_state: UsbState,
}

/// The recovery action the orchestrator must take this tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RecoveryAction {
    /// Nothing to do (chip healthy, or wedge still debouncing).
    None,
    /// Reload the `WiFi` driver (`rmmod`/`modprobe brcmfmac`).
    ResetChip,
    /// Last resort: reboot the Pi (USB proven idle).
    RebootPi,
    /// A reboot is wanted but USB is not provably idle — wait, do not reboot.
    WaitUsbBusy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// Chip healthy.
    Healthy,
    /// Looks wedged; debouncing before acting.
    Suspected { since_ms: i64 },
    /// A reset was issued; judging health again at `verify_at_ms`.
    Verifying { verify_at_ms: i64 },
    /// Chip-reset budget exhausted; a reboot is wanted (subject to the gate).
    RebootWanted,
}

/// The pure recovery watchdog.
pub(crate) struct Watchdog {
    wedge_confirm_ms: i64,
    reset_verify_window_ms: i64,
    max_chip_resets_before_reboot: u32,
    reboot_idle_grace_ms: i64,
    heartbeat_max_age_ms: i64,

    phase: Phase,
    /// Chip resets that have each failed to restore health (judged by observed
    /// health after the verify window).
    failed_resets: u32,
}

fn dur_ms(d: std::time::Duration) -> i64 {
    i64::try_from(d.as_millis()).unwrap_or(i64::MAX)
}

impl Watchdog {
    /// Build a watchdog from config, starting [`Phase::Healthy`].
    pub(crate) fn new(cfg: &WatchdogConfig) -> Self {
        Self {
            wedge_confirm_ms: dur_ms(cfg.wedge_confirm),
            reset_verify_window_ms: dur_ms(cfg.reset_verify_window),
            max_chip_resets_before_reboot: cfg.max_chip_resets_before_reboot,
            reboot_idle_grace_ms: dur_ms(cfg.reboot_idle_grace),
            heartbeat_max_age_ms: dur_ms(cfg.heartbeat_max_age),
            phase: Phase::Healthy,
            failed_resets: 0,
        }
    }

    /// True while a reset has been issued / a reboot is pending — the input that
    /// forces the throttle to publish `ChipRecovery` (uploads off).
    pub(crate) fn is_recovering(&self) -> bool {
        matches!(self.phase, Phase::Verifying { .. } | Phase::RebootWanted)
    }

    /// Advance the watchdog one tick.
    ///
    /// `self_boot_id` is `wifid`'s own boot identity, compared against the
    /// heartbeat to reject a stale cross-boot reading.
    pub(crate) fn step(
        &mut self,
        chip: ChipObservation,
        heartbeat: Option<WriteHeartbeat>,
        self_boot_id: u64,
        now_ms: i64,
    ) -> RecoveryAction {
        if chip.healthy {
            // Recovered (or never wedged): clear all escalation state.
            self.phase = Phase::Healthy;
            self.failed_resets = 0;
            return RecoveryAction::None;
        }
        match self.phase {
            Phase::Healthy => {
                self.phase = Phase::Suspected { since_ms: now_ms };
                RecoveryAction::None
            }
            Phase::Suspected { since_ms } => {
                if now_ms - since_ms >= self.wedge_confirm_ms {
                    self.escalate(heartbeat, self_boot_id, now_ms)
                } else {
                    RecoveryAction::None
                }
            }
            Phase::Verifying { verify_at_ms } => {
                if now_ms < verify_at_ms {
                    // Still waiting for the last reset to take effect.
                    RecoveryAction::None
                } else {
                    // Window elapsed and the chip is still unhealthy: that reset
                    // failed (judged by health, not command status).
                    self.failed_resets = self.failed_resets.saturating_add(1);
                    self.escalate(heartbeat, self_boot_id, now_ms)
                }
            }
            Phase::RebootWanted => self.reboot_gate(heartbeat, self_boot_id, now_ms),
        }
    }

    fn escalate(
        &mut self,
        heartbeat: Option<WriteHeartbeat>,
        self_boot_id: u64,
        now_ms: i64,
    ) -> RecoveryAction {
        if self.failed_resets < self.max_chip_resets_before_reboot {
            self.phase = Phase::Verifying {
                verify_at_ms: now_ms + self.reset_verify_window_ms,
            };
            RecoveryAction::ResetChip
        } else {
            self.phase = Phase::RebootWanted;
            self.reboot_gate(heartbeat, self_boot_id, now_ms)
        }
    }

    /// The fail-safe USB-idle reboot gate. Returns [`RecoveryAction::RebootPi`]
    /// only when every safety condition holds; otherwise
    /// [`RecoveryAction::WaitUsbBusy`].
    fn reboot_gate(
        &self,
        heartbeat: Option<WriteHeartbeat>,
        self_boot_id: u64,
        now_ms: i64,
    ) -> RecoveryAction {
        if self.usb_idle_for_reboot(heartbeat, self_boot_id, now_ms) {
            RecoveryAction::RebootPi
        } else {
            RecoveryAction::WaitUsbBusy
        }
    }

    fn usb_idle_for_reboot(
        &self,
        heartbeat: Option<WriteHeartbeat>,
        self_boot_id: u64,
        now_ms: i64,
    ) -> bool {
        // Absent reading ⇒ assume the car may be writing.
        let Some(hb) = heartbeat else {
            return false;
        };
        // Cross-boot reading ⇒ meaningless monotonic timestamps.
        if hb.boot_id != self_boot_id {
            return false;
        }
        // Monotonic-ms fields are never negative; a negative (or our own clock
        // being negative) means a corrupt/untrusted reading ⇒ fail-safe. This
        // also rules out the `i64` subtraction below ever overflowing.
        if hb.produced_mono_ms < 0 || hb.last_write_mono_ms < 0 || now_ms < 0 {
            return false;
        }
        // Future or stale production timestamp ⇒ untrustworthy.
        if hb.produced_mono_ms > now_ms
            || now_ms.saturating_sub(hb.produced_mono_ms) > self.heartbeat_max_age_ms
        {
            return false;
        }
        // Anything but a clean Idle ⇒ never reboot.
        if hb.usb_state != UsbState::Idle {
            return false;
        }
        // Future write timestamp ⇒ untrustworthy.
        if hb.last_write_mono_ms > now_ms {
            return false;
        }
        // Finally: idle long enough.
        now_ms.saturating_sub(hb.last_write_mono_ms) >= self.reboot_idle_grace_ms
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{ChipObservation, RecoveryAction, UsbState, Watchdog, WriteHeartbeat};
    use crate::config::WifidConfig;

    const BOOT: u64 = 42;

    fn wd() -> Watchdog {
        Watchdog::new(&WifidConfig::default().watchdog)
    }

    fn healthy() -> ChipObservation {
        ChipObservation { healthy: true }
    }
    fn wedged() -> ChipObservation {
        ChipObservation { healthy: false }
    }

    fn idle_hb(now: i64) -> WriteHeartbeat {
        WriteHeartbeat {
            boot_id: BOOT,
            produced_mono_ms: now,
            // idle well past the 30s grace
            last_write_mono_ms: now - 60_000,
            usb_state: UsbState::Idle,
        }
    }

    /// Run the watchdog until it escalates past chip resets, returning the
    /// action once the reboot path is reached. `hb` builder is invoked per step.
    fn drive_to_reboot_decision(
        w: &mut Watchdog,
        hb: impl Fn(i64) -> Option<WriteHeartbeat>,
    ) -> RecoveryAction {
        let mut t: i64 = 0;
        let mut last = RecoveryAction::None;
        // 200 ticks of 1s is far more than (confirm + 3*verify).
        for _ in 0..200 {
            last = w.step(wedged(), hb(t), BOOT, t);
            if matches!(last, RecoveryAction::RebootPi | RecoveryAction::WaitUsbBusy) {
                return last;
            }
            t += 1000;
        }
        last
    }

    #[test]
    fn healthy_chip_yields_no_action() {
        let mut w = wd();
        assert_eq!(w.step(healthy(), None, BOOT, 0), RecoveryAction::None);
        assert!(!w.is_recovering());
    }

    #[test]
    fn wedge_is_debounced_then_resets_chip_first() {
        let mut w = wd();
        // First sighting: debouncing, no action.
        assert_eq!(w.step(wedged(), None, BOOT, 0), RecoveryAction::None);
        // Before wedge_confirm (15s): still nothing.
        assert_eq!(w.step(wedged(), None, BOOT, 10_000), RecoveryAction::None);
        // After confirm: the FIRST recovery action is a chip reset, never reboot.
        assert_eq!(
            w.step(wedged(), None, BOOT, 15_000),
            RecoveryAction::ResetChip
        );
        assert!(w.is_recovering());
    }

    #[test]
    fn chip_reset_that_restores_health_stops_escalation() {
        let mut w = wd();
        w.step(wedged(), None, BOOT, 0);
        w.step(wedged(), None, BOOT, 15_000); // ResetChip issued
        // Chip comes back healthy within the verify window.
        assert_eq!(w.step(healthy(), None, BOOT, 20_000), RecoveryAction::None);
        assert!(!w.is_recovering());
    }

    #[test]
    fn reboot_is_refused_while_usb_is_writing() {
        let mut w = wd();
        let writing = |now: i64| {
            Some(WriteHeartbeat {
                boot_id: BOOT,
                produced_mono_ms: now,
                last_write_mono_ms: now,
                usb_state: UsbState::Writing,
            })
        };
        let action = drive_to_reboot_decision(&mut w, writing);
        assert_eq!(
            action,
            RecoveryAction::WaitUsbBusy,
            "rebooted while the car was writing"
        );
    }

    #[test]
    fn reboot_is_refused_when_heartbeat_absent() {
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |_| None);
        assert_eq!(action, RecoveryAction::WaitUsbBusy);
    }

    #[test]
    fn reboot_is_refused_on_wrong_boot_id() {
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |now| {
            let mut hb = idle_hb(now);
            hb.boot_id = BOOT + 1; // different boot
            Some(hb)
        });
        assert_eq!(action, RecoveryAction::WaitUsbBusy);
    }

    #[test]
    fn reboot_is_refused_on_stale_heartbeat() {
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |now| {
            let mut hb = idle_hb(now);
            hb.produced_mono_ms = now - 60_000; // older than heartbeat_max_age (10s)
            Some(hb)
        });
        assert_eq!(action, RecoveryAction::WaitUsbBusy);
    }

    #[test]
    fn reboot_is_refused_on_future_timestamp() {
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |now| {
            let mut hb = idle_hb(now);
            hb.produced_mono_ms = now + 5_000; // future
            Some(hb)
        });
        assert_eq!(action, RecoveryAction::WaitUsbBusy);
    }

    #[test]
    fn reboot_permitted_only_after_chip_resets_exhausted_and_usb_idle() {
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |now| Some(idle_hb(now)));
        assert_eq!(action, RecoveryAction::RebootPi);
    }

    #[test]
    fn reboot_is_refused_on_negative_timestamps_without_panicking() {
        // A corrupt heartbeat with extreme-negative monotonic fields must be
        // rejected fail-safe, and must never overflow the freshness/idle math.
        let mut w = wd();
        let action = drive_to_reboot_decision(&mut w, |_now| {
            Some(WriteHeartbeat {
                boot_id: BOOT,
                produced_mono_ms: i64::MIN,
                last_write_mono_ms: i64::MIN,
                usb_state: UsbState::Idle,
            })
        });
        assert_eq!(action, RecoveryAction::WaitUsbBusy);
    }

    #[test]
    fn chip_reset_is_tried_exactly_max_times_before_reboot() {
        let mut w = wd();
        let mut resets = 0;
        let mut t: i64 = 0;
        let max = WifidConfig::default()
            .watchdog
            .max_chip_resets_before_reboot;
        loop {
            let a = w.step(wedged(), Some(idle_hb(t)), BOOT, t);
            match a {
                RecoveryAction::ResetChip => resets += 1,
                RecoveryAction::RebootPi => break,
                RecoveryAction::WaitUsbBusy => panic!("unexpected wait with idle usb"),
                RecoveryAction::None => {}
            }
            t += 1000;
            assert!(t < 500_000, "never reached reboot");
        }
        assert_eq!(
            resets, max,
            "expected exactly {max} chip resets before reboot"
        );
    }
}
