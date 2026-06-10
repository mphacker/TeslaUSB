//! Static, operator-tunable configuration for `wifid`.
//!
//! **No policy numbers are hardcoded into logic.** Every threshold lives here
//! as an explicit parameter with a conservative provisional default. Two
//! classes are called out:
//!
//! * `// CALIBRATION-GATED (Task 2.6)` — the value depends on the **measured**
//!   BCM43436 SDIO-deadlock TX threshold from the Phase-2 `WiFi` TX-cap spike,
//!   which has **not run**. The default is deliberately low so that, until the
//!   spike runs, `wifid` errs toward *too slow* rather than risking the bus.
//! * `// TUNABLE` — a timing/escalation knob whose real value is a field/HW
//!   tuning decision (`wifid.md` §6 "ASK FIRST" items). Encoded as config, not
//!   invented policy.

use std::time::Duration;

/// Complete runtime configuration. Built from defaults and (on the device)
/// overlaid by an operator config file; the pure core only ever reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WifidConfig {
    /// Link-plane / state-machine timing.
    pub(crate) link: LinkConfig,
    /// TX throttle (token bucket) parameters.
    pub(crate) throttle: ThrottleConfig,
    /// Liveness watchdog + recovery escalation.
    pub(crate) watchdog: WatchdogConfig,
    /// Platform/hardware integration knobs (interface + NM profile names).
    pub(crate) platform: PlatformConfig,
}

/// Names the production [`crate::nmcli`] controller needs to drive the real
/// radio. **No policy is hardcoded into logic** — the interface, the two
/// `NetworkManager` profile names `wifid` toggles, and the kernel module it
/// reloads all live here so the device's provisioning (owned elsewhere) and
/// `wifid` agree by configuration, not by a magic string buried in code.
///
/// `wifid` never *creates* these profiles (that is the device-setup concern,
/// which also owns the SSID + secrets); it only brings the pre-provisioned
/// profiles up/down and observes them, so a secret never reaches a command
/// line through this daemon.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PlatformConfig {
    /// The Wi-Fi interface (e.g. `wlan0`).
    pub(crate) wifi_iface: String,
    /// The pre-provisioned `NetworkManager` connection profile for station
    /// (home-WiFi) mode.
    pub(crate) sta_profile: String,
    /// The pre-provisioned `NetworkManager` connection profile for the WPA2
    /// onboarding access point. Always WPA2 (never an open AP); if it is not
    /// provisioned, bringing the AP up simply fails — `wifid` never falls back
    /// to an open network.
    pub(crate) ap_profile: String,
    /// The Wi-Fi kernel module the chip-reset watchdog reloads to recover a
    /// wedged BCM43436 (`rmmod`/`modprobe`).
    pub(crate) wifi_module: String,
}

/// STA/AP state-machine timing knobs (`wifid.md` §2.1: debounce flaps).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LinkConfig {
    /// STA must stay viable continuously this long before it counts as "up"
    /// (gates full upload throttle, avoids flapping the cap). TUNABLE.
    pub(crate) sta_up_debounce: Duration,
    /// STA must stay non-viable continuously this long before falling back to
    /// AP — the core anti-thrash debounce (`wifid.md` §2.1). TUNABLE.
    pub(crate) sta_down_debounce: Duration,
    /// Once in AP onboarding, the *base* interval before tearing AP down to
    /// re-probe STA. Grows with backoff up to [`Self::ap_sta_retry_max`].
    /// TUNABLE.
    pub(crate) ap_sta_retry_base: Duration,
    /// Cap on the (backed-off) AP→STA retry interval. TUNABLE.
    pub(crate) ap_sta_retry_max: Duration,
    /// Minimum time the AP must stay up once started, so a phone has a stable
    /// window to join and submit credentials before any STA re-probe can tear
    /// it down. TUNABLE.
    pub(crate) ap_min_uptime: Duration,
    /// Grace period after the machine *commands* a mode transition during which
    /// an observation that does not yet reflect the new mode is treated as the
    /// command still settling (radio/hostapd bring-up lag) rather than as
    /// genuine drift. This prevents the machine from abandoning an in-flight
    /// transition — e.g. issuing `StartSta` while a just-commanded `StartAp` is
    /// still launching, which could leave both radios up across ticks. Must
    /// cover a few control-loop ticks. TUNABLE.
    pub(crate) transition_settle: Duration,
}

