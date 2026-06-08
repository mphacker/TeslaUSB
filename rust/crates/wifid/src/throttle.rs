//! TX rate cap (`wifid.md` §2.3) and the published **throttle state** (contract
//! D4, `docs/specs/contracts/wifi-upload-throttle.md`).
//!
//! Two pieces, both pure and host-tested:
//!
//! * [`TokenBucket`] — the enforcement primitive. `wifid` owns the hard cap;
//!   the live executor mirrors it as a kernel `tc` egress limit (the "braces"),
//!   while `uploadd` self-paces to the published state (the "belt").
//! * [`ThrottlePublisher`] — derives a versioned [`ThrottleState`] from link +
//!   recovery inputs and bumps `seq` whenever the published body changes, so
//!   `uploadd` can ignore stale/out-of-order messages.
//!
//! **Fail-closed:** uploads are only ever allowed in confirmed STA mode with
//! the `tc` cap applied and no recovery in flight. Every transitional or
//! ambiguous state publishes `uploads_allowed = false`.

use serde::Serialize;

use crate::config::ThrottleConfig;
use crate::link::LinkMode;

/// How `uploadd` must yield when `uploads_allowed` is false (contract §3.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PauseAction {
    /// Proceed at `max_tx_bytes_per_s`.
    Run,
    /// Finish the in-flight file, then stop dequeuing new work.
    DrainNoNew,
    /// Stop now, even mid-file; rely on the resumable queue to retry.
    AbortResumeLater,
    // NOTE: the contract's storage-plane `pause_at_checkpoint` action is owned
    // by retentiond, not wifid; the link plane only ever emits the variants
    // above, so it is intentionally not represented here.
}

/// Why uploads are paused / capped (contract §3, link-plane reasons only).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PauseReason {
    /// STA up, full cap.
    None,
    /// AP onboarding active — no cloud path.
    ApMode,
    /// Not associated / no reachability.
    LinkDown,
    /// SDIO watchdog resetting `brcmfmac`.
    ChipRecovery,
    /// Backing off to stay under the SDIO threshold.
    NearDeadlock,
}

/// The published throttle body (everything except the sequence counter).
/// Equality on the body is what decides whether `seq` must advance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) struct ThrottleBody {
    /// Current link mode (never `Sta`+`Ap`).
    pub(crate) link_mode: LinkMode,
    /// Whether `uploadd` may transmit at all right now.
    pub(crate) uploads_allowed: bool,
    /// Hard TX ceiling in bytes/sec (0 when uploads are not allowed).
    pub(crate) max_tx_bytes_per_s: u64,
    /// Per-write chunk ceiling (`uploadd` enforces; `tc` cannot).
    pub(crate) max_chunk_bytes: u32,
    /// How `uploadd` must yield.
    pub(crate) action: PauseAction,
    /// Why.
    pub(crate) reason: PauseReason,
}

/// The full versioned state published to `uploadd` / `webd`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) struct ThrottleState {
    /// Monotonic sequence counter; bumps on every body change (staleness guard).
    pub(crate) seq: u64,
    /// The throttle body.
    #[serde(flatten)]
    pub(crate) body: ThrottleBody,
}

/// Inputs the publisher reduces into a [`ThrottleState`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(clippy::struct_excessive_bools)] // distinct, independent link facts; an enum would obscure them
pub(crate) struct ThrottleInputs {
    /// Current link mode from the state machine.
    pub(crate) link_mode: LinkMode,
    /// STA is confirmed stably up (`LinkStep::sta_link_up`).
    pub(crate) sta_link_up: bool,
    /// The SDIO chip-reset watchdog is mid-recovery.
    pub(crate) chip_recovering: bool,
    /// The link is nearing the deadlock threshold (executor-observed).
    pub(crate) near_deadlock: bool,
    /// The kernel `tc` cap has been successfully applied.
    pub(crate) tc_applied: bool,
}

/// A token bucket enforcing a sustained byte/sec cap with a bounded burst.
///
/// Pure integer math over a monotonic millisecond clock — no floats, no RTC.
#[derive(Debug, Clone)]
pub(crate) struct TokenBucket {
    capacity: u64,
    rate_per_s: u64,
    tokens: u64,
    last_ms: i64,
}

