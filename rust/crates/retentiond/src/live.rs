//! Live Unix seam implementations for `retentiond`.
//!
//! This module is intentionally bin-internal; the pure policy core remains in
//! the library.
#![allow(unsafe_code)]
#![allow(dead_code)]

use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use retentiond::archive::ArchiveStore;
use retentiond::delete::RandGen;
use retentiond::durability::{canonicalize_under_root, make_temp_path, sync_dir, sync_dir_chain};
use retentiond::governor::Statfs;
use retentiond::io::{ContentHash, FileIdentity, FsStat};
use retentiond::read_client::{MAX_READ_LEN, ReadFileClient, read_full_file_to_writer};
use retentiond::time::{BootId, Clock, MonoMs};
use sha2::{Digest, Sha256};

pub(crate) struct LiveClock;

impl Clock for LiveClock {
    fn mono_now(&self) -> MonoMs {
        let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
        debug_assert_eq!(
            rc, 0,
            "clock_gettime(CLOCK_MONOTONIC) should not fail with valid args"
        );
        if rc != 0 {
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
        let ptr = unsafe { bytes.as_mut_ptr().add(filled) }.cast::<libc::c_void>();
        let remaining = bytes.len() - filled;
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
    let counter = FALLBACK_COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = u64::from(std::process::id());
    let probe = 0_u8;
    let addr = std::ptr::addr_of!(probe) as usize as u64;
    let lo =
        splitmix64(counter ^ pid.rotate_left(13) ^ addr.rotate_left(29) ^ 0xa076_1d64_78bd_642f);
    let hi = splitmix64(
        counter.wrapping_add(0x9e37_79b9_7f4a_7c15)
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
const READ_WINDOW_LEN: u32 = MAX_READ_LEN;

/// Live Unix `ArchiveStore` seam.
pub(crate) struct LiveArchiveStore {
    archive_root: PathBuf,
    read_client: Box<dyn ReadFileClient>,
}

impl LiveArchiveStore {
    #[must_use]
    pub(crate) fn new(
        read_client: Box<dyn ReadFileClient>,
        archive_root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            archive_root: archive_root.into(),
            read_client,
        }
    }
}

impl ArchiveStore for LiveArchiveStore {
    fn copy_and_hash_dest(&self, src_rel: &str, dest_rel: &str) -> io::Result<ContentHash> {
        retentiond::watchdog::pet();
        validate_source_rel_path(src_rel)?;

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
            let mut writer = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temp_path)?;
            let _identity = read_full_file_to_writer(
                self.read_client.as_ref(),
                src_rel,
                READ_WINDOW_LEN,
                &mut writer,
            )
            .map_err(|err| io::Error::other(err.to_string()))?;
            // Fresh keepalive right before the discrete blocking steps
            // (fsync/rename/dir-sync) that are not inside the chunk loops, so a
            // stalled sync_all under idle-I/O starvation gets a full budget.
            retentiond::watchdog::pet();
            writer.sync_all()?;
            drop(writer);
            retentiond::watchdog::pet();
            fs::rename(&temp_path, &dest_path)?;
            sync_dir(&canonical_parent)?;
            let landed = canonicalize_under_root(&self.archive_root, &dest_path)?;
            hash_file_sha256(&landed)
        })();

        if result.is_err() {
            let _ = fs::remove_file(&temp_path);
        }
        result
    }

    fn remove_dest(&self, dest_rel: &str) -> io::Result<()> {
        let dest_path = jailed_join(&self.archive_root, dest_rel)?;
        match fs::remove_file(dest_path) {
            Ok(()) => Ok(()),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(err) => Err(err),
        }
    }

    fn promote_dest(&self, staging_rel: &str, final_rel: &str) -> io::Result<()> {
        retentiond::watchdog::pet();
        let staging_path = jailed_join(&self.archive_root, staging_rel)?;
        let final_path = jailed_join(&self.archive_root, final_rel)?;
        let final_parent = final_path.parent().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "destination path must include a parent directory",
            )
        })?;
        validate_archive_parent_path(&self.archive_root, final_parent)?;
        fs::create_dir_all(final_parent)?;
        let canonical_final_parent = canonicalize_under_root(&self.archive_root, final_parent)?;
        sync_dir_chain(&self.archive_root, &canonical_final_parent)?;
        fs::rename(staging_path, &final_path)?;
        sync_dir(&canonical_final_parent)
    }

    fn probe_dest_playability(
        &self,
        dest_rel: &str,
    ) -> io::Result<retentiond::probe::ArchivePlayability> {
        let dest_path = jailed_join(&self.archive_root, dest_rel)?;
        let landed = canonicalize_under_root(&self.archive_root, &dest_path)?;
        retentiond::probe::probe_file_playability(&landed)
    }

    fn source_identity(&self, _src_rel: &str) -> io::Result<FileIdentity> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "source identity is provided by ReadFile ClipIdentity; direct source probing is retired",
        ))
    }

    fn list_source_rel_names(&self, _src_dir: &str) -> io::Result<Vec<String>> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "mounted source listing is retired; inventory comes from indexd SQLite candidates",
        ))
    }
}

