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
use crate::creds::{ApMode, CredentialStore, CredentialUpdate, Credentials, apply_update};
use crate::error::{Result, WifidError};
use crate::link::{LinkMachine, LinkMode, LinkObservation, WifiAction};
use crate::overlay::{
    AP_OVERLAY_GATEWAY_IP, ApIfaceType, ApOverlay, ApOverlayObservation, ApParams,
};
use crate::status::{ApStatus, WifiStatus};
use crate::throttle::{ThrottleInputs, ThrottlePublisher, ThrottleState, TokenBucket};
use crate::traits::{Clock, HeartbeatSource, NetworkController, RebootController};
use crate::watchdog::{RecoveryAction, Watchdog};

/// After this many consecutive failed AP bring-up attempts (hostapd alive but
/// uap0 never reaches `type AP` — a brcmfmac `START_AP` settling race), stop
/// hammering the shared radio and cool down before retrying.
const AP_BRINGUP_MAX_FAIL_STREAK: u32 = 3;
/// AP-only quiet period (ms) after the failure streak trips, before retrying,
/// so a wedged firmware gets a breather without churning the radio or the STA.
const AP_BRINGUP_COOLDOWN_MS: i64 = 45_000;
/// Settle window (ms) between creating/ups `uap0` and firing hostapd's
/// `START_AP`. The BCM43430 brcmfmac `FullMAC` firmware rejects `START_AP`
/// ("Failed to set beacon parameters") when it fires immediately after the
/// vif comes up while the STA is associated; a live experiment measured
/// 6/6 success at 2s vs 1/6 at 0s. With a 2s `TICK_INTERVAL` this defers
/// hostapd to the next tick. NOT a blocking sleep — the tick cadence waits.
const AP_BRINGUP_SETTLE_MS: i64 = 2_000;
/// Consecutive ticks we tolerate `uap0`'s nl80211 type reading back `Unknown`
/// (an `iw` query hiccup) while hostapd is alive before giving up. A transient
/// query failure must not bounce a possibly-healthy AP, but a *persistent* one
/// (e.g. firmware left uap0 in an unreadable non-AP state after a failed
/// `START_AP`) must not wedge bring-up forever — past the cap we fall through
/// to the counted teardown/cooldown path so recovery still happens.
const AP_UNKNOWN_MAX_STREAK: u32 = 3;

/// An administrative command (delivered over IPC from `webd` on the device).
pub(crate) enum AdminCommand {
    /// Replace the stored credentials (validated, persisted `0600`).
    UpdateCredentials(CredentialUpdate),
}

/// A request delivered over the control-plane UDS. Deliberately derives NO
/// `Debug`: the `Mutate` variant carries a plaintext passphrase.
pub(crate) enum IpcRequest {
    /// Read the last published status (never mutates).
    GetStatus,
    /// Apply a validated credential update (AP mode / SSID / passphrase).
    Mutate(CredentialUpdate),
}

/// The reply to an [`IpcRequest`]. `Status` is boxed to keep the enum small
/// (`WifiStatus` is comparatively large).
pub(crate) enum IpcResponse {
    Status(Box<WifiStatus>),
    Unavailable,
    Ok,
    Err { code: &'static str, message: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ApPlan {
    pub(crate) desired: bool,
    pub(crate) channel: Option<u32>,
}

pub(crate) fn resolve_ap_overlay(
    ap_mode: ApMode,
    ap_provisioned: bool,
    chip_recovering: bool,
    link_mode: LinkMode,
    sta_running: bool,
    sta_channel: Option<u32>,
    default_channel: u32,
) -> ApPlan {
    if chip_recovering || !ap_provisioned {
        return ApPlan {
            desired: false,
            channel: None,
        };
    }
    match ap_mode {
        ApMode::ForceOff => ApPlan {
            desired: false,
            channel: None,
        },
        ApMode::Auto => {
            let desired = link_mode == LinkMode::Ap && !sta_running;
            ApPlan {
                desired,
                channel: desired.then_some(default_channel),
            }
        }
        ApMode::ForceOn => ApPlan {
            desired: true,
            // Single-radio brcmfmac forces the AP onto the STA's channel, so the
            // concurrent AP can only follow a 2.4GHz STA channel (1..=14). If the
            // STA is on 5GHz or its channel is unknown this tick, withhold the
            // channel (bring-up is skipped) rather than beacon on a mismatched
            // channel — the firmware rejects that with -52. With no STA up the
            // radio is free, so the 2.4GHz default channel is safe.
            channel: if sta_running {
                sta_channel.filter(|&c| (1..=14).contains(&c))
            } else {
                Some(default_channel)
            },
        },
    }
}

/// The wired daemon.
pub(crate) struct Daemon<C, N, H, R, S, O> {
    clock: C,
    net: N,
    heartbeat: H,
    reboot: R,
    store: S,
    overlay: O,
    cfg: WifidConfig,
    boot_id: u64,

    machine: LinkMachine,
    watchdog: Watchdog,
    throttle: ThrottlePublisher,
    bucket: TokenBucket,
    creds: Credentials,

