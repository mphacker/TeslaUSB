//! The STA/AP link state machine (`wifid.md` §2.1) — **pure**, host-tested.
//!
//! Connect to home `WiFi` (STA) when reachable; fall back to a WPA2 access point
//! (AP) for onboarding when it is not. The two **hard** rules this module
//! enforces:
//!
//! 1. **Never AP and STA at once.** A transition always *stops the current
//!    radio before starting the other* (`emit_transition`), and every step
//!    **reconciles against the actually-observed radio state** — so OS drift, a
//!    daemon restart, or a partial executor failure that leaves both radios up
//!    is detected and corrected with an emergency stop-both. Mutual exclusion
//!    is enforced against reality, not merely against intent.
//! 2. **Debounce flaps.** STA only falls back to AP after it has been
//!    non-viable continuously for `sta_down_debounce`; the onboarding AP is
//!    kept *sticky* (minimum uptime, never torn down while a client is joined)
//!    and STA re-probes use capped exponential backoff, so a permanently-down
//!    home network cannot thrash the AP a phone is trying to use.
//!
//! "Reachable" deliberately means **gateway/LAN** reachability, not cloud/WAN:
//! a cloud outage must not drop the user off their own LAN UI. Cloud
//! reachability is `uploadd`'s concern, surfaced via the throttle/upload plane,
//! not a reason to switch radio mode.

use std::time::Duration;

use serde::Serialize;

/// Which radio is active. Exactly one value at a time — the type itself cannot
/// represent "both", and the executor reconciliation upholds that against the
/// live system.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum LinkMode {
    /// Neither radio active (initial / between transitions / post-emergency).
    Down,
    /// Station mode: associating to / connected to home `WiFi`.
    Sta,
    /// WPA2 access-point onboarding mode (hostapd + dnsmasq).
    Ap,
}

/// One sample of the world fed to [`LinkMachine::step`]. Carries both the
/// *actual* radio-running state (for reconciliation) and the link facts (for
/// viability).
// Seven independent boolean facts read from distinct sources (creds presence,
// two radio-running flags, association, carrier, gateway probe, AP clients);
// folding them into an enum would lose that 1:1 correspondence with what the
// netlink/driver layer reports.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct LinkObservation {
    /// STA credentials are present (we have something to connect to).
    pub(crate) sta_configured: bool,
    /// STA (client) mode is actually running right now.
    pub(crate) sta_running: bool,
    /// AP mode is actually running right now.
    pub(crate) ap_running: bool,
    /// Associated to the home BSSID.
    pub(crate) associated: bool,
    /// Carrier + IP are up on the STA interface.
    pub(crate) carrier_up: bool,
    /// The cheap gateway/LAN reachability probe (ping/DNS) succeeded.
    pub(crate) gateway_reachable: bool,
    /// At least one station is currently associated to our AP (onboarding in
    /// progress) — keeps the AP sticky.
    pub(crate) ap_has_clients: bool,
    /// Last known STA signal strength, for status reporting only.
    pub(crate) signal_dbm: Option<i32>,
}

impl LinkObservation {
    /// STA is *viable* only when configured, associated, carrier/IP up, **and**
    /// the gateway probe passes — never on mere association (`wifid.md` §2.1).
    pub(crate) fn sta_viable(&self) -> bool {
        self.sta_configured && self.associated && self.carrier_up && self.gateway_reachable
    }

    /// The radio mode the system is *actually* in, or `None` if both radios are
    /// running — an illegal state that demands an emergency stop-both.
    fn observed_mode(&self) -> Option<LinkMode> {
        match (self.sta_running, self.ap_running) {
            (true, true) => None,
            (true, false) => Some(LinkMode::Sta),
            (false, true) => Some(LinkMode::Ap),
            (false, false) => Some(LinkMode::Down),
        }
    }
}

/// An ordered radio action for the executor. A transition emits at most a stop
/// followed by a start, never two starts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WifiAction {
    /// Bring up station mode.
    StartSta,
    /// Tear down station mode.
    StopSta,
    /// Bring up the WPA2 AP.
    StartAp,
    /// Tear down the AP.
    StopAp,
}

