//! `uap0` AP overlay executor (W2): owns virtual-AP lifecycle in isolation.
//!
//! This module mirrors the proven on-device sequence for BCM43436/brcmfmac:
//! create `uap0`, assign a distinct locally-administered MAC, configure
//! `192.168.4.1/24`, launch `hostapd` + a dedicated `dnsmasq`, support
//! `hostapd_cli reconfigure`, and tear down minimally.

use std::cell::Cell;
use std::path::{Path, PathBuf};

use crate::creds::Secret;
use crate::error::{Result, WifidError};
use crate::exec::write_owner_only_atomic;
use crate::nmcli::{capture, count_stations, run_ok, run_ok_quiet};

const DEFAULT_UAP0: &str = "uap0";
const DEFAULT_IP_CIDR: &str = "192.168.4.1/24";
const DEFAULT_GATEWAY_IP: &str = "192.168.4.1";
const DEFAULT_DNSMASQ_RANGE: &str = "192.168.4.10,192.168.4.50,255.255.255.0";
const DEFAULT_RUN_DIR: &str = "/run/teslausb";
const DEFAULT_HOSTAPD_CONF: &str = "/run/teslausb/hostapd-uap0.conf";
pub(crate) const AP_OVERLAY_GATEWAY_IP: &str = DEFAULT_GATEWAY_IP;

#[derive(Debug)]
pub(crate) struct ApParams {
    pub(crate) ssid: String,
    pub(crate) passphrase: Secret,
    pub(crate) channel: u32,
}

/// The nl80211 interface type of `uap0`, parsed from `iw dev uap0 info`.
/// `Ap` is the only state in which the overlay is actually beaconing; the
/// brcmfmac firmware can leave the vif in a non-AP type after a failed
/// `START_AP`, and `Unknown` means the type line could not be read this tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ApIfaceType {
    Ap,
    Other,
    Unknown,
}

#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ApOverlayObservation {
    pub(crate) iface_exists: bool,
    pub(crate) hostapd_alive: bool,
    pub(crate) dnsmasq_alive: bool,
    pub(crate) ip_present: bool,
    pub(crate) channel: Option<u32>,
    pub(crate) iface_type: ApIfaceType,
    pub(crate) client_count: Option<u32>,
    pub(crate) active: bool,
}

#[allow(dead_code)] // wired into the composite state machine in W3
pub(crate) trait ApOverlay {
    /// Bring the overlay AP up (iface + hostapd). Idempotent: success if already
    /// up. Does NOT (re)assert the IP or start DHCP — the live path drives the
    /// split-tick bring-up (`ensure_iface_up` → settle → `ensure_ap_started` →
    /// confirm `type AP` → `ensure_ip` → `ensure_dhcp`); this convenience is
    /// unused there.
    fn ensure_up(&self, params: &ApParams) -> Result<()>;
    /// Bring `uap0` into existence (create+MAC+IP+up+unmanage) but do NOT start
    /// hostapd — phase 1 of the split-tick bring-up; the firmware needs a settle
    /// window before `START_AP`.
    fn ensure_iface_up(&self) -> Result<()>;
    /// Start (or reconfigure) hostapd on an already-up `uap0` — phase 2, called a
    /// tick after `ensure_iface_up` so the firmware has settled. hostapd's
    /// `START_AP` mode-transition flushes uap0's IPv4 on this single-radio `FullMAC`
    /// chip, so neither the IP re-assert nor DHCP happen here (they would race the
    /// async flush); the IP re-assert happens in `ensure_ip` and dnsmasq in
    /// `ensure_dhcp` once the vif is confirmed `type AP`.
    fn ensure_ap_started(&self, params: &ApParams) -> Result<()>;
    /// Re-assert uap0's IPv4 — phase 3, called only once `observe()` reports the
    /// vif is beaconing (`type AP`) but has lost its IP. `type AP` means
    /// `START_AP` is complete, so the flush already happened and this `ip addr
    /// add` sticks (no pending async flush to race). Does NOT start DHCP: dnsmasq
    /// is started separately (`ensure_dhcp`) once the restored IP has proven
    /// stable across a tick, so it never binds during the post-`START_AP` flush
    /// window. Idempotent (no-op if the address is already present).
    fn ensure_ip(&self) -> Result<()>;
    /// (Re)start dnsmasq on uap0 — called only once the AP is confirmed stably
    /// `active` (IP present and survived a tick). dnsmasq needs the restored IPv4
    /// to bind uap0 or it exits "unknown interface uap0". Idempotent (no-op if the
    /// dnsmasq PID is already running).
    fn ensure_dhcp(&self) -> Result<()>;
    /// Disable radio power-save on the STA (`wlan0`) and AP (`uap0`) vifs.
    /// MUST be called every tick the AP overlay is desired (not just at
    /// creation): `NetworkManager` re-enables power-save on STA reconnect/roam,
    /// which sleeps the single shared radio and silently drops client
    /// associations on the AP vif. Best-effort; never fails.
    fn disable_power_save(&self);
    /// Bring the overlay AP down. Idempotent: success if already down.
    fn ensure_down(&self) -> Result<()>;
    /// Apply AP config edits in place using `hostapd_cli reconfigure`.
    fn reconfigure(&self, params: &ApParams) -> Result<()>;
    /// Observe current AP overlay state.
    fn observe(&self) -> ApOverlayObservation;
}

