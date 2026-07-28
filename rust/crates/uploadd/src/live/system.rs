use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::process::CommandExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::error::SourceError;
use crate::rclone::{CommandOutput, CommandRunner};
use crate::source::{ArchivePath, ArchiveSource};
use crate::throttle::{PauseAction, StoragePressure, ThrottleSnapshot, ThrottleSource, WifiThrottle};
use crate::time::{BootId, Clock, MonoMs, Waiter};

const WIFID_SOCKET_PATH: &str = "/run/teslausb/wifid.sock";
const RETENTION_GOVERNOR_PATH: &str = "/run/teslausb/retentiond.governor.json";
const THROTTLE_REFRESH_AGE_MS: u64 = 1_000;
const THROTTLE_MAX_STALE_MS: u64 = 5_000;
const WIFID_FRAME_CAP: u32 = 1 << 20;

/// Live archive filesystem reader.
pub struct LiveArchiveSource;

impl ArchiveSource for LiveArchiveSource {
    fn size(&self, path: &ArchivePath) -> Result<u64, SourceError> {
        let meta = std::fs::metadata(path.as_str()).map_err(|error| SourceError::Io(error.to_string()))?;
        Ok(meta.len())
    }

    fn read_chunk(&self, path: &ArchivePath, offset: u64, len: usize) -> Result<Vec<u8>, SourceError> {
        let mut file = File::open(path.as_str()).map_err(|error| SourceError::Io(error.to_string()))?;
        file.seek(SeekFrom::Start(offset))
            .map_err(|error| SourceError::Io(error.to_string()))?;
        let mut buffer = vec![0_u8; len];
        let read = file
            .read(&mut buffer)
            .map_err(|error| SourceError::Io(error.to_string()))?;
        buffer.truncate(read);
        Ok(buffer)
    }
}

#[derive(Debug, Clone)]
struct CachedWifi {
    value: WifiThrottle,
    read_at: Instant,
}

/// Live throttle source backed by `wifid` and `retentiond`.
pub struct LiveThrottleSource {
    wifid_socket_path: PathBuf,
    governor_path: PathBuf,
    cache: std::sync::Mutex<Option<CachedWifi>>,
}

impl LiveThrottleSource {
    #[must_use]
    /// Build with default socket and governor paths.
    pub fn new() -> Self {
        Self::with_paths(WIFID_SOCKET_PATH, RETENTION_GOVERNOR_PATH)
    }

    #[must_use]
    /// Build with explicit socket and governor paths.
    pub fn with_paths(wifid_socket_path: impl Into<PathBuf>, governor_path: impl Into<PathBuf>) -> Self {
        Self {
            wifid_socket_path: wifid_socket_path.into(),
            governor_path: governor_path.into(),
            cache: std::sync::Mutex::new(None),
        }
    }

    fn refresh_wifi(&self) -> Result<WifiThrottle, String> {
        let mut stream =
            UnixStream::connect(&self.wifid_socket_path).map_err(|err| format!("connect failed: {err}"))?;
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .map_err(|err| format!("set_read_timeout failed: {err}"))?;
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .map_err(|err| format!("set_write_timeout failed: {err}"))?;
        let request = serde_json::to_vec(&GetApStatusRequest {
            cmd: "get_ap_status",
        })
        .map_err(|err| format!("encode request failed: {err}"))?;
        write_frame(&mut stream, &request, WIFID_FRAME_CAP).map_err(|err| format!("write failed: {err}"))?;
        let payload = read_frame(&mut stream, WIFID_FRAME_CAP).map_err(|err| format!("read failed: {err}"))?;
        let status: WifidStatusWire =
            serde_json::from_slice(&payload).map_err(|err| format!("decode failed: {err}"))?;
        Ok(status.throttle)
    }

