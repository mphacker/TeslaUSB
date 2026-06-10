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

use std::path::Path;
use std::process::Command;

use crate::config::PlatformConfig;
use crate::error::{Result, WifidError};
use crate::link::LinkObservation;
use crate::traits::NetworkController;
use crate::watchdog::ChipObservation;

/// `NetworkManager`/`nmcli`-driven controller bound to one Wi-Fi interface and
/// its two pre-provisioned connection profiles.
pub(crate) struct NmcliNetworkController {
    cfg: PlatformConfig,
}

impl NmcliNetworkController {
    /// Build a controller for the configured interface + profiles.
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
                "NAME,DEVICE,STATE",
                "connection",
                "show",
                "--active",
            ],
        )
        .unwrap_or_default();
        let sta_running = active_state(&active, &self.cfg.sta_profile, iface).is_some();
        let ap_running = active_state(&active, &self.cfg.ap_profile, iface).is_some();

        let link = capture("iw", &["dev", iface, "link"]).unwrap_or_default();
        let associated = iw_connected(&link);
        let signal_dbm = parse_iw_signal_dbm(&link);

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
        let gateway_reachable = nmcli_field(&dev_show, "IP4.GATEWAY")
            .is_some_and(|gw| run_ok("ping", &["-c", "1", "-W", "1", &gw]));

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
            associated,
            carrier_up,
            gateway_reachable,
            ap_has_clients,
            signal_dbm,
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

    fn start_ap(&self) -> Result<()> {
        // Brings up the pre-provisioned WPA2 AP. If it is not provisioned this
        // simply fails — `wifid` never stands up an open network.
        up_profile(&self.cfg.ap_profile)
    }

    fn stop_ap(&self) -> Result<()> {
        down_profile(&self.cfg.ap_profile)
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
fn run_ok(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .status()
        .is_ok_and(|s| s.success())
}

/// Run a command and capture its stdout as UTF-8 on success, else `None`.
fn capture(program: &str, args: &[&str]) -> Option<String> {
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

/// Find the `STATE` of an active connection matching both `name` and `device`
/// in terse (`nmcli -t`) `NAME:DEVICE:STATE` output.
///
/// NOTE: terse mode escapes a literal `:` inside a field as `\:`; the
/// configured profile names contain no colons, so a plain split is correct here
/// and a name containing an escaped colon simply will not match (fail-safe:
/// treated as "not active").
fn active_state(active_list: &str, name: &str, device: &str) -> Option<String> {
    for line in active_list.lines() {
        let mut fields = line.splitn(3, ':');
        let n = fields.next()?;
        let d = fields.next().unwrap_or_default();
        let s = fields.next().unwrap_or_default();
        if n == name && d == device {
            return Some(s.to_owned());
        }
    }
    None
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
fn count_stations(dump: &str) -> usize {
    dump.lines()
        .filter(|l| l.trim_start().starts_with("Station "))
        .count()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        active_state, count_stations, has_ip, iw_connected, nmcli_field, parse_iw_signal_dbm,
        tc_cap_args,
    };

    #[test]
    fn active_state_matches_name_and_device() {
        let out = "teslausb-sta:wlan0:activated\nWired connection 1:eth0:activated\n";
        assert_eq!(
            active_state(out, "teslausb-sta", "wlan0").as_deref(),
            Some("activated")
        );
        // Right name, wrong device ⇒ no match (mutual-exclusion safety).
        assert!(active_state(out, "teslausb-sta", "eth0").is_none());
        // Absent profile ⇒ not active.
        assert!(active_state(out, "teslausb-ap", "wlan0").is_none());
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
