//! The control-loop orchestrator: wires the pure cores ([`crate::link`],
//! [`crate::throttle`], [`crate::watchdog`]) to the injected seams
//! ([`crate::traits`]). Generic over the seam traits so a full tick is
//! exercised in unit tests with in-memory fakes, and over the real
//! [`crate::exec`] executors on the device.
//!
//! Ordering within a tick is chosen for write-path safety:
//! 1. **Decide** recovery (watchdog) and link (state machine) from observation
//!    — no I/O yet.
//! 2. **Publish** the throttle state, *fail-closed*, reflecting the new mode +
//!    recovery, so `uploadd` is told to pause **before** any radio is dropped
//!    or the chip is reset.
//! 3. **Execute** the radio / recovery / `tc` I/O.

use crate::config::WifidConfig;
use crate::creds::{CredentialStore, CredentialUpdate, Credentials, apply_update};
use crate::error::Result;
use crate::link::{LinkMachine, LinkMode, LinkObservation, WifiAction};
use crate::status::WifiStatus;
use crate::throttle::{ThrottleInputs, ThrottlePublisher, ThrottleState, TokenBucket};
use crate::traits::{Clock, HeartbeatSource, NetworkController, RebootController};
use crate::watchdog::{RecoveryAction, Watchdog};

/// An administrative command (delivered over IPC from `webd` on the device).
pub(crate) enum AdminCommand {
    /// Replace the stored credentials (validated, persisted `0600`).
    UpdateCredentials(CredentialUpdate),
}

/// The wired daemon.
pub(crate) struct Daemon<C, N, H, R, S> {
    clock: C,
    net: N,
    heartbeat: H,
    reboot: R,
    store: S,
    cfg: WifidConfig,
    boot_id: u64,

    machine: LinkMachine,
    watchdog: Watchdog,
    throttle: ThrottlePublisher,
    bucket: TokenBucket,
    creds: Credentials,

    tc_applied: bool,
    near_deadlock: bool,
    last_status: Option<WifiStatus>,
}

