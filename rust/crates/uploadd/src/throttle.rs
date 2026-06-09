//! Consuming the `wifid` TX cap (contract **D4**) and `retentiond` storage
//! backpressure, plus the client-side [`Pacer`] that keeps a transfer under the
//! cap (the "belt").
//!
//! `uploadd` is the **consumer** in D4 ([`wifi-upload-throttle.md`]): `wifid`
//! owns and enforces the hard cap (kernel `tc`, the "braces") and publishes a
//! versioned [`WifiThrottle`]; `uploadd` subscribes and **self-paces** its own
//! transfer to the published `max_tx_bytes_per_s` / `max_chunk_bytes` so it can
//! never saturate the link or trip the SDIO deadlock — even if the kernel cap
//! were momentarily absent. Storage backpressure is a **separate plane**
//! ([`wifi-upload-throttle.md`] §2): a [`StoragePressure`] signal straight from
//! `retentiond`. The effective go/no-go is `wifi_allows && storage_allows`
//! ([`wifi-upload-throttle.md`] §2, [`crate::throttle::ThrottleSnapshot::gate`]).
//!
//! These types **mirror** `wifid::throttle` so the published shape and the
//! consumed shape stay byte-compatible; convergence onto
//! `teslausb-core::contracts::throttle` is the supervisor's. The cap numbers are
//! never hardcoded here — they arrive in the published state and are
//! `// CALIBRATION-GATED (Task 2.6)`.
//!
//! [`wifi-upload-throttle.md`]: ../../../../docs/specs/contracts/wifi-upload-throttle.md

use serde::{Deserialize, Serialize};

/// Link mode (D4 §3). Never `Sta`+`Ap` concurrently.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkMode {
    /// Station mode — a usable cloud path may exist.
    Sta,
    /// Access-point onboarding — no cloud path.
    Ap,
    /// No link.
    Down,
}

/// How `uploadd` must yield when capped/paused (D4 §3.1). The full set the two
/// planes can emit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PauseAction {
    /// Proceed at `max_tx_bytes_per_s`.
    Run,
    /// Finish the in-flight file, then stop dequeuing new work.
    DrainNoNew,
    /// Checkpoint the current transfer ASAP and park (resumable).
    PauseAtCheckpoint,
    /// Stop now, even mid-file; rely on the resumable queue to retry.
    AbortResumeLater,
}

/// Why uploads are paused/capped on the **link** plane (D4 §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PauseReason {
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

/// The link-plane throttle state published by `wifid` and consumed here
/// (mirrors `wifid::throttle::ThrottleState` flattened). `uploadd` ignores any
/// message whose [`Self::seq`] is not newer than the last applied one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WifiThrottle {
    /// Monotonic sequence counter; bumps on every body change (staleness guard).
    pub seq: u64,
    /// Current link mode.
    pub link_mode: LinkMode,
    /// Whether `uploadd` may transmit at all right now.
    pub uploads_allowed: bool,
    /// Hard TX ceiling in bytes/sec (`0` when uploads are not allowed).
    // CALIBRATION-GATED (Task 2.6): value originates from the wifid spike, never
    // hardcoded by uploadd.
    pub max_tx_bytes_per_s: u64,
    /// Per-write chunk ceiling `uploadd` must honor (`tc` cannot enforce it).
    // CALIBRATION-GATED (Task 2.6).
    pub max_chunk_bytes: u32,
    /// How `uploadd` must yield.
    pub action: PauseAction,
    /// Why.
    pub reason: PauseReason,
}

impl WifiThrottle {
    /// A fail-safe "no uploads" state used before the first real message is
    /// received from `wifid` (D4 fail-closed default).
    #[must_use]
    pub const fn closed() -> Self {
        Self {
            seq: 0,
            link_mode: LinkMode::Down,
            uploads_allowed: false,
            max_tx_bytes_per_s: 0,
            max_chunk_bytes: 0,
            action: PauseAction::DrainNoNew,
            reason: PauseReason::LinkDown,
        }
    }
}

/// Storage-plane backpressure from `retentiond` (D4 §3, a *separate* channel
/// from the link plane). `false` at Emergency/Exhausted ("stop dequeue").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct StoragePressure {
    /// Monotonic sequence counter (staleness guard).
    pub seq: u64,
    /// Whether the storage governor currently permits uploads to proceed.
    pub uploads_allowed: bool,
    /// How `uploadd` must yield when not allowed.
    pub action: PauseAction,
}

