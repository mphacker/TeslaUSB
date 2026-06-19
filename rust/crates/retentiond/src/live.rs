//! Live Unix seam implementations for `retentiond`.
//!
//! This module is intentionally bin-internal; the pure policy core remains in
//! the library.
#![allow(unsafe_code)]
#![allow(dead_code)]

use std::ffi::CString;
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use retentiond::archive::ArchiveStore;
use retentiond::delete::RandGen;
use retentiond::governor::Statfs;
use retentiond::io::{ContentHash, FileIdentity, FsStat};
use retentiond::recent_facts::{RecentDirReader, RecentFileObservation};
use retentiond::time::{BootId, Clock, MonoMs};
use sha2::{Digest, Sha256};

pub(crate) struct LiveClock;

impl Clock for LiveClock {
    fn mono_now(&self) -> MonoMs {
        // SAFETY: `timespec` is plain old data and zero-init is valid before
        // passing a mutable pointer to `clock_gettime`.
        let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
        // SAFETY: `CLOCK_MONOTONIC` is a valid clock id and `ts` points to
        // writable memory for the syscall output.
        let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
        debug_assert_eq!(
            rc, 0,
            "clock_gettime(CLOCK_MONOTONIC) should not fail with valid args"
        );
        if rc != 0 {
            // Unreachable in normal Linux operation with valid arguments; trait
            // cannot return Result, so preserve a deterministic fallback.
            return MonoMs(0);
        }

        let sec_ms = i128::from(ts.tv_sec).saturating_mul(1_000);
        let nsec_ms = i128::from(ts.tv_nsec) / 1_000_000;
        let total_ms = sec_ms.saturating_add(nsec_ms);
        MonoMs(i128_to_i64_clamped(total_ms))
    }

    fn boot_id(&self) -> BootId {
        match std::fs::read_to_string("/proc/sys/kernel/random/boot_id") {
            Ok(s) => BootId(s.trim().to_owned()),
            Err(_) => BootId("unknown-boot".to_owned()),
        }
    }
}

pub(crate) struct LiveRand;

impl RandGen for LiveRand {
    fn next_u128(&self) -> u128 {
        if let Some(value) = getrandom_u128() {
            return value;
        }
        if let Some(value) = urandom_u128() {
            return value;
        }
        fallback_random_u128()
    }
}

static FALLBACK_COUNTER: AtomicU64 = AtomicU64::new(0);
static LAST_RESORT_WARNED: AtomicBool = AtomicBool::new(false);

fn getrandom_u128() -> Option<u128> {
    const MAX_RETRIES: usize = 256;
    let mut bytes = [0_u8; 16];
    let mut filled = 0usize;

    for _ in 0..MAX_RETRIES {
        if filled == bytes.len() {
            return Some(u128::from_le_bytes(bytes));
        }
        // SAFETY: `filled <= bytes.len()` and pointer is advanced only within
        // the destination buffer's bounds.
        let ptr = unsafe { bytes.as_mut_ptr().add(filled) }.cast::<libc::c_void>();
        let remaining = bytes.len() - filled;
        // SAFETY: `ptr` targets `remaining` writable bytes in `bytes`.
        let n = unsafe { libc::getrandom(ptr, remaining, 0) };
        if n > 0 {
            let Ok(read) = usize::try_from(n) else {
                break;
            };
            filled += read;
            continue;
        }
        if n == -1 {
            let err = io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EINTR) {
                continue;
            }
        }
        break;
    }

    if filled == bytes.len() {
        Some(u128::from_le_bytes(bytes))
    } else {
        None
    }
}

fn urandom_u128() -> Option<u128> {
    let mut bytes = [0_u8; 16];
    let mut file = File::open("/dev/urandom").ok()?;
    file.read_exact(&mut bytes).ok()?;
    Some(u128::from_le_bytes(bytes))
}