#[allow(dead_code)] // wired into the composite state machine in W3
pub(crate) struct Uap0Overlay {
    iface: String,
    uap0: String,
    ip_cidr: String,
    gateway_ip: String,
    conf_path: PathBuf,
    pid_dir: PathBuf,
    // Latch: set once a wlan0 power-save-off failure has been logged, so the
    // per-tick reassert transition-logs (onset + recovery) instead of spamming.
    power_save_warned: Cell<bool>,
}

#[allow(dead_code)] // wired into the composite state machine in W3
impl Uap0Overlay {
    pub(crate) fn new(iface: impl Into<String>) -> Self {
        Self {
            iface: iface.into(),
            uap0: DEFAULT_UAP0.to_owned(),
            ip_cidr: DEFAULT_IP_CIDR.to_owned(),
            gateway_ip: DEFAULT_GATEWAY_IP.to_owned(),
            conf_path: PathBuf::from(DEFAULT_HOSTAPD_CONF),
            pid_dir: PathBuf::from(DEFAULT_RUN_DIR),
            power_save_warned: Cell::new(false),
        }
    }

    fn hostapd_pid_path(&self) -> PathBuf {
        self.pid_dir.join("hostapd-uap0.pid")
    }

    fn dnsmasq_conf_path(&self) -> PathBuf {
        self.pid_dir.join("dnsmasq-uap0.conf")
    }

    fn dnsmasq_pid_path(&self) -> PathBuf {
        self.pid_dir.join("dnsmasq-uap0.pid")
    }
}

#[allow(dead_code)] // wired into the composite state machine in W3
impl ApOverlay for Uap0Overlay {
    fn ensure_up(&self, params: &ApParams) -> Result<()> {
        self.ensure_iface_up()?;
        self.ensure_ap_started(params)
    }