impl StoragePressure {
    /// The default "no pressure, uploads allowed" storage state.
    #[must_use]
    pub const fn open() -> Self {
        Self {
            seq: 0,
            uploads_allowed: true,
            action: PauseAction::Run,
        }
    }
}

/// Why the combined gate is paused — which plane asked, and the link reason if
/// it was the link plane.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateReason {
    /// The `wifid` link plane paused uploads (carries the link reason).
    Link(PauseReason),
    /// The `retentiond` storage plane paused uploads (governor backpressure).
    Storage,
}

/// The effective, combined go/no-go decision (`wifi_allows && storage_allows`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UploadGate {
    /// Proceed, paced to these published ceilings.
    Run {
        /// Sustained TX cap (bytes/sec) to seed the [`Pacer`].
        max_tx_bytes_per_s: u64,
        /// Per-write chunk ceiling.
        max_chunk_bytes: u32,
    },
    /// Do not proceed; yield per `action` for the given `reason`.
    Pause {
        /// How to yield (drain / checkpoint / abort).
        action: PauseAction,
        /// Which plane paused, and why.
        reason: GateReason,
    },
}

/// A point-in-time read of both throttle planes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ThrottleSnapshot {
    /// Link-plane state from `wifid`.
    pub wifi: WifiThrottle,
    /// Storage-plane state from `retentiond`.
    pub storage: StoragePressure,
}

impl ThrottleSnapshot {
    /// Combine both planes into the effective gate. The **link** plane is
    /// checked first (a wedged bus / AP mode is the most urgent stop); then the
    /// storage plane; only if both allow does it return [`UploadGate::Run`].
    #[must_use]
    pub fn gate(&self) -> UploadGate {
        if !self.wifi.uploads_allowed || self.wifi.max_tx_bytes_per_s == 0 {
            return UploadGate::Pause {
                action: self.wifi.action,
                reason: GateReason::Link(self.wifi.reason),
            };
        }
        if !self.storage.uploads_allowed {
            return UploadGate::Pause {
                action: self.storage.action,
                reason: GateReason::Storage,
            };
        }
        UploadGate::Run {
            max_tx_bytes_per_s: self.wifi.max_tx_bytes_per_s,
            max_chunk_bytes: self.wifi.max_chunk_bytes,
        }
    }
}

/// The seam that supplies the current [`ThrottleSnapshot`]. The live impl tracks
/// the latest `wifid` push (subscribe) and `retentiond` `StoragePressure`,
/// applying the `seq` staleness guard; tests inject a fixed snapshot.
pub trait ThrottleSource {
    /// The most recent combined throttle state.
    fn current(&self) -> ThrottleSnapshot;
}

/// A token bucket that paces `uploadd`'s own transfer to a byte/sec cap with a
/// bounded burst — the "belt" in D4's belt-and-braces model.
///
/// Pure integer math over a monotonic millisecond clock — no floats, no RTC.
/// Mirrors `wifid::throttle::TokenBucket` so the self-pace and the kernel `tc`
/// braces enforce the *same* cap.
#[derive(Debug, Clone)]
pub struct Pacer {
    capacity: u64,
    rate_per_s: u64,
    tokens: u64,
    last_ms: i64,
}

impl Pacer {
    /// New pacer starting full (one burst of `capacity` is allowed immediately,
    /// then sustained flow is bounded by `rate_per_s`).
    #[must_use]
    pub const fn new(rate_per_s: u64, capacity: u64, now_ms: i64) -> Self {
        Self {
            capacity,
            rate_per_s,
            tokens: capacity,
            last_ms: now_ms,
        }
    }