fn fallback_random_u128() -> u128 {
    if LAST_RESORT_WARNED
        .compare_exchange(false, true, Ordering::Relaxed, Ordering::Relaxed)
        .is_ok()
    {
        eprintln!(
            "retentiond: WARNING: OS CSPRNG unavailable (getrandom and /dev/urandom failed); \
using degraded best-effort random token fallback"
        );
    }
    // RandGen cannot return Result and is currently consumed only by the Phase-2
    // delete path (not yet wired), so this ASLR/PID/counter mix avoids wall-clock
    // derivation across boots; true fail-closed is deferred to a trait change.
    let counter = FALLBACK_COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = u64::from(std::process::id());
    let probe = 0_u8;
    let addr = std::ptr::addr_of!(probe) as usize as u64;
    let lo = splitmix64(
        counter
            ^ pid.rotate_left(13)
            ^ addr.rotate_left(29)
            ^ 0xa076_1d64_78bd_642f,
    );
    let hi = splitmix64(
        counter
            .wrapping_add(0x9e37_79b9_7f4a_7c15)
            ^ pid.rotate_left(41)
            ^ addr.rotate_left(7)
            ^ 0xe703_7ed1_a0b4_28db,
    );
    (u128::from(hi) << 64) | u128::from(lo)
}

fn splitmix64(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    x ^ (x >> 31)
}

fn i128_to_i64_clamped(value: i128) -> i64 {
    if value > i128::from(i64::MAX) {
        i64::MAX
    } else if value < i128::from(i64::MIN) {
        i64::MIN
    } else {
        match i64::try_from(value) {
            Ok(v) => v,
            Err(_) => i64::MIN,
        }
    }
}

fn to_u64_saturating<T>(value: T) -> u64
where
    u64: TryFrom<T>,
{
    match u64::try_from(value) {
        Ok(v) => v,
        Err(_) => u64::MAX,
    }
}

const COPY_BUFFER_SIZE: usize = 64 * 1024;
static COPY_TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Live Unix `ArchiveStore` seam.
///
/// `ContentHash` values from this store are SHA-256 digests of file contents.
///
/// # Security invariant
///
/// Containment relies on two trust-boundary invariants, not solely on the
/// lexical path jail and `canonicalize` checks below:
///
/// * `archive_root` is **service-owned** and writable **only** by `retentiond`,
///   which is the sole writer/deleter of the Pi-side archive. No untrusted actor
///   may create entries inside it concurrently with an archive pass. (An actor
///   that *could* would already be able to corrupt or delete archives directly,
///   so the residual create/rename TOCTOU window grants no extra capability.)
/// * `source_root` is a **read-only exFAT** mount of the car-visible volume,
///   which cannot hold symlinks; source-side symlink injection is not possible.
///
/// Under those invariants the lexical [`jailed_join`] (rejects `..`/absolute),
/// [`canonicalize_under_root`] containment check, [`validate_archive_parent_path`]
/// symlink-component rejection, and the post-rename re-anchor in
/// [`ArchiveStore::copy_and_hash_dest`] are sufficient. If `archive_root` ever
/// becomes writable by an untrusted actor, switch the create/rename/read-back to
/// dir-fd-anchored `openat`/`renameat` with `O_NOFOLLOW` semantics.
pub(crate) struct LiveArchiveStore {
    source_root: PathBuf,
    archive_root: PathBuf,
}

impl LiveArchiveStore {
    #[must_use]
    pub(crate) fn new(source_root: impl Into<PathBuf>, archive_root: impl Into<PathBuf>) -> Self {
        Self {
            source_root: source_root.into(),
            archive_root: archive_root.into(),
        }
    }
}

/// Live filesystem-backed `RecentDirReader` for one `RecentClips` slot.
pub(crate) struct LiveRecentDirReader {
    source_root: PathBuf,
    recentclips_dir: String,
    slot: u8,
}

impl LiveRecentDirReader {
    #[must_use]
    pub(crate) fn new(
        source_root: impl Into<PathBuf>,
        recentclips_dir: impl Into<String>,
        slot: u8,
    ) -> Self {
        Self {
            source_root: source_root.into(),
            recentclips_dir: recentclips_dir.into(),
            slot,
        }
    }
}

