//! The status shape `wifid` exposes to `webd` (`wifid.md` §6).
//!
//! Read-only: mode, link facts, signal, throttle state, and whether chip
//! recovery is in flight. By construction it contains **no credential field**,
//! so a secret can never reach the SPA through status.

use serde::Serialize;

use crate::creds::ApMode;
use crate::link::{LinkMode, LinkObservation};
use crate::throttle::ThrottleState;

/// Link-layer facts safe to surface to the UI.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) struct LinkSummary {
    /// Associated to home `WiFi` (STA).
    pub(crate) associated: bool,
    /// Carrier + IP up.
    pub(crate) carrier_up: bool,
    /// Gateway/LAN reachability probe passed.
    pub(crate) gateway_reachable: bool,
    /// STA signal strength in dBm, if known.
    pub(crate) signal_dbm: Option<i32>,
}

impl From<&LinkObservation> for LinkSummary {
    fn from(o: &LinkObservation) -> Self {
        Self {
            associated: o.associated,
            carrier_up: o.carrier_up,
            gateway_reachable: o.gateway_reachable,
            signal_dbm: o.signal_dbm,
        }
    }
}

/// The full status document `webd` reads. No secrets, ever.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct WifiStatus {
    /// Current radio mode.
    pub(crate) mode: LinkMode,
    /// Link facts.
    pub(crate) link: LinkSummary,
    /// Published throttle state (seq + body).
    pub(crate) throttle: ThrottleState,
    /// Whether the SDIO chip-reset watchdog is mid-recovery.
    pub(crate) recovering: bool,
    /// Access-point status (non-secret fields only).
    pub(crate) ap: ApStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct ApStatus {
    pub(crate) mode: ApMode,
    pub(crate) active: bool,
    pub(crate) ssid: Option<String>,
    pub(crate) client_count: u32,
    pub(crate) ip: Option<String>,
}

impl WifiStatus {
    /// Assemble the status from the current core state.
    pub(crate) fn new(
        mode: LinkMode,
        obs: &LinkObservation,
        throttle: ThrottleState,
        recovering: bool,
        ap: ApStatus,
    ) -> Self {
        Self {
            mode,
            link: LinkSummary::from(obs),
            throttle,
            recovering,
            ap,
        }
    }

    /// A copy with the radio signal neutralised, for suppressing duplicate log
    /// lines.
    ///
    /// `signal_dbm` drifts by a few dBm on essentially every tick, so comparing
    /// whole statuses would report a change every time and defeat the
    /// suppression. Every other field is genuinely event-driven. The live
    /// signal is always readable over the control socket; the periodic
    /// heartbeat line keeps a coarse sample of it in the journal.
    pub(crate) fn log_key(&self) -> Self {
        let mut key = self.clone();
        key.link.signal_dbm = None;
        key
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{ApStatus, WifiStatus};
    use crate::config::WifidConfig;
    use crate::creds::ApMode;
    use crate::link::{LinkMode, LinkObservation};
    use crate::throttle::{ThrottleInputs, ThrottlePublisher};

    fn observation() -> LinkObservation {
        LinkObservation {
            sta_configured: true,
            sta_running: true,
            ap_running: false,
            ap_fallback_suppressed: false,
            mutation_hold: false,
            associated: true,
            carrier_up: true,
            gateway_reachable: true,
            ap_has_clients: false,
            signal_dbm: Some(-55),
            sta_channel: Some(1),
        }
    }

    #[test]
    fn status_serialises_without_any_credential_field() {
        let cfg = WifidConfig::default();
        let mut pub_ = ThrottlePublisher::new(&cfg.throttle);
        let throttle = pub_.update(ThrottleInputs {
            link_mode: LinkMode::Sta,
            sta_link_up: true,
            chip_recovering: false,
            near_deadlock: false,
            tc_applied: true,
            ap_overlay_active: false,
            ap_cap_applied: false,
        });
        let status = WifiStatus::new(
            LinkMode::Sta,
            &observation(),
            throttle,
            false,
            ApStatus {
                mode: ApMode::ForceOff,
                active: false,
                ssid: Some("TeslaUSB".to_owned()),
                client_count: 0,
                ip: None,
            },
        );
        let json = serde_json::to_string(&status).expect("serialise");
        // Sanity: expected shape present.
        assert!(json.contains("\"mode\":\"sta\""));
        assert!(json.contains("\"signal_dbm\":-55"));
        assert!(json.contains("\"uploads_allowed\":true"));
        assert!(json.contains("\"ap\""));
        assert!(json.contains("\"force_off\""));
        // Safety: no secret-bearing keys exist in the shape at all.
        for forbidden in ["psk", "passphrase", "secret", "password"] {
            assert!(
                !json.to_lowercase().contains(forbidden),
                "status JSON exposed `{forbidden}`: {json}"
            );
        }
    }

    fn status_from(obs: &LinkObservation, recovering: bool) -> WifiStatus {
        let cfg = WifidConfig::default();
        let mut pub_ = ThrottlePublisher::new(&cfg.throttle);
        let throttle = pub_.update(ThrottleInputs {
            link_mode: LinkMode::Sta,
            sta_link_up: true,
            chip_recovering: false,
            near_deadlock: false,
            tc_applied: true,
            ap_overlay_active: false,
            ap_cap_applied: false,
        });
        WifiStatus::new(
            LinkMode::Sta,
            obs,
            throttle,
            recovering,
            ApStatus {
                mode: ApMode::ForceOff,
                active: false,
                ssid: Some("TeslaUSB".to_owned()),
                client_count: 0,
                ip: None,
            },
        )
    }

    #[test]
    fn log_key_ignores_signal_drift_but_not_real_change() {
        let base = status_from(&observation(), false);

        // Signal drifts a few dBm on essentially every tick. If that counted as
        // a change, the serve loop's duplicate suppression would never suppress
        // anything and the journal flood would be back.
        let mut drifted = observation();
        drifted.signal_dbm = Some(-71);
        assert_eq!(base.log_key(), status_from(&drifted, false).log_key());

        // Real state changes must still be reported, or suppression would hide
        // the events the journal exists to capture.
        assert_ne!(base.log_key(), status_from(&observation(), true).log_key());

        let mut gateway_lost = observation();
        gateway_lost.gateway_reachable = false;
        assert_ne!(base.log_key(), status_from(&gateway_lost, false).log_key());
    }
}
