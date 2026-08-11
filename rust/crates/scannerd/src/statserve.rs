//! Dedicated stat socket: TeslaCam exFAT free space. Additive to the read
//! socket; never touches it. Single-flight + short TTL cache so a UI refresh
//! storm cannot spam the recording drive with bitmap reads.

use std::io;
use std::os::unix::fs::FileTypeExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use scannerd::boot::{ExfatParams, parse_boot_sector};
use scannerd::error::ScannerError;
use scannerd::freespace::{VolumeStats, volume_free_space};
use scannerd::mbr::parse_mbr;
use scannerd::proto::{
    MAX_REQUEST_FRAME, VolumeStatsReply, VolumeStatsRequest, read_frame, write_frame,
};
use scannerd::reader::BlockReader;
use scannerd::volume::Volume;

use crate::io::PreadReader;

/// Default dedicated stats socket path.
pub const DEFAULT_STAT_SOCKET: &str = "/run/teslausb/scannerd-stat.sock";
const MAX_CONNECTIONS: usize = 4;
const READ_TIMEOUT: Duration = Duration::from_secs(120);
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);
const SLOT0: u8 = 0;
const STAT_TTL: Duration = Duration::from_secs(5);

#[derive(Clone)]
enum CachedStat {
    Ok(VolumeStats),
    Unavailable,
    Error(String),
}

static STAT_CACHE: LazyLock<Mutex<Option<(Instant, CachedStat)>>> =
    LazyLock::new(|| Mutex::new(None));

/// Start the stat server on a dedicated thread.
///
/// # Errors
///
/// Returns an error when socket setup/bind fails or thread spawn fails.
pub fn start(reader: Arc<PreadReader>, socket_path: &Path) -> io::Result<JoinHandle<()>> {
    let listener = bind_listener(socket_path)?;
    let path_text = socket_path.display().to_string();
    let handle = thread::Builder::new()
        .name("scannerd-statserve".to_owned())
        .spawn(move || run_listener(&listener, &reader))
        .map_err(|e| io::Error::other(format!("spawn statserve: {e}")))?;
    println!("scannerd stat serve: listening on {path_text}");
    Ok(handle)
}

fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))?;
    }
    match std::fs::symlink_metadata(socket_path) {
        Ok(meta) => {
            if !meta.file_type().is_socket() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!(
                        "refusing to unlink non-socket at stat socket path: {}",
                        socket_path.display()
                    ),
                ));
            }
            std::fs::remove_file(socket_path)?;
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    Ok(listener)
}

fn run_listener(listener: &UnixListener, reader: &Arc<PreadReader>) {
    let active = Arc::new(AtomicUsize::new(0));
    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let current = active.fetch_add(1, Ordering::AcqRel);
                if current >= MAX_CONNECTIONS {
                    active.fetch_sub(1, Ordering::AcqRel);
                    if let Err(e) = write_error_and_close(stream, "too many stat connections") {
                        eprintln!("scannerd stat serve: reject failed: {e}");
                    }
                    continue;
                }
                let reader = Arc::clone(reader);
                let active_guard = Arc::clone(&active);
                let spawned = thread::Builder::new()
                    .name("scannerd-stat-conn".to_owned())
                    .spawn(move || {
                        let _counter = ActiveCounter::new(active_guard);
                        if let Err(e) = handle_conn(stream, reader.as_ref()) {
                            eprintln!("scannerd stat serve: connection ended: {e}");
                        }
                    });
                if let Err(e) = spawned {
                    active.fetch_sub(1, Ordering::AcqRel);
                    eprintln!("scannerd stat serve: connection thread spawn failed: {e}");
                }
            }
            Err(e) => eprintln!("scannerd stat serve: accept error: {e}"),
        }
    }
}

struct ActiveCounter {
    active: Arc<AtomicUsize>,
}

impl ActiveCounter {
    fn new(active: Arc<AtomicUsize>) -> Self {
        Self { active }
    }
}

impl Drop for ActiveCounter {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::AcqRel);
    }
}