impl RecentDirReader for LiveRecentDirReader {
    fn list(&self, slot: u8) -> io::Result<Vec<RecentFileObservation>> {
        if slot != self.slot {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("slot mismatch: requested={slot} configured={}", self.slot),
            ));
        }

        let source_dir = jailed_join(&self.source_root, &self.recentclips_dir)?;
        let source_dir = match canonicalize_under_root(&self.source_root, &source_dir) {
            Ok(path) => path,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => return Err(err),
        };

        let mut files = Vec::new();
        let entries = match fs::read_dir(source_dir) {
            Ok(entries) => entries,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => return Err(err),
        };
        for entry_result in entries {
            let entry = match entry_result {
                Ok(entry) => entry,
                Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
                Err(err) => return Err(err),
            };

            let Ok(name) = entry.file_name().into_string() else {
                continue;
            };
            let is_mp4 = Path::new(&name)
                .extension()
                .and_then(std::ffi::OsStr::to_str)
                .is_some_and(|ext| ext.eq_ignore_ascii_case("mp4"));
            if !is_mp4 {
                continue;
            }

            let metadata = match entry.metadata() {
                Ok(metadata) => metadata,
                Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
                Err(err) => return Err(err),
            };
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
                Err(err) => return Err(err),
            };
            if !file_type.is_file() {
                continue;
            }
            let mtime_ms = metadata
                .modified()
                .map(system_time_to_epoch_ms_saturating)
                .unwrap_or(0);
            files.push(RecentFileObservation {
                name,
                size: metadata.len(),
                mtime_ms,
            });
        }
        Ok(files)
    }
}

impl ArchiveStore for LiveArchiveStore {
    fn copy_and_hash_dest(&self, src_rel: &str, dest_rel: &str) -> io::Result<ContentHash> {
        let source_path = jailed_join(&self.source_root, src_rel)?;
        let source_path = canonicalize_under_root(&self.source_root, &source_path)?;
        let dest_path = jailed_join(&self.archive_root, dest_rel)?;
        let parent = dest_path.parent().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "destination path must include a parent directory",
            )
        })?;
        validate_archive_parent_path(&self.archive_root, parent)?;
        fs::create_dir_all(parent)?;
        let canonical_parent = canonicalize_under_root(&self.archive_root, parent)?;
        sync_dir_chain(&self.archive_root, &canonical_parent)?;

        let temp_path = make_temp_path(&dest_path)?;
        let result = (|| -> io::Result<ContentHash> {
            copy_streaming(&source_path, &temp_path)?;
            fs::rename(&temp_path, &dest_path)?;
            sync_dir(&canonical_parent)?;
            // Re-anchor before reading the bytes back: re-resolve the landed path
            // and re-assert archive-root containment. If a parent component was
            // raced into a symlink between validation and the rename, this turns a
            // silent jail escape into a detected error rather than hashing (and
            // reporting "verified") bytes that landed outside the archive.
            let landed = canonicalize_under_root(&self.archive_root, &dest_path)?;
            hash_file_sha256(&landed)
        })();

        if result.is_err() {
            let _ = fs::remove_file(&temp_path);
        }
        result
    }

    fn source_identity(&self, src_rel: &str) -> io::Result<FileIdentity> {
        let source_path = jailed_join(&self.source_root, src_rel)?;
        let source_path = canonicalize_under_root(&self.source_root, &source_path)?;
        let source_file = File::open(&source_path)?;
        let size = source_file.metadata()?.len();
        let mut reader = BufReader::with_capacity(COPY_BUFFER_SIZE, source_file);
        let hash = hash_reader_sha256(&mut reader)?;
        Ok(FileIdentity { size, hash })
    }

    fn list_source_rel_names(&self, src_dir: &str) -> io::Result<Vec<String>> {
        let source_dir = jailed_join(&self.source_root, src_dir)?;
        let source_dir = canonicalize_under_root(&self.source_root, &source_dir)?;
        let mut names = Vec::new();
        for entry_result in fs::read_dir(source_dir)? {
            let entry = entry_result?;
            if !entry.file_type()?.is_file() {
                continue;
            }
            let name = entry.file_name().into_string().map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "source entry name is not valid UTF-8",
                )
            })?;
            names.push(name);
        }
        Ok(names)
    }
}

