//! Production [`NetworkController`] driven by `NetworkManager` (`nmcli`), `iw`,
//! `tc`, and `modprobe` — the real radio I/O for the Pi (`wifid.md` §2,
//! `SPEC.md` §2 invariant 4).
//!
//! Design constraints this module is built around:
//!
//! * **Secret-safe by construction.** `wifid` never *creates* a connection
//!   profile and never passes a PSK/passphrase on a command line. It only
//!   brings **pre-provisioned** profiles (named in [`PlatformConfig`]) up/down,
//!   so a secret can never leak into `ps`/the journal through this daemon. The
//!   SSID + secrets live in the profiles the device-setup layer owns.
//! * **Never knock SSH offline.** [`NmcliNetworkController::stop_sta`] refuses
//!   to tear down a STA that is *currently a working management path*
//!   (associated + carrier + gateway reachable). The link machine only ever
//!   asks for that once STA has been non-viable for the debounce, so the guard
//!   is belt-and-braces — but it makes "WiFi/SSH must never go offline" hold
//!   even under a logic bug or a racing observation.
//! * **Mutual exclusion against reality.** Observation reads the *actual*
//!   active connections so the pure [`crate::link`] core reconciles AP/STA
//!   against the live radio, never against intent.
//! * **Host-testable logic behind the seam.** Every parser / argument builder
//!   here is a pure free function with unit tests; the only untestable part is
//!   the thin `Command` shell-out, which is exercised on-device.
//!
//! The argument vectors and output parsers are intentionally tolerant: a
//! failed/absent helper degrades to the conservative value (mode down, not
//! viable) rather than erroring the whole observation, so a transient `nmcli`
//! hiccup can never crash the daemon.

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};

use crate::config::PlatformConfig;
use crate::error::{Result, WifidError};
use crate::link::LinkObservation;
use crate::overlay::parse_iface_channel;
use crate::traits::NetworkController;
use crate::watchdog::ChipObservation;

const MUTATION_HOLD_PATH: &str = "/run/teslausb/wifi-mutation.hold";
/// Must outlive webd's 60s checkpoint window plus recovery slack.
const MUTATION_HOLD_TTL_SECS: f64 = 70.0;

/// `NetworkManager`/`nmcli`-driven controller bound to one Wi-Fi interface and
/// its pre-provisioned STA connection profile.
pub(crate) struct NmcliNetworkController {
    cfg: PlatformConfig,
}

impl NmcliNetworkController {
    /// Build a controller for the configured interface + profile.
    pub(crate) fn new(cfg: PlatformConfig) -> Self {
        Self { cfg }
    }

    /// Is the STA currently a *working* management path? Used as the SSH-safety
    /// guard before any STA teardown. If we cannot tell, assume yes (refuse the
    /// teardown) — fail safe toward never cutting SSH.
    fn sta_is_working_management_path(&self) -> bool {
        match self.observe_link() {
            Ok(o) => o.sta_running && o.associated && o.carrier_up && o.gateway_reachable,
            Err(_) => true,
        }
    }
}

impl NetworkController for NmcliNetworkController {
    fn observe_link(&self) -> Result<LinkObservation> {
        let iface = self.cfg.wifi_iface.as_str();

        let active = capture(
            "nmcli",
            &[
                "-t",
                "-f",
                "TYPE,DEVICE,STATE,NAME",
                "connection",
                "show",
                "--active",
            ],
        )
        .unwrap_or_default();
        let sta_running = any_active_wifi_sta(&active, iface);
        let ap_running = false;

        let link = capture("iw", &["dev", iface, "link"]).unwrap_or_default();
        let associated = sta_associated(&link, sta_running);
        let signal_dbm = parse_iw_signal_dbm(&link);
        let sta_channel = capture("iw", &["dev", iface, "info"])
            .and_then(|info| parse_iface_channel(&info));

        let dev_show = capture(
            "nmcli",
            &[
                "-t",
                "-f",
                "IP4.ADDRESS,IP4.GATEWAY",
                "device",
                "show",
                iface,
            ],
        )
        .unwrap_or_default();
        let carrier_up = has_ip(&dev_show);
        // Quiet: this runs on every tick, and `ping` prints five lines per
        // invocation. Inheriting that output floods the journal hard enough to
        // evict every other daemon's history (see `run_ok_quiet`).
        let gateway_reachable = nmcli_field(&dev_show, "IP4.GATEWAY")
            .is_some_and(|gw| run_ok_quiet("ping", &["-c", "1", "-W", "1", &gw]));

        let ap_has_clients = ap_running && {
            let dump = capture("iw", &["dev", iface, "station", "dump"]).unwrap_or_default();
            count_stations(&dump) > 0
        };

        Ok(LinkObservation {
            // The daemon overwrites this from the credential store (the source
            // of truth for "is STA configured"); the radio cannot know it.
            sta_configured: false,
            sta_running,
            ap_running,
            ap_fallback_suppressed: false,
            mutation_hold: read_mutation_hold(),
            associated,
            carrier_up,
            gateway_reachable,
            ap_has_clients,
            signal_dbm,
            sta_channel,
        })
    }