fn write_error_and_close(mut stream: UnixStream, message: &str) -> io::Result<()> {
    stream.set_write_timeout(Some(WRITE_TIMEOUT))?;
    let reply = VolumeStatsReply::Error {
        message: message.to_owned(),
    };
    let payload = serde_json::to_vec(&reply).map_err(io::Error::other)?;
    write_frame(&mut stream, &payload)
}

fn handle_conn<R: BlockReader + ?Sized>(mut stream: UnixStream, reader: &R) -> io::Result<()> {
    stream.set_read_timeout(Some(READ_TIMEOUT))?;
    stream.set_write_timeout(Some(WRITE_TIMEOUT))?;

    let payload = read_frame(&mut stream, MAX_REQUEST_FRAME)?;
    let request: VolumeStatsRequest = match serde_json::from_slice(&payload) {
        Ok(req) => req,
        Err(e) => {
            let reply = VolumeStatsReply::Error {
                message: format!("invalid request json: {e}"),
            };
            let encoded = serde_json::to_vec(&reply).map_err(io::Error::other)?;
            return write_frame(&mut stream, &encoded);
        }
    };

    let reply = if request.slot == SLOT0 {
        cached_or_compute(reader)
    } else {
        VolumeStatsReply::Unavailable
    };
    let encoded = serde_json::to_vec(&reply).map_err(io::Error::other)?;
    write_frame(&mut stream, &encoded)
}

fn cached_or_compute<R: BlockReader + ?Sized>(reader: &R) -> VolumeStatsReply {
    cached_or_compute_in(&STAT_CACHE, reader)
}

fn cached_or_compute_in<R: BlockReader + ?Sized>(
    cache: &Mutex<Option<(Instant, CachedStat)>>,
    reader: &R,
) -> VolumeStatsReply {
    let Ok(mut guard) = cache.lock() else {
        return VolumeStatsReply::Error {
            message: "stat cache lock poisoned".to_owned(),
        };
    };
    if let Some((stored_at, cached)) = guard.as_ref() {
        if cache_is_fresh(*stored_at, Instant::now(), STAT_TTL) {
            return reply_from_cached(cached);
        }
    }

    let outcome = match compute_stats_raw(reader) {
        Ok(Some(stats)) => CachedStat::Ok(stats),
        Ok(None) => CachedStat::Unavailable,
        Err(err) => CachedStat::Error(err.to_string()),
    };
    let stored_at = Instant::now();
    *guard = Some((stored_at, outcome.clone()));
    reply_from_cached(&outcome)
}

fn compute_stats_raw<R: BlockReader + ?Sized>(
    reader: &R,
) -> Result<Option<VolumeStats>, ScannerError> {
    let Some(params) = parse_slot0(reader)? else {
        return Ok(None);
    };
    let volume = Volume::new(reader, params);
    volume_free_space(&volume).map(Some)
}

fn parse_slot0<R: BlockReader + ?Sized>(reader: &R) -> Result<Option<ExfatParams>, ScannerError> {
    let partitions = parse_mbr(reader)?;
    let slot0 = partitions.into_iter().find(|entry| entry.slot == SLOT0);
    let Some(slot0) = slot0 else {
        return Ok(None);
    };
    if !slot0.is_exfat() {
        return Ok(None);
    }
    let params = parse_boot_sector(reader, slot0.start_lba)?;
    Ok(Some(params))
}

fn reply_from(stats: Option<VolumeStats>) -> VolumeStatsReply {
    stats.map_or(VolumeStatsReply::Unavailable, |stats| {
        VolumeStatsReply::Ok {
            cluster_count: stats.cluster_count,
            bytes_per_cluster: stats.bytes_per_cluster,
            used_clusters: stats.used_clusters,
            free_clusters: stats.free_clusters,
            total_bytes: stats.total_bytes,
            used_bytes: stats.used_bytes,
            free_bytes: stats.free_bytes,
            stable: stats.stable,
        }
    })
}

fn reply_from_cached(cached: &CachedStat) -> VolumeStatsReply {
    match cached {
        CachedStat::Ok(stats) => reply_from(Some(*stats)),
        CachedStat::Unavailable => VolumeStatsReply::Unavailable,
        CachedStat::Error(message) => VolumeStatsReply::Error {
            message: message.clone(),
        },
    }
}