/// Result of one [`LinkMachine::step`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LinkStep {
    /// Actions to apply, in order. Empty ⇒ stay put.
    pub(crate) actions: Vec<WifiAction>,
    /// The mode the machine now intends (reconciled + transitioned).
    pub(crate) mode: LinkMode,
    /// STA is confirmed *stably up* (viable ≥ `sta_up_debounce`): the gate for
    /// full upload throttle. Always false outside STA mode.
    pub(crate) sta_link_up: bool,
}

/// The pure link state machine. Owns the believed mode, the viability debounce
/// timers, and the AP→STA retry backoff.
pub(crate) struct LinkMachine {
    sta_up_debounce_ms: i64,
    sta_down_debounce_ms: i64,
    ap_sta_retry_base_ms: i64,
    ap_sta_retry_max_ms: i64,
    ap_min_uptime_ms: i64,
    transition_settle_ms: i64,

    mode: LinkMode,
    mode_entered_ms: i64,
    viable_since_ms: Option<i64>,
    nonviable_since_ms: Option<i64>,
    /// Deadline until which an observation that disagrees with the believed
    /// mode is treated as a just-commanded transition still settling, not as
    /// genuine drift. `None` once confirmed or expired.
    settle_until_ms: Option<i64>,
    /// Consecutive failed STA attempts — drives capped exponential backoff on
    /// the AP→STA re-probe interval. Reset on confirmed STA-up or a credential
    /// change.
    failed_sta_attempts: u32,
}

fn dur_ms(d: Duration) -> i64 {
    i64::try_from(d.as_millis()).unwrap_or(i64::MAX)
}

impl LinkMachine {
    /// Build a machine from link timing config, starting in [`LinkMode::Down`].
    pub(crate) fn new(cfg: &crate::config::LinkConfig, now_ms: i64) -> Self {
        Self {
            sta_up_debounce_ms: dur_ms(cfg.sta_up_debounce),
            sta_down_debounce_ms: dur_ms(cfg.sta_down_debounce),
            ap_sta_retry_base_ms: dur_ms(cfg.ap_sta_retry_base),
            ap_sta_retry_max_ms: dur_ms(cfg.ap_sta_retry_max),
            ap_min_uptime_ms: dur_ms(cfg.ap_min_uptime),
            transition_settle_ms: dur_ms(cfg.transition_settle),
            mode: LinkMode::Down,
            mode_entered_ms: now_ms,
            viable_since_ms: None,
            nonviable_since_ms: None,
            settle_until_ms: None,
            failed_sta_attempts: 0,
        }
    }

    /// Reset the backoff and force a fresh STA attempt — called when `webd`
    /// updates the STA credentials (a new PSK deserves an immediate retry, not
    /// a backed-off one).
    pub(crate) fn notify_credentials_changed(&mut self) {
        self.failed_sta_attempts = 0;
    }

