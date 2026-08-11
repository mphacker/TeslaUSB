//! Credential storage abstraction (`wifid.md` §2.2, `SPEC.md` §7 security).
//!
//! `wifid` **owns** the STA PSK and the AP WPA2 passphrase. They are persisted
//! root-only (`0600`), **never logged, never serialised into status, never
//! handed to the SPA**. `webd` requests changes over IPC ([`CredentialUpdate`])
//! but never reads the secrets back.
//!
//! Enforcement in this module:
//! * [`Secret`] redacts itself in `Debug`/`Display` and implements **no**
//!   `Serialize`, so a secret cannot leak through a log line or a status frame.
//! * Validation messages are static and describe *what* was wrong, never the
//!   rejected value.
//! * The live [`CredentialStore`] writes atomically with `0600` and never
//!   passes a secret on a command line (the executor renders config files).

use crate::error::{Result, WifidError};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ApMode {
    Auto,
    ForceOn,
    ForceOff,
}

impl Default for ApMode {
    fn default() -> Self {
        Self::Auto
    }
}

impl ApMode {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::ForceOn => "force_on",
            Self::ForceOff => "force_off",
        }
    }

    pub(crate) fn from_persisted(s: &str) -> ApMode {
        match s {
            "force_on" => Self::ForceOn,
            "force_off" => Self::ForceOff,
            _ => Self::Auto,
        }
    }
}

/// A secret string (PSK / passphrase) that refuses to reveal itself except
/// through the explicit [`Secret::reveal`] accessor.
///
/// Deliberately implements neither `serde::Serialize` nor `std::fmt::Display`
/// of its contents, and a custom redacting `Debug`.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Secret(String);

impl Secret {
    /// Wrap a secret value. The value is not validated here — use
    /// [`validate_wpa2_passphrase`] at the trust boundary.
    pub(crate) fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// Reveal the underlying secret. The **only** way to read it. Call sites are
    /// limited to the credential store / executor rendering a config file — and
    /// must never pass the value via process arguments or a log.
    pub(crate) fn reveal(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Redacted: never print the value, but reveal whether one is set.
        if self.0.is_empty() {
            f.write_str("Secret(<empty>)")
        } else {
            f.write_str("Secret(<redacted>)")
        }
    }
}

/// The credentials `wifid` owns.
///
/// Both fields are optional so that an **unprovisioned** appliance — one whose
/// credential store does not exist yet — is representable as
/// [`Credentials::empty`] rather than forcing a fatal error at startup. A
/// missing store must never crash the daemon (it previously crash-looped on
/// `ENOENT`); it simply means "nothing configured yet", and the state machine
/// continues into AP-onboarding / idle until `webd` provisions credentials.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Credentials {
    /// Home-WiFi PSK. `None` ⇒ no STA configured ⇒ AP-onboarding only.
    pub(crate) sta_psk: Option<Secret>,
    /// The WPA2 passphrase the onboarding AP advertises (never open). `None`
    /// ⇒ not provisioned yet: the AP **cannot** be brought up (an open AP is
    /// forbidden), so the executor refuses to start it until a passphrase is
    /// set. Never `None` once `set-credentials` has run.
    pub(crate) ap_passphrase: Option<Secret>,
    /// AP SSID (public, non-secret). `None` means AP is not provisioned yet.
    pub(crate) ap_ssid: Option<String>,
    /// AP policy mode.
    pub(crate) ap_mode: ApMode,
}

impl Credentials {
    /// The credentials of a freshly-imaged, unprovisioned appliance: nothing
    /// configured. Used when the credential store file does not exist yet, so a
    /// missing store is a benign empty config rather than a fatal error.
    pub(crate) fn empty() -> Self {
        Self {
            sta_psk: None,
            ap_passphrase: None,
            ap_ssid: None,
            ap_mode: ApMode::Auto,
        }
    }

    /// Whether a home-WiFi PSK is configured (drives `sta_configured` in the
    /// link state machine). Reads no secret value.
    pub(crate) fn sta_configured(&self) -> bool {
        self.sta_psk.is_some()
    }