impl TokenBucket {
    /// New bucket starting full (one burst of `capacity` is allowed
    /// immediately, then sustained flow is bounded by `rate_per_s`).
    pub(crate) fn new(rate_per_s: u64, capacity: u64, now_ms: i64) -> Self {
        Self {
            capacity,
            rate_per_s,
            tokens: capacity,
            last_ms: now_ms,
        }
    }

    /// Update the cap live (e.g. on a `NearDeadlock` backoff). Clamps the
    /// current balance to the new capacity.
    pub(crate) fn set_rate(&mut self, rate_per_s: u64, capacity: u64) {
        self.rate_per_s = rate_per_s;
        self.capacity = capacity;
        self.tokens = self.tokens.min(capacity);
    }

    fn refill(&mut self, now_ms: i64) {
        let elapsed_ms = u64::try_from(now_ms.saturating_sub(self.last_ms).max(0)).unwrap_or(0);
        if elapsed_ms == 0 {
            return;
        }
        self.last_ms = now_ms;
        let added = u128::from(self.rate_per_s) * u128::from(elapsed_ms) / 1000;
        let total = u128::from(self.tokens) + added;
        self.tokens = u64::try_from(total.min(u128::from(self.capacity))).unwrap_or(self.capacity);
    }

    /// Try to consume `bytes` of allowance at `now_ms`. Returns `true` if the
    /// bucket had enough (and debits it); `false` if the send must wait.
    pub(crate) fn try_consume(&mut self, bytes: u64, now_ms: i64) -> bool {
        self.refill(now_ms);
        if self.tokens >= bytes {
            self.tokens -= bytes;
            true
        } else {
            false
        }
    }
}

/// Derives and versions the published [`ThrottleState`].
pub(crate) struct ThrottlePublisher {
    full_rate: u64,
    max_chunk_bytes: u32,
    near_deadlock_divisor: u64,
    seq: u64,
    last_body: Option<ThrottleBody>,
}

impl ThrottlePublisher {
    /// Build a publisher from the throttle config. `seq` starts at 0.
    pub(crate) fn new(cfg: &ThrottleConfig) -> Self {
        Self {
            full_rate: cfg.max_tx_bytes_per_s,
            max_chunk_bytes: cfg.max_chunk_bytes,
            near_deadlock_divisor: cfg.near_deadlock_divisor.max(1),
            seq: 0,
            last_body: None,
        }
    }

    /// Reduce `inputs` into the current body (fail-closed) and return the
    /// versioned state, advancing `seq` only if the body changed.
    pub(crate) fn update(&mut self, inputs: ThrottleInputs) -> ThrottleState {
        let body = self.derive_body(inputs);
        if self.last_body != Some(body) {
            self.seq += 1;
            self.last_body = Some(body);
        }
        ThrottleState {
            seq: self.seq,
            body,
        }
    }