fn validate_source_rel_path(rel: &str) -> io::Result<()> {
    if rel.is_empty() || rel.as_bytes().contains(&0) || rel.contains('\\') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("invalid source relative path: {rel}"),
        ));
    }
    for component in Path::new(rel).components() {
        match component {
            Component::Normal(_) => {}
            Component::Prefix(_)
            | Component::RootDir
            | Component::CurDir
            | Component::ParentDir => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("source relative path escapes jail: {rel}"),
                ));
            }
        }
    }
    Ok(())
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

fn hash_file_sha256(path: &Path) -> io::Result<ContentHash> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(COPY_BUFFER_SIZE, file);
    hash_reader_sha256(&mut reader)
}

fn hash_reader_sha256(reader: &mut dyn Read) -> io::Result<ContentHash> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; COPY_BUFFER_SIZE];
    loop {
        retentiond::watchdog::pet();
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

pub(crate) struct LiveStatfs;

impl Statfs for LiveStatfs {
    fn statfs(&self, path: &str) -> io::Result<FsStat> {
        let c_path = CString::new(path).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "path contains interior NUL byte",
            )
        })?;
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::stat(c_path.as_ptr(), &mut st) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c_path.as_ptr(), &mut s) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let frsize = if s.f_frsize == 0 {
            s.f_bsize
        } else {
            s.f_frsize
        };
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
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::indexing_slicing)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::fs;
    use std::io;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    use sha2::{Digest, Sha256};

    use super::{LiveArchiveStore, LiveClock, LiveRand, LiveStatfs};
    use retentiond::archive::ArchiveStore;
    use retentiond::archive_driver::{DriverState, archive_recent_once};
    use retentiond::candidates::{Candidate, CandidateAngle, CandidateSource};
    use retentiond::delete::RandGen;
    use retentiond::governor::Statfs;
    use retentiond::io::ContentHash;
    use retentiond::read_client::{
        ClipIdentity, ReadFileClient, ReadFileError, ReadFileOk, ReadFileRequest,
    };
    use retentiond::register_client::{
        ArchiveRegistration, RegisterClient, RegisterError, RegistrationOk,
    };
    use retentiond::time::Clock;

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn new_temp_dir() -> PathBuf {
        let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!("retentiond-live-{}-{unique}", std::process::id());
        let dir = std::env::temp_dir().join(name);
        fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    fn hash_bytes(bytes: &[u8]) -> ContentHash {
        let digest = Sha256::digest(bytes);
        let mut out = [0_u8; 32];
        out.copy_from_slice(&digest);
        ContentHash::new(out)
    }

    fn box32(name: [u8; 4], body: &[u8]) -> Vec<u8> {
        let size = u32::try_from(8 + body.len()).expect("box size");
        let mut out = Vec::with_capacity(8 + body.len());
        out.extend_from_slice(&size.to_be_bytes());
        out.extend_from_slice(&name);
        out.extend_from_slice(body);
        out
    }

    fn playable_mp4_bytes() -> Vec<u8> {
        let mut mdhd = vec![0_u8; 4];
        mdhd.extend_from_slice(&0_u32.to_be_bytes());
        mdhd.extend_from_slice(&0_u32.to_be_bytes());
        mdhd.extend_from_slice(&30_000_u32.to_be_bytes());
        mdhd.extend_from_slice(&90_000_u32.to_be_bytes());
        mdhd.extend_from_slice(&[0_u8; 4]);
        let mdhd = box32(*b"mdhd", &mdhd);
        let mdia = box32(*b"mdia", &mdhd);
        let trak = box32(*b"trak", &mdia);
        let moov = box32(*b"moov", &trak);
        let ftyp = box32(*b"ftyp", b"isom");
        let mdat = box32(*b"mdat", &[0_u8; 32]);
        [ftyp, moov, mdat].concat()
    }

    struct FakeReadClient {
        scripted: RefCell<VecDeque<Result<ReadFileOk, ReadFileError>>>,
        requests: RefCell<Vec<ReadFileRequest>>,
    }

    impl FakeReadClient {
        fn new(scripted: Vec<Result<ReadFileOk, ReadFileError>>) -> Self {
            Self {
                scripted: RefCell::new(scripted.into()),
                requests: RefCell::new(Vec::new()),
            }
        }
    }

    impl ReadFileClient for FakeReadClient {
        fn read_file(&self, req: &ReadFileRequest) -> Result<ReadFileOk, ReadFileError> {
            self.requests.borrow_mut().push(req.clone());
            self.scripted.borrow_mut().pop_front().unwrap_or_else(|| {
                Err(ReadFileError::Decode(
                    "missing scripted response".to_owned(),
                ))
            })
        }
    }

    #[derive(Default)]
    struct FakeCandidates {
        clips: RefCell<Vec<Candidate>>,
    }

    impl CandidateSource for FakeCandidates {
        fn list_candidates(&self) -> io::Result<Vec<Candidate>> {
            Ok(self.clips.borrow().clone())
        }
    }

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

        fn register_quarantined(
            &self,
            reg: &ArchiveRegistration,
        ) -> Result<RegistrationOk, RegisterError> {
            self.register(reg)
        }
    }

    fn make_candidate() -> Candidate {
        Candidate {
            clip_id: 1,
            canonical_key: "0:TeslaCam/RecentClips/2026-06-19_10-00-00".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_700_000_000,
            ended_at: 1_700_000_060,
            duration_s: Some(60),
            source_volume_serial: 0x1234_5678,
            source_fingerprint: "live-test-fingerprint".to_owned(),
            angles: vec![
                CandidateAngle {
                    camera: "front".to_owned(),
                    file_ref: "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4".to_owned(),
                    offset_ms: 0,
                    duration_s: Some(60),
                    size_bytes: 11,
                },
                CandidateAngle {
                    camera: "back".to_owned(),
                    file_ref: "TeslaCam/RecentClips/2026-06-19_10-00-00-back.mp4".to_owned(),
                    offset_ms: 500,
                    duration_s: Some(59),
                    size_bytes: 13,
                },
            ],
        }
    }

    fn identity() -> ClipIdentity {
        ClipIdentity {
            first_cluster: 1,
            total_size: 1024,
            name_hash: 2,
            chain_digest: None,
        }
    }

    #[test]
    fn live_clock_is_monotonic() {
        let clock = LiveClock;
        let first = clock.mono_now();
        let second = clock.mono_now();
        assert!(second.0 >= first.0);
    }

    #[test]
    fn live_clock_boot_id_nonempty() {
        let clock = LiveClock;
        let first = clock.boot_id();
        let second = clock.boot_id();
        assert!(!first.0.is_empty());
        assert_eq!(first.0, second.0);
    }

    #[test]
    fn live_rand_differs_and_nonzero() {
        let rand = LiveRand;
        let first = rand.next_u128();
        let second = rand.next_u128();
        assert_ne!(first, second);
        assert!(!(first == 0 && second == 0));
    }

    #[test]
    fn live_statfs_root_ok() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("/");
        assert!(result.is_ok());
    }

    #[test]
    fn live_statfs_bad_path_err() {
        let statfs = LiveStatfs;
        assert!(statfs.statfs("/nonexistent/teslausb/zzz").is_err());
    }

    #[test]
    fn live_statfs_interior_nul_err() {
        let statfs = LiveStatfs;
        assert!(statfs.statfs("a\0b").is_err());
    }

    #[test]
    fn copy_and_hash_dest_reads_via_readfile_and_hashes_landed_bytes() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        fs::create_dir_all(&archive_root).expect("create archive root");
        let bytes = b"hello-live-read";
        let client = FakeReadClient::new(vec![Ok(ReadFileOk {
            identity: identity(),
            readable_size: bytes.len() as u64,
            eof: true,
            bytes: bytes.to_vec(),
        })]);
        let store = LiveArchiveStore::new(Box::new(client), &archive_root);
        let hash = store
            .copy_and_hash_dest(
                "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
                "RecentClips/2026-06-19/2026-06-19_10-00-00/front.mp4",
            )
            .expect("copy succeeds");
        assert_eq!(hash, hash_bytes(bytes));
        let landed = archive_root.join("RecentClips/2026-06-19/2026-06-19_10-00-00/front.mp4");
        assert_eq!(fs::read(landed).expect("read landed file"), bytes);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn copy_and_hash_dest_streams_multi_window_clip_into_landed_file() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        fs::create_dir_all(&archive_root).expect("create archive root");
        let id = identity();
        let scripted = vec![
            Ok(ReadFileOk {
                identity: id,
                readable_size: 9,
                eof: false,
                bytes: b"abc".to_vec(),
            }),
            Ok(ReadFileOk {
                identity: id,
                readable_size: 9,
                eof: false,
                bytes: b"def".to_vec(),
            }),
            Ok(ReadFileOk {
                identity: id,
                readable_size: 9,
                eof: true,
                bytes: b"ghi".to_vec(),
            }),
        ];
        let store = LiveArchiveStore::new(Box::new(FakeReadClient::new(scripted)), &archive_root);
        let hash = store
            .copy_and_hash_dest(
                "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
                "RecentClips/2026-06-19/2026-06-19_10-00-00/front.mp4",
            )
            .expect("streaming copy succeeds");
        let expected = b"abcdefghi";
        assert_eq!(hash, hash_bytes(expected));
        let landed = archive_root.join("RecentClips/2026-06-19/2026-06-19_10-00-00/front.mp4");
        assert_eq!(fs::read(landed).expect("read landed file"), expected);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn path_jail_rejects_parent_components() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        fs::create_dir_all(&archive_root).expect("create archive root");
        let client = FakeReadClient::new(vec![Ok(ReadFileOk {
            identity: identity(),
            readable_size: 1,
            eof: true,
            bytes: b"x".to_vec(),
        })]);
        let store = LiveArchiveStore::new(Box::new(client), &archive_root);
        let err = store
            .copy_and_hash_dest("../escape.mp4", "RecentClips/out.mp4")
            .expect_err("path traversal should fail");
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn path_jail_rejects_symlink_escape() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        let outside = root.join("outside");
        fs::create_dir_all(&archive_root).expect("create archive root");
        fs::create_dir_all(&outside).expect("create outside");
        symlink(&outside, archive_root.join("symlink-out")).expect("create symlink");
        let client = FakeReadClient::new(vec![Ok(ReadFileOk {
            identity: identity(),
            readable_size: 1,
            eof: true,
            bytes: b"x".to_vec(),
        })]);
        let store = LiveArchiveStore::new(Box::new(client), &archive_root);
        let err = store
            .copy_and_hash_dest(
                "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4",
                "symlink-out/escape.mp4",
            )
            .expect_err("symlink destination escape must be rejected");
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn archive_recent_once_lands_bytes_and_registers_archive_relative_file_refs() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        fs::create_dir_all(&archive_root).expect("create archive root");
        let front_bytes = playable_mp4_bytes();
        let back_bytes = playable_mp4_bytes();
        let id = identity();
        let scripted = vec![
            Ok(ReadFileOk {
                identity: id,
                readable_size: front_bytes.len() as u64,
                eof: true,
                bytes: front_bytes.clone(),
            }),
            Ok(ReadFileOk {
                identity: id,
                readable_size: back_bytes.len() as u64,
                eof: true,
                bytes: back_bytes.clone(),
            }),
        ];
        let store = LiveArchiveStore::new(Box::new(FakeReadClient::new(scripted)), &archive_root);
        let candidates = FakeCandidates::default();
        *candidates.clips.borrow_mut() = vec![make_candidate()];
        let register = CapturingRegister::default();
        let mut state = DriverState::new();

        let report =
            archive_recent_once(&candidates, &store, &register, &mut state, 1_750_000_000).unwrap();
        assert_eq!(report.registered, 1);
        assert_eq!(report.copy_failed, 0);

        let front = archive_root
            .join("RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-front.mp4");
        let back = archive_root
            .join("RecentClips/2026-06-19/2026-06-19_10-00-00/2026-06-19_10-00-00-back.mp4");
        assert_eq!(fs::read(front).expect("read front"), front_bytes);
        assert_eq!(fs::read(back).expect("read back"), back_bytes);

        let calls = register.calls.borrow();
        assert_eq!(calls.len(), 1);
        let reg = &calls[0];
        assert_eq!(
            reg.archive.path,
            "RecentClips/2026-06-19/2026-06-19_10-00-00"
        );
        assert_eq!(reg.angles.len(), 2);
        for angle in &reg.angles {
            assert!(
                angle
                    .file_ref
                    .starts_with("RecentClips/2026-06-19/2026-06-19_10-00-00/")
            );
        }
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn archive_recent_once_changed_mid_copy_aborts_without_registering() {
        let root = new_temp_dir();
        let archive_root = root.join("archive");
        fs::create_dir_all(&archive_root).expect("create archive root");
        let scripted = vec![
            Err(ReadFileError::Changed),
            Ok(ReadFileOk {
                identity: identity(),
                readable_size: 5,
                eof: true,
                bytes: b"other".to_vec(),
            }),
        ];
        let store = LiveArchiveStore::new(Box::new(FakeReadClient::new(scripted)), &archive_root);
        let candidates = FakeCandidates::default();
        *candidates.clips.borrow_mut() = vec![make_candidate()];
        let register = CapturingRegister::default();
        let mut state = DriverState::new();

        let report = archive_recent_once(&candidates, &store, &register, &mut state, 1).unwrap();
        assert_eq!(report.copy_failed, 1);
        assert_eq!(report.registered, 0);
        assert!(register.calls.borrow().is_empty());
        let _ = fs::remove_dir_all(root);
    }
}
