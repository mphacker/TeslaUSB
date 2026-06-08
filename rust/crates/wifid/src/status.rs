//! The status shape `wifid` exposes to `webd` (`wifid.md` §6).
//!
//! Read-only: mode, link facts, signal, throttle state, and whether chip
//! recovery is in flight. By construction it contains **no credential field**,
//! so a secret can never reach the SPA through status.

use serde::Serialize;

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
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) struct WifiStatus {
    /// Current radio mode.
    pub(crate) mode: LinkMode,
    /// Link facts.
    pub(crate) link: LinkSummary,
    /// Published throttle state (seq + body).
    pub(crate) throttle: ThrottleState,
    /// Whether the SDIO chip-reset watchdog is mid-recovery.
    pub(crate) recovering: bool,
}

impl WifiStatus {
    /// Assemble the status from the current core state.
    pub(crate) fn new(
        mode: LinkMode,
        obs: &LinkObservation,
        throttle: ThrottleState,
        recovering: bool,
    ) -> Self {
        Self {
            mode,
            link: LinkSummary::from(obs),
            throttle,
            recovering,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::WifiStatus;
    use crate::config::WifidConfig;
    use crate::link::{LinkMode, LinkObservation};
    use crate::throttle::{ThrottleInputs, ThrottlePublisher};

    fn observation() -> LinkObservation {
        LinkObservation {
            sta_configured: true,
            sta_running: true,
            ap_running: false,
            associated: true,
            carrier_up: true,
            gateway_reachable: true,
            ap_has_clients: false,
            signal_dbm: Some(-55),
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
        });
        let status = WifiStatus::new(LinkMode::Sta, &observation(), throttle, false);
        let json = serde_json::to_string(&status).expect("serialise");
        // Sanity: expected shape present.
        assert!(json.contains("\"mode\":\"sta\""));
        assert!(json.contains("\"signal_dbm\":-55"));
        assert!(json.contains("\"uploads_allowed\":true"));
        // Safety: no secret-bearing keys exist in the shape at all.
        for forbidden in ["psk", "passphrase", "secret", "password"] {
            assert!(
                !json.to_lowercase().contains(forbidden),
                "status JSON exposed `{forbidden}`: {json}"
            );
        }
    }
}