    /// Advance the machine by one observation at monotonic time `now_ms`.
    pub(crate) fn step(&mut self, obs: &LinkObservation, now_ms: i64) -> LinkStep {
        // 1. Reconcile against reality first.
        let Some(observed) = obs.observed_mode() else {
            // Both radios up — illegal. Stop both, drop to Down, re-evaluate
            // next step. This is the safety net for partial executor failure or
            // OS drift (a start that raced a failed stop). It overrides any
            // settling grace: a both-up observation is never "settling".
            self.set_mode(LinkMode::Down, now_ms);
            self.settle_until_ms = None;
            return LinkStep {
                actions: vec![WifiAction::StopAp, WifiAction::StopSta],
                mode: LinkMode::Down,
                sta_link_up: false,
            };
        };
        if observed == self.mode {
            // Reality matches belief: any in-flight transition has landed.
            // NOTE: AP min-uptime / backoff are measured from `mode_entered_ms`
            // (command time), not confirmed-up time. With the ~2s control loop
            // the bring-up lag erodes the floor by at most `transition_settle`
            // (~8s of 120s); accepting that is simpler and more robust than
            // inferring the true up-time from sparse observations.
            self.settle_until_ms = None;
        } else if matches!(self.settle_until_ms, Some(deadline) if now_ms < deadline) {
            // A transition we *just commanded* has not yet shown up in the
            // observation (radio/hostapd bring-up lag). Hold our belief and
            // issue nothing this tick — adopting the transient state here could
            // make us command the other radio up while this one is still
            // starting, leaving both up across ticks. Wait for it to settle.
            return LinkStep {
                actions: Vec::new(),
                mode: self.mode,
                sta_link_up: self.sta_link_up(now_ms),
            };
        } else {
            // No transition in flight (or the settle window expired without the
            // command taking effect): this is genuine drift — a crash/restart,
            // a failed transition, or external interference. Adopt reality.
            self.set_mode(observed, now_ms);
            self.settle_until_ms = None;
        }

        // 2. Update viability debounce timers.
        self.update_viability(obs, now_ms);

        // 3. Decide the target mode.
        let current = self.mode;
        let target = self.choose_target(obs, now_ms);

        // 4. Backoff bookkeeping on STA-attempt outcome.
        if current == LinkMode::Sta && target == LinkMode::Ap {
            self.failed_sta_attempts = self.failed_sta_attempts.saturating_add(1);
        }
        let sta_link_up = self.sta_link_up(now_ms);
        if sta_link_up {
            self.failed_sta_attempts = 0;
        }

        // 5. Emit the (stop-before-start) transition.
        let mut actions = Vec::new();
        emit_transition(current, target, &mut actions);
        if target != current {
            self.set_mode(target, now_ms);
            // Give the executor a bounded grace to make this transition visible
            // before we'd interpret a lagging observation as drift.
            self.settle_until_ms = Some(now_ms.saturating_add(self.transition_settle_ms));
        }

        LinkStep {
            actions,
            mode: self.mode,
            sta_link_up: self.sta_link_up(now_ms),
        }
    }

    fn choose_target(&self, obs: &LinkObservation, now_ms: i64) -> LinkMode {
        if !obs.sta_configured {
            // Nothing to connect to: AP onboarding is the only useful mode.
            return LinkMode::Ap;
        }
        match self.mode {
            LinkMode::Down => LinkMode::Sta,
            LinkMode::Sta => self.target_from_sta(obs, now_ms),
            LinkMode::Ap => self.target_from_ap(obs, now_ms),
        }
    }

    fn target_from_sta(&self, obs: &LinkObservation, now_ms: i64) -> LinkMode {
        if obs.sta_viable() {
            return LinkMode::Sta;
        }
        // Only fall back once non-viability has persisted past the debounce.
        match self.nonviable_since_ms {
            Some(since) if now_ms - since >= self.sta_down_debounce_ms => LinkMode::Ap,
            _ => LinkMode::Sta,
        }
    }

    fn target_from_ap(&self, obs: &LinkObservation, now_ms: i64) -> LinkMode {
        // Sticky AP: never disrupt an in-progress onboarding session.
        if obs.ap_has_clients {
            return LinkMode::Ap;
        }
        let in_ap = now_ms - self.mode_entered_ms;
        if in_ap < self.ap_min_uptime_ms {
            return LinkMode::Ap;
        }
        if in_ap >= self.retry_interval_ms() {
            LinkMode::Sta
        } else {
            LinkMode::Ap
        }
    }

    /// Capped exponential backoff: `base * 2^failed_attempts`, clamped to max.
    fn retry_interval_ms(&self) -> i64 {
        let mut interval = self.ap_sta_retry_base_ms;
        for _ in 0..self.failed_sta_attempts {
            interval = interval.saturating_mul(2);
            if interval >= self.ap_sta_retry_max_ms {
                return self.ap_sta_retry_max_ms;
            }
        }
        interval.min(self.ap_sta_retry_max_ms)
    }