    fn observe_chip(&self) -> Result<ChipObservation> {
        // Coarse but reliable SDIO-wedge signal: a wedged BCM43436 drops its
        // netdev. Presence of the interface in sysfs ⇒ the driver is alive. The
        // watchdog debounces this over `wedge_confirm` and judges recovery by
        // re-reading it, never by a command's exit status, so a coarse signal
        // is sufficient and is tuned on-device.
        let present = Path::new("/sys/class/net")
            .join(&self.cfg.wifi_iface)
            .exists();
        Ok(ChipObservation { healthy: present })
    }

    fn start_sta(&self) -> Result<()> {
        up_profile(&self.cfg.sta_profile)
    }

    fn stop_sta(&self) -> Result<()> {
        if self.sta_is_working_management_path() {
            // SSH safety net (see module docs). Should never fire in normal
            // operation because the link machine only asks once STA is dead.
            return Err(WifidError::Network(
                "refusing to stop STA: it is the active management path (SSH safety)".to_owned(),
            ));
        }
        down_profile(&self.cfg.sta_profile)
    }

    fn apply_tx_cap(&self, bytes_per_s: u64) -> Result<()> {
        let cap_args = tc_cap_args(&self.cfg.wifi_iface, bytes_per_s);
        let argv: Vec<&str> = cap_args.iter().map(String::as_str).collect();
        if run_ok("tc", &argv) {
            Ok(())
        } else {
            Err(WifidError::Network(format!(
                "tc egress cap on {} failed",
                self.cfg.wifi_iface
            )))
        }
    }

    fn apply_ap_tx_cap(&self, bytes_per_s: u64) -> Result<()> {
        let cap_args = tc_cap_args("uap0", bytes_per_s);
        let argv: Vec<&str> = cap_args.iter().map(String::as_str).collect();
        if run_ok("tc", &argv) {
            Ok(())
        } else {
            Err(WifidError::Network("tc egress cap on uap0 failed".to_owned()))
        }
    }

    fn reset_chip(&self) -> Result<()> {
        // Chip-only recovery: reload brcmfmac. The unload is best-effort (it may
        // already be gone); success is judged by the reload here and ultimately
        // by observed chip health next tick, never by exit status alone.
        let module = self.cfg.wifi_module.as_str();
        let _ = run_ok("modprobe", &["-r", module]);
        if run_ok("modprobe", &[module]) {
            Ok(())
        } else {
            Err(WifidError::Network(format!("modprobe {module} failed")))
        }
    }
}

/// Bring a pre-provisioned `NetworkManager` profile up.
fn up_profile(profile: &str) -> Result<()> {
    if run_ok("nmcli", &["connection", "up", profile]) {
        Ok(())
    } else {
        Err(WifidError::Network(format!(
            "nmcli connection up {profile} failed"
        )))
    }
}

/// Bring a `NetworkManager` profile down. Idempotent on the device (downing an
/// already-down profile is reported as success).
fn down_profile(profile: &str) -> Result<()> {
    if run_ok("nmcli", &["connection", "down", profile]) {
        Ok(())
    } else {
        Err(WifidError::Network(format!(
            "nmcli connection down {profile} failed"
        )))
    }
}

/// Run a command, returning whether it exited successfully. A spawn failure
/// (binary absent) is `false`, never a panic.
pub(crate) fn run_ok(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .status()
        .is_ok_and(|s| s.success())
}