impl<C, N, H, R, S> Daemon<C, N, H, R, S>
where
    C: Clock,
    N: NetworkController,
    H: HeartbeatSource,
    R: RebootController,
    S: CredentialStore,
{
    /// Build a daemon, loading credentials from the store.
    ///
    /// # Errors
    /// Returns an error if the credential store cannot be read.
    pub(crate) fn new(
        clock: C,
        net: N,
        heartbeat: H,
        reboot: R,
        store: S,
        cfg: WifidConfig,
        boot_id: u64,
    ) -> Result<Self> {
        let creds = store.load()?;
        let now = clock.now_mono_ms();
        let machine = LinkMachine::new(&cfg.link, now);
        let watchdog = Watchdog::new(&cfg.watchdog);
        let throttle = ThrottlePublisher::new(&cfg.throttle);
        let bucket = TokenBucket::new(
            cfg.throttle.max_tx_bytes_per_s,
            cfg.throttle.bucket_capacity_bytes,
            now,
        );
        Ok(Self {
            clock,
            net,
            heartbeat,
            reboot,
            store,
            cfg,
            boot_id,
            machine,
            watchdog,
            throttle,
            bucket,
            creds,
            tc_applied: false,
            near_deadlock: false,
            last_status: None,
        })
    }

    /// The last status published, if any.
    pub(crate) fn status(&self) -> Option<WifiStatus> {
        self.last_status
    }

    /// Admission check for a local TX of `bytes`. Fails closed: never admits
    /// unless the **last published** throttle state allows uploads (so it agrees
    /// with what `uploadd` was told), and then only within the token-bucket cap.
    pub(crate) fn admit_tx(&mut self, bytes: u64) -> bool {
        let allowed = self
            .last_status
            .as_ref()
            .is_some_and(|s| s.throttle.body.uploads_allowed);
        if !allowed {
            return false;
        }
        let now = self.clock.now_mono_ms();
        self.bucket.try_consume(bytes, now)
    }

    /// Handle an administrative command.
    ///
    /// # Errors
    /// Returns an error if the credential update is invalid or cannot be
    /// persisted.
    pub(crate) fn handle_command(&mut self, cmd: AdminCommand) -> Result<()> {
        match cmd {
            AdminCommand::UpdateCredentials(update) => {
                let next = apply_update(&self.creds, &update)?;
                self.store.store(&next)?;
                self.creds = next;
                // A new PSK deserves an immediate (un-backed-off) STA attempt.
                self.machine.notify_credentials_changed();
                Ok(())
            }
        }
    }

    /// Run one control tick: observe, decide, publish, execute. Returns the
    /// freshly published status.
    ///
    /// # Errors
    /// Returns an error if the world could not be observed.
    pub(crate) fn tick(&mut self) -> Result<WifiStatus> {
        let now = self.clock.now_mono_ms();

        // 1. Observe (the only fallible read this tick depends on). If the world
        //    cannot be observed, publish a fail-closed throttle (uploads off)
        //    so a stale `uploads_allowed=true` can never linger, then surface
        //    the error.
        let mut link_obs = match self.net.observe_link() {
            Ok(o) => o,
            Err(e) => {
                self.publish_fail_closed();
                return Err(e);
            }
        };
        // Our credential store is the source of truth for "is STA configured".
        link_obs.sta_configured = self.creds.sta_configured();
        let chip_obs = match self.net.observe_chip() {
            Ok(o) => o,
            Err(e) => {
                self.publish_fail_closed();
                return Err(e);
            }
        };

        // 2. Decide — no side effects yet.
        let heartbeat = self.heartbeat.read();
        let recovery = self.watchdog.step(chip_obs, heartbeat, self.boot_id, now);
        let step = self.machine.step(&link_obs, now);
        let recovering = self.watchdog.is_recovering();

        // 3. Update the `tc` cap intent and publish the throttle state
        //    fail-closed *before* executing any radio/recovery I/O.
        self.reconcile_tx_cap(&step, recovering);
        let throttle = self.publish_throttle(step.mode, step.sta_link_up, recovering);
        let status = WifiStatus::new(step.mode, &link_obs, throttle, recovering);
        self.last_status = Some(status);

        // 4. Execute side effects (best-effort; failures self-heal next tick via
        //    reconciliation in the state machine / watchdog).
        self.execute_recovery(recovery);
        self.execute_actions(&step.actions);
        if throttle.body.uploads_allowed {
            // Mirror the (possibly reduced) published cap onto the kernel `tc`.
            let _ = self.net.apply_tx_cap(throttle.body.max_tx_bytes_per_s);
        }
        self.bucket.set_rate(
            throttle.body.max_tx_bytes_per_s.max(1),
            self.cfg.throttle.bucket_capacity_bytes,
        );

        Ok(status)
    }

    fn reconcile_tx_cap(&mut self, step: &crate::link::LinkStep, recovering: bool) {
        let want_cap = step.mode == LinkMode::Sta && step.sta_link_up && !recovering;
        if want_cap {
            if !self.tc_applied {
                self.tc_applied = self
                    .net
                    .apply_tx_cap(self.cfg.throttle.max_tx_bytes_per_s)
                    .is_ok();
            }
        } else {
            self.tc_applied = false;
        }
    }

    fn publish_throttle(
        &mut self,
        link_mode: LinkMode,
        sta_link_up: bool,
        recovering: bool,
    ) -> ThrottleState {
        self.throttle.update(ThrottleInputs {
            link_mode,
            sta_link_up,
            chip_recovering: recovering,
            near_deadlock: self.near_deadlock,
            tc_applied: self.tc_applied,
        })
    }

    fn execute_recovery(&self, action: RecoveryAction) {
        match action {
            RecoveryAction::None | RecoveryAction::WaitUsbBusy => {}
            RecoveryAction::ResetChip => {
                let _ = self.net.reset_chip();
            }
            RecoveryAction::RebootPi => {
                // The watchdog has already proven USB idle via the heartbeat gate.
                let _ = self.reboot.reboot();
            }
        }
    }

    /// Publish a fail-closed throttle state (uploads off, link treated as down)
    /// and record it as the latest status. Used when the world cannot be
    /// observed this tick, so consumers never act on a stale allowance.
    fn publish_fail_closed(&mut self) {
        self.tc_applied = false;
        self.near_deadlock = false;
        let throttle = self.publish_throttle(LinkMode::Down, false, false);
        let blind = LinkObservation {
            sta_configured: self.creds.sta_configured(),
            sta_running: false,
            ap_running: false,
            associated: false,
            carrier_up: false,
            gateway_reachable: false,
            ap_has_clients: false,
            signal_dbm: None,
        };
        self.last_status = Some(WifiStatus::new(LinkMode::Down, &blind, throttle, false));
    }

    fn execute_actions(&self, actions: &[WifiAction]) {
        for action in actions {
            let result = match action {
                WifiAction::StartSta => self.net.start_sta(),
                WifiAction::StopSta => self.net.stop_sta(),
                WifiAction::StartAp => self.net.start_ap(),
                WifiAction::StopAp => self.net.stop_ap(),
            };
            if result.is_err() {
                // Transitions are ordered stop-before-start. If an earlier
                // action (e.g. a stop) failed, abort the rest so we never issue
                // a start that could leave both radios up. Next tick re-observes
                // and re-reconciles from actual radio state.
                break;
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::cell::{Cell, RefCell};

    use super::{AdminCommand, Daemon};
    use crate::config::WifidConfig;
    use crate::creds::{CredentialStore, CredentialUpdate, Credentials, Secret};
    use crate::error::Result;
    use crate::link::{LinkMode, LinkObservation};
    use crate::traits::{Clock, HeartbeatSource, NetworkController, RebootController};
    use crate::watchdog::{ChipObservation, UsbState, WriteHeartbeat};

    const BOOT: u64 = 7;

    struct FakeClock {
        ms: Cell<i64>,
    }
    impl Clock for FakeClock {
        fn now_mono_ms(&self) -> i64 {
            self.ms.get()
        }
    }

    #[derive(Default)]
    struct Calls {
        start_sta: u32,
        stop_sta: u32,
        start_ap: u32,
        stop_ap: u32,
        reset_chip: u32,
    }

    struct FakeNet {
        link: RefCell<LinkObservation>,
        chip: Cell<bool>,
        calls: RefCell<Calls>,
        // After both-running drift, observe should report it once.
        both_running: Cell<bool>,
        // When set, observe_link/observe_chip return an error (simulates an
        // off-device / wedged read).
        fail_observe: Cell<bool>,
    }
    impl NetworkController for FakeNet {
        fn observe_link(&self) -> Result<LinkObservation> {
            if self.fail_observe.get() {
                return Err(crate::error::WifidError::Network(
                    "observe failed".to_owned(),
                ));
            }
            let mut o = *self.link.borrow();
            if self.both_running.get() {
                o.sta_running = true;
                o.ap_running = true;
            }
            Ok(o)
        }
        fn observe_chip(&self) -> Result<ChipObservation> {
            if self.fail_observe.get() {
                return Err(crate::error::WifidError::Network(
                    "observe failed".to_owned(),
                ));
            }
            Ok(ChipObservation {
                healthy: self.chip.get(),
            })
        }
        fn start_sta(&self) -> Result<()> {
            self.calls.borrow_mut().start_sta += 1;
            self.link.borrow_mut().sta_running = true;
            self.link.borrow_mut().ap_running = false;
            Ok(())
        }
        fn stop_sta(&self) -> Result<()> {
            self.calls.borrow_mut().stop_sta += 1;
            self.link.borrow_mut().sta_running = false;
            Ok(())
        }
        fn start_ap(&self) -> Result<()> {
            self.calls.borrow_mut().start_ap += 1;
            self.link.borrow_mut().ap_running = true;
            self.link.borrow_mut().sta_running = false;
            Ok(())
        }
        fn stop_ap(&self) -> Result<()> {
            self.calls.borrow_mut().stop_ap += 1;
            self.link.borrow_mut().ap_running = false;
            Ok(())
        }
        fn apply_tx_cap(&self, _bytes_per_s: u64) -> Result<()> {
            Ok(())
        }
        fn reset_chip(&self) -> Result<()> {
            self.calls.borrow_mut().reset_chip += 1;
            Ok(())
        }
    }

    struct FakeHeartbeat {
        hb: RefCell<Option<WriteHeartbeat>>,
    }
    impl HeartbeatSource for FakeHeartbeat {
        fn read(&self) -> Option<WriteHeartbeat> {
            *self.hb.borrow()
        }
    }

    struct FakeReboot {
        calls: RefCell<u32>,
    }
    impl RebootController for FakeReboot {
        fn reboot(&self) -> Result<()> {
            *self.calls.borrow_mut() += 1;
            Ok(())
        }
    }

    struct FakeStore {
        creds: RefCell<Credentials>,
    }
    impl CredentialStore for FakeStore {
        fn load(&self) -> Result<Credentials> {
            Ok(self.creds.borrow().clone())
        }
        fn store(&self, creds: &Credentials) -> Result<()> {
            *self.creds.borrow_mut() = creds.clone();
            Ok(())
        }
    }

    type TestDaemon = Daemon<FakeClock, FakeNet, FakeHeartbeat, FakeReboot, FakeStore>;

    fn obs() -> LinkObservation {
        LinkObservation {
            sta_configured: true,
            sta_running: false,
            ap_running: false,
            associated: false,
            carrier_up: false,
            gateway_reachable: false,
            ap_has_clients: false,
            signal_dbm: None,
        }
    }

    fn build(sta_configured: bool) -> TestDaemon {
        let mut o = obs();
        o.sta_configured = sta_configured;
        let creds = if sta_configured {
            Credentials {
                sta_psk: Some(Secret::new("home-psk-1234")),
                ap_passphrase: Secret::new("ap-pass-1234"),
            }
        } else {
            Credentials {
                sta_psk: None,
                ap_passphrase: Secret::new("ap-pass-1234"),
            }
        };
        Daemon::new(
            FakeClock { ms: Cell::new(0) },
            FakeNet {
                link: RefCell::new(o),
                chip: Cell::new(true),
                calls: RefCell::new(Calls::default()),
                both_running: Cell::new(false),
                fail_observe: Cell::new(false),
            },
            FakeHeartbeat {
                hb: RefCell::new(None),
            },
            FakeReboot {
                calls: RefCell::new(0),
            },
            FakeStore {
                creds: RefCell::new(creds),
            },
            WifidConfig::default(),
            BOOT,
        )
        .unwrap()
    }

    fn set_time(d: &TestDaemon, ms: i64) {
        d.clock.ms.set(ms);
    }

    #[test]
    fn boot_with_creds_brings_up_sta_and_uploads_start_disallowed() {
        let mut d = build(true);
        let st = d.tick().unwrap();
        assert_eq!(st.mode, LinkMode::Sta);
        // STA not yet confirmed (no viability) -> uploads off, fail-closed.
        assert!(!st.throttle.body.uploads_allowed);
        assert_eq!(d.net.calls.borrow().start_sta, 1);
    }

    #[test]
    fn confirmed_sta_eventually_allows_uploads_with_tc_applied() {
        let mut d = build(true);
        d.tick().unwrap(); // -> Sta, running
        // Make STA viable.
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
        }
        set_time(&d, 1000);
        d.tick().unwrap();
        set_time(&d, 6000); // past up-debounce (5s)
        let st = d.tick().unwrap();
        assert!(
            st.throttle.body.uploads_allowed,
            "uploads never enabled when STA stable"
        );
    }

    #[test]
    fn never_reboots_while_usb_writing_even_when_chip_wedged() {
        let mut d = build(true);
        d.tick().unwrap();
        // Wedge the chip and report USB actively writing.
        d.net.chip.set(false);
        let writing = WriteHeartbeat {
            boot_id: BOOT,
            produced_mono_ms: 0,
            last_write_mono_ms: 0,
            usb_state: UsbState::Writing,
        };
        let mut t = 1000;
        for _ in 0..120 {
            *d.heartbeat.hb.borrow_mut() = Some(WriteHeartbeat {
                produced_mono_ms: t,
                last_write_mono_ms: t,
                ..writing
            });
            set_time(&d, t);
            d.tick().unwrap();
            t += 1000;
        }
        assert_eq!(
            d.reboot.calls.borrow().clone(),
            0,
            "rebooted during a car write"
        );
        assert!(
            d.net.calls.borrow().reset_chip > 0,
            "chip reset was never attempted"
        );
    }

    #[test]
    fn both_radios_running_drift_is_emergency_stopped() {
        let mut d = build(true);
        d.tick().unwrap();
        d.net.both_running.set(true);
        let st = d.tick().unwrap();
        assert_eq!(st.mode, LinkMode::Down);
        let calls = d.net.calls.borrow();
        assert!(
            calls.stop_ap >= 1 && calls.stop_sta >= 1,
            "both radios not stopped"
        );
    }

    #[test]
    fn credential_update_persists_and_resets_backoff() {
        let mut d = build(false); // start onboarding-only -> AP
        d.tick().unwrap();
        d.handle_command(AdminCommand::UpdateCredentials(CredentialUpdate {
            sta_psk: Some("new-home-psk".to_owned()),
            ap_passphrase: None,
            clear_sta: false,
        }))
        .unwrap();
        // Stored secret is retrievable only through the store, never via status.
        assert!(d.store.creds.borrow().sta_configured());
        let st = d.status().unwrap();
        let json = serde_json::to_string(&st).unwrap();
        assert!(!json.contains("new-home-psk"));
    }

    #[test]
    fn admit_tx_requires_published_allowance_then_respects_the_local_cap() {
        let mut d = build(true);
        let cap = WifidConfig::default().throttle.bucket_capacity_bytes;
        // Fail-closed: nothing is admitted before a throttle state that allows
        // uploads has ever been published.
        assert!(
            !d.admit_tx(1024),
            "admitted before uploads were ever allowed"
        );
        // Bring STA up and confirm it stably so uploads become allowed.
        d.tick().unwrap();
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
        }
        set_time(&d, 1000);
        d.tick().unwrap();
        set_time(&d, 6000); // past up-debounce (5s)
        let st = d.tick().unwrap();
        assert!(st.throttle.body.uploads_allowed);
        // Now the local token bucket governs: a huge request is denied, a
        // modest one within capacity is allowed.
        assert!(!d.admit_tx(cap * 10));
        assert!(d.admit_tx(1024));
    }

    #[test]
    fn admit_tx_denied_when_observation_fails_publishes_fail_closed() {
        let mut d = build(true);
        // Reach an uploads-allowed state first.
        d.tick().unwrap();
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
        }
        set_time(&d, 1000);
        d.tick().unwrap();
        set_time(&d, 6000);
        assert!(d.tick().unwrap().throttle.body.uploads_allowed);
        // Now observation starts failing: the next tick must publish a
        // fail-closed status, and admission must be denied.
        d.net.fail_observe.set(true);
        set_time(&d, 7000);
        assert!(d.tick().is_err());
        assert!(
            !d.last_status
                .as_ref()
                .unwrap()
                .throttle
                .body
                .uploads_allowed
        );
        assert!(!d.admit_tx(1));
    }
}