    /// AP is provisioned only when both SSID and WPA2 passphrase are present.
    pub(crate) fn ap_provisioned(&self) -> bool {
        self.ap_ssid.is_some() && self.ap_passphrase.is_some()
    }
}

/// A change requested by `webd` over IPC. `webd` sets values; it never reads
/// them back. A `None` field leaves that credential unchanged; `clear_sta`
/// removes the STA PSK (revert to onboarding-only).
#[derive(Clone)]
pub(crate) struct CredentialUpdate {
    /// New STA PSK, if changing.
    pub(crate) sta_psk: Option<String>,
    /// New AP passphrase, if changing.
    pub(crate) ap_passphrase: Option<String>,
    /// New AP SSID, if changing.
    pub(crate) ap_ssid: Option<String>,
    /// New AP mode, if changing.
    pub(crate) ap_mode: Option<ApMode>,
    /// Remove the STA PSK entirely.
    pub(crate) clear_sta: bool,
}

impl std::fmt::Debug for CredentialUpdate {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CredentialUpdate")
            .field("sta_psk", &self.sta_psk.as_ref().map(|_| "<redacted>"))
            .field(
                "ap_passphrase",
                &self.ap_passphrase.as_ref().map(|_| "<redacted>"),
            )
            .field("ap_ssid", &self.ap_ssid)
            .field("ap_mode", &self.ap_mode)
            .field("clear_sta", &self.clear_sta)
            .finish()
    }
}

/// WPA2-PSK passphrase rule: 8..=63 printable ASCII characters (IEEE 802.11i).
///
/// # Errors
/// Returns a static reason (never the value) if the passphrase is out of range
/// or contains a non-printable-ASCII byte.
pub(crate) fn validate_wpa2_passphrase(value: &str) -> std::result::Result<(), &'static str> {
    let len = value.len();
    if !(8..=63).contains(&len) {
        return Err("WPA2 passphrase must be 8..=63 characters");
    }
    if !value.bytes().all(|b| (0x20..=0x7e).contains(&b)) {
        return Err("WPA2 passphrase must be printable ASCII");
    }
    Ok(())
}

/// AP SSID rule: 1..=32 bytes, no ASCII control bytes, no leading/trailing
/// ASCII whitespace.
///
/// # Errors
/// Returns a static reason (never the value) if the SSID is invalid.
pub(crate) fn validate_ssid(value: &str) -> std::result::Result<(), &'static str> {
    let len = value.len();
    if !(1..=32).contains(&len) {
        return Err("SSID must be 1..=32 bytes");
    }
    if value.bytes().any(|b| b < 0x20 || b == 0x7f) {
        return Err("SSID must not contain ASCII control characters");
    }
    if value
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_whitespace)
        || value.as_bytes().last().is_some_and(u8::is_ascii_whitespace)
    {
        return Err("SSID must not have leading or trailing ASCII whitespace");
    }
    Ok(())
}

/// Apply a validated [`CredentialUpdate`] to `current`, returning the new
/// [`Credentials`]. Validation failures never echo the rejected value.
///
/// # Errors
/// Returns [`WifidError::InvalidCredential`] if a supplied value fails WPA2
/// validation, or if the update would clear and set the STA PSK at once.
pub(crate) fn apply_update(
    current: &Credentials,
    update: &CredentialUpdate,
) -> Result<Credentials> {
    if update.clear_sta && update.sta_psk.is_some() {
        return Err(WifidError::InvalidCredential(
            "cannot set and clear the STA PSK in one update",
        ));
    }

    let sta_psk = if update.clear_sta {
        None
    } else if let Some(psk) = &update.sta_psk {
        validate_wpa2_passphrase(psk).map_err(WifidError::InvalidCredential)?;
        Some(Secret::new(psk.clone()))
    } else {
        current.sta_psk.clone()
    };

    let ap_passphrase = if let Some(ap) = &update.ap_passphrase {
        validate_wpa2_passphrase(ap).map_err(WifidError::InvalidCredential)?;
        Some(Secret::new(ap.clone()))
    } else {
        current.ap_passphrase.clone()
    };

    let ap_ssid = if let Some(ssid) = &update.ap_ssid {
        validate_ssid(ssid).map_err(WifidError::InvalidCredential)?;
        Some(ssid.clone())
    } else {
        current.ap_ssid.clone()
    };

    let ap_mode = update.ap_mode.unwrap_or(current.ap_mode);

    Ok(Credentials {
        sta_psk,
        ap_passphrase,
        ap_ssid,
        ap_mode,
    })
}