/// Like [`run_ok`], but discards the child's stdout/stderr. For best-effort
/// commands re-issued on every tick (e.g. reasserting radio power-save), where
/// the caller handles failure itself and the child's own error output would
/// otherwise flood the journal.
pub(crate) fn run_ok_quiet(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|s| s.success())
}

/// Run a command and capture its stdout as UTF-8 on success, else `None`.
pub(crate) fn capture(program: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(program).args(args).output().ok()?;
    if out.status.success() {
        String::from_utf8(out.stdout).ok()
    } else {
        None
    }
}

/// The `tc` argument vector for an idempotent egress token-bucket cap.
///
/// `tc qdisc replace … root tbf rate <bits>bit burst <bytes> latency 50ms`.
/// `replace` is idempotent, so re-applying the same cap is a no-op and changing
/// it does not require tearing the old qdisc down first. The *rate value* is
/// calibration-gated (Task 2.6); this only builds the mechanism.
fn tc_cap_args(iface: &str, bytes_per_s: u64) -> Vec<String> {
    let bits = bytes_per_s.saturating_mul(8);
    // One second of data, with a small floor so a tiny cap still admits a
    // single full-size frame.
    let burst = bytes_per_s.max(1600);
    vec![
        "qdisc".to_owned(),
        "replace".to_owned(),
        "dev".to_owned(),
        iface.to_owned(),
        "root".to_owned(),
        "tbf".to_owned(),
        "rate".to_owned(),
        format!("{bits}bit"),
        "burst".to_owned(),
        burst.to_string(),
        "latency".to_owned(),
        "50ms".to_owned(),
    ]
}

/// `true` when any active row is a Wi-Fi STA on `iface`: terse (`nmcli -t`)
/// `TYPE:DEVICE:STATE:NAME` with `TYPE=802-11-wireless` and `STATE` in
/// {`activated`, `activating`}.
///
/// `activating` counts as "running" so a mid-association or roaming STA (the
/// live NM-owned home connection briefly re-associating) reads as present, not
/// absent. Viability is a separate, stricter gate (`sta_viable`: associated +
/// carrier + gateway), so an `activating` STA is running-but-not-yet-viable and
/// falls through the debounced path rather than the un-debounced
/// `!sta_configured → AP` path — preventing a transient re-associate from
/// flipping the daemon to AP mode (and suppressing uploads) on every roam.
///
fn any_active_wifi_sta(active_list: &str, iface: &str) -> bool {
    active_list.lines().any(|line| {
        let mut fields = line.splitn(4, ':');
        let ty = fields.next().unwrap_or_default();
        let device = fields.next().unwrap_or_default();
        let state = fields.next().unwrap_or_default();
        let _name = fields.next().unwrap_or_default();
        ty == "802-11-wireless"
            && device == iface
            && matches!(state, "activated" | "activating")
    })
}

/// Read the current monotonic uptime (seconds since boot) from `/proc/uptime`
/// (its first whitespace token). Monotonic and immune to wall-clock/NTP steps:
/// the Pi Zero 2 W has no RTC and steps its clock forward when NTP disciplines
/// it after Wi-Fi comes up, so a wall-clock (`mtime`) freshness check could
/// wrongly expire or extend the hold.
fn read_uptime_secs() -> Option<f64> {
    let raw = fs::read_to_string("/proc/uptime").ok()?;
    raw.split_whitespace().next()?.parse::<f64>().ok()
}

/// Parse the hold file's stored start-uptime (its first whitespace token).
fn read_hold_start_secs() -> Option<f64> {
    let raw = fs::read_to_string(MUTATION_HOLD_PATH).ok()?;
    raw.split_whitespace().next()?.parse::<f64>().ok()
}

/// `true` while a `webd` Wi-Fi mutation hold is in effect. The hold file holds
/// the monotonic uptime-seconds captured by `webd` at mutation start; the hold
/// is fresh for [`MUTATION_HOLD_TTL_SECS`] after that instant. A missing, empty,
/// malformed, or expired file — or a `start` in the future relative to now —
/// reads as "not held" (fail toward normal link management; the NM checkpoint is
/// the primary join safety, this hold is only belt-and-braces). `/run` is tmpfs,
/// so a stale file can never survive a reboot.
fn read_mutation_hold() -> bool {
    let age_secs = match (read_hold_start_secs(), read_uptime_secs()) {
        (Some(start), Some(now)) => Some(now - start),
        _ => None,
    };
    mutation_hold_fresh(age_secs)
}