fn cache_is_fresh(stored_at: Instant, now: Instant, ttl: Duration) -> bool {
    now.checked_duration_since(stored_at)
        .is_some_and(|age| age <= ttl)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use scannerd::reader::ReaderError;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn cache_freshness_helper_obeys_ttl() {
        let start = Instant::now();
        assert!(cache_is_fresh(
            start,
            start + Duration::from_secs(5),
            Duration::from_secs(5)
        ));
        assert!(!cache_is_fresh(
            start,
            start + Duration::from_secs(6),
            Duration::from_secs(5),
        ));
    }

    #[test]
    fn reply_mapping_roundtrips_success_and_error() {
        let stats = VolumeStats {
            cluster_count: 100,
            bytes_per_cluster: 512,
            used_clusters: 25,
            free_clusters: 75,
            total_bytes: 51_200,
            used_bytes: 12_800,
            free_bytes: 38_400,
            stable: true,
        };
        assert!(matches!(
            reply_from(Some(stats)),
            VolumeStatsReply::Ok {
                cluster_count: 100,
                ..
            }
        ));
        assert!(matches!(reply_from(None), VolumeStatsReply::Unavailable));
        assert!(matches!(
            reply_from_cached(&CachedStat::Error("boom".to_owned())),
            VolumeStatsReply::Error { .. }
        ));
    }

    #[test]
    fn negative_outcome_is_cached_single_flight() {
        let reader = CountingReader::new(vec![0_u8; 512]);
        let cache = Mutex::new(None);
        let r1 = cached_or_compute_in(&cache, &reader);
        let reads_after_first = reader.reads();
        let r2 = cached_or_compute_in(&cache, &reader);
        let reads_after_second = reader.reads();
        assert_eq!(reads_after_first, reads_after_second);
        assert!(matches!(
            r1,
            VolumeStatsReply::Unavailable | VolumeStatsReply::Error { .. }
        ));
        assert!(matches!(
            r2,
            VolumeStatsReply::Unavailable | VolumeStatsReply::Error { .. }
        ));
        assert!(reads_after_first > 0);
    }

    #[test]
    fn bind_listener_refuses_non_socket_path() {
        let dir = unique_temp_dir("statserve-refuse");
        let path = dir.join("teslacam.img");
        std::fs::write(&path, b"not a socket").unwrap();
        let err = bind_listener(&path).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);
        assert!(path.exists(), "must not delete a regular file");
        std::fs::remove_file(&path).ok();
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bind_listener_replaces_stale_socket() {
        let dir = unique_temp_dir("statserve-stale");
        let path = dir.join("scannerd-stat.sock");
        let l1 = bind_listener(&path).unwrap();
        drop(l1);
        let _l2 = bind_listener(&path).unwrap();
        assert!(path.exists());
        std::fs::remove_file(&path).ok();
        std::fs::remove_dir_all(&dir).ok();
    }

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock before unix epoch")
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("{prefix}-{nanos}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    struct CountingReader {
        image: Vec<u8>,
        reads: AtomicUsize,
    }

    impl CountingReader {
        fn new(image: Vec<u8>) -> Self {
            Self {
                image,
                reads: AtomicUsize::new(0),
            }
        }

        fn reads(&self) -> usize {
            self.reads.load(Ordering::Relaxed)
        }
    }

    impl BlockReader for CountingReader {
        fn size_bytes(&self) -> u64 {
            u64::try_from(self.image.len()).unwrap_or(u64::MAX)
        }

        fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
            self.reads.fetch_add(1, Ordering::Relaxed);
            let start = usize::try_from(offset).map_err(|_| ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size: self.size_bytes(),
            })?;
            let end = start
                .checked_add(buf.len())
                .ok_or(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                })?;
            if end > self.image.len() {
                return Err(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                });
            }
            let src = self.image.get(start..end).ok_or(ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size: self.size_bytes(),
            })?;
            buf.copy_from_slice(src);
            Ok(())
        }
    }
}
