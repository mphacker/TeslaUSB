//! Crate-level boundary error type (`SPEC.md` §7: typed errors via
//! `thiserror`, no `unwrap`/`expect` in service paths).
//!
//! Variants are deliberately coarse: the pure decision core returns rich
//! domain values (actions, recovery decisions), and only the I/O seams and the
//! credential layer surface these errors. Error `Display` text must **never**
//! echo a secret (PSK / AP passphrase); see [`crate::creds`].

/// Errors raised by `wifid`'s I/O seams and credential layer.
#[derive(Debug, thiserror::Error)]
pub(crate) enum WifidError {
    /// A network-control operation (associate, bring up AP, apply `tc`, reset
    /// chip) failed at the hardware/OS boundary.
    #[error("network control failed: {0}")]
    Network(String),

    /// Credential load/store failed (missing file, bad permissions, malformed
    /// contents). The message never contains the secret value itself.
    #[error("credential store error: {0}")]
    Credentials(String),

    /// A supplied credential failed validation (e.g. WPA2 PSK length).
    /// The message describes *what* was wrong, never the rejected value.
    #[error("invalid credential: {0}")]
    InvalidCredential(&'static str),

    /// An operation is only available on the live Linux device and the
    /// hardware-gated executor has not been validated yet (Phase 2 spikes).
    #[error("hardware-gated: {0}")]
    HardwareGated(&'static str),

    /// Underlying I/O error.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
}

/// Crate result alias.
pub(crate) type Result<T> = std::result::Result<T, WifidError>;