/// A hold `age` (now-uptime minus start-uptime, seconds) is fresh iff it is
/// non-negative and within the TTL. A negative age (start in the future) is a
/// clock/parse anomaly and is treated as not-held.
fn mutation_hold_fresh(age_secs: Option<f64>) -> bool {
    matches!(age_secs, Some(age) if (0.0..=MUTATION_HOLD_TTL_SECS).contains(&age))
}

/// Read a single-valued `KEY:value` field from terse `nmcli … show` output,
/// treating an empty value or NM's `--` placeholder as absent.
fn nmcli_field(show: &str, key: &str) -> Option<String> {
    for line in show.lines() {
        if let Some((k, v)) = line.split_once(':') {
            if k == key {
                let v = v.trim();
                if !v.is_empty() && v != "--" {
                    return Some(v.to_owned());
                }
            }
        }
    }
    None
}

/// Does the device have an IPv4 address? (`IP4.ADDRESS[n]:…` in terse output.)
fn has_ip(show: &str) -> bool {
    show.lines().any(|line| {
        line.split_once(':').is_some_and(|(k, v)| {
            let v = v.trim();
            k.starts_with("IP4.ADDRESS") && !v.is_empty() && v != "--"
        })
    })
}

/// Is the STA associated to a BSSID? (`iw dev <if> link` prints `Connected to …`
/// when associated, `Not connected.` otherwise.)
fn iw_connected(link: &str) -> bool {
    link.lines()
        .any(|l| l.trim_start().starts_with("Connected to"))
}

/// `true` when the STA is associated. Prefers `iw`'s radio-level association,
/// but falls back to `NetworkManager`'s authority (`sta_running` = an `activated`
/// non-AP Wi-Fi connection on the interface) so a base image that does not ship
/// `iw` still reports association correctly instead of oscillating STA↔AP and
/// re-suppressing uploads. This is only ever an OR — it can add association,
/// never remove it — and viability keeps its independent `carrier_up`
/// (has-IP) and `gateway_reachable` (live ping) gates, so a genuinely dead link
/// is still caught even when NM's `activated` state momentarily lags reality.
fn sta_associated(iw_link: &str, sta_running: bool) -> bool {
    iw_connected(iw_link) || sta_running
}

/// Parse the STA signal strength in dBm from `iw dev <if> link` (`signal: -55
/// dBm`). `None` when not present.
fn parse_iw_signal_dbm(link: &str) -> Option<i32> {
    for line in link.lines() {
        if let Some(rest) = line.trim().strip_prefix("signal:") {
            return rest.split_whitespace().next()?.parse::<i32>().ok();
        }
    }
    None
}