fn jailed_join(root: &Path, rel: &str) -> io::Result<PathBuf> {
    let mut path = root.to_path_buf();
    for component in Path::new(rel).components() {
        match component {
            Component::Normal(name) => path.push(name),
            Component::Prefix(_)
            | Component::RootDir
            | Component::CurDir
            | Component::ParentDir => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("relative path escapes jail: {rel}"),
                ));
            }
        }
    }
    Ok(path)
}

fn copy_streaming(src: &Path, dest: &Path) -> io::Result<()> {
    let source_file = File::open(src)?;
    let mut reader = BufReader::with_capacity(COPY_BUFFER_SIZE, source_file);
    let mut writer = OpenOptions::new().create_new(true).write(true).open(dest)?;
    let mut buffer = vec![0_u8; COPY_BUFFER_SIZE];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        let chunk = buffer
            .get(..read)
            .ok_or_else(|| io::Error::other("copy read exceeded buffer size"))?;
        writer.write_all(chunk)?;
    }
    writer.sync_all()
}

fn validate_archive_parent_path(root: &Path, parent: &Path) -> io::Result<()> {
    let rel_parent = parent.strip_prefix(root).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "destination parent is outside archive root (root={}, parent={})",
                root.display(),
                parent.display()
            ),
        )
    })?;
    let mut current = root.to_path_buf();
    for component in rel_parent.components() {
        let Component::Normal(name) = component else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("destination parent contains invalid component: {component:?}"),
            ));
        };
        current.push(name);
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                let file_type = meta.file_type();
                if file_type.is_symlink() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!(
                            "destination parent contains symlink component: {}",
                            current.display()
                        ),
                    ));
                }
                if !file_type.is_dir() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!(
                            "destination parent component is not a directory: {}",
                            current.display()
                        ),
                    ));
                }
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => break,
            Err(err) => return Err(err),
        }
    }
    Ok(())
}

fn canonicalize_under_root(root: &Path, path: &Path) -> io::Result<PathBuf> {
    let canonical_root = root.canonicalize()?;
    let canonical_path = path.canonicalize()?;
    if canonical_path.starts_with(&canonical_root) {
        Ok(canonical_path)
    } else {
        Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "path escapes jail (root={}, path={})",
                root.display(),
                path.display()
            ),
        ))
    }
}

fn sync_dir_chain(root: &Path, leaf: &Path) -> io::Result<()> {
    let canonical_root = root.canonicalize()?;
    let mut current = leaf.to_path_buf();
    if !current.starts_with(&canonical_root) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "directory escapes jail during sync (root={}, path={})",
                canonical_root.display(),
                current.display()
            ),
        ));
    }
    loop {
        sync_dir(&current)?;
        if current == canonical_root {
            break;
        }
        let Some(parent) = current.parent() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("directory {} has no parent while syncing", current.display()),
            ));
        };
        current = parent.to_path_buf();
    }
    Ok(())
}

fn make_temp_path(dest_path: &Path) -> io::Result<PathBuf> {
    let Some(file_name) = dest_path.file_name() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "destination path must include a file name",
        ));
    };
    let unique = COPY_TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut temp_name = OsString::from(file_name);
    temp_name.push(format!(".tmp-{}-{unique}", std::process::id()));
    Ok(dest_path.with_file_name(temp_name))
}

fn sync_dir(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

fn hash_file_sha256(path: &Path) -> io::Result<ContentHash> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(COPY_BUFFER_SIZE, file);
    hash_reader_sha256(&mut reader)
}

fn hash_reader_sha256(reader: &mut dyn Read) -> io::Result<ContentHash> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_SIZE];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        let chunk = buffer
            .get(..read)
            .ok_or_else(|| io::Error::other("hash read exceeded buffer size"))?;
        hasher.update(chunk);
    }
    let out: [u8; 32] = hasher.finalize().into();
    Ok(ContentHash::new(out))
}