    tc_applied: bool,
    ap_cap_applied: bool,
    ap_bringup_fail_streak: u32,
    ap_bringup_cooldown_until_ms: i64,
    /// When >0, `uap0` was created (phase 1) and hostapd `START_AP` is deferred
    /// until `now >= this` (the settle window). 0 = no bring-up in progress.
    ap_start_ap_after_ms: i64,
    /// Consecutive ticks `uap0`'s type read back `Unknown` while hostapd was
    /// alive. Bounds the "tolerate a transient `iw` hiccup" guard so a
    /// persistent unreadable type can't wedge recovery. Reset on every path
    /// except the tolerated re-observe.
    ap_unknown_streak: u32,
    /// Consecutive stability count toward starting dnsmasq: incremented while the
    /// AP is `active`, reset only on genuine IP loss (`!ip_present`, the `START_AP`
    /// flush), and HELD across benign non-active reads (e.g. a transient `iw`
    /// type-read hiccup while the IP is present). dnsmasq starts only once this
    /// reaches 2, proving the re-asserted IP survived a full tick and is not the
    /// pre-`START_AP` address about to be flushed -- so dnsmasq never binds `uap0`
    /// during the flush window, without livelocking when observation flaps.
    ap_active_streak: u32,
    near_deadlock: bool,
    last_status: Option<WifiStatus>,
}

impl<C, N, H, R, S, O> Daemon<C, N, H, R, S, O>
where
    C: Clock,
    N: NetworkController,
    H: HeartbeatSource,
    R: RebootController,
    S: CredentialStore,
    O: ApOverlay,
{
    /// Build a daemon, loading credentials from the store.
    ///
    /// # Errors
    /// Returns an error if the credential store cannot be read.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        clock: C,
        net: N,
        heartbeat: H,
        reboot: R,
        store: S,
        overlay: O,
        cfg: WifidConfig,
        boot_id: u64,
    ) -> Result<Self> {
        // A missing credential store is a benign empty config (fresh appliance),
        // never a fatal error — this is the fix for the on-device crash-loop
        // where `serve` exited every few seconds because the credential file did
        // not exist yet. We continue into the normal state machine; with no
        // credentials the link machine onboards via AP (which the executor will
        // refuse to start until a passphrase is provisioned).
        let creds = store.load()?.unwrap_or_else(Credentials::empty);
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
            overlay,
            cfg,
            boot_id,
            machine,
            watchdog,
            throttle,
            bucket,
            creds,
            tc_applied: false,
            ap_cap_applied: false,
            ap_bringup_fail_streak: 0,
            ap_bringup_cooldown_until_ms: 0,
            ap_start_ap_after_ms: 0,
            ap_unknown_streak: 0,
            ap_active_streak: 0,
            near_deadlock: false,
            last_status: None,
        })
    }

    /// The last status published, if any.
    pub(crate) fn status(&self) -> Option<WifiStatus> {
        self.last_status.clone()
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

    /// Handle a control-plane IPC request on the control-loop thread. Never
    /// panics; maps credential errors to a stable `(code, message)` without ever
    /// echoing the rejected value.
    pub(crate) fn handle_ipc(&mut self, request: IpcRequest) -> IpcResponse {
        match request {
            IpcRequest::GetStatus => match self.status() {
                Some(status) => IpcResponse::Status(Box::new(status)),
                None => IpcResponse::Unavailable,
            },
            IpcRequest::Mutate(update) => {
                match self.handle_command(AdminCommand::UpdateCredentials(update)) {
                    Ok(()) => IpcResponse::Ok,
                    Err(WifidError::InvalidCredential(reason)) => IpcResponse::Err {
                        code: "invalid_argument",
                        message: reason.to_owned(),
                    },
                    Err(e) => {
                        eprintln!("wifid ipc: mutation failed: {e}");
                        IpcResponse::Err {
                            code: "internal",
                            message: "failed to apply change".to_owned(),
                        }
                    }
                }
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
        // The live NM-owned STA counts as "configured" even without a wifid cred file.
        link_obs.sta_configured = self.creds.sta_configured() || link_obs.sta_running;
        let ap_obs = self.overlay.observe();
        link_obs.ap_fallback_suppressed = !matches!(self.creds.ap_mode, ApMode::Auto);
        link_obs.ap_running = matches!(self.creds.ap_mode, ApMode::Auto) && ap_obs.active;
        link_obs.ap_has_clients = matches!(self.creds.ap_mode, ApMode::Auto)
            && ap_obs.active
            && ap_obs.client_count.is_none_or(|c| c > 0);
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
        let ap_plan = resolve_ap_overlay(
            self.creds.ap_mode,
            self.creds.ap_provisioned(),
            recovering,
            step.mode,
            link_obs.sta_running,
            link_obs.sta_channel,
            self.cfg.platform.ap_default_channel,
        );

        // 3. Update the `tc` cap intent and publish the throttle state
        //    fail-closed *before* executing any radio/recovery I/O.
        self.reconcile_tx_cap(&step, recovering);
        // Cap/fail-safe key off whether uap0 is physically ON-AIR (beaconing), not
        // `active` (which also requires the IPv4). During the post-START_AP flush
        // window the AP still beacons with `ip_present==false`, so gating on
        // `active` would leave the STA uncapped while the shared single radio is
        // carrying AP traffic. `ap_beaconing` closes that window.
        let ap_beaconing =
            ap_obs.iface_exists && ap_obs.hostapd_alive && ap_obs.iface_type == ApIfaceType::Ap;
        self.reconcile_ap_tx_cap(ap_beaconing, &step, recovering);
        let throttle = self.publish_throttle(step.mode, step.sta_link_up, recovering, ap_beaconing);
        let ap = self.build_ap_status(ap_obs);
        let status = WifiStatus::new(step.mode, &link_obs, throttle, recovering, ap);
        self.last_status = Some(status.clone());

        // 4. Execute side effects (best-effort; failures self-heal next tick via
        //    reconciliation in the state machine / watchdog).
        self.execute_recovery(recovery);
        let ap_down_ok = self.apply_ap_overlay_teardown(&ap_plan, ap_obs);
        self.execute_actions(&step.actions, ap_down_ok);
        let sta_uploading = step.mode == LinkMode::Sta && step.sta_link_up && !recovering;
        self.apply_ap_overlay_bringup(&ap_plan, ap_obs, sta_uploading, now);
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

    fn reconcile_ap_tx_cap(
        &mut self,
        ap_active: bool,
        step: &crate::link::LinkStep,
        recovering: bool,
    ) {
        // Cap uap0 only when it is concurrently up WHILE the STA is actually
        // uploading; otherwise there is nothing to protect and the AP may use
        // full bandwidth. Fail-safe is enforced downstream: if the cap is not
        // applied while the AP is active, the throttle pauses uploads.
        let want = ap_active && step.mode == LinkMode::Sta && step.sta_link_up && !recovering;
        if want {
            if !self.ap_cap_applied {
                self.ap_cap_applied = self
                    .net
                    .apply_ap_tx_cap(self.cfg.throttle.ap_tx_bytes_per_s)
                    .is_ok();
            }
        } else {
            self.ap_cap_applied = false;
        }
    }

    fn build_ap_status(&self, ap_obs: ApOverlayObservation) -> ApStatus {
        ApStatus {
            mode: self.creds.ap_mode,
            active: ap_obs.active,
            ssid: self.creds.ap_ssid.clone(),
            client_count: ap_obs.client_count.unwrap_or(0),
            ip: ap_obs.active.then_some(AP_OVERLAY_GATEWAY_IP.to_owned()),
        }
    }

    fn ap_params_for_channel(&self, channel: u32) -> Option<ApParams> {
        match (&self.creds.ap_ssid, &self.creds.ap_passphrase) {
            (Some(ssid), Some(passphrase)) => Some(ApParams {
                ssid: ssid.clone(),
                passphrase: passphrase.clone(),
                channel,
            }),
            _ => None,
        }
    }

    fn apply_ap_overlay_teardown(&self, plan: &ApPlan, ap_obs: ApOverlayObservation) -> bool {
        if plan.desired {
            return true;
        }
        // Tear down on ANY footprint, not just a fully-`active` overlay, so a
        // partial/half-torn AP can never linger in ForceOff or emergency-stop.
        if ap_obs.iface_exists || ap_obs.hostapd_alive || ap_obs.dnsmasq_alive || ap_obs.ip_present
        {
            return self.overlay.ensure_down().is_ok();
        }
        true
    }

    fn apply_ap_overlay_bringup(
        &mut self,
        plan: &ApPlan,
        ap_obs: ApOverlayObservation,
        sta_uploading: bool,
        now: i64,
    ) {
        // Track consecutive stability toward starting dnsmasq. Increment while the
        // AP is fully `active`; reset ONLY on genuine IP loss (`!ip_present` -- the
        // START_AP flush). A benign non-active read (e.g. a transient `iw` type-read
        // hiccup while the IP is still present) HOLDS the streak, so an otherwise
        // healthy AP whose observation flaps never livelocks short of the DHCP gate.
        self.ap_active_streak = if ap_obs.active {
            self.ap_active_streak.saturating_add(1)
        } else if !ap_obs.ip_present {
            0
        } else {
            self.ap_active_streak
        };
        if !plan.desired {
            self.ap_bringup_fail_streak = 0;
            self.ap_bringup_cooldown_until_ms = 0;
            self.ap_start_ap_after_ms = 0;
            self.ap_unknown_streak = 0;
            return;
        }
        // Re-assert radio power-save OFF every desired tick (active, settling, or
        // cooling down) -- not just at uap0 creation. NetworkManager re-enables
        // power-save on STA reconnect/roam (its connection default), which sleeps
        // the single shared radio and silently drops client associations on the AP
        // vif. Best-effort; never tears down the AP or disturbs the STA.
        self.overlay.disable_power_save();
        if ap_obs.active {
            // Healthy: uap0 is `type AP` and beaconing. Clear failure/settle state
            // and apply any in-place channel-follow reconfigure (STA roamed).
            self.ap_bringup_fail_streak = 0;
            self.ap_bringup_cooldown_until_ms = 0;
            self.ap_start_ap_after_ms = 0;
            self.ap_unknown_streak = 0;
            // Start DHCP only once the AP has been `active` for TWO consecutive
            // ticks (`ap_active_streak >= 2`). `type AP` is a netdev mode set at
            // uap0 creation (phase 1), so it does NOT prove START_AP finished; the
            // first active tick could carry the pre-START_AP IPv4 that is about to
            // be flushed. Requiring the IP to survive a full tick guarantees it is
            // the re-asserted, post-flush address -- so dnsmasq never binds uap0
            // during the flush window, whatever the flush latency. Idempotent
            // (skipped once dnsmasq is already running); best-effort so a dnsmasq
            // hiccup never tears the AP down or disturbs the STA. (Once dnsmasq has
            // ever run, the streak stays >= 2 in steady state, so a crash is
            // re-healed with no extra delay.)
            if self.ap_active_streak >= 2 && !ap_obs.dnsmasq_alive {
                let _ = self.overlay.ensure_dhcp();
            }
            if let (Some(desired), Some(running)) = (plan.channel, ap_obs.channel) {
                if desired != running {
                    if let Some(params) = self.ap_params_for_channel(desired) {
                        let _ = self.overlay.reconfigure(&params);
                    }
                }
            }
            return;
        }
        // Desired but not beaconing (`type AP` not reached).
        if now < self.ap_bringup_cooldown_until_ms {
            // Cooling down after a failure streak: keep the radio quiet.
            let _ = self.overlay.ensure_down();
            self.ap_start_ap_after_ms = 0;
            self.ap_unknown_streak = 0;
            return;
        }
        if ap_obs.hostapd_alive && ap_obs.iface_exists && ap_obs.iface_type == ApIfaceType::Unknown
        {
            // uap0 exists and hostapd is alive but its type was unreadable this
            // tick (transient `iw` failure): do not tear down a possibly-healthy
            // AP on a query hiccup. Tolerate only a bounded run of these so a
            // *persistent* unreadable type can't wedge recovery forever — past
            // the cap, fall through to the counted teardown/cooldown path below.
            self.ap_unknown_streak = self.ap_unknown_streak.saturating_add(1);
            if self.ap_unknown_streak < AP_UNKNOWN_MAX_STREAK {
                return;
            }
            self.ap_unknown_streak = 0;
        } else {
            // Any tick whose type is readable (or where there is no live-hostapd
            // footprint) breaks the run: reset so the streak counts strictly
            // *consecutive* unreadable ticks and a stray earlier hiccup can never
            // prematurely trip a teardown on a later single transient one.
            self.ap_unknown_streak = 0;
        }
        // Phase 2: uap0 was created on a prior tick and we are waiting to fire
        // START_AP once the firmware has settled.
        if self.ap_start_ap_after_ms > 0 {
            if ap_obs.iface_exists && !ap_obs.hostapd_alive {
                if now >= self.ap_start_ap_after_ms {
                    self.ap_start_ap_after_ms = 0;
                    if let Some(channel) = plan.channel {
                        if let Some(params) = self.ap_params_for_channel(channel) {
                            let up_ok = self.overlay.ensure_ap_started(&params).is_ok();
                            // After START_AP, uap0 can be effectively active
                            // (hostapd broadcasting, observe().active true) even if
                            // `ensure_ap_started` returns Err at a late step.
                            // Re-observe on the error path so a partially-up AP is
                            // never left up-and-uncapped while the STA is uploading.
                            let now_active = up_ok || self.overlay.observe().active;
                            if now_active && sta_uploading {
                                let ok = self
                                    .net
                                    .apply_ap_tx_cap(self.cfg.throttle.ap_tx_bytes_per_s)
                                    .is_ok();
                                self.ap_cap_applied = ok;
                            }
                        }
                    }
                }
                // Still settling (now < deadline): wait this tick.
                return;
            }
            // The settling iface vanished or hostapd appeared unexpectedly:
            // abandon this attempt and re-evaluate via the branches below.
            self.ap_start_ap_after_ms = 0;
        }
        // Phase 3: uap0 is beaconing (`type AP`, hostapd alive) but its IPv4 was
        // flushed by hostapd's START_AP mode-transition on the single-radio chip.
        // Re-assert the IP only -- `type AP` proves START_AP is complete, so the
        // flush has already happened and this `ip addr add` sticks (no pending
        // async flush to race). DHCP is deliberately NOT started here; it waits for
        // the healthy branch on a later tick, once the re-added IP has proven
        // stable, so dnsmasq never binds during the flush window. On success wait
        // for the next tick to confirm `active`; on failure FALL THROUGH to the
        // counted teardown below so a genuinely wedged vif still escalates.
        if ap_obs.iface_exists
            && ap_obs.hostapd_alive
            && ap_obs.iface_type == ApIfaceType::Ap
            && !ap_obs.ip_present
            && self.overlay.ensure_ip().is_ok()
        {
            // Reaching `type AP` and restoring the IP is forward progress: clear
            // any pre-beacon failure count so it can't prematurely trip cooldown.
            self.ap_bringup_fail_streak = 0;
            return;
        }
        if ap_obs.iface_exists || ap_obs.hostapd_alive {
            // Desired but not beaconing, yet a stale AP footprint survives (failed
            // START_AP left uap0 non-AP, orphan hostapd, or leftover iface). Tear
            // it down so the next tick recreates uap0 clean and count the failure.
            self.note_ap_bringup_failure(now);
            return;
        }
        // Truly clean (no interface, no live hostapd): PHASE 1 — create+up+unmanage
        // uap0 and arm the settle window. hostapd START_AP fires a later tick.
        if plan.channel.is_some() {
            if self.overlay.ensure_iface_up().is_ok() {
                self.ap_start_ap_after_ms = now + AP_BRINGUP_SETTLE_MS;
            } else {
                // Phase-1 iface bring-up can fail leaving no footprint (e.g. the
                // `iw add uap0` create itself failed), which the stale-footprint
                // branch above would never see — count it here so a persistently
                // failing radio cools down instead of retrying create every tick.
                self.note_ap_bringup_failure(now);
            }
        }
    }

    /// Record a failed AP bring-up attempt: tear down any footprint, bump the
    /// fail streak, and once it hits the cap trip the AP-only cooldown so a
    /// wedged firmware/radio stops churning the shared chip. Also clears the
    /// transient unreadable-type streak — this attempt is being abandoned.
    fn note_ap_bringup_failure(&mut self, now: i64) {
        self.ap_unknown_streak = 0;
        self.ap_bringup_fail_streak = self.ap_bringup_fail_streak.saturating_add(1);
        let _ = self.overlay.ensure_down();
        if self.ap_bringup_fail_streak >= AP_BRINGUP_MAX_FAIL_STREAK {
            self.ap_bringup_cooldown_until_ms = now + AP_BRINGUP_COOLDOWN_MS;
            self.ap_bringup_fail_streak = 0;
        }
    }

    fn publish_throttle(
        &mut self,
        link_mode: LinkMode,
        sta_link_up: bool,
        recovering: bool,
        ap_overlay_active: bool,
    ) -> ThrottleState {
        self.throttle.update(ThrottleInputs {
            link_mode,
            sta_link_up,
            chip_recovering: recovering,
            near_deadlock: self.near_deadlock,
            tc_applied: self.tc_applied,
            ap_overlay_active,
            ap_cap_applied: self.ap_cap_applied,
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
        self.ap_cap_applied = false;
        self.near_deadlock = false;
        let throttle = self.publish_throttle(LinkMode::Down, false, false, false);
        let ap_obs = self.overlay.observe();
        let blind = LinkObservation {
            sta_configured: self.creds.sta_configured(),
            sta_running: false,
            ap_running: false,
            ap_fallback_suppressed: false,
            mutation_hold: false,
            associated: false,
            carrier_up: false,
            gateway_reachable: false,
            ap_has_clients: false,
            signal_dbm: None,
            sta_channel: None,
        };
        self.last_status = Some(WifiStatus::new(
            LinkMode::Down,
            &blind,
            throttle,
            false,
            self.build_ap_status(ap_obs),
        ));
    }

    fn execute_actions(&self, actions: &[WifiAction], ap_down_ok: bool) {
        for action in actions {
            // If the AP overlay could not be torn down this tick, refuse to start
            // the wlan0 STA radio: bringing STA up while uap0 is still up creates
            // an illegal both-radios-up state (emergency teardown burst). Stop
            // actions still run so the emergency-both-down path can break concurrency.
            if matches!(action, WifiAction::StartSta) && !ap_down_ok {
                continue;
            }
            let result = match action {
                WifiAction::StartSta => self.net.start_sta(),
                WifiAction::StopSta => self.net.stop_sta(),
                // AP overlay actions are resolved separately by overlay reconciliation.
                WifiAction::StartAp | WifiAction::StopAp => Ok(()),
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

    use super::{
        AP_BRINGUP_COOLDOWN_MS, AP_BRINGUP_MAX_FAIL_STREAK, AP_BRINGUP_SETTLE_MS,
        AP_UNKNOWN_MAX_STREAK, AdminCommand, ApPlan, Daemon, IpcRequest, IpcResponse,
        resolve_ap_overlay,
    };
    use crate::config::WifidConfig;
    use crate::creds::{ApMode, CredentialStore, CredentialUpdate, Credentials, Secret};
    use crate::error::Result;
    use crate::link::{LinkMode, LinkObservation};
    use crate::overlay::{ApIfaceType, ApOverlay, ApOverlayObservation, ApParams};
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
        apply_ap_tx_cap: u32,
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
        fail_ap_cap: Cell<bool>,
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
        fn apply_tx_cap(&self, _bytes_per_s: u64) -> Result<()> {
            Ok(())
        }
        fn apply_ap_tx_cap(&self, _bytes_per_s: u64) -> Result<()> {
            self.calls.borrow_mut().apply_ap_tx_cap += 1;
            if self.fail_ap_cap.get() {
                Err(crate::error::WifidError::Network(
                    "apply ap tx cap failed".to_owned(),
                ))
            } else {
                Ok(())
            }
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
        creds: RefCell<Option<Credentials>>,
    }
    impl CredentialStore for FakeStore {
        fn load(&self) -> Result<Option<Credentials>> {
            Ok(self.creds.borrow().clone())
        }
        fn store(&self, creds: &Credentials) -> Result<()> {
            *self.creds.borrow_mut() = Some(creds.clone());
            Ok(())
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum OverlayOp {
        IfaceUp,
        Up(u32),
        Down,
        Reconfigure(u32),
        DisablePowerSave,
        EnsureIp,
        EnsureDhcp,
    }

    struct FakeOverlay {
        obs: RefCell<ApOverlayObservation>,
        ops: RefCell<Vec<OverlayOp>>,
        auto_activate_on_up: Cell<bool>,
        fail_up: Cell<bool>,
        fail_iface_up: Cell<bool>,
        partial_up: Cell<bool>,
        fail_down: Cell<bool>,
        fail_ip: Cell<bool>,
    }

    impl FakeOverlay {
        fn set_active(&self, active: bool, channel: Option<u32>, clients: Option<u32>) {
            let mut obs = self.obs.borrow_mut();
            obs.iface_exists = active;
            obs.hostapd_alive = active;
            obs.ip_present = active;
            obs.iface_type = if active {
                ApIfaceType::Ap
            } else {
                ApIfaceType::Unknown
            };
            obs.dnsmasq_alive = active;
            obs.active = active;
            obs.channel = channel;
            obs.client_count = clients;
        }

        fn set_broken_hostapd(&self, iface_type: ApIfaceType) {
            let mut obs = self.obs.borrow_mut();
            obs.iface_exists = true;
            obs.hostapd_alive = true;
            obs.ip_present = true;
            obs.iface_type = iface_type;
            obs.active = false;
            obs.channel = None;
            obs.client_count = Some(0);
        }

        /// Set an arbitrary non-active AP footprint (iface/hostapd presence +
        /// nl80211 type) to exercise the failed-bring-up teardown branch for
        /// footprints other than the "live hostapd, non-AP type" shape.
        fn set_footprint(&self, iface_exists: bool, hostapd_alive: bool, iface_type: ApIfaceType) {
            let mut obs = self.obs.borrow_mut();
            obs.iface_exists = iface_exists;
            obs.hostapd_alive = hostapd_alive;
            obs.ip_present = false;
            obs.iface_type = iface_type;
            obs.active = false;
            obs.channel = None;
            obs.client_count = Some(0);
        }

        fn set_auto_activate_on_up(&self, enabled: bool) {
            self.auto_activate_on_up.set(enabled);
        }

        fn set_fail_down(&self, enabled: bool) {
            self.fail_down.set(enabled);
        }

        fn set_fail_up(&self, enabled: bool) {
            self.fail_up.set(enabled);
        }

        fn set_partial_up(&self, enabled: bool) {
            self.partial_up.set(enabled);
        }

        fn set_fail_iface_up(&self, enabled: bool) {
            self.fail_iface_up.set(enabled);
        }

        fn set_fail_ip(&self, enabled: bool) {
            self.fail_ip.set(enabled);
        }
    }

    impl ApOverlay for FakeOverlay {
        fn ensure_up(&self, params: &ApParams) -> Result<()> {
            self.ensure_iface_up()?;
            self.ensure_ap_started(params)
        }

        fn ensure_iface_up(&self) -> Result<()> {
            self.ops.borrow_mut().push(OverlayOp::IfaceUp);
            if self.fail_iface_up.get() {
                // Model a phase-1 create failure that leaves no footprint (the
                // `iw add uap0` itself failed): observation stays clean.
                return Err(crate::error::WifidError::Network(
                    "overlay ensure_iface_up failed".to_owned(),
                ));
            }
            let mut obs = self.obs.borrow_mut();
            obs.iface_exists = true;
            obs.ip_present = true;
            obs.hostapd_alive = false;
            obs.iface_type = ApIfaceType::Ap;
            obs.active = false;
            obs.channel = None;
            obs.client_count = Some(0);
            Ok(())
        }

        fn ensure_ap_started(&self, params: &ApParams) -> Result<()> {
            self.ops.borrow_mut().push(OverlayOp::Up(params.channel));
            if self.fail_up.get() {
                return Err(crate::error::WifidError::Network(
                    "overlay ensure_up failed".to_owned(),
                ));
            }
            if self.partial_up.get() {
                // Late failure: uap0 + hostapd are up (observe().active == true)
                // but a later step (e.g. dnsmasq) failed, so ensure_up reports Err.
                self.set_active(true, Some(params.channel), Some(0));
                return Err(crate::error::WifidError::Network(
                    "overlay ensure_up partial (dnsmasq) failure".to_owned(),
                ));
            }
            if self.auto_activate_on_up.get() {
                self.set_active(true, Some(params.channel), Some(0));
            }
            Ok(())
        }

        fn ensure_ip(&self) -> Result<()> {
            self.ops.borrow_mut().push(OverlayOp::EnsureIp);
            if self.fail_ip.get() {
                return Err(crate::error::WifidError::Network(
                    "overlay ensure_ip failed".to_owned(),
                ));
            }
            let mut obs = self.obs.borrow_mut();
            obs.ip_present = true;
            // Re-derive `active` now the IP is restored, mirroring observe():
            // active requires iface + hostapd + ip + `type AP`.
            obs.active = obs.iface_exists && obs.hostapd_alive && obs.iface_type == ApIfaceType::Ap;
            Ok(())
        }

        fn ensure_dhcp(&self) -> Result<()> {
            self.ops.borrow_mut().push(OverlayOp::EnsureDhcp);
            self.obs.borrow_mut().dnsmasq_alive = true;
            Ok(())
        }

        fn disable_power_save(&self) {
            self.ops.borrow_mut().push(OverlayOp::DisablePowerSave);
        }

        fn ensure_down(&self) -> Result<()> {
            if self.fail_down.get() {
                return Err(crate::error::WifidError::Network(
                    "overlay ensure_down failed".to_owned(),
                ));
            }
            self.ops.borrow_mut().push(OverlayOp::Down);
            self.set_active(false, None, Some(0));
            Ok(())
        }

        fn reconfigure(&self, params: &ApParams) -> Result<()> {
            self.ops
                .borrow_mut()
                .push(OverlayOp::Reconfigure(params.channel));
            self.set_active(true, Some(params.channel), Some(0));
            Ok(())
        }

        fn observe(&self) -> ApOverlayObservation {
            *self.obs.borrow()
        }
    }

    type TestDaemon = Daemon<FakeClock, FakeNet, FakeHeartbeat, FakeReboot, FakeStore, FakeOverlay>;

    fn obs() -> LinkObservation {
        LinkObservation {
            sta_configured: true,
            sta_running: false,
            ap_running: false,
            ap_fallback_suppressed: false,
            mutation_hold: false,
            associated: false,
            carrier_up: false,
            gateway_reachable: false,
            ap_has_clients: false,
            signal_dbm: None,
            sta_channel: None,
        }
    }

    fn build(sta_configured: bool) -> TestDaemon {
        let mut o = obs();
        o.sta_configured = sta_configured;
        let creds = if sta_configured {
            Credentials {
                sta_psk: Some(Secret::new("home-psk-1234")),
                ap_passphrase: Some(Secret::new("ap-pass-1234")),
                ap_ssid: Some("TeslaUSB".to_owned()),
                ap_mode: crate::creds::ApMode::Auto,
            }
        } else {
            Credentials {
                sta_psk: None,
                ap_passphrase: Some(Secret::new("ap-pass-1234")),
                ap_ssid: Some("TeslaUSB".to_owned()),
                ap_mode: crate::creds::ApMode::Auto,
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
                fail_ap_cap: Cell::new(false),
            },
            FakeHeartbeat {
                hb: RefCell::new(None),
            },
            FakeReboot {
                calls: RefCell::new(0),
            },
            FakeStore {
                creds: RefCell::new(Some(creds)),
            },
            FakeOverlay {
                obs: RefCell::new(ApOverlayObservation {
                    iface_exists: false,
                    hostapd_alive: false,
                    dnsmasq_alive: false,
                    ip_present: false,
                    channel: None,
                    iface_type: ApIfaceType::Unknown,
                    client_count: Some(0),
                    active: false,
                }),
                ops: RefCell::new(Vec::new()),
                auto_activate_on_up: Cell::new(true),
                fail_up: Cell::new(false),
                fail_iface_up: Cell::new(false),
                partial_up: Cell::new(false),
                fail_down: Cell::new(false),
                fail_ip: Cell::new(false),
            },
            WifidConfig::default(),
            BOOT,
        )
        .unwrap()
    }

    /// Build a daemon whose credential store is **absent** (returns `Ok(None)`)
    /// — the on-device crash-loop scenario. `Daemon::new` must succeed with an
    /// empty config rather than erroring.
    fn build_absent_store() -> TestDaemon {
        let o = obs();
        Daemon::new(
            FakeClock { ms: Cell::new(0) },
            FakeNet {
                link: RefCell::new(o),
                chip: Cell::new(true),
                calls: RefCell::new(Calls::default()),
                both_running: Cell::new(false),
                fail_observe: Cell::new(false),
                fail_ap_cap: Cell::new(false),
            },
            FakeHeartbeat {
                hb: RefCell::new(None),
            },
            FakeReboot {
                calls: RefCell::new(0),
            },
            FakeStore {
                creds: RefCell::new(None),
            },
            FakeOverlay {
                obs: RefCell::new(ApOverlayObservation {
                    iface_exists: false,
                    hostapd_alive: false,
                    dnsmasq_alive: false,
                    ip_present: false,
                    channel: None,
                    iface_type: ApIfaceType::Unknown,
                    client_count: Some(0),
                    active: false,
                }),
                ops: RefCell::new(Vec::new()),
                auto_activate_on_up: Cell::new(true),
                fail_up: Cell::new(false),
                fail_iface_up: Cell::new(false),
                partial_up: Cell::new(false),
                fail_down: Cell::new(false),
                fail_ip: Cell::new(false),
            },
            WifidConfig::default(),
            BOOT,
        )
        .expect("missing credential store must not be fatal")
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
        d.overlay.set_active(true, Some(6), Some(0));
        d.net.both_running.set(true);
        let st = d.tick().unwrap();
        assert_eq!(st.mode, LinkMode::Down);
        let calls = d.net.calls.borrow();
        assert!(calls.stop_sta >= 1, "STA was not stopped");
        assert!(
            d.overlay.ops.borrow().contains(&OverlayOp::Down),
            "overlay was not commanded down"
        );
    }

    #[test]
    fn credential_update_persists_and_resets_backoff() {
        let mut d = build(false); // start onboarding-only -> AP
        d.tick().unwrap();
        d.handle_command(AdminCommand::UpdateCredentials(CredentialUpdate {
            sta_psk: Some("new-home-psk".to_owned()),
            ap_passphrase: None,
            ap_ssid: None,
            ap_mode: None,
            clear_sta: false,
        }))
        .unwrap();
        // Stored secret is retrievable only through the store, never via status.
        assert!(
            d.store
                .creds
                .borrow()
                .as_ref()
                .is_some_and(Credentials::sta_configured)
        );
        let st = d.status().unwrap();
        let json = serde_json::to_string(&st).unwrap();
        assert!(!json.contains("new-home-psk"));
    }

    #[test]
    fn ipc_get_status_unavailable_until_first_tick() {
        let mut d = build(false);
        assert!(matches!(
            d.handle_ipc(IpcRequest::GetStatus),
            IpcResponse::Unavailable
        ));
        d.tick().unwrap();
        assert!(matches!(
            d.handle_ipc(IpcRequest::GetStatus),
            IpcResponse::Status(_)
        ));
    }

    #[test]
    fn ipc_mutate_ap_mode_persists() {
        let mut d = build(false);
        let response = d.handle_ipc(IpcRequest::Mutate(CredentialUpdate {
            sta_psk: None,
            ap_passphrase: None,
            ap_ssid: None,
            ap_mode: Some(ApMode::ForceOn),
            clear_sta: false,
        }));
        assert!(matches!(response, IpcResponse::Ok));
        assert_eq!(
            d.store.creds.borrow().as_ref().unwrap().ap_mode,
            ApMode::ForceOn
        );
    }

    #[test]
    fn missing_credential_store_boots_into_ap_onboarding_not_a_crash() {
        // Regression for the on-device crash-loop: with no credential file the
        // daemon must come up (no error) and run the state machine. With no STA
        // configured the link machine onboards via AP, and the AP is actually
        // started (not merely intended).
        let mut d = build_absent_store();
        assert!(
            !d.creds.sta_configured(),
            "absent store must be empty config"
        );
        let st = d.tick().unwrap();
        assert_eq!(
            st.mode,
            LinkMode::Ap,
            "unprovisioned daemon should onboard via AP"
        );
        assert!(!st.throttle.body.uploads_allowed);
        assert!(d.overlay.ops.borrow().is_empty(), "overlay must stay down");
    }

    #[test]
    fn sta_configured_but_unreachable_falls_back_to_the_ap_backstop() {
        // Connectivity backstop, end-to-end through the daemon: STA is
        // configured but the home network is never reachable (no gateway), so
        // after the down-debounce wifid must fall back to AP onboarding and
        // actually bring the AP up — stopping STA first (never both at once).
        let mut d = build(true);
        d.tick().unwrap(); // Down -> Sta (StartSta); STA runs but is never viable.
        let mut t = 1000;
        let mut reached_ap = false;
        while t <= 60_000 {
            set_time(&d, t);
            if d.tick().unwrap().mode == LinkMode::Ap {
                reached_ap = true;
                break;
            }
            t += 1000;
        }
        assert!(
            reached_ap,
            "unreachable STA never fell back to the AP backstop"
        );
        set_time(&d, t + 1_000);
        d.tick().unwrap();
        set_time(&d, t + 3_000);
        d.tick().unwrap();
        set_time(&d, t + 4_000);
        d.tick().unwrap();
        assert!(
            d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(_))),
            "AP backstop was never started"
        );
        let calls = d.net.calls.borrow();
        assert!(
            calls.stop_sta >= 1,
            "STA was not stopped before the AP came up"
        );
    }

    #[test]
    fn resolve_ap_overlay_truth_table() {
        assert_eq!(
            resolve_ap_overlay(ApMode::Auto, true, false, LinkMode::Ap, false, None, 6),
            ApPlan {
                desired: true,
                channel: Some(6),
            }
        );
        assert_eq!(
            resolve_ap_overlay(ApMode::Auto, true, false, LinkMode::Ap, true, Some(11), 6),
            ApPlan {
                desired: false,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(ApMode::Auto, true, false, LinkMode::Sta, false, None, 6),
            ApPlan {
                desired: false,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(
                ApMode::ForceOn,
                true,
                false,
                LinkMode::Sta,
                true,
                Some(11),
                6
            ),
            ApPlan {
                desired: true,
                channel: Some(11),
            }
        );
        assert_eq!(
            // 5GHz STA channel cannot be followed by a 2.4GHz (hw_mode=g) AP on
            // the single radio; withhold the channel so bring-up is skipped
            // instead of beaconing on a mismatched channel (firmware -52).
            resolve_ap_overlay(
                ApMode::ForceOn,
                true,
                false,
                LinkMode::Sta,
                true,
                Some(36),
                6
            ),
            ApPlan {
                desired: true,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(ApMode::ForceOn, true, false, LinkMode::Sta, true, None, 6),
            ApPlan {
                desired: true,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(ApMode::ForceOn, true, false, LinkMode::Down, false, None, 6),
            ApPlan {
                desired: true,
                channel: Some(6),
            }
        );
        assert_eq!(
            resolve_ap_overlay(
                ApMode::ForceOff,
                true,
                false,
                LinkMode::Ap,
                false,
                Some(11),
                6
            ),
            ApPlan {
                desired: false,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(
                ApMode::ForceOn,
                true,
                true,
                LinkMode::Ap,
                false,
                Some(11),
                6
            ),
            ApPlan {
                desired: false,
                channel: None,
            }
        );
        assert_eq!(
            resolve_ap_overlay(
                ApMode::ForceOn,
                false,
                false,
                LinkMode::Ap,
                false,
                Some(11),
                6
            ),
            ApPlan {
                desired: false,
                channel: None,
            }
        );
    }

    #[test]
    fn auto_fallback_overlay_status_reflects_observation() {
        let mut d = build(true);
        d.tick().unwrap();
        let mut t = 1_000;
        while t <= 60_000 {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                break;
            }
            t += 1_000;
        }
        set_time(&d, t + 1_000);
        d.tick().unwrap();
        set_time(&d, t + 3_000);
        d.tick().unwrap();
        set_time(&d, t + 4_000);
        d.tick().unwrap();
        assert!(
            d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(6)))
        );
        let status = d.status().expect("status exists");
        assert!(status.ap.active);
    }

    #[test]
    fn auto_delayed_overlay_up_stays_in_ap_mode() {
        let mut d = build(true);
        d.overlay.set_auto_activate_on_up(false);
        d.tick().unwrap();
        let mut switched_at = None;
        for t in (1_000..=60_000).step_by(1_000) {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                switched_at = Some(t);
                break;
            }
        }
        let t0 = switched_at.expect("never entered ap");
        set_time(&d, t0 + 1_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Ap);
        set_time(&d, t0 + 2_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Ap);
        d.overlay.set_active(true, Some(6), Some(0));
        set_time(&d, t0 + 3_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Ap);
    }

    #[test]
    fn ap_reasserts_ip_then_starts_dhcp_after_hostapd_flush_without_teardown() {
        // Regression (single-radio brcmfmac): hostapd's START_AP mode-transition
        // flushes uap0's IPv4, leaving the vif beaconing (`type AP`, hostapd alive)
        // but not `active` (ip_present=false). The overlay must RE-ASSERT the IP
        // (phase 3) instead of tearing the AP down -- teardown churns the shared
        // radio and blips the STA/SSH lifeline -- then start DHCP only once the AP
        // is confirmed stably active on a LATER tick (never during the flush window).
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        // Post-flush footprint: iface up, hostapd alive, `type AP`, IP flushed.
        d.overlay.set_footprint(true, true, ApIfaceType::Ap);

        // Tick 1: phase 3 re-asserts the IP; must NOT tear the AP down, must NOT
        // start DHCP yet, and must not count a failure.
        d.tick().unwrap();
        {
            let ops = d.overlay.ops.borrow();
            assert!(
                ops.contains(&OverlayOp::EnsureIp),
                "phase 3 must re-add the flushed IP"
            );
            assert!(
                !ops.contains(&OverlayOp::Down),
                "must not tear the beaconing AP down"
            );
            assert!(
                !ops.contains(&OverlayOp::EnsureDhcp),
                "DHCP must wait until the IP proves stable"
            );
        }
        assert_eq!(d.ap_bringup_fail_streak, 0, "IP recovery is not a failure");
        assert!(
            d.overlay.observe().active,
            "AP is active once the IP is restored"
        );

        // Tick 2: first tick observing `active` -- the IP has not yet survived a
        // full tick, so DHCP must still wait (two-consecutive-active gate).
        set_time(&d, 2_000);
        d.tick().unwrap();
        assert!(
            !d.overlay.ops.borrow().contains(&OverlayOp::EnsureDhcp),
            "DHCP waits until the re-added IP has survived a full tick"
        );
        assert!(!d.overlay.ops.borrow().contains(&OverlayOp::Down));

        // Tick 3: second consecutive active tick -> the IP is proven stable (not
        // the pre-START_AP address), so start DHCP exactly once, still no teardown.
        set_time(&d, 4_000);
        d.tick().unwrap();
        assert!(
            d.overlay.ops.borrow().contains(&OverlayOp::EnsureDhcp),
            "DHCP starts once the AP has been active two ticks running"
        );
        assert!(!d.overlay.ops.borrow().contains(&OverlayOp::Down));

        // Tick 4: dnsmasq already alive -> do not restart it.
        d.overlay.ops.borrow_mut().clear();
        set_time(&d, 6_000);
        d.tick().unwrap();
        assert!(
            !d.overlay.ops.borrow().contains(&OverlayOp::EnsureDhcp),
            "dnsmasq is not restarted once already running"
        );
    }

    #[test]
    fn ap_phase3_ensure_ip_failure_falls_through_to_counted_teardown() {
        // If re-asserting the flushed IP genuinely fails (a wedged vif, not just a
        // transient flush), phase 3 must NOT silently loop forever: it falls through
        // to the existing bounded teardown so the fail streak escalates toward the
        // AP-only cooldown, exactly as any other stale-footprint failure would.
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.overlay.set_footprint(true, true, ApIfaceType::Ap);
        d.overlay.set_fail_ip(true);

        d.tick().unwrap();

        let ops = d.overlay.ops.borrow();
        assert!(
            ops.contains(&OverlayOp::EnsureIp),
            "phase 3 attempts the IP re-assert"
        );
        assert!(
            ops.contains(&OverlayOp::Down),
            "a failed IP re-assert falls through to teardown"
        );
        assert_eq!(
            d.ap_bringup_fail_streak, 1,
            "the failed attempt is counted toward cooldown"
        );
    }

    #[test]
    fn beaconing_ap_without_ip_still_caps_sta_uploads() {
        // Regression (Finding 1): during the post-START_AP flush window uap0 is
        // beaconing (`type AP`, hostapd alive) but has no IP, so observe().active
        // is false. The tx-cap/fail-safe must key off the AP being physically
        // ON-AIR, not `active`, or the STA would upload uncapped over the shared
        // single radio while the AP beacons.
        let mut d = build(true);
        d.creds.ap_ssid = Some("TeslaUSB".to_owned());
        d.creds.ap_passphrase = Some(Secret::new("ap-pass-1234"));
        d.creds.ap_mode = ApMode::ForceOff;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        // Warm the STA link through its viability window (no AP yet).
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        d.tick().unwrap();
        // Present a beaconing-but-IP-less AP (the flush window) under ForceOn.
        d.net.calls.borrow_mut().apply_ap_tx_cap = 0;
        d.overlay.set_footprint(true, true, ApIfaceType::Ap);
        d.creds.ap_mode = ApMode::ForceOn;
        set_time(&d, 8_000);
        d.tick().unwrap();
        assert!(
            d.net.calls.borrow().apply_ap_tx_cap > 0,
            "a beaconing AP with no IP must still cap STA uploads"
        );
    }

    #[test]
    fn dhcp_gate_streak_survives_transient_type_read_hiccup() {
        // Regression (Finding B): the two-active-tick dnsmasq gate must not livelock
        // when observe() flaps active/non-active because of a transient
        // `iw dev uap0 info` type-read hiccup. The streak resets only on real IP
        // loss (`ip_present==false`, the flush), NOT on a benign non-active read
        // while the IP is still present -- otherwise a healthy AP whose type read
        // flaps every other tick would never reach streak 2 and never serve DHCP.
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        d.creds.ap_ssid = Some("TeslaUSB".to_owned());
        d.creds.ap_passphrase = Some(Secret::new("ap-pass-1234"));
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        // One fully-active tick -> streak 1.
        d.overlay.set_active(true, Some(6), Some(0));
        d.tick().unwrap();
        assert_eq!(d.ap_active_streak, 1);

        // Transient type-read hiccup: the AP is physically up (IP present, hostapd
        // alive) but `iw` reported a non-AP type this tick, so active is false.
        {
            let mut o = d.overlay.obs.borrow_mut();
            o.iface_exists = true;
            o.hostapd_alive = true;
            o.ip_present = true;
            o.iface_type = ApIfaceType::Unknown;
            o.active = false;
        }
        set_time(&d, 2_000);
        d.tick().unwrap();
        assert_eq!(
            d.ap_active_streak, 1,
            "a benign non-active read must not reset the DHCP-stability streak"
        );

        // Real IP loss (the flush) DOES restart the stability proof.
        {
            let mut o = d.overlay.obs.borrow_mut();
            o.ip_present = false;
            o.iface_type = ApIfaceType::Ap;
            o.active = false;
        }
        set_time(&d, 4_000);
        d.tick().unwrap();
        assert_eq!(
            d.ap_active_streak, 0,
            "real IP loss restarts the stability proof"
        );
    }

    #[test]
    fn auto_ap_with_client_stays_sticky_and_no_client_retries_sta() {
        let mut d = build(true);
        d.tick().unwrap();
        let mut switched_at = None;
        for t in (1_000..=60_000).step_by(1_000) {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                switched_at = Some(t);
                break;
            }
        }
        let t0 = switched_at.expect("never entered ap fallback");
        d.overlay.set_active(true, Some(6), Some(1));
        set_time(&d, t0 + 300_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Ap);

        {
            let mut l = d.net.link.borrow_mut();
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
        }
        d.overlay.set_active(true, Some(6), Some(0));
        set_time(&d, t0 + 600_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Sta);
    }

    #[test]
    fn auto_ap_with_unknown_client_count_is_sticky() {
        let mut d = build(true);
        d.tick().unwrap();
        let mut switched_at = None;
        for t in (1_000..=60_000).step_by(1_000) {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                switched_at = Some(t);
                break;
            }
        }
        let t0 = switched_at.expect("never entered ap fallback");
        d.overlay.set_active(true, Some(6), None);
        set_time(&d, t0 + 300_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Ap);
    }

    #[test]
    fn auto_leaving_ap_tears_down_overlay_before_starting_sta() {
        let mut d = build(true);
        d.tick().unwrap();
        let mut switched_at = None;
        for t in (1_000..=60_000).step_by(1_000) {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                switched_at = Some(t);
                break;
            }
        }
        let t0 = switched_at.expect("never entered ap fallback");
        d.overlay.set_active(true, Some(6), Some(0));
        {
            let mut l = d.net.link.borrow_mut();
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
        }
        d.overlay.ops.borrow_mut().clear();
        set_time(&d, t0 + 300_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Sta);
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
    }

    #[test]
    fn auto_teardown_failure_defers_sta_start() {
        let mut d = build(true);
        d.tick().unwrap();
        let mut switched_at = None;
        for t in (1_000..=60_000).step_by(1_000) {
            set_time(&d, t);
            let st = d.tick().unwrap();
            if st.mode == LinkMode::Ap {
                switched_at = Some(t);
                break;
            }
        }
        let t0 = switched_at.expect("never entered ap fallback");
        d.overlay.set_active(true, Some(6), Some(0));
        {
            let mut l = d.net.link.borrow_mut();
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_running = false;
        }
        d.overlay.ops.borrow_mut().clear();
        d.overlay.set_fail_down(true);
        let starts_before = d.net.calls.borrow().start_sta;
        set_time(&d, t0 + 300_000);
        assert_eq!(d.tick().unwrap().mode, LinkMode::Sta);
        assert_eq!(d.net.calls.borrow().start_sta, starts_before);
        assert!(!d.net.link.borrow().sta_running);

        d.overlay.set_fail_down(false);
        let mut started = false;
        for t in ((t0 + 301_000)..=(t0 + 320_000)).step_by(1_000) {
            set_time(&d, t);
            assert_eq!(d.tick().unwrap().mode, LinkMode::Sta);
            if d.net.calls.borrow().start_sta == starts_before + 1 {
                started = true;
                break;
            }
        }
        assert!(
            started,
            "STA start was not retried after teardown recovered"
        );
        assert!(d.net.link.borrow().sta_running);
    }

    #[test]
    fn force_on_runs_concurrent_and_reconfigures_in_place() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        let st = d.tick().unwrap();
        assert_eq!(st.mode, LinkMode::Sta);
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::IfaceUp));
        assert!(!d.overlay.ops.borrow().contains(&OverlayOp::Up(11)));
        assert!(d.ap_start_ap_after_ms > 0);

        set_time(&d, 2_000);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Up(11)));

        d.overlay.set_active(true, Some(11), Some(0));
        d.net.link.borrow_mut().sta_channel = Some(6);
        set_time(&d, 4_000);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Reconfigure(6)));

        // ForceOn with the channel withheld (STA on an unknown/5GHz channel):
        // AP bring-up is withheld, but power-save MUST still be reasserted -- an
        // already-active AP can be dropped by an NM roam that makes the channel
        // unknown, so gating the reassert on the channel would reintroduce the
        // original failure. Prove the tick reasserts power-save yet performs no
        // AP-disturbing lifecycle op.
        d.overlay.ops.borrow_mut().clear();
        d.net.link.borrow_mut().sta_channel = None;
        set_time(&d, 6_000);
        d.tick().unwrap();
        let ops = d.overlay.ops.borrow();
        assert!(
            ops.contains(&OverlayOp::DisablePowerSave),
            "power-save must be reasserted even when the AP channel is withheld"
        );
        let disturbing = ops
            .iter()
            .filter(|op| !matches!(op, OverlayOp::DisablePowerSave))
            .count();
        assert_eq!(disturbing, 0, "AP overlay was disturbed on unknown channel");
    }

    #[test]
    fn ap_bringup_creates_iface_and_defers_start_ap() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::IfaceUp));
        assert!(
            !d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(_)))
        );
        assert!(d.ap_start_ap_after_ms > 0);
    }

    #[test]
    fn ap_bringup_waits_for_settle_before_start_ap() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        let armed = d.ap_start_ap_after_ms;
        set_time(&d, 1_000);
        d.tick().unwrap();
        assert!(
            !d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(_)))
        );
        assert!(d.ap_start_ap_after_ms > 0);
        assert_eq!(d.ap_start_ap_after_ms, armed);
    }

    #[test]
    fn ap_bringup_starts_hostapd_after_settle() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        set_time(&d, 2_000);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Up(11)));
        assert!(d.overlay.observe().active);
        assert_eq!(d.ap_start_ap_after_ms, 0);
    }

    #[test]
    fn ap_reasserts_power_save_every_desired_tick_including_while_active() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        // Bring the AP fully up (phase 1 -> settle -> START_AP -> active).
        d.tick().unwrap();
        set_time(&d, 2_000);
        d.tick().unwrap();
        assert!(d.overlay.observe().active);

        // A later tick with the AP already active takes the early-return active
        // branch (no IfaceUp / Up), yet MUST still re-assert power-save off: an NM
        // STA roam can re-enable it and silently break client association again.
        d.overlay.ops.borrow_mut().clear();
        set_time(&d, 4_000);
        d.tick().unwrap();
        let ops = d.overlay.ops.borrow();
        assert!(ops.contains(&OverlayOp::DisablePowerSave));
        assert!(!ops.contains(&OverlayOp::IfaceUp));
        assert!(!ops.iter().any(|op| matches!(op, OverlayOp::Up(_))));
        assert!(!ops.contains(&OverlayOp::Down));
        assert!(!ops.iter().any(|op| matches!(op, OverlayOp::Reconfigure(_))));
    }

    #[test]
    fn ap_does_not_touch_power_save_when_overlay_not_desired() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOff;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        assert!(
            !d.overlay
                .ops
                .borrow()
                .contains(&OverlayOp::DisablePowerSave)
        );
    }

    #[test]
    fn ap_bringup_recreates_broken_hostapd() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.overlay.set_broken_hostapd(ApIfaceType::Other);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
        assert_eq!(d.ap_bringup_fail_streak, 1);
    }

    #[test]
    fn ap_bringup_persistent_unknown_type_eventually_tears_down() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        // hostapd is alive but uap0's nl80211 type reads back Unknown every tick
        // (a persistent `iw` query failure). The guard tolerates a bounded run of
        // these, then must fall through to the counted teardown so recovery still
        // happens instead of wedging forever.
        d.overlay.set_broken_hostapd(ApIfaceType::Unknown);
        for i in 0..(AP_UNKNOWN_MAX_STREAK - 1) {
            set_time(&d, i64::from(i) * 2_000);
            d.tick().unwrap();
            assert!(
                !d.overlay.ops.borrow().contains(&OverlayOp::Down),
                "tore down a possibly-healthy AP on a transient unknown-type tick"
            );
        }
        set_time(&d, i64::from(AP_UNKNOWN_MAX_STREAK) * 2_000);
        d.tick().unwrap();
        assert!(
            d.overlay.ops.borrow().contains(&OverlayOp::Down),
            "persistent unknown type never tore down"
        );
        assert_eq!(d.ap_bringup_fail_streak, 1);
    }

    #[test]
    fn ap_bringup_phase1_failure_counts_toward_cooldown() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        // Phase-1 iface bring-up fails at creation, leaving no footprint. Without
        // counting, each clean retry would hammer the radio every tick forever; it
        // must instead trip the same fail-streak cooldown and never fire START_AP.
        d.overlay.set_fail_iface_up(true);
        for i in 0..AP_BRINGUP_MAX_FAIL_STREAK {
            set_time(&d, i64::from(i) * 2_000);
            d.tick().unwrap();
        }
        assert!(
            d.ap_bringup_cooldown_until_ms > 0,
            "persistent phase-1 failure never tripped the cooldown"
        );
        assert!(
            !d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(_))),
            "START_AP fired despite phase-1 never succeeding"
        );
    }

    #[test]
    fn ap_bringup_unknown_streak_resets_on_intervening_readable_tick() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        // A single transient unknown-type tick bumps the streak but must not tear
        // down the (possibly-healthy) AP.
        d.overlay.set_broken_hostapd(ApIfaceType::Unknown);
        set_time(&d, 0);
        d.tick().unwrap();
        assert_eq!(d.ap_unknown_streak, 1);
        assert!(!d.overlay.ops.borrow().contains(&OverlayOp::Down));
        // A following tick with no live-hostapd footprint breaks the run and must
        // reset the streak, so non-consecutive hiccups never accumulate toward a
        // premature teardown.
        d.overlay.set_active(false, None, Some(0));
        set_time(&d, 2_000);
        d.tick().unwrap();
        assert_eq!(
            d.ap_unknown_streak, 0,
            "non-consecutive unknown ticks must not accumulate"
        );
    }

    #[test]
    fn ap_bringup_unknown_type_does_not_teardown() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.overlay.set_broken_hostapd(ApIfaceType::Unknown);
        d.tick().unwrap();
        assert!(!d.overlay.ops.borrow().contains(&OverlayOp::Down));
        assert_eq!(d.ap_bringup_fail_streak, 0);
    }

    #[test]
    fn ap_bringup_recreates_stale_iface_without_hostapd() {
        // Regression: a failed START_AP can leave uap0 in a non-AP type AND take
        // hostapd down with it. That surviving footprint must still be torn down
        // and counted, not silently re-`ensure_up`'d every tick (radio churn).
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.overlay.set_footprint(true, false, ApIfaceType::Other);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
        assert_eq!(d.ap_bringup_fail_streak, 1);
    }

    #[test]
    fn ap_bringup_tears_down_orphan_hostapd_when_iface_absent() {
        // Regression: hostapd pidfile alive but uap0 is gone -> `iw` yields
        // Unknown. This must NOT be treated as a transient read hiccup (only safe
        // while the interface still exists); the orphan hostapd is torn down and
        // counted so bring-up can recover instead of stalling forever.
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.overlay.set_footprint(false, true, ApIfaceType::Unknown);
        d.tick().unwrap();
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
        assert_eq!(d.ap_bringup_fail_streak, 1);
    }

    #[test]
    fn ap_bringup_cooldown_after_fail_streak() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        set_time(&d, 1_000);
        d.overlay.set_broken_hostapd(ApIfaceType::Other);
        d.tick().unwrap();
        set_time(&d, 2_000);
        d.overlay.set_broken_hostapd(ApIfaceType::Other);
        d.tick().unwrap();
        set_time(&d, 3_000);
        d.overlay.set_broken_hostapd(ApIfaceType::Other);
        d.tick().unwrap();
        assert_eq!(d.ap_bringup_fail_streak, 0);
        assert_eq!(
            d.ap_bringup_cooldown_until_ms,
            3_000 + AP_BRINGUP_COOLDOWN_MS
        );
        let ops_before = d.overlay.ops.borrow().len();
        set_time(&d, 4_000);
        d.tick().unwrap();
        let ops_after = d.overlay.ops.borrow().len();
        assert!(
            ops_after > ops_before,
            "cooldown tick should keep trying ensure_down"
        );
        assert!(
            !d.overlay
                .ops
                .borrow()
                .iter()
                .skip(ops_before)
                .any(|op| matches!(op, OverlayOp::Up(_))),
            "cooldown must not attempt ensure_up"
        );
        d.overlay.set_active(false, None, Some(0));
        set_time(&d, d.ap_bringup_cooldown_until_ms + 1);
        d.tick().unwrap();
        set_time(
            &d,
            d.ap_bringup_cooldown_until_ms + 1 + AP_BRINGUP_SETTLE_MS,
        );
        d.tick().unwrap();
        assert!(
            d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(6)))
        );
    }

    #[test]
    fn force_off_ensures_overlay_down_and_link_not_ap() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOff;
        d.overlay.set_active(true, Some(6), Some(0));
        let st = d.tick().unwrap();
        assert_ne!(st.mode, LinkMode::Ap);
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
    }

    #[test]
    fn force_off_tears_down_partial_footprint() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOff;
        *d.overlay.obs.borrow_mut() = ApOverlayObservation {
            iface_exists: false,
            hostapd_alive: true,
            dnsmasq_alive: false,
            ip_present: false,
            channel: Some(6),
            iface_type: ApIfaceType::Unknown,
            client_count: Some(0),
            active: false,
        };
        let st = d.tick().unwrap();
        assert_ne!(st.mode, LinkMode::Ap);
        assert!(d.overlay.ops.borrow().contains(&OverlayOp::Down));
    }

    #[test]
    fn chip_recovery_suppresses_overlay_until_clear() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        d.overlay.set_active(true, Some(6), Some(0));
        d.net.chip.set(false);
        for t in (0..=20_000).step_by(1_000) {
            set_time(&d, t);
            let _ = d.tick();
        }
        assert!(
            d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Down))
        );
        d.overlay.ops.borrow_mut().clear();
        d.net.chip.set(true);
        d.net.link.borrow_mut().sta_channel = Some(6);
        set_time(&d, 45_000);
        d.tick().unwrap();
        set_time(&d, 47_000);
        d.tick().unwrap();
        assert!(
            d.overlay
                .ops
                .borrow()
                .iter()
                .any(|op| matches!(op, OverlayOp::Up(_)))
        );
    }

    #[test]
    fn force_off_without_sta_creds_targets_down_and_overlay_stays_down() {
        let mut d = build(false);
        d.creds.ap_mode = ApMode::ForceOff;
        let st = d.tick().unwrap();
        assert_eq!(st.mode, LinkMode::Down);
        assert!(d.overlay.ops.borrow().is_empty());
    }

    #[test]
    fn force_on_concurrent_applies_uap0_cap_and_reduces_rate() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        d.overlay.set_active(true, Some(11), Some(0));
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        let st = d.tick().unwrap();
        assert_eq!(
            st.throttle.body.reason,
            crate::throttle::PauseReason::ApConcurrent
        );
        assert_eq!(
            st.throttle.body.max_tx_bytes_per_s,
            d.cfg.throttle.max_tx_bytes_per_s / d.cfg.throttle.ap_concurrent_divisor
        );
        assert!(d.net.calls.borrow().apply_ap_tx_cap > 0);
    }

    #[test]
    fn force_on_ap_capped_on_activation_tick() {
        let mut d = build(true);
        d.creds.ap_ssid = Some("TeslaUSB".to_owned());
        d.creds.ap_passphrase = Some(Secret::new("ap-pass-1234"));
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.creds.ap_mode = ApMode::ForceOff;
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        d.tick().unwrap();
        d.net.calls.borrow_mut().apply_ap_tx_cap = 0;
        d.creds.ap_mode = ApMode::ForceOn;
        set_time(&d, 7_000);
        d.tick().unwrap();
        set_time(&d, 9_000);
        d.tick().unwrap();
        assert!(
            d.net.calls.borrow().apply_ap_tx_cap > 0,
            "uap0 cap must be applied on the activation tick"
        );
    }

    #[test]
    fn force_on_broken_ap_does_not_pause_uploads() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        d.creds.ap_ssid = Some("TeslaUSB".to_owned());
        d.creds.ap_passphrase = Some(Secret::new("ap-pass-1234"));
        d.overlay.set_fail_up(true);
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        let st = d.tick().unwrap();
        assert!(st.throttle.body.uploads_allowed);
        assert_ne!(
            st.throttle.body.reason,
            crate::throttle::PauseReason::ApConcurrent
        );
    }

    #[test]
    fn force_on_partial_bringup_failure_still_caps_uap0() {
        let mut d = build(true);
        d.creds.ap_ssid = Some("TeslaUSB".to_owned());
        d.creds.ap_passphrase = Some(Secret::new("ap-pass-1234"));
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(6);
        }
        d.creds.ap_mode = ApMode::ForceOff;
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        d.tick().unwrap();
        d.net.calls.borrow_mut().apply_ap_tx_cap = 0;
        d.overlay.set_partial_up(true);
        d.creds.ap_mode = ApMode::ForceOn;
        set_time(&d, 7_000);
        d.tick().unwrap();
        set_time(&d, 9_000);
        d.tick().unwrap();
        assert!(
            d.net.calls.borrow().apply_ap_tx_cap > 0,
            "uap0 cap must be applied even on a late (dnsmasq) ensure_up failure"
        );
    }

    #[test]
    fn force_on_concurrent_cap_failure_pauses_uploads() {
        let mut d = build(true);
        d.creds.ap_mode = ApMode::ForceOn;
        d.net.fail_ap_cap.set(true);
        d.overlay.set_active(true, Some(11), Some(0));
        {
            let mut l = d.net.link.borrow_mut();
            l.sta_running = true;
            l.associated = true;
            l.carrier_up = true;
            l.gateway_reachable = true;
            l.sta_channel = Some(11);
        }
        d.tick().unwrap();
        set_time(&d, 1_000);
        d.tick().unwrap();
        set_time(&d, 6_000);
        let st = d.tick().unwrap();
        assert!(!st.throttle.body.uploads_allowed);
        assert_eq!(
            st.throttle.body.reason,
            crate::throttle::PauseReason::ApConcurrent
        );
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