    fn ensure_iface_up(&self) -> Result<()> {
        std::fs::create_dir_all(&self.pid_dir)?;

        if !interface_exists(&self.uap0) {
            run_or_net(
                "iw",
                &iw_add_uap0_args(&self.iface, &self.uap0),
                "iw add uap0 failed",
            )?;
            // Set the distinct locally-administered MAC only at creation, while
            // uap0 is still down: a live interface rejects a MAC change with
            // EBUSY, so doing it every call would break ensure_up idempotency
            // and could bounce an AP that clients are already joined to.
            let base_mac = read_iface_mac_bytes(&self.iface)?;
            let uap_mac = format_mac(derive_uap0_mac(base_mac));
            run_or_net(
                "ip",
                &ip_set_mac_args(&self.uap0, &uap_mac),
                "ip link set uap0 mac failed",
            )?;
        }

        if !iface_has_addr(&self.uap0, &self.ip_cidr) {
            run_or_net(
                "ip",
                &ip_addr_add_args(&self.uap0, &self.ip_cidr),
                "ip addr add on uap0 failed",
            )?;
        }

        run_or_net(
            "ip",
            &ip_link_up_args(&self.uap0),
            "ip link set uap0 up failed",
        )?;

        // Release uap0 from NetworkManager before hostapd starts, mirroring the
        // proven v1 sequence. NM otherwise manages the freshly-created vif
        // (brings it up, scans, reconfigures, races hostapd for the interface),
        // which destabilizes wlan0's channel and can make NL80211_CMD_START_AP
        // fail with -52. Best-effort: NM may be absent or not yet aware of uap0,
        // in which case the persistent unmanaged-devices rule (setup.sh) covers it.
        let unmanaged = nmcli_set_unmanaged_args(&self.uap0);
        let unmanaged_refs: Vec<&str> = unmanaged.iter().map(String::as_str).collect();
        let _ = run_ok("nmcli", &unmanaged_refs);

        // Radio power-save is disabled by the orchestrator via `disable_power_save`
        // every desired tick (not here) so it stays off across NM STA reconnects
        // and roams, not only at uap0 creation.
        Ok(())
    }

    fn ensure_ap_started(&self, params: &ApParams) -> Result<()> {
        write_owner_only_atomic(&self.conf_path, render_hostapd_conf(params).as_bytes())?;

        let hostapd_pid = self.hostapd_pid_path();
        if pid_is_running(&hostapd_pid) {
            run_or_net(
                "hostapd_cli",
                &hostapd_reconfigure_args(&self.uap0),
                "hostapd_cli reconfigure failed",
            )?;
        } else {
            // Close the resolve->beacon TOCTOU: the planned channel was resolved
            // at tick start, but the STA can roam before START_AP fires. On a
            // single-radio brcmfmac the AP MUST beacon on the STA's exact live
            // channel or hostapd's START_AP is rejected -52; re-read wlan0 here
            // and abort (retry next tick) on a mismatch rather than beacon stale.
            let live_channel = capture("iw", &["dev", self.iface.as_str(), "info"])
                .and_then(|info| parse_iface_channel(&info));
            beacon_channel_ok(live_channel, params.channel)?;
            run_or_net(
                "hostapd",
                &[
                    "-B".to_owned(),
                    "-P".to_owned(),
                    hostapd_pid.to_string_lossy().into_owned(),
                    self.conf_path.to_string_lossy().into_owned(),
                ],
                "hostapd start failed",
            )?;
        }

        Ok(())
    }

    fn ensure_ip(&self) -> Result<()> {
        // hostapd's START_AP mode-transition flushed uap0's IPv4 (single-radio
        // brcmfmac FullMAC). The caller invokes this only after `observe()`
        // confirmed the vif is `type AP` -- START_AP is complete and the flush
        // has already happened -- so re-adding the address now sticks (nothing
        // left to race).
        if !iface_has_addr(&self.uap0, &self.ip_cidr) {
            run_or_net(
                "ip",
                &ip_addr_add_args(&self.uap0, &self.ip_cidr),
                "ip addr re-add on uap0 failed",
            )?;
        }
        Ok(())
    }

    fn ensure_dhcp(&self) -> Result<()> {
        // dnsmasq needs a usable IPv4 on uap0 or it exits "unknown interface
        // uap0". The caller gates this on the AP being stably `active` (IP present
        // and survived a tick) so dnsmasq never binds during the post-START_AP
        // flush window.
        let dnsmasq_conf = self.dnsmasq_conf_path();
        let dnsmasq_pid = self.dnsmasq_pid_path();
        write_owner_only_atomic(
            &dnsmasq_conf,
            render_dnsmasq_conf(&self.uap0, &self.gateway_ip).as_bytes(),
        )?;
        if !pid_is_running(&dnsmasq_pid) {
            run_or_net(
                "dnsmasq",
                &dnsmasq_args(&self.uap0, &dnsmasq_conf, &dnsmasq_pid),
                "dnsmasq start failed",
            )?;
        }
        Ok(())
    }