/// Count associated stations in `iw dev <if> station dump` (one `Station …`
/// header per client). Used only in AP mode to keep onboarding sticky.
pub(crate) fn count_stations(dump: &str) -> usize {
    dump.lines()
        .filter(|l| l.trim_start().starts_with("Station "))
        .count()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        any_active_wifi_sta, count_stations, has_ip, iw_connected, mutation_hold_fresh, nmcli_field,
        parse_iw_signal_dbm, sta_associated, tc_cap_args,
    };

    #[test]
    fn any_active_wifi_sta_detects_live_nm_owned_sta_only() {
        // Terse layout is TYPE:DEVICE:STATE:NAME (NAME last, colon-tolerant).
        assert!(any_active_wifi_sta(
            "802-11-wireless:wlan0:activated:netplan-wlan0-Trez\n",
            "wlan0"
        ));
        // `activating` (mid-association / roam) counts as a live STA so a
        // transient re-associate never reads as absent and flips us to AP.
        assert!(any_active_wifi_sta(
            "802-11-wireless:wlan0:activating:netplan-wlan0-Trez\n",
            "wlan0"
        ));
        // A profile NAME with an escaped colon (SSID containing `:`) still
        // parses: NAME is the `splitn(4)` remainder, so TYPE/DEVICE/STATE are
        // unaffected and the STA is correctly detected.
        assert!(any_active_wifi_sta(
            "802-11-wireless:wlan0:activated:netplan-wlan0-Guest\\:5G\n",
            "wlan0"
        ));
        // Ethernet is not a Wi-Fi STA.
        assert!(!any_active_wifi_sta(
            "802-3-ethernet:eth0:activated:netplan-eth0\n",
            "wlan0"
        ));
        // Wrong interface.
        assert!(!any_active_wifi_sta(
            "802-11-wireless:wlan1:activated:netplan-wlan0-Trez\n",
            "wlan0"
        ));
        // `deactivating` is a real drop in progress ⇒ not running.
        assert!(!any_active_wifi_sta(
            "802-11-wireless:wlan0:deactivating:netplan-wlan0-Trez\n",
            "wlan0"
        ));
    }

    #[test]
    fn mutation_hold_fresh_uses_ttl() {
        assert!(!mutation_hold_fresh(None));
        assert!(mutation_hold_fresh(Some(0.0)));
        assert!(mutation_hold_fresh(Some(70.0)));
        assert!(!mutation_hold_fresh(Some(70.1)));
        // A negative age (hold start in the future — a clock/parse anomaly) is
        // treated as not-held rather than a stuck-on hold.
        assert!(!mutation_hold_fresh(Some(-1.0)));
    }

    #[test]
    fn nmcli_field_skips_empty_and_placeholder() {
        let show = "IP4.GATEWAY:192.168.1.1\nIP4.DNS:--\nIP6.GATEWAY:\n";
        assert_eq!(
            nmcli_field(show, "IP4.GATEWAY").as_deref(),
            Some("192.168.1.1")
        );
        assert!(nmcli_field(show, "IP4.DNS").is_none());
        assert!(nmcli_field(show, "IP6.GATEWAY").is_none());
        assert!(nmcli_field(show, "MISSING").is_none());
    }

    #[test]
    fn has_ip_detects_indexed_address_keys() {
        assert!(has_ip("IP4.ADDRESS[1]:192.168.1.50/24\n"));
        assert!(!has_ip("IP4.ADDRESS[1]:--\n"));
        assert!(!has_ip("IP4.GATEWAY:192.168.1.1\n"));
    }

    #[test]
    fn iw_connected_reads_association_state() {
        assert!(iw_connected(
            "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: home\n"
        ));
        assert!(!iw_connected("Not connected.\n"));
    }

    #[test]
    fn sta_associated_prefers_iw_then_falls_back_to_nm_authority() {
        // `iw` confirms radio-level association ⇒ associated regardless of NM.
        assert!(sta_associated(
            "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n",
            false
        ));
        // No `iw` (e.g. not installed ⇒ empty output), but NM reports an
        // activated STA (`sta_running`) ⇒ associated via NM authority.
        assert!(sta_associated("", true));
        // Neither `iw` association nor an NM-active STA ⇒ not associated.
        assert!(!sta_associated("Not connected.\n", false));
        assert!(!sta_associated("", false));
    }

    #[test]
    fn parse_signal_reads_negative_dbm() {
        let link = "Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tsignal: -55 dBm\n";
        assert_eq!(parse_iw_signal_dbm(link), Some(-55));
        assert_eq!(parse_iw_signal_dbm("Not connected.\n"), None);
    }

    #[test]
    fn count_stations_counts_ap_clients() {
        let dump = "Station aa:bb:cc:dd:ee:01 (on wlan0)\n\tinactive time: 10 ms\n\
                    Station aa:bb:cc:dd:ee:02 (on wlan0)\n";
        assert_eq!(count_stations(dump), 2);
        assert_eq!(count_stations(""), 0);
    }

    #[test]
    fn tc_cap_args_encodes_rate_in_bits_and_is_idempotent_replace() {
        let args = tc_cap_args("wlan0", 1024 * 1024);
        assert_eq!(args.first().map(String::as_str), Some("qdisc"));
        assert!(
            args.iter().any(|a| a == "replace"),
            "must use idempotent replace"
        );
        assert!(args.iter().any(|a| a == "wlan0"));
        // 1 MiB/s × 8 = 8388608 bits/s.
        assert!(
            args.iter().any(|a| a == "8388608bit"),
            "rate must be expressed in bits: {args:?}"
        );
    }
}
