//! Process-global systemd watchdog keepalive.
//!
//! Best-effort by design: all parse/socket errors are swallowed so retention
//! behavior is never blocked by watchdog signaling.

#[cfg(any(unix, test))]
#[derive(Debug, Clone, PartialEq, Eq)]
struct Decision {
    kind: AddrKind,
    interval_ms: i64,
}

#[cfg(any(unix, test))]
#[derive(Debug, Clone, PartialEq, Eq)]
enum AddrKind {
    Path(String),
    Abstract(String),
}

#[cfg(any(unix, test))]
fn decide(
    notify_socket: Option<&str>,
    watchdog_pid: Option<&str>,
    watchdog_usec: Option<&str>,
    my_pid: u32,
) -> Option<Decision> {
    let socket = notify_socket?;
    if socket.is_empty() {
        return None;
    }

    if let Some(pid_raw) = watchdog_pid {
        if !pid_raw.is_empty() {
            let Ok(pid) = pid_raw.parse::<u32>() else {
                return None;
            };
            if pid != my_pid {
                return None;
            }
        }
    }

    // Pet at half the systemd timeout (its recommended cadence), but cap the
    // interval at 10s so the hot-loop pets keep the deadline fresh through a
    // long copy/hash and a stalled blocking syscall (sync_all/rename) gets a
    // near-full WatchdogSec budget rather than starting on a stale clock.
    let interval_ms = watchdog_usec
        .and_then(|raw| raw.parse::<i64>().ok())
        .map_or(1000_i64, |usec| (usec / 1000 / 2).clamp(1000_i64, 10_000_i64));

    let kind = if let Some(name) = socket
        .strip_prefix('@')
        .or_else(|| socket.strip_prefix('\0'))
    {
        AddrKind::Abstract(name.to_owned())
    } else {
        AddrKind::Path(socket.to_owned())
    };

    Some(Decision { kind, interval_ms })
}

#[cfg(unix)]
mod imp {
    use std::os::linux::net::SocketAddrExt;
    use std::os::unix::net::{SocketAddr, UnixDatagram};
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicI64, Ordering};
    use std::time::Instant;

    use super::{AddrKind, decide};

    pub(super) struct Watchdog {
        socket: UnixDatagram,
        addr: SocketAddr,
        interval_ms: i64,
        last_pet_ms: AtomicI64,
        base: Instant,
    }

    impl Watchdog {
        fn now_ms(&self) -> i64 {
            i64::try_from(self.base.elapsed().as_millis()).unwrap_or(i64::MAX)
        }
    }

    static WATCHDOG: OnceLock<Option<Watchdog>> = OnceLock::new();

    pub(super) fn init() {
        let _ = WATCHDOG.get_or_init(|| {
            let notify_socket = std::env::var("NOTIFY_SOCKET").ok();
            let watchdog_pid = std::env::var("WATCHDOG_PID").ok();
            let watchdog_usec = std::env::var("WATCHDOG_USEC").ok();

            let decision = decide(
                notify_socket.as_deref(),
                watchdog_pid.as_deref(),
                watchdog_usec.as_deref(),
                std::process::id(),
            )?;

            let addr = match decision.kind {
                AddrKind::Path(path) => SocketAddr::from_pathname(path).ok()?,
                AddrKind::Abstract(name) => SocketAddr::from_abstract_name(name.as_bytes()).ok()?,
            };

            let socket = UnixDatagram::unbound().ok()?;
            // Best-effort keepalive must never block the work loop if systemd's
            // notify socket buffer is momentarily full.
            let _ = socket.set_nonblocking(true);

            Some(Watchdog {
                socket,
                addr,
                interval_ms: decision.interval_ms,
                last_pet_ms: AtomicI64::new(i64::MIN),
                base: Instant::now(),
            })
        });
    }

    pub(super) fn pet() {
        let Some(Some(watchdog)) = WATCHDOG.get() else {
            return;
        };

        let now_ms = watchdog.now_ms();
        let should_send = watchdog
            .last_pet_ms
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |last| {
                if now_ms.saturating_sub(last) >= watchdog.interval_ms {
                    Some(now_ms)
                } else {
                    None
                }
            })
            .is_ok();

        if should_send {
            let _ = watchdog.socket.send_to_addr(b"WATCHDOG=1", &watchdog.addr);
        }
    }
}

/// Initialize process-global watchdog signaling state (idempotent).
///
/// On unix, this reads systemd watchdog environment variables once and stores
/// best-effort signaling state. On non-unix, this is a no-op.
pub fn init() {
    #[cfg(unix)]
    imp::init();
}

/// Send a best-effort `WATCHDOG=1` keepalive.
///
/// This is lock-free and safe to call from any thread. It never panics. On
/// non-unix, this is a no-op.
pub fn pet() {
    #[cfg(unix)]
    imp::pet();
}

#[cfg(test)]
mod tests {
    use super::{AddrKind, decide, pet};

    #[test]
    fn decide_enables_for_path_socket_without_watchdog_pid() {
        let decision = decide(Some("/run/systemd/notify"), None, Some("5000000"), 1234);
        assert!(decision.is_some());
        let decision = match decision {
            Some(decision) => decision,
            None => return,
        };
        assert!(matches!(decision.kind, AddrKind::Path(ref p) if p == "/run/systemd/notify"));
        assert_eq!(decision.interval_ms, 2500);
    }

    #[test]
    fn decide_parses_abstract_socket_at_marker() {
        let decision = decide(Some("@foo"), None, None, 77);
        assert!(decision.is_some());
        let decision = match decision {
            Some(decision) => decision,
            None => return,
        };
        assert!(matches!(decision.kind, AddrKind::Abstract(ref n) if n == "foo"));
        assert_eq!(decision.interval_ms, 1000);
    }

    #[test]
    fn decide_disables_without_notify_socket() {
        assert!(decide(None, None, Some("1000000"), 55).is_none());
        assert!(decide(Some(""), None, Some("1000000"), 55).is_none());
    }

    #[test]
    fn decide_honors_watchdog_pid_match() {
        assert!(decide(Some("/run/systemd/notify"), Some("43"), None, 42).is_none());
        assert!(decide(Some("/run/systemd/notify"), Some("42"), None, 42).is_some());
    }

    #[test]
    fn decide_derives_interval_with_defaults_and_clamp() {
        let twenty_seconds = decide(Some("/run/systemd/notify"), None, Some("20000000"), 1);
        assert_eq!(
            twenty_seconds.map(|d| d.interval_ms),
            Some(10_000),
            "20s timeout should pet every 10s"
        );

        let missing = decide(Some("/run/systemd/notify"), None, None, 1);
        assert_eq!(missing.map(|d| d.interval_ms), Some(1000));

        let tiny = decide(Some("/run/systemd/notify"), None, Some("100000"), 1);
        assert_eq!(tiny.map(|d| d.interval_ms), Some(1000));

        let large = decide(Some("/run/systemd/notify"), None, Some("180000000"), 1);
        assert_eq!(
            large.map(|d| d.interval_ms),
            Some(10_000),
            "a 180s timeout should cap the pet interval at 10s, not 90s"
        );
    }

    #[test]
    fn pet_without_init_is_noop() {
        pet();
    }
}