/// Persists and retrieves [`Credentials`]. The live implementation writes a
/// root-only `0600` file atomically; tests use an in-memory fake.
pub(crate) trait CredentialStore {
    /// Load the persisted credentials.
    ///
    /// Returns `Ok(None)` when the store **does not exist yet** (an
    /// unprovisioned appliance) — this is a benign empty config, never a fatal
    /// error, so the daemon must not crash-loop on a missing file. Returns
    /// `Ok(Some(..))` when credentials were loaded, and `Err(..)` only for a
    /// *real* fault (I/O error other than not-found, bad permissions, or
    /// malformed contents), which is surfaced rather than silently ignored.
    ///
    /// # Errors
    /// Returns [`WifidError::Credentials`] / [`WifidError::Io`] if the store
    /// exists but cannot be read or parsed.
    fn load(&self) -> Result<Option<Credentials>>;

    /// Persist `creds` (atomically, `0600`).
    ///
    /// # Errors
    /// Returns [`WifidError::Credentials`] / [`WifidError::Io`] on write failure.
    fn store(&self, creds: &Credentials) -> Result<()>;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        ApMode, CredentialUpdate, Credentials, Secret, apply_update, validate_ssid,
        validate_wpa2_passphrase,
    };

    const SENTINEL: &str = "hunter2-supersecret-psk";

    fn base() -> Credentials {
        Credentials {
            sta_psk: Some(Secret::new(SENTINEL)),
            ap_passphrase: Some(Secret::new("onboarding-pass")),
            ap_ssid: Some("TeslaUSB".to_owned()),
            ap_mode: ApMode::Auto,
        }
    }

    #[test]
    fn secret_debug_is_redacted() {
        let s = Secret::new(SENTINEL);
        let shown = format!("{s:?}");
        assert!(
            !shown.contains(SENTINEL),
            "secret leaked via Debug: {shown}"
        );
        assert!(shown.contains("redacted"));
    }

    #[test]
    fn credentials_debug_never_contains_a_secret() {
        let shown = format!("{:?}", base());
        assert!(
            !shown.contains(SENTINEL),
            "secret leaked via Credentials Debug"
        );
    }

    #[test]
    fn credential_update_debug_redacts_secrets() {
        let shown = format!(
            "{:?}",
            CredentialUpdate {
                sta_psk: Some("secretpsk123".to_owned()),
                ap_passphrase: Some("secretpass456".to_owned()),
                ap_ssid: Some("Vis".to_owned()),
                ap_mode: None,
                clear_sta: false,
            }
        );
        assert!(!shown.contains("secretpsk123"));
        assert!(!shown.contains("secretpass456"));
        assert!(shown.contains("Vis"));
        assert!(shown.contains("<redacted>"));
    }

    #[test]
    fn wpa2_validation_enforces_length() {
        assert!(validate_wpa2_passphrase("short").is_err());
        assert!(validate_wpa2_passphrase("12345678").is_ok());
        assert!(validate_wpa2_passphrase(&"x".repeat(63)).is_ok());
        assert!(validate_wpa2_passphrase(&"x".repeat(64)).is_err());
    }

    #[test]
    fn validation_error_does_not_echo_the_value() {
        let err = validate_wpa2_passphrase("bad").unwrap_err();
        assert!(!err.contains("bad"), "validation error echoed the value");
    }

    #[test]
    fn ssid_validation_enforces_bounds_and_characters() {
        assert!(validate_ssid("").is_err());
        assert!(validate_ssid("a").is_ok());
        assert!(validate_ssid(&"x".repeat(32)).is_ok());
        assert!(validate_ssid(&"x".repeat(33)).is_err());
        assert!(validate_ssid("abc\x1fdef").is_err());
        assert!(validate_ssid(" leading").is_err());
        assert!(validate_ssid("trailing ").is_err());
    }

    #[test]
    fn ssid_validation_error_does_not_echo_the_value() {
        let value = " bad-ssid";
        let err = validate_ssid(value).unwrap_err();
        assert!(!err.contains(value), "validation error echoed the value");
    }

    #[test]
    fn apply_update_changes_psk_and_keeps_ap() {
        let updated = apply_update(
            &base(),
            &CredentialUpdate {
                sta_psk: Some("newpassword".to_owned()),
                ap_passphrase: None,
                ap_ssid: None,
                ap_mode: None,
                clear_sta: false,
            },
        )
        .unwrap();
        assert_eq!(updated.sta_psk.as_ref().unwrap().reveal(), "newpassword");
        assert_eq!(
            updated.ap_passphrase.as_ref().unwrap().reveal(),
            "onboarding-pass"
        );
    }

    #[test]
    fn apply_update_can_clear_sta_for_onboarding_only() {
        let updated = apply_update(
            &base(),
            &CredentialUpdate {
                sta_psk: None,
                ap_passphrase: None,
                ap_ssid: None,
                ap_mode: None,
                clear_sta: true,
            },
        )
        .unwrap();
        assert!(!updated.sta_configured());
    }

    #[test]
    fn apply_update_rejects_set_and_clear_together() {
        let r = apply_update(
            &base(),
            &CredentialUpdate {
                sta_psk: Some("12345678".to_owned()),
                ap_passphrase: None,
                ap_ssid: None,
                ap_mode: None,
                clear_sta: true,
            },
        );
        assert!(r.is_err());
    }

    #[test]
    fn apply_update_rejects_invalid_ap_passphrase() {
        let r = apply_update(
            &base(),
            &CredentialUpdate {
                sta_psk: None,
                ap_passphrase: Some("short".to_owned()),
                ap_ssid: None,
                ap_mode: None,
                clear_sta: false,
            },
        );
        assert!(r.is_err());
    }

    #[test]
    fn apply_update_sets_ap_ssid_and_mode_without_touching_secrets() {
        let updated = apply_update(
            &base(),
            &CredentialUpdate {
                sta_psk: None,
                ap_passphrase: None,
                ap_ssid: Some("TeslaUSB AP".to_owned()),
                ap_mode: Some(ApMode::ForceOn),
                clear_sta: false,
            },
        )
        .unwrap();
        assert_eq!(updated.ap_ssid.as_deref(), Some("TeslaUSB AP"));
        assert_eq!(updated.ap_mode, ApMode::ForceOn);
        assert_eq!(updated.sta_psk.as_ref().unwrap().reveal(), SENTINEL);
        assert_eq!(
            updated.ap_passphrase.as_ref().unwrap().reveal(),
            "onboarding-pass"
        );
    }

    #[test]
    fn ap_provisioned_requires_both_ssid_and_passphrase() {
        let both = Credentials {
            sta_psk: None,
            ap_passphrase: Some(Secret::new("onboarding-pass")),
            ap_ssid: Some("TeslaUSB".to_owned()),
            ap_mode: ApMode::Auto,
        };
        assert!(both.ap_provisioned());
        let missing_ssid = Credentials {
            ap_ssid: None,
            ..both.clone()
        };
        assert!(!missing_ssid.ap_provisioned());
        let missing_pass = Credentials {
            ap_passphrase: None,
            ..both
        };
        assert!(!missing_pass.ap_provisioned());
    }
}
