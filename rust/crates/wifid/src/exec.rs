//! Live I/O executors for the seam traits.
//!
//! Two classes live here, kept apart on purpose:
//!
//! * **Host-complete, platform-portable** helpers that are safe to ship and
//!   exercise off-device: [`MonotonicClock`] (a boot-scoped clock over
//!   `std::time::Instant`) and [`FileCredentialStore`] (atomic `0600`
//!   persistence).
//! * **`// HARDWARE-GATED` stubs** for everything that needs the real Pi:
//!   netlink/`wpa_supplicant`/`hostapd`/`dnsmasq`/`tc`/`rmmod` ([`HardwareNetworkController`]),
//!   the gadgetd write-heartbeat client ([`GadgetHeartbeatSource`]), and the
//!   reboot path ([`HardwareRebootController`]). These return
//!   [`WifidError::HardwareGated`] (or the fail-safe `None`) until the Phase-2
//!   spikes (esp. Task 2.6, the TX-cap calibration) validate them on hardware.
//!   They never silently pretend to succeed.

use std::time::Instant;

use crate::creds::{CredentialStore, Credentials, Secret};
use crate::error::{Result, WifidError};
use crate::link::LinkObservation;
use crate::traits::{Clock, HeartbeatSource, NetworkController, RebootController};
use crate::watchdog::{ChipObservation, WriteHeartbeat};

/// Boot-scoped monotonic clock. `Instant` is monotonic on every supported
/// platform and resets per process start; combined with a matching `boot_id`
/// it gives the no-RTC timing the reboot gate needs.
pub(crate) struct MonotonicClock {
    start: Instant,
}

impl Default for MonotonicClock {
    fn default() -> Self {
        Self {
            start: Instant::now(),
        }
    }
}

impl Clock for MonotonicClock {
    fn now_mono_ms(&self) -> i64 {
        i64::try_from(self.start.elapsed().as_millis()).unwrap_or(i64::MAX)
    }
}

/// Read the kernel boot identity as a `u64`, used to reject a gadgetd
/// heartbeat carried over from a previous boot. On Linux this hashes
/// `/proc/sys/kernel/random/boot_id`; elsewhere (dev hosts) it returns 0, which
/// is harmless because the reboot path is never reached off-device.
pub(crate) fn current_boot_id() -> u64 {
    match std::fs::read_to_string("/proc/sys/kernel/random/boot_id") {
        Ok(s) => fnv1a(s.trim().as_bytes()),
        Err(_) => 0,
    }
}

/// Small FNV-1a hash (no external crate; this is not security-sensitive — it
/// only needs to differ across boots).
fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        hash ^= u64::from(b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// HARDWARE-GATED: drives the `WiFi` radio + `tc` + chip reset on the Pi. Every
/// method is a stub returning [`WifidError::HardwareGated`] until validated on
/// device (netlink/`wpa_supplicant`/`hostapd`/`dnsmasq`/`tc`/`rmmod`).
pub(crate) struct HardwareNetworkController;

impl NetworkController for HardwareNetworkController {
    fn observe_link(&self) -> Result<LinkObservation> {
        // HARDWARE-GATED: query nl80211 association/signal + carrier/IP + a
        // gateway reachability probe, plus actual sta/ap-running state.
        Err(WifidError::Network(
            "observe_link (nl80211) not available off-device".to_owned(),
        ))
    }

    fn observe_chip(&self) -> Result<ChipObservation> {
        // HARDWARE-GATED: read brcmfmac/SDIO health (driver responsive?).
        Err(WifidError::Network(
            "observe_chip (brcmfmac) not available off-device".to_owned(),
        ))
    }

    fn start_sta(&self) -> Result<()> {
        // HARDWARE-GATED: wpa_supplicant up in client mode.
        Err(WifidError::HardwareGated("start_sta not on device"))
    }

    fn stop_sta(&self) -> Result<()> {
        // HARDWARE-GATED: wpa_supplicant down (verify stopped).
        Err(WifidError::HardwareGated("stop_sta not on device"))
    }

    fn start_ap(&self) -> Result<()> {
        // HARDWARE-GATED: hostapd (WPA2) + dnsmasq up.
        Err(WifidError::HardwareGated("start_ap not on device"))
    }

    fn stop_ap(&self) -> Result<()> {
        // HARDWARE-GATED: hostapd + dnsmasq down (verify stopped).
        Err(WifidError::HardwareGated("stop_ap not on device"))
    }

    fn apply_tx_cap(&self, _bytes_per_s: u64) -> Result<()> {
        // HARDWARE-GATED (Task 2.6): tc egress cap. The *value* is calibration-
        // gated; the mechanism is device-only.
        Err(WifidError::HardwareGated("apply_tx_cap (tc) not on device"))
    }

    fn reset_chip(&self) -> Result<()> {
        // HARDWARE-GATED: rmmod + modprobe brcmfmac (chip-only reset).
        Err(WifidError::HardwareGated(
            "reset_chip (brcmfmac) not on device",
        ))
    }
}

/// HARDWARE-GATED: reads gadgetd's write-heartbeat over `gadgetd.sock`
/// (`gadget_status()` → `write_heartbeat_mono_ms`, `usb_state`). The stub
/// returns `None`, which the watchdog treats as "car may be writing ⇒ never
/// reboot" — the safe default.
///
/// CONVERGENCE NOTE: the D4 contract (OQ-5) flags that gadgetd must actually
/// expose a `write_heartbeat` + `boot_id` in its status RPC; that field does
/// not exist in `gadgetd` yet (Task 3.2). Until it does, this stays a stub.
pub(crate) struct GadgetHeartbeatSource;

impl HeartbeatSource for GadgetHeartbeatSource {
    fn read(&self) -> Option<WriteHeartbeat> {
        // HARDWARE-GATED: connect gadgetd.sock, request gadget_status, parse the
        // heartbeat. Fail-safe stub: no reading ⇒ no reboot.
        None
    }
}

/// HARDWARE-GATED: the single sanctioned non-gadgetd reboot. Left unimplemented
/// (returns an error) so unvalidated code can never actually reboot the Pi.
pub(crate) struct HardwareRebootController;

impl RebootController for HardwareRebootController {
    fn reboot(&self) -> Result<()> {
        // HARDWARE-GATED: `systemctl reboot` (or reboot(2)) — only reachable
        // after the watchdog proved USB idle. Intentionally not wired up until
        // on-device validation (SPEC.md §2 invariant 4).
        Err(WifidError::HardwareGated(
            "reboot path not validated on device",
        ))
    }
}

/// Atomic, root-only (`0600`) credential persistence.
///
/// File format is a tiny `key=value` document (one secret per line). Secrets
/// are written but **never logged**; the file is created with `0600` and
/// replaced atomically via a temp file + rename.
pub(crate) struct FileCredentialStore {
    path: std::path::PathBuf,
}

impl FileCredentialStore {
    /// Build a store backed by `path`.
    pub(crate) fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self { path: path.into() }
    }

    fn serialize(creds: &Credentials) -> String {
        let mut out = String::new();
        if let Some(psk) = &creds.sta_psk {
            out.push_str("sta_psk=");
            out.push_str(psk.reveal());
            out.push('\n');
        }
        out.push_str("ap_passphrase=");
        out.push_str(creds.ap_passphrase.reveal());
        out.push('\n');
        out
    }

    fn parse(contents: &str) -> Result<Credentials> {
        let mut sta_psk = None;
        let mut ap_passphrase = None;
        for line in contents.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                return Err(WifidError::Credentials(
                    "malformed credential line".to_owned(),
                ));
            };
            match key {
                "sta_psk" => sta_psk = Some(Secret::new(value)),
                "ap_passphrase" => ap_passphrase = Some(Secret::new(value)),
                _ => {} // forward-compatible: ignore unknown keys
            }
        }
        let ap_passphrase = ap_passphrase
            .ok_or_else(|| WifidError::Credentials("missing ap_passphrase".to_owned()))?;
        Ok(Credentials {
            sta_psk,
            ap_passphrase,
        })
    }
}