fn system_time_to_epoch_ms_saturating(time: SystemTime) -> i64 {
    match time.duration_since(UNIX_EPOCH) {
        Ok(duration) => {
            let millis = i128::try_from(duration.as_millis()).unwrap_or(i128::MAX);
            i128_to_i64_clamped(millis)
        }
        Err(err) => {
            let millis = i128::try_from(err.duration().as_millis()).unwrap_or(i128::MAX);
            i128_to_i64_clamped(-millis)
        }
    }
}

pub(crate) struct LiveStatfs;

impl Statfs for LiveStatfs {
    fn statfs(&self, path: &str) -> io::Result<FsStat> {
        let c_path = CString::new(path).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "path contains interior NUL byte")
        })?;

        // SAFETY: `stat` is POD and may be zero-initialized before syscall fill.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: `c_path` is a valid NUL-terminated C string and `st` is a valid
        // output pointer for `stat(2)`.
        if unsafe { libc::stat(c_path.as_ptr(), &mut st) } != 0 {
            return Err(io::Error::last_os_error());
        }

        // SAFETY: `statvfs` is POD and may be zero-initialized before syscall fill.
        let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
        // SAFETY: `c_path` is valid and `s` is writable output storage.
        if unsafe { libc::statvfs(c_path.as_ptr(), &mut s) } != 0 {
            return Err(io::Error::last_os_error());
        }

        let frsize = if s.f_frsize == 0 { s.f_bsize } else { s.f_frsize };
        let frsize = to_u64_saturating(frsize);

        Ok(FsStat {
            dev_id: to_u64_saturating(st.st_dev),
            free_bytes: to_u64_saturating(s.f_bavail).saturating_mul(frsize),
            total_bytes: to_u64_saturating(s.f_blocks).saturating_mul(frsize),
            free_inodes: to_u64_saturating(s.f_favail),
            total_inodes: to_u64_saturating(s.f_files),
        })
    }
}

#[cfg(all(test, unix))]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::indexing_slicing
)]
mod tests {
    use std::cell::RefCell;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    use sha2::{Digest, Sha256};