/// TX rate-cap parameters (`wifid.md` §2.3; contract D4). The byte values are
/// the spike-#4 outputs and stay provisional until measured.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ThrottleConfig {
    /// Sustained TX ceiling in bytes/sec, enforced by the token bucket and the
    /// kernel `tc` cap.
    // CALIBRATION-GATED (Task 2.6): provisional; real value from the 2.6 WiFi
    // TX-cap spike. 1 MiB/s is a conservative placeholder well under any
    // plausible SDIO-deadlock threshold.
    pub(crate) max_tx_bytes_per_s: u64,
    /// Per-write chunk ceiling published to `uploadd` (`tc` cannot enforce
    /// chunking; that is `uploadd`'s job).
    // CALIBRATION-GATED (Task 2.6): provisional; real value from the 2.6 WiFi
    // TX-cap spike.
    pub(crate) max_chunk_bytes: u32,
    /// Token-bucket burst capacity in bytes (how much unused allowance may
    /// accumulate). Kept to one second of cap so a burst can never exceed the
    /// per-second ceiling by more than a small margin. TUNABLE.
    pub(crate) bucket_capacity_bytes: u64,
    /// Divisor applied to [`Self::max_tx_bytes_per_s`] when the watchdog reports
    /// the link is nearing the SDIO-deadlock threshold (`NearDeadlock` backoff).
    /// TUNABLE.
    pub(crate) near_deadlock_divisor: u64,
}

/// Liveness-watchdog + recovery-escalation knobs (`wifid.md` §2.4, §6).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WatchdogConfig {
    /// The chip must look wedged continuously this long before recovery starts
    /// (debounces a transient stall). TUNABLE.
    pub(crate) wedge_confirm: Duration,
    /// After issuing a chip reset, wait this long before judging whether the
    /// chip recovered. Recovery is judged by *observed health*, never by the
    /// `rmmod/modprobe` exit status. TUNABLE.
    pub(crate) reset_verify_window: Duration,
    /// Number of chip resets that must each fail to restore health before a Pi
    /// reboot is even *considered*. Chip reset is always preferred
    /// (`SPEC.md` §2 invariant 4). ASK-FIRST escalation policy — TUNABLE.
    pub(crate) max_chip_resets_before_reboot: u32,
    /// The gadgetd write-heartbeat must read USB-idle for at least this long
    /// before a last-resort reboot is permitted. TUNABLE.
    pub(crate) reboot_idle_grace: Duration,
    /// Maximum age of a gadgetd heartbeat reading before it is treated as
    /// stale/unknown ("car may be writing" ⇒ never reboot). TUNABLE.
    pub(crate) heartbeat_max_age: Duration,
}

impl Default for WifidConfig {
    fn default() -> Self {
        Self {
            link: LinkConfig {
                sta_up_debounce: Duration::from_secs(5),
                sta_down_debounce: Duration::from_secs(20),
                ap_sta_retry_base: Duration::from_secs(60),
                ap_sta_retry_max: Duration::from_secs(900),
                ap_min_uptime: Duration::from_secs(120),
                transition_settle: Duration::from_secs(8),
            },
            throttle: ThrottleConfig {
                // CALIBRATION-GATED (Task 2.6): provisional defaults.
                max_tx_bytes_per_s: 1024 * 1024,
                max_chunk_bytes: 256 * 1024,
                bucket_capacity_bytes: 1024 * 1024,
                near_deadlock_divisor: 2,
            },
            watchdog: WatchdogConfig {
                wedge_confirm: Duration::from_secs(15),
                reset_verify_window: Duration::from_secs(20),
                max_chip_resets_before_reboot: 3,
                reboot_idle_grace: Duration::from_secs(30),
                heartbeat_max_age: Duration::from_secs(10),
            },
            platform: PlatformConfig {
                wifi_iface: "wlan0".to_owned(),
                sta_profile: "teslausb-sta".to_owned(),
                ap_profile: "teslausb-ap".to_owned(),
                wifi_module: "brcmfmac".to_owned(),
            },
        }
    }
}