    fn disable_power_save(&self) {
        // On the single-radio BCM43430/43436 FullMAC chip, STA (wlan0) power-save
        // periodically sleeps the shared radio; while asleep the AP vif never
        // receives incoming client association frames, so clients see the beacon
        // but their join is silently dropped by the firmware (zero MLME events,
        // num_sta stays 0). Keeping power-save off on wlan0 (and uap0) lets uap0
        // hear and accept clients concurrently with STA -- the missing piece that
        // made concurrent AP+STA client association fail. Mirrors the proven v1
        // sequence. Best-effort: a failure must not tear down a working AP and
        // does not endanger the STA. These fire every ~2s tick, so the child's
        // own stderr is suppressed (`run_ok_quiet`) to avoid flooding the journal.

        // wlan0 (STA, load-bearing): reassert every tick. Transition-log so a
        // persistent failure is visible once (and its recovery), never tens of
        // thousands of identical lines a day.
        let wlan0 = self.iface.as_str();
        let args = iw_set_power_save_off_args(wlan0);
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        if run_ok_quiet("iw", &refs) {
            if self.power_save_warned.replace(false) {
                eprintln!("wifid: {wlan0} power_save disabled after prior failure");
            }
        } else if !self.power_save_warned.replace(true) {
            eprintln!("wifid: warning: failed to disable {wlan0} power_save");
        }

        // uap0 (AP vif): only exists once bring-up has created it. Skip silently
        // when absent -- expected on channel-withheld / cooldown / early ticks --
        // so we neither fork a doomed `iw` nor log a spurious error every tick.
        if interface_exists(&self.uap0) {
            let args = iw_set_power_save_off_args(&self.uap0);
            let refs: Vec<&str> = args.iter().map(String::as_str).collect();
            let _ = run_ok_quiet("iw", &refs);
        }
    }

    fn ensure_down(&self) -> Result<()> {
        stop_by_pidfile(&self.dnsmasq_pid_path())?;
        stop_by_pidfile(&self.hostapd_pid_path())?;

        if interface_exists(&self.uap0) {
            run_or_net("iw", &iw_del_uap0_args(&self.uap0), "iw del uap0 failed")?;
        }
        Ok(())
    }

    fn reconfigure(&self, params: &ApParams) -> Result<()> {
        std::fs::create_dir_all(&self.pid_dir)?;
        write_owner_only_atomic(&self.conf_path, render_hostapd_conf(params).as_bytes())?;
        run_or_net(
            "hostapd_cli",
            &hostapd_reconfigure_args(&self.uap0),
            "hostapd_cli reconfigure failed",
        )
    }

    fn observe(&self) -> ApOverlayObservation {
        let iface_exists = interface_exists(&self.uap0);
        let hostapd_alive = pid_is_running(&self.hostapd_pid_path());
        let dnsmasq_alive = pid_is_running(&self.dnsmasq_pid_path());
        let ip_present = iface_has_addr(&self.uap0, &self.ip_cidr);
        let info = capture("iw", &["dev", self.uap0.as_str(), "info"]);
        let channel = info.as_deref().and_then(parse_iface_channel);
        let iface_type = match info.as_deref() {
            Some(text) => parse_iface_type(text).unwrap_or(ApIfaceType::Unknown),
            None => ApIfaceType::Unknown,
        };
        let client_count = capture("iw", &["dev", self.uap0.as_str(), "station", "dump"])
            .map(|dump| parse_station_count(&dump));
        // A live hostapd is NOT sufficient: the firmware can leave uap0 in a
        // non-AP type after a failed START_AP while hostapd stays alive. Require
        // the vif to actually be `type AP` (beaconing) before reporting active.
        let active = iface_exists && hostapd_alive && ip_present && iface_type == ApIfaceType::Ap;
        ApOverlayObservation {
            iface_exists,
            hostapd_alive,
            dnsmasq_alive,
            ip_present,
            channel,
            iface_type,
            client_count,
            active,
        }
    }
}

