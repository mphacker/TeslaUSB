//! Live I/O executors for the seam traits.
//!
//! Two classes live here, kept apart on purpose:
//!
//! * **Host-complete, platform-portable** helpers that are safe to ship and
//!   exercise off-device: [`MonotonicClock`] (a boot-scoped clock over
//!   `std::time::Instant`) and [`FileCredentialStore`] (atomic `0600`
//!   persistence).
//! * **Device-only executors** for the parts that need the real Pi: the
//!   gadgetd write-heartbeat client ([`GadgetHeartbeatSource`], still a
//!   fail-safe `None` stub until gadgetd exposes the heartbeat — Task 3.2) and
//!   the last-resort reboot ([`HardwareRebootController`]). The radio/`tc`/chip
//!   controller lives in [`crate::nmcli`].

use std::time::Instant;

use crate::creds::{CredentialStore, Credentials, Secret};
use crate::error::{Result, WifidError};
use crate::traits::{Clock, HeartbeatSource, RebootController};
use crate::watchdog::WriteHeartbeat;

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

/// Reads gadgetd's write-heartbeat over `gadgetd.sock` (`gadget_status()` →
/// `write_heartbeat_mono_ms`, `usb_state`). The current implementation is a
/// fail-safe `None` stub, which the watchdog treats as "car may be writing ⇒
/// never reboot" — the safe default that keeps the reboot path closed.
///
/// CONVERGENCE NOTE: the D4 contract (OQ-5) flags that gadgetd must actually
/// expose a `write_heartbeat` + `boot_id` in its status RPC; that field does
/// not exist in `gadgetd` yet (Task 3.2, a different lane). Until it does, this
/// stays a stub — and because it returns `None`, the (now real) reboot
/// controller below can never actually fire, so wiring it up carries no
/// behavioural risk.
pub(crate) struct GadgetHeartbeatSource;

impl HeartbeatSource for GadgetHeartbeatSource {
    fn read(&self) -> Option<WriteHeartbeat> {
        // Fail-safe stub: no reading ⇒ no reboot. Replaced once gadgetd exposes
        // the heartbeat over its status RPC (Task 3.2).
        None
    }
}

/// The single sanctioned non-gadgetd reboot (`SPEC.md` §2 invariant 4): the
/// last-resort SDIO-deadlock recovery. Only ever invoked by the orchestrator
/// after the watchdog has (a) exhausted chip resets and (b) proven USB idle via
/// gadgetd's heartbeat gate. With the heartbeat source still a `None` stub that
/// gate never opens, so this is currently unreachable in practice — it is wired
/// for real so the moment gadgetd's heartbeat lands the recovery path is whole.
pub(crate) struct HardwareRebootController;

impl RebootController for HardwareRebootController {
    fn reboot(&self) -> Result<()> {
        // The watchdog has already proven USB idle (heartbeat gate) before we
        // get here. Prefer systemd's orderly reboot.
        let ok = std::process::Command::new("systemctl")
            .arg("reboot")
            .status()
            .is_ok_and(|s| s.success());
        if ok {
            Ok(())
        } else {
            Err(WifidError::Network(
                "systemctl reboot failed to initiate".to_owned(),
            ))
        }
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
        if let Some(ap) = &creds.ap_passphrase {
            out.push_str("ap_passphrase=");
            out.push_str(ap.reveal());
            out.push('\n');
        }
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
        Ok(Credentials {
            sta_psk,
            ap_passphrase,
        })
    }
}

impl CredentialStore for FileCredentialStore {
    fn load(&self) -> Result<Option<Credentials>> {
        match std::fs::read_to_string(&self.path) {
            Ok(contents) => Self::parse(&contents).map(Some),
            // The store simply does not exist yet (fresh / unprovisioned
            // appliance, or its parent dir is absent — both surface as
            // NotFound). This is a benign empty config, NOT a fatal error: the
            // daemon must continue into the normal STA/AP state machine instead
            // of crash-looping. Only NotFound is treated as "absent"; ENOTDIR,
            // permission-denied, etc. are real faults and are surfaced.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(WifidError::Credentials(format!(
                "read {}: {e}",
                self.path.display()
            ))),
        }
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
            ap_passphrase: Some(Secret::new("ap-pass-value")),
        };
        store.store(&creds).unwrap();
        let loaded = store.load().unwrap().expect("credentials present");
        assert_eq!(loaded, creds);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_store_loads_as_empty_config_not_error() {
        // The live crash-loop fix: a credential file that does not exist must
        // load as `Ok(None)` (benign empty config), never as an error that
        // would make the daemon exit and systemd restart it forever.
        let dir = std::env::temp_dir().join(format!("wifid-absent-{}", std::process::id()));
        // Point at a path under a parent dir that does not exist either, to
        // cover the fresh-appliance case (both file and parent absent).
        let path = dir.join("does-not-exist").join("creds");
        let store = FileCredentialStore::new(&path);
        match store.load() {
            Ok(None) => {}
            other => panic!("missing store must be Ok(None), got {other:?}"),
        }
    }

    #[test]
    fn malformed_store_is_surfaced_as_error_not_silently_empty() {
        // A file that *exists* but is corrupt is a real fault and must be
        // surfaced (distinct from the benign missing-file case above).
        let dir = std::env::temp_dir().join(format!("wifid-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("creds");
        std::fs::write(
            &path,
            b"this is not a valid key=value? line\nno-equals-here\n",
        )
        .unwrap();
        let store = FileCredentialStore::new(&path);
        assert!(store.load().is_err(), "malformed store must error");
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
                ap_passphrase: Some(Secret::new("ap-pass-value")),
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