impl WifidConfig {
    /// Validate cross-field invariants that the rest of the core assumes.
    ///
    /// # Errors
    /// Returns a static reason if a duration ordering or a zeroed cap would
    /// make the core misbehave (e.g. a zero TX cap with uploads "allowed").
    pub(crate) fn validate(&self) -> std::result::Result<(), &'static str> {
        if self.throttle.max_tx_bytes_per_s == 0 {
            return Err("max_tx_bytes_per_s must be > 0");
        }
        if self.throttle.max_chunk_bytes == 0 {
            return Err("max_chunk_bytes must be > 0");
        }
        if self.throttle.near_deadlock_divisor == 0 {
            return Err("near_deadlock_divisor must be >= 1");
        }
        if self.throttle.bucket_capacity_bytes < self.throttle.max_tx_bytes_per_s {
            return Err("bucket_capacity_bytes must be >= max_tx_bytes_per_s");
        }
        if self.link.ap_sta_retry_max < self.link.ap_sta_retry_base {
            return Err("ap_sta_retry_max must be >= ap_sta_retry_base");
        }
        if self.link.sta_down_debounce < self.link.transition_settle {
            return Err("sta_down_debounce must be >= transition_settle");
        }
        if self.watchdog.max_chip_resets_before_reboot == 0 {
            // Chip-reset is always preferred over a Pi reboot; zero would send a
            // confirmed wedge straight to the reboot gate (invariant 2).
            return Err("max_chip_resets_before_reboot must be >= 1");
        }
        if self.platform.wifi_iface.is_empty() {
            return Err("platform.wifi_iface must not be empty");
        }
        if self.platform.sta_profile.is_empty() {
            return Err("platform.sta_profile must not be empty");
        }
        if self.platform.ap_profile.is_empty() {
            return Err("platform.ap_profile must not be empty");
        }
        if self.platform.sta_profile == self.platform.ap_profile {
            // Toggling between modes relies on two *distinct* NM profiles;
            // collapsing them would make stop-before-start ambiguous.
            return Err("platform.sta_profile and ap_profile must differ");
        }
        if self.platform.wifi_module.is_empty() {
            return Err("platform.wifi_module must not be empty");
        }
        Ok(())
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::WifidConfig;

    #[test]
    fn default_config_is_self_consistent() {
        WifidConfig::default().validate().expect("default valid");
    }

    #[test]
    fn zero_tx_cap_is_rejected() {
        let mut cfg = WifidConfig::default();
        cfg.throttle.max_tx_bytes_per_s = 0;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn bucket_smaller_than_rate_is_rejected() {
        let mut cfg = WifidConfig::default();
        cfg.throttle.bucket_capacity_bytes = cfg.throttle.max_tx_bytes_per_s - 1;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn zero_chip_resets_before_reboot_is_rejected() {
        // Chip-reset must always precede a reboot (invariant 2).
        let mut cfg = WifidConfig::default();
        cfg.watchdog.max_chip_resets_before_reboot = 0;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn settle_window_longer_than_down_debounce_is_rejected() {
        let mut cfg = WifidConfig::default();
        cfg.link.transition_settle = cfg.link.sta_down_debounce + std::time::Duration::from_secs(1);
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn identical_sta_and_ap_profiles_are_rejected() {
        let mut cfg = WifidConfig::default();
        cfg.platform.ap_profile = cfg.platform.sta_profile.clone();
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn empty_platform_names_are_rejected() {
        let mut cfg = WifidConfig::default();
        cfg.platform.wifi_iface = String::new();
        assert!(cfg.validate().is_err());
    }
}