fn run_or_net(program: &str, args: &[String], error: &'static str) -> Result<()> {
    let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
    if run_ok(program, &arg_refs) {
        Ok(())
    } else {
        Err(WifidError::Network(error.to_owned()))
    }
}

fn interface_exists(iface: &str) -> bool {
    Path::new("/sys/class/net").join(iface).exists()
}

fn iface_has_addr(iface: &str, ip_cidr: &str) -> bool {
    capture("ip", &["addr", "show", "dev", iface]).is_some_and(|out| {
        let needle = format!("inet {ip_cidr}");
        out.lines()
            .any(|line| line.trim_start().starts_with(&needle))
    })
}

fn stop_by_pidfile(pidfile: &Path) -> Result<()> {
    let Some(pid) = read_pid(pidfile) else {
        return Ok(());
    };
    if run_ok("kill", &["-TERM", &pid]) || !run_ok("kill", &["-0", &pid]) {
        let _ = std::fs::remove_file(pidfile);
        Ok(())
    } else {
        Err(WifidError::Network(format!(
            "failed to stop process from {}",
            pidfile.display()
        )))
    }
}

fn read_pid(pidfile: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(pidfile).ok()?;
    let pid = raw.trim();
    if pid.is_empty() {
        None
    } else {
        Some(pid.to_owned())
    }
}

fn pid_is_running(pidfile: &Path) -> bool {
    read_pid(pidfile).is_some_and(|pid| run_ok("kill", &["-0", &pid]))
}

fn read_iface_mac_bytes(iface: &str) -> Result<[u8; 6]> {
    let path = Path::new("/sys/class/net").join(iface).join("address");
    let value = std::fs::read_to_string(&path)
        .map_err(|e| WifidError::Network(format!("read {}: {e}", path.display())))?;
    parse_mac(value.trim())
        .ok_or_else(|| WifidError::Network(format!("invalid MAC in {}", path.display())))
}

fn parse_mac(s: &str) -> Option<[u8; 6]> {
    let mut out = [0_u8; 6];
    let mut bytes = s.split(':');
    for slot in &mut out {
        let part = bytes.next()?;
        if part.len() != 2 {
            return None;
        }
        let byte = u8::from_str_radix(part, 16).ok()?;
        *slot = byte;
    }
    if bytes.next().is_none() {
        Some(out)
    } else {
        None
    }
}

fn render_dnsmasq_conf(uap0: &str, gateway_ip: &str) -> String {
    [
        "bind-interfaces".to_owned(),
        format!("interface={uap0}"),
        "except-interface=lo".to_owned(),
        format!("dhcp-range={DEFAULT_DNSMASQ_RANGE}"),
        format!("dhcp-option=3,{gateway_ip}"),
    ]
    .join("\n")
        + "\n"
}

fn render_hostapd_conf(params: &ApParams) -> String {
    [
        "interface=uap0".to_owned(),
        // brcmfmac concurrent AP+STA needs the nl80211 driver stated explicitly
        // and WMM off, matching the proven v1/RaspAP config; without them the
        // FullMAC firmware can reject the concurrent BSS beacon.
        "driver=nl80211".to_owned(),
        // hostapd_cli talks to hostapd over this control socket; without it the
        // in-place `reconfigure` edit path (which avoids the fragile
        // stop-while-STA-active teardown burst) cannot attach.
        "ctrl_interface=/var/run/hostapd".to_owned(),
        format!("ssid={}", params.ssid),
        "hw_mode=g".to_owned(),
        format!("channel={}", params.channel),
        "wmm_enabled=0".to_owned(),
        "wpa=2".to_owned(),
        "wpa_key_mgmt=WPA-PSK".to_owned(),
        "rsn_pairwise=CCMP".to_owned(),
        "wpa_pairwise=CCMP".to_owned(),
        "auth_algs=1".to_owned(),
        format!("wpa_passphrase={}", params.passphrase.reveal()),
        "ignore_broadcast_ssid=0".to_owned(),
    ]
    .join("\n")
        + "\n"
}