impl CredentialStore for FileCredentialStore {
    fn load(&self) -> Result<Credentials> {
        let contents = std::fs::read_to_string(&self.path)
            .map_err(|e| WifidError::Credentials(format!("read {}: {e}", self.path.display())))?;
        Self::parse(&contents)
    }

    fn store(&self, creds: &Credentials) -> Result<()> {
        let tmp = self.path.with_extension("tmp");
        let body = Self::serialize(creds);
        // Clear any stale temp from a previous crash so the fresh create below
        // is the one that fixes the mode.
        let _ = std::fs::remove_file(&tmp);
        write_owner_only(&tmp, body.as_bytes())?;
        std::fs::rename(&tmp, &self.path)?;
        Ok(())
    }
}

/// Write `bytes` to `path`, creating it owner read/write only (`0600`) from the
/// outset — the secret must never exist even briefly under the looser umask
/// default that a write-then-chmod would leave it in.
#[cfg(unix)]
fn write_owner_only(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}

/// Non-unix hosts (dev boxes) cannot set POSIX modes; the production target is
/// always Linux, so this is a test-only fallback.
#[cfg(not(unix))]
fn write_owner_only(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    std::fs::write(path, bytes)?;
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{FileCredentialStore, MonotonicClock};
    use crate::creds::{CredentialStore, Credentials, Secret};
    use crate::traits::Clock;

    #[test]
    fn monotonic_clock_is_non_decreasing() {
        let c = MonotonicClock::default();
        let a = c.now_mono_ms();
        let b = c.now_mono_ms();
        assert!(b >= a);
    }

    #[test]
    fn credential_file_roundtrips() {
        let dir = std::env::temp_dir().join(format!("wifid-creds-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("creds");
        let store = FileCredentialStore::new(&path);

        let creds = Credentials {
            sta_psk: Some(Secret::new("home-psk-value")),
            ap_passphrase: Secret::new("ap-pass-value"),
        };
        store.store(&creds).unwrap();
        let loaded = store.load().unwrap();
        assert_eq!(loaded, creds);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn credential_file_persisted_bytes_are_owner_only() {
        // On unix, assert the 0600 bit; elsewhere just exercise the write path.
        let dir = std::env::temp_dir().join(format!("wifid-perm-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("creds");
        let store = FileCredentialStore::new(&path);
        store
            .store(&Credentials {
                sta_psk: None,
                ap_passphrase: Secret::new("ap-pass-value"),
            })
            .unwrap();

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600, "credential file is not 0600");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
