//! Static, operator-tunable configuration for `uploadd`.
//!
//! **No policy numbers are hardcoded into logic.** Every threshold lives here as
//! an explicit parameter with a conservative provisional default. Two classes
//! are called out, exactly as in the sibling `wifid`/`retentiond` lanes:
//!
//! * `// CALIBRATION-GATED (Task 2.6)` — the value depends on the **measured**
//!   BCM43436 SDIO-deadlock TX threshold from the Phase-2 `WiFi` TX-cap spike,
//!   which has **not run**. `uploadd` only ever *consumes* the cap published by
//!   `wifid`; this local default is the fail-safe floor used before any throttle
//!   state has been received, deliberately low so we err toward *too slow*.
//! * `// TUNABLE` — a timing/retry knob whose real value is a field/HW tuning
//!   decision (`single-writer-lease.md` OQ-5). Encoded as config, not invented.

use crate::priority::PriorityPolicy;

/// Complete runtime configuration for the upload core. Built from defaults and
/// (on the device) overlaid by an operator config file; the pure core only ever
/// reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UploaddConfig {
    /// Lease TTL + renew cadence.
    pub lease: LeaseConfig,
    /// Self-pacing token-bucket parameters (the "belt"; consumes the `wifid`
    /// cap).
    pub throttle: ThrottleConfig,
    /// Retry policy for failed transfers.
    pub retry: RetryConfig,
    /// User-policy upload ordering.
    pub priority: PriorityPolicy,
    /// Lease holder identity (`service` or `service:instance`), reported on
    /// `/api/storage` for diagnostics ([`single-writer-lease.md`] §2.1). Purely
    /// informational; never drives logic.
    ///
    /// [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md
    pub holder_id: String,
}

/// Upload-lease timing ([`single-writer-lease.md`] §2.2, OQ-5).
///
/// [`single-writer-lease.md`]: ../../../../docs/specs/contracts/single-writer-lease.md
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LeaseConfig {
    /// Lease time-to-live in milliseconds. A crashed holder frees the item once
    /// this elapses without a renew. TUNABLE (OQ-5 proposed `ttl_s = 60`).
    pub ttl_ms: i64,
    /// How often the transfer loop renews the lease while a transfer is active.
    /// Must be comfortably shorter than [`Self::ttl_ms`] to survive a stall.
    /// TUNABLE (OQ-5 proposed `ttl_s / 3 ≈ 20 s`).
    pub renew_interval_ms: i64,
}

/// Self-pacing token-bucket parameters (`wifi-upload-throttle.md` §2, the
/// "belt"). The byte values mirror the `wifid` cap and stay provisional until
/// the spike measures them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ThrottleConfig {
    /// Fail-safe sustained TX ceiling (bytes/sec) used before the first `wifid`
    /// throttle state is received. The *effective* cap is always the smaller of
    /// this and the published `max_tx_bytes_per_s`.
    // CALIBRATION-GATED (Task 2.6): provisional; real value from the 2.6 WiFi
    // TX-cap spike. 1 MiB/s is a conservative placeholder well under any
    // plausible SDIO-deadlock threshold.
    pub fallback_max_tx_bytes_per_s: u64,
    /// Fail-safe per-write chunk ceiling used before the first throttle state.
    // CALIBRATION-GATED (Task 2.6): provisional; real value from the 2.6 WiFi
    // TX-cap spike.
    pub fallback_max_chunk_bytes: u32,
    /// Token-bucket burst capacity in bytes (how much unused allowance may
    /// accumulate). Kept to one second of cap so a burst can never exceed the
    /// per-second ceiling by more than a small margin. TUNABLE.
    pub bucket_capacity_bytes: u64,
}

/// Retry policy for transfers that fail or fail integrity ([`uploadd.md`] §4).
///
/// [`uploadd.md`]: ../../../../docs/specs/uploadd.md
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetryConfig {
    /// Maximum transfer attempts before an item is parked as terminally
    /// `Failed` (it stays in the queue for operator inspection — never deleted).
    /// TUNABLE.
    pub max_attempts: u32,
}

impl Default for UploaddConfig {
    fn default() -> Self {
        Self {
            lease: LeaseConfig {
                ttl_ms: 60_000,            // TUNABLE (OQ-5)
                renew_interval_ms: 20_000, // TUNABLE (OQ-5)
            },
            throttle: ThrottleConfig {
                // CALIBRATION-GATED (Task 2.6): provisional defaults.
                fallback_max_tx_bytes_per_s: 1024 * 1024,
                fallback_max_chunk_bytes: 256 * 1024,
                bucket_capacity_bytes: 1024 * 1024,
            },
            retry: RetryConfig { max_attempts: 5 }, // TUNABLE
            priority: PriorityPolicy::default(),
            holder_id: "uploadd".to_owned(),
        }
    }
}

impl UploaddConfig {
    /// Validate cross-field invariants the rest of the core assumes.
    ///
    /// # Errors
    /// Returns a static reason if a value would make the core misbehave (a
    /// non-positive TTL, a renew cadence not shorter than the TTL, a zeroed cap,
    /// or a burst capacity below one second of cap).
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.lease.ttl_ms <= 0 {
            return Err("lease.ttl_ms must be > 0");
        }
        if self.lease.renew_interval_ms <= 0 {
            return Err("lease.renew_interval_ms must be > 0");
        }
        if self.lease.renew_interval_ms >= self.lease.ttl_ms {
            // A renew cadence at/above the TTL means a single missed renew lets
            // the lease lapse mid-transfer — the governor could then evict the
            // file we are reading.
            return Err("lease.renew_interval_ms must be < lease.ttl_ms");
        }
        if self.throttle.fallback_max_tx_bytes_per_s == 0 {
            return Err("throttle.fallback_max_tx_bytes_per_s must be > 0");
        }
        if self.throttle.fallback_max_chunk_bytes == 0 {
            return Err("throttle.fallback_max_chunk_bytes must be > 0");
        }
        if self.throttle.bucket_capacity_bytes < self.throttle.fallback_max_tx_bytes_per_s {
            return Err("throttle.bucket_capacity_bytes must be >= fallback_max_tx_bytes_per_s");
        }
        if self.retry.max_attempts == 0 {
            return Err("retry.max_attempts must be >= 1");
        }
        Ok(())
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::UploaddConfig;

    #[test]
    fn default_config_is_self_consistent() {
        UploaddConfig::default().validate().expect("default valid");
    }

    #[test]
    fn renew_at_or_above_ttl_is_rejected() {
        let mut cfg = UploaddConfig::default();
        cfg.lease.renew_interval_ms = cfg.lease.ttl_ms;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn zero_tx_cap_is_rejected() {
        let mut cfg = UploaddConfig::default();
        cfg.throttle.fallback_max_tx_bytes_per_s = 0;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn bucket_smaller_than_rate_is_rejected() {
        let mut cfg = UploaddConfig::default();
        cfg.throttle.bucket_capacity_bytes = cfg.throttle.fallback_max_tx_bytes_per_s - 1;
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn zero_attempts_is_rejected() {
        let mut cfg = UploaddConfig::default();
        cfg.retry.max_attempts = 0;
        assert!(cfg.validate().is_err());
    }
}