fn derive_uap0_mac(base: [u8; 6]) -> [u8; 6] {
    let mut mac = base;
    mac[0] |= 0x02;
    mac[0] &= 0xfe;
    mac[5] ^= 0x01;
    if mac == base {
        mac[4] ^= 0x01;
    }
    mac
}

fn format_mac(mac: [u8; 6]) -> String {
    format!(
        "{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
    )
}

fn parse_station_count(dump: &str) -> u32 {
    let count = count_stations(dump);
    match u32::try_from(count) {
        Ok(v) => v,
        Err(_) => u32::MAX,
    }
}

pub(crate) fn parse_iface_channel(info: &str) -> Option<u32> {
    for line in info.lines() {
        let trimmed = line.trim_start();
        if let Some(rest) = trimmed.strip_prefix("channel ") {
            let value = rest.split_whitespace().next()?;
            if let Ok(channel) = value.parse::<u32>() {
                return Some(channel);
            }
        }
    }
    None
}

/// Parse the nl80211 interface type from `iw dev <if> info`. The line looks like
/// `\ttype AP` or `\ttype managed`. Returns `None` when no type line is present
/// (caller maps that to `Unknown`).
pub(crate) fn parse_iface_type(info: &str) -> Option<ApIfaceType> {
    for line in info.lines() {
        let trimmed = line.trim_start();
        if let Some(rest) = trimmed.strip_prefix("type ") {
            let kind = rest.split_whitespace().next()?;
            return Some(if kind == "AP" {
                ApIfaceType::Ap
            } else {
                ApIfaceType::Other
            });
        }
    }
    None
}

fn iw_add_uap0_args(iface: &str, uap0: &str) -> Vec<String> {
    vec![
        "dev".to_owned(),
        iface.to_owned(),
        "interface".to_owned(),
        "add".to_owned(),
        uap0.to_owned(),
        "type".to_owned(),
        "__ap".to_owned(),
    ]
}

fn iw_del_uap0_args(uap0: &str) -> Vec<String> {
    vec!["dev".to_owned(), uap0.to_owned(), "del".to_owned()]
}

fn ip_set_mac_args(uap0: &str, mac: &str) -> Vec<String> {
    vec![
        "link".to_owned(),
        "set".to_owned(),
        uap0.to_owned(),
        "address".to_owned(),
        mac.to_owned(),
    ]
}

fn ip_addr_add_args(uap0: &str, ip_cidr: &str) -> Vec<String> {
    vec![
        "addr".to_owned(),
        "add".to_owned(),
        ip_cidr.to_owned(),
        "dev".to_owned(),
        uap0.to_owned(),
    ]
}

fn ip_link_up_args(uap0: &str) -> Vec<String> {
    vec![
        "link".to_owned(),
        "set".to_owned(),
        uap0.to_owned(),
        "up".to_owned(),
    ]
}

fn nmcli_set_unmanaged_args(uap0: &str) -> Vec<String> {
    vec![
        "device".to_owned(),
        "set".to_owned(),
        uap0.to_owned(),
        "managed".to_owned(),
        "no".to_owned(),
    ]
}

fn iw_set_power_save_off_args(iface: &str) -> Vec<String> {
    vec![
        "dev".to_owned(),
        iface.to_owned(),
        "set".to_owned(),
        "power_save".to_owned(),
        "off".to_owned(),
    ]
}

fn beacon_channel_ok(live_channel: Option<u32>, planned: u32) -> Result<()> {
    // Single-radio brcmfmac forces the AP onto the STA's channel: if the STA is
    // associated on a channel that differs from the one we are about to beacon
    // on, NL80211_CMD_START_AP fails -52. When wlan0 has no readable live
    // channel (STA down / not associated) the radio is free, so any planned
    // channel is allowed.
    match live_channel {
        Some(live) if live != planned => Err(WifidError::Network(format!(
            "uap0 beacon aborted: wlan0 live channel {live} != planned AP channel {planned}"
        ))),
        _ => Ok(()),
    }
}