    fn wifi_snapshot(&self) -> WifiThrottle {
        let now = Instant::now();
        let Ok(mut cache) = self.cache.lock() else {
            return WifiThrottle::closed();
        };
        let cache_age = cache
            .as_ref()
            .map_or(u64::MAX, |cached| {
                let millis = now.saturating_duration_since(cached.read_at).as_millis();
                u64::try_from(millis).unwrap_or(u64::MAX)
            });
        let should_refresh = cache.is_none() || cache_age > THROTTLE_REFRESH_AGE_MS;
        if should_refresh {
            match self.refresh_wifi() {
                Ok(value) => {
                    *cache = Some(CachedWifi {
                        value,
                        read_at: now,
                    });
                    return value;
                }
                Err(_) if cache_age <= THROTTLE_MAX_STALE_MS => {}
                Err(_) => return WifiThrottle::closed(),
            }
        }
        cache
            .as_ref()
            .map_or_else(WifiThrottle::closed, |cached| cached.value)
    }

    fn storage_snapshot(&self) -> StoragePressure {
        let Ok(raw) = std::fs::read_to_string(&self.governor_path) else {
            return StoragePressure {
                seq: 0,
                uploads_allowed: false,
                action: PauseAction::PauseAtCheckpoint,
            };
        };
        let Ok(governor) = serde_json::from_str::<GovernorWire>(&raw) else {
            return StoragePressure {
                seq: 0,
                uploads_allowed: false,
                action: PauseAction::PauseAtCheckpoint,
            };
        };
        if governor.uploads_allowed {
            StoragePressure {
                seq: governor.seq.unwrap_or(0),
                uploads_allowed: true,
                action: PauseAction::Run,
            }
        } else {
            StoragePressure {
                seq: governor.seq.unwrap_or(0),
                uploads_allowed: false,
                action: PauseAction::PauseAtCheckpoint,
            }
        }
    }
}

impl ThrottleSource for LiveThrottleSource {
    fn current(&self) -> ThrottleSnapshot {
        ThrottleSnapshot {
            wifi: self.wifi_snapshot(),
            storage: self.storage_snapshot(),
        }
    }
}

#[derive(Serialize)]
struct GetApStatusRequest {
    cmd: &'static str,
}

#[derive(Deserialize)]
struct WifidStatusWire {
    throttle: WifiThrottle,
}

#[derive(Deserialize)]
struct GovernorWire {
    uploads_allowed: bool,
    #[serde(default)]
    seq: Option<u64>,
}

/// Monotonic live clock and boot-id provider.
pub struct LiveClock {
    start: Instant,
    boot_id: BootId,
}

impl LiveClock {
    #[must_use]
    /// Build a new live clock.
    pub fn new() -> Self {
        let boot_id = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| format!("process-{}", std::process::id()));
        Self {
            start: Instant::now(),
            boot_id: BootId(boot_id),
        }
    }
}

impl Default for LiveClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Default for LiveThrottleSource {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for LiveClock {
    fn mono_now(&self) -> MonoMs {
        let elapsed_ms_u128 = self.start.elapsed().as_millis();
        let elapsed_ms = i64::try_from(elapsed_ms_u128).unwrap_or(i64::MAX);
        MonoMs(elapsed_ms)
    }

    fn boot_id(&self) -> BootId {
        self.boot_id.clone()
    }
}

/// Live waiter backed by thread sleep.
pub struct LiveWaiter;

impl Waiter for LiveWaiter {
    fn wait_ms(&self, ms: u64) {
        if ms == 0 {
            return;
        }
        std::thread::sleep(Duration::from_millis(ms));
    }
}

/// Live subprocess runner used by the rclone backend.
pub struct LiveCommandRunner {
    after_run: Option<Box<dyn Fn()>>,
}

impl LiveCommandRunner {
    /// Create a runner that performs no work after each subprocess.
    #[must_use]
    pub fn new() -> Self {
        Self { after_run: None }
    }

