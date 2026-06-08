//! The injected I/O **seams**. Every real side effect (monotonic time,
//! netlink / `wpa_supplicant` / `hostapd` / `dnsmasq` / `tc` / `rmmod`, the
//! gadgetd write-heartbeat, and the actual reboot) goes through one of these
//! traits, so the entire decision core in [`crate::link`], [`crate::throttle`]
//! and [`crate::watchdog`] is driven by fakes in unit tests on any host.
//!
//! The live implementations live in [`crate::exec`] and are
//! `#[cfg(target_os = "linux")]` HARDWARE-GATED stubs until the Phase-2 spikes
//! validate them on the Pi.

use crate::error::Result;
use crate::link::LinkObservation;
use crate::watchdog::{ChipObservation, WriteHeartbeat};

/// Monotonic, boot-scoped clock. Values only have meaning as *deltas* within a
/// single boot — there is no RTC on the Pi Zero 2 W, so wall-clock time is
/// never used for any safety decision (consistent with the gadgetd handoff
/// guard and the single-writer-lease contract).
pub(crate) trait Clock {
    /// Milliseconds since an arbitrary boot-scoped epoch. Monotonic
    /// non-decreasing.
    fn now_mono_ms(&self) -> i64;
}

/// Drives the `WiFi` radio, the kernel `tc` egress cap, and the SDIO chip reset.
///
/// The pure core never assumes an action took effect: after a transition it
/// re-reads [`Self::observe_link`] and reconciles against the *actual* radio
/// state, so OS drift or a partial failure can be detected and corrected
/// (mutual exclusion is enforced against reality, not against intent).
pub(crate) trait NetworkController {
    /// Read the current link facts **and** which radios are actually running.
    ///
    /// # Errors
    /// Returns [`crate::error::WifidError::Network`] if the radio state cannot
    /// be read.
    fn observe_link(&self) -> Result<LinkObservation>;

    /// Read the chip's liveness signal for the watchdog.
    ///
    /// # Errors
    /// Returns an error if the driver/chip state cannot be read.
    fn observe_chip(&self) -> Result<ChipObservation>;

    /// Bring up station (client) mode and begin associating to home `WiFi`.
    ///
    /// # Errors
    /// Returns an error if STA could not be started.
    fn start_sta(&self) -> Result<()>;

    /// Tear down station mode. Idempotent (stopping an already-stopped STA is
    /// success).
    ///
    /// # Errors
    /// Returns an error if STA could not be confirmed stopped.
    fn stop_sta(&self) -> Result<()>;

    /// Bring up the WPA2 access point (hostapd + dnsmasq) for onboarding.
    ///
    /// # Errors
    /// Returns an error if the AP could not be started.
    fn start_ap(&self) -> Result<()>;

    /// Tear down the access point. Idempotent.
    ///
    /// # Errors
    /// Returns an error if the AP could not be confirmed stopped.
    fn stop_ap(&self) -> Result<()>;

    /// Apply (or update) the kernel `tc` egress cap to `bytes_per_s`. This is
    /// the "braces" half of the D4 belt-and-braces cap; `uploadd` self-paces as
    /// the "belt".
    ///
    /// # Errors
    /// Returns an error if the cap could not be applied.
    fn apply_tx_cap(&self, bytes_per_s: u64) -> Result<()>;

    /// Reset the `WiFi` chip only (`rmmod` + `modprobe brcmfmac`) — **never** a
    /// Pi reboot. This is the first-line SDIO-deadlock recovery.
    ///
    /// # Errors
    /// Returns an error if the module could not be reloaded.
    fn reset_chip(&self) -> Result<()>;
}

/// Reads gadgetd's write-heartbeat (read-only dependency; `wifid` never touches
/// the gadget). Used solely to gate the last-resort reboot on USB-idle.
pub(crate) trait HeartbeatSource {
    /// Read the latest heartbeat, or `None` if gadgetd is unreachable / the
    /// reading is unavailable. A `None` (or any malformed/stale value) is
    /// treated by the watchdog as "car may be writing" ⇒ **do not reboot**.
    fn read(&self) -> Option<WriteHeartbeat>;
}

/// Performs the single sanctioned non-gadgetd reboot (`SPEC.md` §2 invariant
/// 4). Only ever invoked by the orchestrator after the watchdog returns
/// [`crate::watchdog::RecoveryAction::RebootPi`].
pub(crate) trait RebootController {
    /// Reboot the Pi. The caller has already proven USB is idle.
    ///
    /// # Errors
    /// Returns an error if the reboot could not be initiated.
    fn reboot(&self) -> Result<()>;
}