    fn derive_body(&self, i: ThrottleInputs) -> ThrottleBody {
        // Priority order matters: recovery and non-STA modes fail closed before
        // any "allowed" path is considered.
        let (uploads_allowed, max_tx, action, reason) = if i.chip_recovering {
            (
                false,
                0,
                PauseAction::AbortResumeLater,
                PauseReason::ChipRecovery,
            )
        } else if i.link_mode == LinkMode::Ap {
            (false, 0, PauseAction::DrainNoNew, PauseReason::ApMode)
        } else if i.link_mode == LinkMode::Down {
            (false, 0, PauseAction::DrainNoNew, PauseReason::LinkDown)
        } else if !i.sta_link_up || !i.tc_applied {
            // STA mode but not yet confirmed up, or the hard cap is not in place
            // — never allow TX without the braces applied.
            (false, 0, PauseAction::DrainNoNew, PauseReason::LinkDown)
        } else if i.near_deadlock {
            let reduced = (self.full_rate / self.near_deadlock_divisor).max(1);
            (true, reduced, PauseAction::Run, PauseReason::NearDeadlock)
        } else {
            (true, self.full_rate, PauseAction::Run, PauseReason::None)
        };
        ThrottleBody {
            link_mode: i.link_mode,
            uploads_allowed,
            max_tx_bytes_per_s: max_tx,
            max_chunk_bytes: self.max_chunk_bytes,
            action,
            reason,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{PauseAction, PauseReason, ThrottleInputs, ThrottlePublisher, TokenBucket};
    use crate::config::WifidConfig;
    use crate::link::LinkMode;

    #[test]
    fn token_bucket_caps_sustained_tx_at_the_configured_rate() {
        // 1000 bytes/sec, 1000-byte burst capacity.
        let rate = 1000;
        let mut b = TokenBucket::new(rate, rate, 0);
        // Drain the initial burst.
        assert!(b.try_consume(rate, 0));
        // Over a 10-second window, requesting 200 bytes every 100ms (= 2000
        // bytes/sec demand) must be capped to ~rate bytes total.
        let mut sent: u64 = 0;
        let mut t = 0;
        while t <= 10_000 {
            if b.try_consume(200, t) {
                sent += 200;
            }
            t += 100;
        }
        // Allow one burst-capacity of slack above the 10s of sustained rate.
        let ceiling = rate * 10 + rate;
        assert!(
            sent <= ceiling,
            "sent {sent} exceeded cap ceiling {ceiling}"
        );
        // And it actually let a meaningful amount through (not starved).
        assert!(sent >= rate * 9, "throttle starved traffic: only {sent}");
    }

    #[test]
    fn token_bucket_never_exceeds_capacity_on_long_idle() {
        let mut b = TokenBucket::new(1000, 1000, 0);
        // Idle for an hour; balance must clamp to capacity, not accumulate.
        assert!(b.try_consume(1000, 3_600_000));
        assert!(!b.try_consume(1, 3_600_000), "burst exceeded capacity");
    }

    fn publisher() -> ThrottlePublisher {
        ThrottlePublisher::new(&WifidConfig::default().throttle)
    }

    fn inputs(link_mode: LinkMode) -> ThrottleInputs {
        ThrottleInputs {
            link_mode,
            sta_link_up: false,
            chip_recovering: false,
            near_deadlock: false,
            tc_applied: false,
        }
    }

    #[test]
    fn ap_mode_forbids_uploads() {
        let mut p = publisher();
        let s = p.update(inputs(LinkMode::Ap));
        assert!(!s.body.uploads_allowed);
        assert_eq!(s.body.reason, PauseReason::ApMode);
        assert_eq!(s.body.max_tx_bytes_per_s, 0);
    }

    #[test]
    fn chip_recovery_forbids_uploads_even_in_sta() {
        let mut p = publisher();
        let mut i = inputs(LinkMode::Sta);
        i.sta_link_up = true;
        i.tc_applied = true;
        i.chip_recovering = true;
        let s = p.update(i);
        assert!(!s.body.uploads_allowed);
        assert_eq!(s.body.reason, PauseReason::ChipRecovery);
        assert_eq!(s.body.action, PauseAction::AbortResumeLater);
    }

    #[test]
    fn sta_up_without_tc_applied_fails_closed() {
        let mut p = publisher();
        let mut i = inputs(LinkMode::Sta);
        i.sta_link_up = true;
        i.tc_applied = false;
        let s = p.update(i);
        assert!(
            !s.body.uploads_allowed,
            "uploaded allowed before tc cap applied"
        );
    }

    #[test]
    fn confirmed_sta_with_cap_allows_full_rate() {
        let mut p = publisher();
        let mut i = inputs(LinkMode::Sta);
        i.sta_link_up = true;
        i.tc_applied = true;
        let s = p.update(i);
        assert!(s.body.uploads_allowed);
        assert_eq!(s.body.reason, PauseReason::None);
        assert_eq!(
            s.body.max_tx_bytes_per_s,
            WifidConfig::default().throttle.max_tx_bytes_per_s
        );
    }

    #[test]
    fn near_deadlock_keeps_running_at_a_reduced_cap() {
        let mut p = publisher();
        let mut i = inputs(LinkMode::Sta);
        i.sta_link_up = true;
        i.tc_applied = true;
        i.near_deadlock = true;
        let s = p.update(i);
        assert!(s.body.uploads_allowed);
        assert_eq!(s.body.reason, PauseReason::NearDeadlock);
        let full = WifidConfig::default().throttle.max_tx_bytes_per_s;
        assert!(s.body.max_tx_bytes_per_s < full && s.body.max_tx_bytes_per_s > 0);
    }

    #[test]
    fn seq_advances_only_on_body_change() {
        let mut p = publisher();
        let s1 = p.update(inputs(LinkMode::Ap));
        let s2 = p.update(inputs(LinkMode::Ap));
        assert_eq!(s1.seq, s2.seq, "seq bumped with no change");
        let s3 = p.update(inputs(LinkMode::Down));
        assert!(s3.seq > s2.seq, "seq did not advance on change");
    }
}