    /// Run `after_run` once after every completed subprocess invocation.
    #[must_use]
    pub fn with_after_run(mut self, after_run: impl Fn() + 'static) -> Self {
        self.after_run = Some(Box::new(after_run));
        self
    }
}

impl Default for LiveCommandRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl CommandRunner for LiveCommandRunner {
    fn run(&self, program: &str, args: &[String]) -> Result<CommandOutput, String> {
        let mut command = Command::new(program);
        command.args(args);
        command.process_group(0);
        let output = command.output().map_err(|err| format!("spawn failed: {err}"))?;
        if let Some(after_run) = &self.after_run {
            after_run();
        }
        Ok(CommandOutput {
            status: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

fn read_frame(stream: &mut impl Read, cap: u32) -> std::io::Result<Vec<u8>> {
    let mut len_buf = [0_u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len_u32 = u32::from_le_bytes(len_buf);
    if len_u32 > cap {
        return Err(std::io::Error::other(format!(
            "frame too large: {len_u32} > {cap}"
        )));
    }
    let len = usize::try_from(len_u32).map_err(|_| std::io::Error::other("frame length overflow"))?;
    let mut payload = vec![0_u8; len];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

fn write_frame(stream: &mut impl Write, payload: &[u8], cap: u32) -> std::io::Result<()> {
    let cap_len = usize::try_from(cap).map_err(|_| std::io::Error::other("frame cap overflow"))?;
    if payload.len() > cap_len {
        return Err(std::io::Error::other(format!(
            "frame too large: {} > {cap_len}",
            payload.len()
        )));
    }
    let len_u32 =
        u32::try_from(payload.len()).map_err(|_| std::io::Error::other("payload length overflow"))?;
    stream.write_all(&len_u32.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::fs;
    use std::os::unix::net::UnixListener;
    use std::path::Path;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;

    use serde_json::json;

    use super::*;
    use crate::throttle::{LinkMode, PauseReason};

    static NEXT_ID: AtomicU64 = AtomicU64::new(0);

    fn unique_path(prefix: &str) -> PathBuf {
        let unique = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!("{prefix}-{}-{unique}", std::process::id()))
    }

    fn write_governor(path: &Path, uploads_allowed: bool) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create governor parent");
        }
        fs::write(path, format!(r#"{{"uploads_allowed":{uploads_allowed},"seq":9}}"#))
            .expect("write governor");
    }

    fn spawn_wifid_once(socket_path: PathBuf, response_json: serde_json::Value) -> thread::JoinHandle<()> {
        thread::spawn(move || {
            if let Some(parent) = socket_path.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let _ = fs::remove_file(&socket_path);
            let listener = UnixListener::bind(&socket_path).expect("bind wifid socket");
            let (mut stream, _) = listener.accept().expect("accept");
            let _ = read_frame(&mut stream, WIFID_FRAME_CAP).expect("read request");
            let payload = serde_json::to_vec(&response_json).expect("encode response");
            write_frame(&mut stream, &payload, WIFID_FRAME_CAP).expect("write response");
        })
    }

    fn wait_for_socket(path: &Path) {
        for _ in 0..200 {
            if path.exists() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("socket not ready: {}", path.display());
    }

    #[test]
    fn throttle_happy_path_reads_wifi_and_storage() {
        let base = unique_path("uploadd-live-throttle-happy");
        let socket = base.join("wifid.sock");
        let governor = base.join("retentiond.governor.json");
        write_governor(&governor, true);
        let server = spawn_wifid_once(
            socket.clone(),
            json!({
                "throttle": {
                    "seq": 7,
                    "link_mode": "sta",
                    "uploads_allowed": true,
                    "max_tx_bytes_per_s": 1024,
                    "max_chunk_bytes": 4096,
                    "action": "run",
                    "reason": "none"
                }
            }),
        );
        wait_for_socket(&socket);
        let source = LiveThrottleSource::with_paths(&socket, &governor);
        let snap = source.current();
        assert!(snap.wifi.uploads_allowed);
        assert_eq!(snap.wifi.link_mode, LinkMode::Sta);
        assert!(snap.storage.uploads_allowed);
        server.join().expect("join server");
        let _ = fs::remove_file(socket);
        let _ = fs::remove_file(governor);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn missing_socket_fails_closed() {
        let base = unique_path("uploadd-live-throttle-missing");
        let governor = base.join("retentiond.governor.json");
        write_governor(&governor, true);
        let source = LiveThrottleSource::with_paths(base.join("missing.sock"), &governor);
        let snap = source.current();
        assert!(!snap.wifi.uploads_allowed);
        assert_eq!(snap.wifi.reason, PauseReason::LinkDown);
        let _ = fs::remove_file(governor);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn garbage_reply_fails_closed() {
        let base = unique_path("uploadd-live-throttle-garbage");
        let socket = base.join("wifid.sock");
        let governor = base.join("retentiond.governor.json");
        write_governor(&governor, true);
        let server = thread::spawn({
            let socket = socket.clone();
            move || {
                if let Some(parent) = socket.parent() {
                    let _ = fs::create_dir_all(parent);
                }
                let _ = fs::remove_file(&socket);
                let listener = UnixListener::bind(&socket).expect("bind");
                let (mut stream, _) = listener.accept().expect("accept");
                let _ = read_frame(&mut stream, WIFID_FRAME_CAP).expect("read request");
                write_frame(&mut stream, b"not-json", WIFID_FRAME_CAP).expect("write");
            }
        });
        wait_for_socket(&socket);
        let source = LiveThrottleSource::with_paths(&socket, &governor);
        let snap = source.current();
        assert!(!snap.wifi.uploads_allowed);
        server.join().expect("join server");
        let _ = fs::remove_file(socket);
        let _ = fs::remove_file(governor);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn stale_cache_fails_closed_when_refresh_fails() {
        let base = unique_path("uploadd-live-throttle-stale");
        let socket = base.join("wifid.sock");
        let governor = base.join("retentiond.governor.json");
        write_governor(&governor, true);
        let server = spawn_wifid_once(
            socket.clone(),
            json!({
                "throttle": {
                    "seq": 8,
                    "link_mode": "sta",
                    "uploads_allowed": true,
                    "max_tx_bytes_per_s": 2048,
                    "max_chunk_bytes": 2048,
                    "action": "run",
                    "reason": "none"
                }
            }),
        );
        wait_for_socket(&socket);
        let source = LiveThrottleSource::with_paths(&socket, &governor);
        let first = source.current();
        assert!(first.wifi.uploads_allowed);
        server.join().expect("join server");
        let _ = fs::remove_file(&socket);
        if let Ok(mut guard) = source.cache.lock() {
            if let Some(cached) = guard.as_mut() {
                if let Some(stale) =
                    Instant::now().checked_sub(Duration::from_millis(THROTTLE_MAX_STALE_MS + 1))
                {
                    cached.read_at = stale;
                }
            }
        }
        let second = source.current();
        assert!(!second.wifi.uploads_allowed);
        let _ = fs::remove_file(governor);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn after_run_hook_fires_after_every_subprocess() {
        let calls = std::sync::Arc::new(AtomicU64::new(0));
        let seen = std::sync::Arc::clone(&calls);
        let runner = LiveCommandRunner::new().with_after_run(move || {
            seen.fetch_add(1, Ordering::Relaxed);
        });

        let first = runner.run("/bin/true", &[]).expect("spawn /bin/true");
        assert_eq!(first.status, 0);
        assert_eq!(calls.load(Ordering::Relaxed), 1);

        // A failing rclone may still have refreshed the token before exiting, so
        // the hook must fire on non-zero exits too.
        let second = runner.run("/bin/false", &[]).expect("spawn /bin/false");
        assert_ne!(second.status, 0);
        assert_eq!(calls.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn after_run_hook_does_not_fire_when_spawn_fails() {
        let calls = std::sync::Arc::new(AtomicU64::new(0));
        let seen = std::sync::Arc::clone(&calls);
        let runner = LiveCommandRunner::new().with_after_run(move || {
            seen.fetch_add(1, Ordering::Relaxed);
        });

        assert!(runner.run("/nonexistent/teslausb-missing-binary", &[]).is_err());
        assert_eq!(calls.load(Ordering::Relaxed), 0);
    }
}