    /// Update the cap live (e.g. on a `NearDeadlock` backoff or a new published
    /// state). Clamps the current balance to the new capacity.
    pub fn set_rate(&mut self, rate_per_s: u64, capacity: u64) {
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

    /// Try to consume `bytes` of allowance at `now_ms`. Returns `true` (and
    /// debits) if the bucket had enough; `false` if the send must wait.
    pub fn try_consume(&mut self, bytes: u64, now_ms: i64) -> bool {
        self.refill(now_ms);
        if self.tokens >= bytes {
            self.tokens -= bytes;
            true
        } else {
            false
        }
    }

    /// Milliseconds to wait from `now_ms` until `bytes` of allowance will be
    /// available, given the current balance and rate. `0` if already available.
    /// Used to drive the [`crate::time::Waiter`] between chunks.
    #[must_use]
    pub fn wait_ms_for(&self, bytes: u64, now_ms: i64) -> u64 {
        // Account for tokens that would refill by `now_ms` without mutating.
        let elapsed_ms = u64::try_from(now_ms.saturating_sub(self.last_ms).max(0)).unwrap_or(0);
        let refilled =
            u128::from(self.tokens) + u128::from(self.rate_per_s) * u128::from(elapsed_ms) / 1000;
        let available = refilled.min(u128::from(self.capacity));
        let want = u128::from(bytes.min(self.capacity));
        if available >= want {
            return 0;
        }
        if self.rate_per_s == 0 {
            return u64::MAX;
        }
        let deficit = want - available;
        // ceil(deficit * 1000 / rate)
        let ms = (deficit * 1000).div_ceil(u128::from(self.rate_per_s));
        u64::try_from(ms).unwrap_or(u64::MAX)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use super::{
        GateReason, LinkMode, Pacer, PauseAction, PauseReason, StoragePressure, ThrottleSnapshot,
        UploadGate, WifiThrottle,
    };

    fn wifi_running(rate: u64) -> WifiThrottle {
        WifiThrottle {
            seq: 1,
            link_mode: LinkMode::Sta,
            uploads_allowed: true,
            max_tx_bytes_per_s: rate,
            max_chunk_bytes: 64 * 1024,
            action: PauseAction::Run,
            reason: PauseReason::None,
        }
    }

    #[test]
    fn gate_runs_only_when_both_planes_allow() {
        let snap = ThrottleSnapshot {
            wifi: wifi_running(1000),
            storage: StoragePressure::open(),
        };
        assert!(matches!(snap.gate(), UploadGate::Run { .. }));
    }

    #[test]
    fn link_pause_dominates() {
        let snap = ThrottleSnapshot {
            wifi: WifiThrottle::closed(),
            storage: StoragePressure::open(),
        };
        match snap.gate() {
            UploadGate::Pause {
                reason: GateReason::Link(PauseReason::LinkDown),
                ..
            } => {}
            other => panic!("expected link pause, got {other:?}"),
        }
    }

    #[test]
    fn storage_pause_when_link_ok_but_governor_stops() {
        let snap = ThrottleSnapshot {
            wifi: wifi_running(1000),
            storage: StoragePressure {
                seq: 3,
                uploads_allowed: false,
                action: PauseAction::PauseAtCheckpoint,
            },
        };
        match snap.gate() {
            UploadGate::Pause {
                action: PauseAction::PauseAtCheckpoint,
                reason: GateReason::Storage,
            } => {}
            other => panic!("expected storage pause, got {other:?}"),
        }
    }

    #[test]
    fn pacer_caps_sustained_tx_at_the_configured_rate() {
        // Mirrors the wifid token-bucket proof: 1000 B/s, 1000 B burst.
        let rate = 1000;
        let mut p = Pacer::new(rate, rate, 0);
        assert!(p.try_consume(rate, 0)); // drain the burst
        let mut sent: u64 = 0;
        let mut t = 0;
        while t <= 10_000 {
            if p.try_consume(200, t) {
                sent += 200;
            }
            t += 100;
        }
        let ceiling = rate * 10 + rate; // 10s sustained + one burst of slack
        assert!(
            sent <= ceiling,
            "sent {sent} exceeded cap ceiling {ceiling}"
        );
        assert!(sent >= rate * 9, "throttle starved traffic: only {sent}");
    }

    #[test]
    fn pacer_never_exceeds_capacity_on_long_idle() {
        let mut p = Pacer::new(1000, 1000, 0);
        assert!(p.try_consume(1000, 3_600_000));
        assert!(!p.try_consume(1, 3_600_000), "burst exceeded capacity");
    }

    #[test]
    fn wait_ms_for_is_zero_when_available_else_positive() {
        let mut p = Pacer::new(1000, 1000, 0);
        assert_eq!(p.wait_ms_for(500, 0), 0);
        assert!(p.try_consume(1000, 0));
        // Empty now; 500 bytes at 1000 B/s ⇒ ~500ms.
        let w = p.wait_ms_for(500, 0);
        assert!((400..=600).contains(&w), "unexpected wait {w}");
    }
}