    fn update_viability(&mut self, obs: &LinkObservation, now_ms: i64) {
        if obs.sta_viable() {
            self.nonviable_since_ms = None;
            if self.viable_since_ms.is_none() {
                self.viable_since_ms = Some(now_ms);
            }
        } else {
            self.viable_since_ms = None;
            if self.nonviable_since_ms.is_none() {
                self.nonviable_since_ms = Some(now_ms);
            }
        }
    }

    fn sta_link_up(&self, now_ms: i64) -> bool {
        if self.mode != LinkMode::Sta {
            return false;
        }
        matches!(self.viable_since_ms, Some(since) if now_ms - since >= self.sta_up_debounce_ms)
    }

    /// Adopt `mode`, resetting transition-scoped state (debounce timers) so
    /// stale viability from the previous mode can never leak across a switch.
    fn set_mode(&mut self, mode: LinkMode, now_ms: i64) {
        if mode != self.mode {
            self.mode = mode;
            self.mode_entered_ms = now_ms;
            self.viable_since_ms = None;
            self.nonviable_since_ms = None;
        }
    }
}

/// Emit a stop-before-start transition. By construction this never issues two
/// starts, so AP and STA can never both be commanded up in one step.
fn emit_transition(current: LinkMode, target: LinkMode, actions: &mut Vec<WifiAction>) {
    if current == target {
        return;
    }
    match current {
        LinkMode::Sta => actions.push(WifiAction::StopSta),
        LinkMode::Ap => actions.push(WifiAction::StopAp),
        LinkMode::Down => {}
    }
    match target {
        LinkMode::Sta => actions.push(WifiAction::StartSta),
        LinkMode::Ap => actions.push(WifiAction::StartAp),
        LinkMode::Down => {}
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{LinkMachine, LinkMode, LinkObservation, WifiAction};
    use crate::config::WifidConfig;

    fn obs(sta_configured: bool, sta_running: bool, ap_running: bool) -> LinkObservation {
        LinkObservation {
            sta_configured,
            sta_running,
            ap_running,
            associated: false,
            carrier_up: false,
            gateway_reachable: false,
            ap_has_clients: false,
            signal_dbm: None,
        }
    }

    fn viable(mut o: LinkObservation) -> LinkObservation {
        o.associated = true;
        o.carrier_up = true;
        o.gateway_reachable = true;
        o
    }

    fn machine() -> LinkMachine {
        LinkMachine::new(&WifidConfig::default().link, 0)
    }

    /// Property asserted after every step in the long-running tests: the
    /// emitted action list never starts both radios.
    fn assert_never_both_started(actions: &[WifiAction]) {
        assert!(
            !(actions.contains(&WifiAction::StartSta) && actions.contains(&WifiAction::StartAp)),
            "a single step started both radios: {actions:?}"
        );
    }

    #[test]
    fn boot_with_creds_attempts_sta() {
        let mut m = machine();
        let s = m.step(&obs(true, false, false), 0);
        assert_eq!(s.actions, vec![WifiAction::StartSta]);
        assert_eq!(s.mode, LinkMode::Sta);
    }

    #[test]
    fn boot_without_creds_starts_ap() {
        let mut m = machine();
        let s = m.step(&obs(false, false, false), 0);
        assert_eq!(s.actions, vec![WifiAction::StartAp]);
        assert_eq!(s.mode, LinkMode::Ap);
    }

    #[test]
    fn sta_to_ap_stops_sta_before_starting_ap() {
        let mut m = machine();
        // Enter STA; the executor brings the radio up in whatever mode the
        // machine last commanded, so observation tracks `running`.
        let mut running = m.step(&obs(true, false, false), 0).mode;
        let mut t = 0;
        let mut transition = None;
        // Hold non-viable; after the down-debounce (20s default) the machine
        // must fall back to AP, stopping STA first.
        while t <= 30_000 {
            let o = LinkObservation {
                sta_configured: true,
                sta_running: running == LinkMode::Sta,
                ap_running: running == LinkMode::Ap,
                associated: false,
                carrier_up: false,
                gateway_reachable: false,
                ap_has_clients: false,
                signal_dbm: None,
            };
            let s = m.step(&o, t);
            assert_never_both_started(&s.actions);
            running = s.mode;
            if s.actions.contains(&WifiAction::StartAp) {
                transition = Some(s.actions.clone());
                break;
            }
            t += 1000;
        }
        let actions = transition.expect("STA never fell back to AP");
        // Critical ordering: StopSta must precede StartAp.
        assert_eq!(actions, vec![WifiAction::StopSta, WifiAction::StartAp]);
        assert_eq!(running, LinkMode::Ap);
    }

    #[test]
    fn just_commanded_ap_is_not_abandoned_while_it_settles() {
        // Regression: a slow hostapd bring-up means the tick right after
        // `[StopSta, StartAp]` can still observe neither radio up. The machine
        // must NOT read that as drift and command STA back up (which would race
        // a both-radios-up state across ticks). It must hold AP while settling.
        let mut m = machine();
        m.step(&obs(true, false, false), 0); // Down -> Sta
        let mut t = 1000;
        let mut switched_at = None;
        while t <= 40_000 {
            // STA running but never viable, until the fallback fires.
            let s = m.step(&obs(true, true, false), t);
            if s.actions.contains(&WifiAction::StartAp) {
                assert_eq!(s.actions, vec![WifiAction::StopSta, WifiAction::StartAp]);
                assert_eq!(s.mode, LinkMode::Ap);
                switched_at = Some(t);
                break;
            }
            t += 1000;
        }
        let t0 = switched_at.expect("never switched to AP");
        // AP is still launching: observe neither radio up, twice, within the
        // 8s settle window. The machine must issue nothing and stay in AP.
        for dt in [1000, 2000] {
            let s = m.step(&obs(true, false, false), t0 + dt);
            assert!(
                s.actions.is_empty(),
                "abandoned settling AP: {:?}",
                s.actions
            );
            assert_eq!(s.mode, LinkMode::Ap);
        }
        // hostapd is now up: the transition is confirmed and AP is sticky.
        let s = m.step(&obs(true, false, true), t0 + 3000);
        assert!(s.actions.is_empty());
        assert_eq!(s.mode, LinkMode::Ap);
    }

    #[test]
    fn settle_window_expiry_readopts_reality_and_retries() {
        // Regression: if a commanded transition genuinely fails (executor never
        // brings the radio up), the machine must still recover after the settle
        // window — adopt reality and re-attempt — so settling never wedges it.
        let mut m = machine();
        let s0 = m.step(&obs(true, false, false), 0); // Down -> Sta, settle until 8s
        assert_eq!(s0.actions, vec![WifiAction::StartSta]);
        // Within the window, the absent STA is tolerated: no new commands.
        let s1 = m.step(&obs(true, false, false), 2000);
        assert!(s1.actions.is_empty());
        assert_eq!(s1.mode, LinkMode::Sta);
        // Past the window the failed bring-up is real drift: adopt Down and
        // re-attempt STA.
        let s2 = m.step(&obs(true, false, false), 9000);
        assert_eq!(s2.actions, vec![WifiAction::StartSta]);
        assert_eq!(s2.mode, LinkMode::Sta);
    }

    #[test]
    fn brief_sta_flap_does_not_switch_to_ap() {
        let mut m = machine();
        m.step(&obs(true, false, false), 0);
        // Viable, then a 5s blip of non-viability, then viable again — well
        // under the 20s down-debounce. Must stay in STA the whole time.
        let mut t = 0;
        m.step(&viable(obs(true, true, false)), t);
        t = 2000;
        // blip
        for _ in 0..5 {
            let s = m.step(&obs(true, true, false), t);
            assert_eq!(s.mode, LinkMode::Sta, "flap caused premature AP switch");
            t += 1000;
        }
        // recovers
        let s = m.step(&viable(obs(true, true, false)), t);
        assert_eq!(s.mode, LinkMode::Sta);
        assert!(s.actions.is_empty());
    }

    #[test]
    fn sta_confirmed_up_only_after_up_debounce() {
        let mut m = machine();
        m.step(&obs(true, false, false), 0);
        // viable at t=0 but up-debounce is 5s
        let s0 = m.step(&viable(obs(true, true, false)), 0);
        assert!(!s0.sta_link_up);
        let s1 = m.step(&viable(obs(true, true, false)), 4000);
        assert!(!s1.sta_link_up);
        let s2 = m.step(&viable(obs(true, true, false)), 5000);
        assert!(s2.sta_link_up, "STA should be confirmed up at the debounce");
    }

    #[test]
    fn both_radios_running_triggers_emergency_stop_both() {
        let mut m = machine();
        m.step(&obs(true, false, false), 0);
        let s = m.step(&obs(true, true, true), 1000);
        assert_eq!(s.mode, LinkMode::Down);
        assert_eq!(s.actions, vec![WifiAction::StopAp, WifiAction::StopSta]);
        assert!(!s.sta_link_up);
    }

    #[test]
    fn restart_while_ap_running_adopts_ap_without_restarting_it() {
        // Fresh machine (simulating a daemon restart) observes AP already up.
        let mut m = machine();
        let s = m.step(&obs(true, false, true), 5000);
        // Must NOT emit StartAp/StartSta — adopt the running AP as-is.
        assert!(
            s.actions.is_empty(),
            "restart re-issued radio actions: {:?}",
            s.actions
        );
        assert_eq!(s.mode, LinkMode::Ap);
    }

    #[test]
    fn ap_is_sticky_while_a_client_is_connected() {
        let mut m = machine();
        // No creds initially → AP.
        m.step(&obs(false, false, false), 0);
        // Creds appear, but a phone is mid-onboarding on the AP. Even long past
        // the retry interval, AP must not be torn down.
        let mut o = obs(true, false, true);
        o.ap_has_clients = true;
        let s = m.step(&o, 10_000_000);
        assert_eq!(
            s.mode,
            LinkMode::Ap,
            "AP dropped a connected onboarding client"
        );
        assert!(s.actions.is_empty());
    }

    #[test]
    fn ap_respects_minimum_uptime_before_retrying_sta() {
        let mut m = machine();
        m.step(&obs(false, false, false), 0); // -> AP at t=0
        // creds now present, no clients, but within ap_min_uptime (120s).
        let s = m.step(&obs(true, false, true), 60_000);
        assert_eq!(s.mode, LinkMode::Ap, "AP retried STA before min-uptime");
    }

    #[test]
    fn ap_retries_sta_after_interval_then_backs_off() {
        let mut m = machine();
        m.step(&obs(false, false, false), 0); // AP at 0
        // creds present; min-uptime 120s, base retry 60s → first retry eligible
        // at max(120s). Advance to 130s.
        let s = m.step(&obs(true, false, true), 130_000);
        assert_eq!(s.mode, LinkMode::Sta);
        assert_eq!(s.actions, vec![WifiAction::StopAp, WifiAction::StartSta]);
        assert_never_both_started(&s.actions);
    }

    #[test]
    fn permanently_down_home_wifi_never_starts_both_over_many_cycles() {
        // Drive a never-viable STA / no-client AP for a long simulated period
        // and assert the invariant holds on every single step.
        let mut m = machine();
        let mut t: i64 = 0;
        let mut running = m.step(&obs(true, false, false), t).mode;
        for _ in 0..2000 {
            // Reflect whatever mode the machine intends as the running radio,
            // so observation tracks the executor (STA never becomes viable).
            let o = LinkObservation {
                sta_configured: true,
                sta_running: running == LinkMode::Sta,
                ap_running: running == LinkMode::Ap,
                associated: false,
                carrier_up: false,
                gateway_reachable: false,
                ap_has_clients: false,
                signal_dbm: None,
            };
            let s = m.step(&o, t);
            assert_never_both_started(&s.actions);
            running = s.mode;
            t += 1000;
        }
    }
}