fn dnsmasq_args(uap0: &str, conf_path: &Path, pid_path: &Path) -> Vec<String> {
    // dnsmasq's option parser requires `--opt=value` for long options; the
    // space-separated form fails on-device with "junk found in command line"
    // and serves no DHCP. `--bind-interfaces` is a valueless flag.
    vec![
        format!("--conf-file={}", conf_path.to_string_lossy()),
        format!("--interface={uap0}"),
        "--bind-interfaces".to_owned(),
        "--except-interface=lo".to_owned(),
        format!("--pid-file={}", pid_path.to_string_lossy()),
    ]
}

fn hostapd_reconfigure_args(uap0: &str) -> Vec<String> {
    vec![
        "-p".to_owned(),
        "/var/run/hostapd".to_owned(),
        "-i".to_owned(),
        uap0.to_owned(),
        "reconfigure".to_owned(),
    ]
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        ApIfaceType, ApParams, DEFAULT_DNSMASQ_RANGE, beacon_channel_ok, derive_uap0_mac,
        dnsmasq_args, format_mac, hostapd_reconfigure_args, ip_addr_add_args, ip_link_up_args,
        ip_set_mac_args, iw_add_uap0_args, iw_del_uap0_args, iw_set_power_save_off_args,
        nmcli_set_unmanaged_args, parse_iface_channel, parse_iface_type, parse_station_count,
        render_hostapd_conf,
    };
    use crate::creds::Secret;
    use std::path::Path;

    #[test]
    fn ap_params_debug_redacts_secret() {
        let params = ApParams {
            ssid: "TeslaUSB".to_owned(),
            passphrase: Secret::new("super-secret-pass"),
            channel: 6,
        };
        let shown = format!("{params:?}");
        assert!(shown.contains("Secret(<redacted>)"));
        assert!(!shown.contains("super-secret-pass"));
    }

    #[test]
    fn render_hostapd_conf_is_deterministic_and_wpa2_only() {
        let conf = render_hostapd_conf(&ApParams {
            ssid: "TeslaUSB AP".to_owned(),
            passphrase: Secret::new("onboarding-pass"),
            channel: 11,
        });
        assert_eq!(
            conf,
            "interface=uap0\ndriver=nl80211\nctrl_interface=/var/run/hostapd\nssid=TeslaUSB AP\nhw_mode=g\nchannel=11\nwmm_enabled=0\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nwpa_pairwise=CCMP\nauth_algs=1\nwpa_passphrase=onboarding-pass\nignore_broadcast_ssid=0\n"
        );
    }

    #[test]
    fn derive_uap0_mac_sets_laa_unicast_and_changes_value() {
        let base = [0x88, 0x77, 0x66, 0x55, 0x44, 0x33];
        let derived = derive_uap0_mac(base);
        assert_ne!(derived, base);
        assert_eq!(derived[0] & 0x02, 0x02);
        assert_eq!(derived[0] & 0x01, 0x00);
    }

    #[test]
    fn format_mac_outputs_lowercase_colon_hex() {
        assert_eq!(
            format_mac([0x8a, 0xab, 0xcd, 0x01, 0x23, 0xef]),
            "8a:ab:cd:01:23:ef"
        );
    }

    #[test]
    fn parse_station_count_counts_station_lines() {
        let dump = "\
Station aa:bb:cc:dd:ee:ff (on uap0)
	inactive time: 30 ms
Station 11:22:33:44:55:66 (on uap0)
	tx bytes: 100
";
        assert_eq!(parse_station_count(dump), 2);
    }

    #[test]
    fn parse_iface_channel_reads_iw_dev_info_channel_line() {
        let info = "\
Interface uap0
\tifindex 7
\twdev 0x2
\taddr 8a:77:66:55:44:32
\ttype AP
\tchannel 11 (2462 MHz), width: 20 MHz, center1: 2462 MHz
";
        assert_eq!(parse_iface_channel(info), Some(11));
        assert_eq!(parse_iface_channel("Interface uap0\n\ttype AP\n"), None);
    }

    #[test]
    fn parse_iface_type_reads_ap_and_managed() {
        let ap = "Interface uap0\n\ttype AP\n\tchannel 11 (2462 MHz)\n";
        assert_eq!(parse_iface_type(ap), Some(ApIfaceType::Ap));
        let managed = "Interface uap0\n\ttype managed\n";
        assert_eq!(parse_iface_type(managed), Some(ApIfaceType::Other));
        assert_eq!(parse_iface_type("Interface uap0\n\tchannel 11\n"), None);
    }

    #[test]
    fn iw_add_uap0_args_match_spike_sequence() {
        assert_eq!(
            iw_add_uap0_args("wlan0", "uap0"),
            vec!["dev", "wlan0", "interface", "add", "uap0", "type", "__ap"]
        );
    }

    #[test]
    fn iw_set_power_save_off_args_target_iface() {
        assert_eq!(
            iw_set_power_save_off_args("wlan0"),
            vec!["dev", "wlan0", "set", "power_save", "off"]
        );
        assert_eq!(
            iw_set_power_save_off_args("uap0"),
            vec!["dev", "uap0", "set", "power_save", "off"]
        );
    }

    #[test]
    fn iw_del_uap0_args_match_spike_sequence() {
        assert_eq!(iw_del_uap0_args("uap0"), vec!["dev", "uap0", "del"]);
    }

    #[test]
    fn ip_set_mac_args_match_spike_sequence() {
        assert_eq!(
            ip_set_mac_args("uap0", "8a:00:00:00:00:01"),
            vec!["link", "set", "uap0", "address", "8a:00:00:00:00:01"]
        );
    }

    #[test]
    fn ip_addr_add_args_match_spike_sequence() {
        assert_eq!(
            ip_addr_add_args("uap0", "192.168.4.1/24"),
            vec!["addr", "add", "192.168.4.1/24", "dev", "uap0"]
        );
    }

    #[test]
    fn ip_link_up_args_match_spike_sequence() {
        assert_eq!(ip_link_up_args("uap0"), vec!["link", "set", "uap0", "up"]);
    }

    #[test]
    fn dnsmasq_args_are_dedicated_to_uap0_with_private_pid() {
        let conf = Path::new("/run/teslausb/dnsmasq-uap0.conf");
        let pid = Path::new("/run/teslausb/dnsmasq-uap0.pid");
        assert_eq!(
            dnsmasq_args("uap0", conf, pid),
            vec![
                "--conf-file=/run/teslausb/dnsmasq-uap0.conf",
                "--interface=uap0",
                "--bind-interfaces",
                "--except-interface=lo",
                "--pid-file=/run/teslausb/dnsmasq-uap0.pid"
            ]
        );
    }

    #[test]
    fn nmcli_set_unmanaged_args_release_uap0_from_networkmanager() {
        assert_eq!(
            nmcli_set_unmanaged_args("uap0"),
            vec!["device", "set", "uap0", "managed", "no"]
        );
    }

    #[test]
    fn beacon_channel_ok_permits_matching_live_channel() {
        assert!(beacon_channel_ok(Some(11), 11).is_ok());
    }

    #[test]
    fn beacon_channel_ok_aborts_on_stale_mismatch() {
        // STA roamed to ch1 after the plan resolved ch11: beaconing would -52.
        assert!(beacon_channel_ok(Some(1), 11).is_err());
    }

    #[test]
    fn beacon_channel_ok_permits_beacon_when_sta_channel_unreadable() {
        // wlan0 has no live channel (STA down / not associated): radio is free.
        assert!(beacon_channel_ok(None, 6).is_ok());
    }

    #[test]
    fn hostapd_reconfigure_args_match_spike_sequence() {
        assert_eq!(
            hostapd_reconfigure_args("uap0"),
            vec!["-p", "/var/run/hostapd", "-i", "uap0", "reconfigure"]
        );
    }

    #[test]
    fn dnsmasq_range_constant_matches_expected_pool() {
        assert_eq!(
            DEFAULT_DNSMASQ_RANGE,
            "192.168.4.10,192.168.4.50,255.255.255.0"
        );
    }
}