    use super::{LiveArchiveStore, LiveClock, LiveRand, LiveRecentDirReader, LiveStatfs};
    use retentiond::archive::ArchiveStore;
    use retentiond::archive_driver::{DriverState, archive_recent_once};
    use retentiond::delete::RandGen;
    use retentiond::governor::Statfs;
    use retentiond::io::ContentHash;
    use retentiond::recent_facts::{RecentDirReader, RecentFactsGatherer};
    use retentiond::register_client::{
        ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk,
    };
    use retentiond::time::Clock;

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!(
            "retentiond-live-{}-{}",
            std::process::id(),
            unique
        );
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn write_file(path: &Path, bytes: &[u8]) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent dir");
        }
        fs::write(path, bytes).expect("write test file");
    }

    fn hash_bytes(bytes: &[u8]) -> ContentHash {
        let digest = Sha256::digest(bytes);
        let mut out = [0_u8; 32];
        out.copy_from_slice(&digest);
        ContentHash::new(out)
    }

    const ARCHIVE_PATH: &str = "RecentClips/2026-06-19/2026-06-19_10-00-00";
    const FRONT_REF: &str =
        "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4";
    const BACK_REF: &str =
        "RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4";

    #[derive(Default)]
    struct CapturingRegister {
        calls: RefCell<Vec<ArchiveRegistration>>,
    }

    impl RegisterClient for CapturingRegister {
        fn register(&self, reg: &ArchiveRegistration) -> Result<RegistrationOk, RegisterError> {
            self.calls.borrow_mut().push(reg.clone());
            Ok(RegistrationOk {
                clip_id: 1,
                archive_item_id: 1,
            })
        }
    }

    fn i64_len(bytes: &[u8]) -> i64 {
        i64::try_from(bytes.len()).expect("test byte length should fit in i64")
    }

    fn assert_archived_bytes(archive_root: &Path, file_ref: &str, expected: &[u8]) {
        let path = archive_root.join(file_ref);
        assert!(
            path.is_file(),
            "archive file must exist at {}",
            path.display()
        );
        assert_eq!(fs::read(&path).expect("read archived file"), expected);
    }

    fn assert_registered_angles(
        reg: &ArchiveRegistration,
        archive_root: &Path,
        front_bytes: &[u8],
        back_bytes: &[u8],
    ) {
        assert_eq!(reg.angles.len(), 2);
        let mut saw_front = false;
        let mut saw_back = false;
        for angle in &reg.angles {
            assert_eq!(angle.offset_ms, 0);
            assert!(angle.duration_s.is_none());
            assert!(
                angle.file_ref == FRONT_REF || angle.file_ref == BACK_REF,
                "unexpected file_ref {}",
                angle.file_ref
            );
            let resolved = archive_root.join(&angle.file_ref);
            assert!(
                resolved.is_file(),
                "file_ref should resolve under archive root: {}",
                resolved.display()
            );
            if angle.file_ref == FRONT_REF {
                saw_front = true;
                assert_eq!(angle.size_bytes, i64_len(front_bytes));
            } else {
                saw_back = true;
                assert_eq!(angle.size_bytes, i64_len(back_bytes));
            }
        }
        assert!(saw_front, "front angle should be present");
        assert!(saw_back, "back angle should be present");
    }

    #[test]
    fn live_clock_is_monotonic() {
        let clock = LiveClock;
        let first = clock.mono_now();
        let second = clock.mono_now();
        assert!(
            second.0 >= first.0,
            "monotonic clock regressed: first={} second={}",
            first.0,
            second.0
        );
    }

    #[test]
    fn live_clock_boot_id_nonempty() {
        let clock = LiveClock;
        let first = clock.boot_id();
        let second = clock.boot_id();
        assert!(!first.0.is_empty(), "boot id should not be empty");
        assert_eq!(first.0, second.0, "boot id should be stable in-process");
    }

    #[test]
    fn live_rand_differs_and_nonzero() {
        let rand = LiveRand;
        let first = rand.next_u128();
        let second = rand.next_u128();
        assert_ne!(first, second, "two random reads should differ");
        assert!(
            !(first == 0 && second == 0),
            "two random reads should not both be zero"
        );
    }

    #[test]
    fn live_statfs_root_ok() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("/");
        assert!(result.is_ok(), "statfs('/') should succeed");
        let Ok(stat) = result else {
            return;
        };
        assert!(stat.total_bytes > 0, "root total bytes should be > 0");
        assert!(stat.dev_id != 0, "root dev id should be non-zero");
    }

    #[test]
    fn live_statfs_bad_path_err() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("/nonexistent/teslausb/zzz");
        assert!(result.is_err(), "missing path should return an error");
    }

    #[test]
    fn live_statfs_interior_nul_err() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("a\0b");
        assert!(result.is_err(), "interior NUL path should return an error");
    }

    #[test]
    fn copy_and_hash_dest_copies_and_hashes_landed_bytes() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");

        let bytes = b"hello";
        write_file(&source_root.join("RecentClips/clip.mp4"), bytes);

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let hash = store
            .copy_and_hash_dest("RecentClips/clip.mp4", "RecentClips/clip.mp4")
            .expect("copy succeeds");

        assert_eq!(hash, hash_bytes(bytes));
        let dest_path = archive_root.join("RecentClips/clip.mp4");
        assert!(dest_path.exists(), "dest file should exist");
        assert_eq!(fs::read(dest_path).expect("read dest"), bytes);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn source_identity_matches_size_and_hash() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");

        let bytes = b"retention";
        write_file(&source_root.join("SavedClips/a.mp4"), bytes);

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let copied_hash = store
            .copy_and_hash_dest("SavedClips/a.mp4", "SavedClips/a.mp4")
            .expect("copy succeeds");
        let identity = store
            .source_identity("SavedClips/a.mp4")
            .expect("source identity succeeds");

        assert_eq!(identity.size, bytes.len() as u64);
        assert_eq!(identity.hash, copied_hash);
        assert_eq!(identity.hash, hash_bytes(bytes));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_source_rel_names_lists_only_direct_files() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");

        let dir = source_root.join("SentryClips/ev1");
        fs::create_dir_all(dir.join("nested")).expect("create nested dir");
        write_file(&dir.join("a.mp4"), b"a");
        write_file(&dir.join("b.mp4"), b"b");
        write_file(&dir.join("nested/c.mp4"), b"c");

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let mut names = store
            .list_source_rel_names("SentryClips/ev1")
            .expect("list succeeds");
        names.sort_unstable();
        assert_eq!(names, vec!["a.mp4".to_owned(), "b.mp4".to_owned()]);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn live_recent_dir_reader_lists_slot_files_and_handles_missing_dir() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        fs::create_dir_all(&source_root).expect("create source root");
        let recent_dir = source_root.join("TeslaCam/RecentClips");
        fs::create_dir_all(recent_dir.join("nested")).expect("create recent dirs");
        write_file(&recent_dir.join("a.mp4"), b"123");
        write_file(&recent_dir.join("b.mp4"), b"12345");
        write_file(&recent_dir.join("ignore.txt"), b"ignored");
        write_file(&recent_dir.join("nested/c.mp4"), b"nested");

        let reader = LiveRecentDirReader::new(&source_root, "TeslaCam/RecentClips", 0);
        let mut files = reader.list(0).expect("list recent files");
        files.sort_by(|a, b| a.name.cmp(&b.name));
        assert_eq!(files.len(), 2);
        assert_eq!(files[0].name, "a.mp4");
        assert_eq!(files[0].size, 3);
        assert_eq!(files[1].name, "b.mp4");
        assert_eq!(files[1].size, 5);

        let slot_err = reader.list(1).expect_err("slot mismatch should fail");
        assert_eq!(slot_err.kind(), std::io::ErrorKind::InvalidInput);

        let missing = LiveRecentDirReader::new(&source_root, "TeslaCam/MissingRecent", 0);
        let listed = missing.list(0).expect("missing directory should be empty");
        assert!(listed.is_empty());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn live_recent_dir_reader_skips_broken_mp4_symlink() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        fs::create_dir_all(&source_root).expect("create source root");
        let recent_dir = source_root.join("TeslaCam/RecentClips");
        fs::create_dir_all(&recent_dir).expect("create recent dir");
        write_file(&recent_dir.join("a.mp4"), b"123");
        symlink("missing-target.mp4", recent_dir.join("broken.mp4"))
            .expect("create broken symlink");

        let reader = LiveRecentDirReader::new(&source_root, "TeslaCam/RecentClips", 0);
        let files = reader.list(0).expect("list should skip broken symlink");
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].name, "a.mp4");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn archive_recent_once_lands_bytes_and_registers_archive_relative_file_refs() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");

        let front_bytes = b"front-bytes";
        let back_bytes = b"back-bytes-longer";
        let recent_dir = source_root.join("TeslaCam/RecentClips");
        write_file(
            &recent_dir.join("2026-06-19_10-00-00-front.mp4"),
            front_bytes,
        );
        write_file(
            &recent_dir.join("2026-06-19_10-00-00-back.mp4"),
            back_bytes,
        );

        let reader = LiveRecentDirReader::new(&source_root, "TeslaCam/RecentClips", 0);
        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let register = CapturingRegister::default();
        let mut gatherer = RecentFactsGatherer::new(2);
        let mut state = DriverState::new();
        let now_epoch_s = 1_750_000_000;

        let first = archive_recent_once(
            &mut gatherer,
            0,
            "TeslaCam/RecentClips",
            &reader,
            &store,
            &register,
            &mut state,
            now_epoch_s,
        )
        .expect("first archive pass should succeed");
        assert_eq!(first.observed, 0);
        assert_eq!(first.registered, 0);

        let second = archive_recent_once(
            &mut gatherer,
            0,
            "TeslaCam/RecentClips",
            &reader,
            &store,
            &register,
            &mut state,
            now_epoch_s,
        )
        .expect("second archive pass should emit and register");
        assert_eq!(second.observed, 1);
        assert_eq!(second.registered, 1);

        assert_archived_bytes(&archive_root, FRONT_REF, front_bytes);
        assert_archived_bytes(&archive_root, BACK_REF, back_bytes);

        let calls = register.calls.borrow();
        assert_eq!(calls.len(), 1);
        let reg = &calls[0];
        assert_eq!(reg.canonical_key, "0:TeslaCam/RecentClips/2026-06-19_10-00-00");
        assert_eq!(reg.partition, "slot0");
        assert_eq!(reg.folder_class, "RecentClips");
        assert_eq!(reg.started_at, reg.ended_at);
        assert!(reg.started_at > 0);
        assert!(reg.duration_s.is_none());
        assert_eq!(reg.archive.archived_at, now_epoch_s);
        assert_eq!(reg.archive.path, ARCHIVE_PATH);
        assert_eq!(reg.archive.file_count, 2);
        assert_eq!(
            reg.archive.size_bytes,
            i64_len(front_bytes) + i64_len(back_bytes)
        );
        assert_registered_angles(reg, &archive_root, front_bytes, back_bytes);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn path_jail_rejects_parent_components() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");
        write_file(&root.join("escape"), b"escape");

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let source_err = store
            .source_identity("../escape")
            .expect_err("source path must be rejected");
        assert_eq!(source_err.kind(), std::io::ErrorKind::InvalidInput);

        let copy_err = store
            .copy_and_hash_dest("../escape", "RecentClips/clip.mp4")
            .expect_err("copy path must be rejected");
        assert_eq!(copy_err.kind(), std::io::ErrorKind::InvalidInput);
        assert!(
            !archive_root.join("RecentClips/clip.mp4").exists(),
            "should not write outside jailed roots"
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn copy_missing_source_returns_error_without_temp_or_dest() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let err = store
            .copy_and_hash_dest("RecentClips/missing.mp4", "RecentClips/out.mp4")
            .expect_err("missing source should fail");
        assert_eq!(err.kind(), std::io::ErrorKind::NotFound);

        let out_path = archive_root.join("RecentClips/out.mp4");
        assert!(
            !out_path.exists(),
            "dest should not exist after copy failure"
        );
        let temp_parent = archive_root.join("RecentClips");
        if temp_parent.exists() {
            for entry in fs::read_dir(&temp_parent).expect("read temp parent") {
                let entry = entry.expect("read dir entry");
                let name = entry.file_name().to_string_lossy().into_owned();
                assert!(
                    !name.contains(".tmp-"),
                    "temp file should be cleaned up: {name}"
                );
            }
        }

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn path_jail_rejects_symlink_escape() {
        let root = new_temp_dir();
        let source_root = root.join("source");
        let archive_root = root.join("archive");
        let outside = root.join("outside");
        fs::create_dir_all(&source_root).expect("create source root");
        fs::create_dir_all(&archive_root).expect("create archive root");
        fs::create_dir_all(&outside).expect("create outside root");
        write_file(&outside.join("escape.mp4"), b"outside");
        write_file(&source_root.join("in.mp4"), b"inside");

        symlink(&outside, source_root.join("symlink-out")).expect("create source symlink");
        symlink(&outside, archive_root.join("symlink-out")).expect("create dest symlink");

        let store = LiveArchiveStore::new(&source_root, &archive_root);
        let source_err = store
            .source_identity("symlink-out/escape.mp4")
            .expect_err("symlink source escape must be rejected");
        assert_eq!(source_err.kind(), std::io::ErrorKind::InvalidInput);

        let dest_err = store
            .copy_and_hash_dest("in.mp4", "symlink-out/escape.mp4")
            .expect_err("symlink destination escape must be rejected");
        assert_eq!(dest_err.kind(), std::io::ErrorKind::InvalidInput);

        let nested_err = store
            .copy_and_hash_dest("in.mp4", "symlink-out/newdir/escape.mp4")
            .expect_err("symlink destination with missing nested dir must be rejected");
        assert_eq!(nested_err.kind(), std::io::ErrorKind::InvalidInput);
        assert_eq!(
            fs::read(outside.join("escape.mp4")).expect("read outside file"),
            b"outside"
        );
        assert!(
            !outside.join("newdir").exists(),
            "must not create directories outside archive jail"
        );

        let _ = fs::remove_dir_all(root);
    }
}
