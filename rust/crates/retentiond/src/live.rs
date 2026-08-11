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
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::{cell::RefCell, collections::HashMap};

use retentiond::archive::ArchiveStore;
use retentiond::delete::{ArchiveDeleteOps, ClaimResult, DeleteRequest, IndexClient, RandGen};
use retentiond::durability::{canonicalize_under_root, make_temp_path, sync_dir, sync_dir_chain};
use retentiond::governor::Statfs;
use retentiond::io::{ContentHash, FileIdentity, FsStat};
use retentiond::lease::DeleteState;
use retentiond::read_client::{MAX_READ_LEN, ReadFileClient, read_full_file_to_writer};
use retentiond::serve::{Catalog, RecoveryRow};
use retentiond::time::{BootId, Clock, MonoMs};
use retentiond::value::{EvictionItem, EvictionKind, Recency};
use retentiond::{durability::Durability, io::ArchiveItemId};
use sha2::{Digest, Sha256};

use retentiond::index_delete_client::{DeleteWireRequest, DeleteWireResponse, IndexDeleteClient};

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

/// Live `indexd` delete-state transition client for the single-deleter protocol.
pub(crate) struct LiveIndexClient {
    client: Rc<IndexDeleteClient>,
}

impl LiveIndexClient {
    /// Build a live delete-state transition client.
    #[must_use]
    pub(crate) fn new(client: Rc<IndexDeleteClient>) -> Self {
        Self { client }
    }
}

impl IndexClient for LiveIndexClient {
    fn claim_archive_delete(&self, id: ArchiveItemId) -> ClaimResult {
        let req = DeleteWireRequest::ClaimEvictionCandidate {
            id: id.0,
            recency_floor_epoch: self.client.recency_floor_epoch(),
            allow_undurable: self.client.allow_undurable(),
        };
        match self.client.send_delete_request(&req) {
            Ok(DeleteWireResponse::Claimed {}) => ClaimResult::Claimed,
            Ok(DeleteWireResponse::ClaimDenied { reason }) => ClaimResult::Denied { reason },
            Ok(DeleteWireResponse::NotFound {}) => ClaimResult::NotFound,
            Ok(
                DeleteWireResponse::Error { message } | DeleteWireResponse::Rejected { message },
            ) => ClaimResult::Denied { reason: message },
            Ok(other) => ClaimResult::Denied {
                reason: format!("unexpected claim response: {other:?}"),
            },
            Err(err) => ClaimResult::Denied {
                reason: format!("claim transport failure: {err}"),
            },
        }
    }

    fn mark_deleting(&self, id: ArchiveItemId) -> io::Result<()> {
        expect_acked(
            self.client
                .send_delete_request(&DeleteWireRequest::MarkArchiveDeleting { id: id.0 })?,
            "mark_archive_deleting",
        )
    }

    fn mark_deleted(&self, id: ArchiveItemId, bytes_freed: u64) -> io::Result<()> {
        let bytes_freed = match i64::try_from(bytes_freed) {
            Ok(value) => value,
            Err(_) => i64::MAX,
        };
        expect_acked(
            self.client
                .send_delete_request(&DeleteWireRequest::MarkArchiveDeleted {
                    id: id.0,
                    bytes_freed,
                })?,
            "mark_archive_deleted",
        )
    }

    fn release_delete_claim(&self, id: ArchiveItemId) -> io::Result<()> {
        expect_acked(
            self.client
                .send_delete_request(&DeleteWireRequest::ReleaseArchiveDeleteClaim { id: id.0 })?,
            "release_archive_delete_claim",
        )
    }

    fn quarantine(&self, id: ArchiveItemId, reason: &str) -> io::Result<()> {
        expect_acked(
            self.client
                .send_delete_request(&DeleteWireRequest::QuarantineArchiveItem {
                    id: id.0,
                    reason: reason.to_owned(),
                })?,
            "quarantine_archive_item",
        )
    }
}

fn expect_acked(response: DeleteWireResponse, op: &str) -> io::Result<()> {
    match response {
        DeleteWireResponse::Acked {} => Ok(()),
        DeleteWireResponse::Error { message } => {
            Err(io::Error::other(format!("indexd {op} error: {message}")))
        }
        DeleteWireResponse::Rejected { message } => {
            Err(io::Error::other(format!("indexd {op} rejected: {message}")))
        }
        other => Err(io::Error::other(format!(
            "unexpected indexd {op} response: {other:?}"
        ))),
    }
}

/// Live `Catalog` seam backed by `indexd` delete-path verbs.
///
/// The same shared [`IndexDeleteClient`] instance is used by both
/// [`LiveCatalog`] and [`LiveIndexClient`] so `set_cycle_context(...)` applies to
/// list and claim paths together.
pub(crate) struct LiveCatalog {
    client: Rc<IndexDeleteClient>,
    archive_root: PathBuf,
    trash_dir: String,
    delete_cache: RefCell<HashMap<i64, (String, u64)>>,
}

impl LiveCatalog {
    /// Build a live governor delete-path catalog seam.
    #[must_use]
    pub(crate) fn new(
        client: Rc<IndexDeleteClient>,
        archive_root: impl Into<PathBuf>,
        trash_dir: impl Into<String>,
    ) -> Self {
        Self {
            client,
            archive_root: archive_root.into(),
            trash_dir: trash_dir.into(),
            delete_cache: RefCell::new(HashMap::new()),
        }
    }
}

impl Catalog for LiveCatalog {
    fn record_verified_pass(
        &self,
        _folder_key: &str,
        _pass: &retentiond::archive::VerifiedArchivePass,
    ) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "LiveCatalog is the governor delete-path seam; verified-pass/mirror bookkeeping is owned by the phase-1 archive driver",
        ))
    }

    fn eviction_items(&self) -> io::Result<Vec<EvictionItem>> {
        let rows = self.client.list_eviction_candidates()?;
        let mut out = Vec::new();
        let mut cache = self.delete_cache.borrow_mut();
        cache.clear();
        for row in rows {
            if row.folder_class != "RecentClips" {
                continue;
            }
            if validate_source_rel_path(&row.path).is_err() {
                continue;
            }
            let abs = jailed_join(&self.archive_root, &row.path)?;
            let size = u64::try_from(row.size_bytes).ok().map_or(0, |value| value);
            cache.insert(row.id, (abs.to_string_lossy().into_owned(), size));
            out.push(EvictionItem {
                id: ArchiveItemId(row.id),
                kind: EvictionKind::RecentMirror,
                durability: Durability::Undurable,
                sentry_flood: false,
                size,
                recency: Recency::VeryOld,
                user_save: false,
                impact_event: false,
                has_telemetry: false,
                event_adjacent: false,
                duplicate_cluster: false,
                user_marked_disposable: false,
                pinned: false,
                leased: false,
                in_grace: false,
                quarantined: false,
                inside_disk_img: false,
            });
        }
        Ok(out)
    }

    fn delete_request(&self, id: ArchiveItemId) -> io::Result<Option<DeleteRequest>> {
        let cache = self.delete_cache.borrow();
        if let Some((source_path, size_bytes)) = cache.get(&id.0) {
            return Ok(Some(DeleteRequest {
                id,
                source_path: source_path.clone(),
                size_bytes: *size_bytes,
            }));
        }
        Ok(None)
    }

    fn recovery_rows(&self) -> io::Result<Vec<RecoveryRow>> {
        let rows = self.client.list_recovery_rows()?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            validate_source_rel_path(&row.path)?;
            let source_path = jailed_join(&self.archive_root, &row.path)?
                .to_string_lossy()
                .into_owned();
            let delete_state = parse_delete_state(&row.delete_state)?;
            let trash_path = if let Some(delete_gen) = row.delete_gen {
                let gen_token = u128::from_str_radix(&delete_gen, 16).map_err(|err| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("invalid delete_gen hex for id {}: {err}", row.id),
                    )
                })?;
                retentiond::delete::trash_path(&self.trash_dir, ArchiveItemId(row.id), gen_token)
            } else {
                String::new()
            };
            let size_bytes = u64::try_from(row.size_bytes).ok().map_or(0, |value| value);
            out.push(RecoveryRow {
                id: ArchiveItemId(row.id),
                delete_state,
                source_path,
                trash_path,
                size_bytes,
            });
        }
        Ok(out)
    }

    fn mark_recent_archived(&self, _key: &str) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "LiveCatalog is the governor delete-path seam; verified-pass/mirror bookkeeping is owned by the phase-1 archive driver",
        ))
    }
}

fn parse_delete_state(raw: &str) -> io::Result<DeleteState> {
    match raw {
        "LIVE" => Ok(DeleteState::Live),
        "DELETE_CLAIMED" => Ok(DeleteState::DeleteClaimed),
        "DELETING" => Ok(DeleteState::Deleting),
        "DELETED" => Ok(DeleteState::Deleted),
        "DELETE_FAILED" => Ok(DeleteState::DeleteFailed),
        "QUARANTINED" => Ok(DeleteState::Quarantined),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unknown delete_state: {raw}"),
        )),
    }
}

/// Live filesystem delete ops for the crash-safe single-deleter protocol.
pub(crate) struct LiveArchiveDeleteOps {
    archive_root: PathBuf,
}

impl LiveArchiveDeleteOps {
    /// Build a live archive delete-ops seam.
    #[must_use]
    pub(crate) fn new(archive_root: impl Into<PathBuf>) -> Self {
        Self {
            archive_root: archive_root.into(),
        }
    }
}

impl ArchiveDeleteOps for LiveArchiveDeleteOps {
    fn exists(&self, path: &str) -> bool {
        fs::symlink_metadata(path).is_ok()
    }

    fn rename_into_trash(&self, src: &str, dst: &str) -> io::Result<()> {
        let src_path = validate_delete_path_in_jail(&self.archive_root, Path::new(src))?;
        let dst_path = validate_delete_path_in_jail(&self.archive_root, Path::new(dst))?;
        fs::rename(src_path, dst_path)
    }

    fn fsync_parent(&self, path: &str) -> io::Result<()> {
        let path = validate_delete_path_in_jail(&self.archive_root, Path::new(path))?;
        let parent = path.parent().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("path has no parent for fsync: {}", path.display()),
            )
        })?;
        validate_archive_parent_path(&self.archive_root, parent)?;
        File::open(parent)?.sync_all()
    }

    fn recursive_delete(&self, path: &str) -> io::Result<()> {
        let path = validate_delete_path_in_jail(&self.archive_root, Path::new(path))?;
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_dir() {
            fs::remove_dir_all(path)
        } else {
            fs::remove_file(path)
        }
    }
}

fn validate_delete_path_in_jail(root: &Path, path: &Path) -> io::Result<PathBuf> {
    if !path.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("path must be absolute: {}", path.display()),
        ));
    }
    let rel = path.strip_prefix(root).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "path is outside archive root (root={}, path={})",
                root.display(),
                path.display()
            ),
        )
    })?;
    for component in rel.components() {
        if !matches!(component, Component::Normal(_)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("path escapes archive root: {}", path.display()),
            ));
        }
    }
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("path has no parent: {}", path.display()),
        )
    })?;
    validate_archive_parent_path(root, parent)?;
    Ok(path.to_path_buf())
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

    #[cfg(all(test, unix))]
    #[allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::indexing_slicing
    )]
    mod delete_path_tests {
        use std::fs;
        use std::io::{self, Read, Write};
        use std::os::unix::net::UnixListener;
        use std::path::PathBuf;
        use std::rc::Rc;
        use std::sync::atomic::{AtomicU64, Ordering};
        use std::thread;

        use super::super::{LiveArchiveDeleteOps, LiveCatalog, LiveIndexClient};
        use retentiond::delete::IndexClient;
        use retentiond::index_delete_client::{
            DeleteWireResponse, EvictionCandidateWire, IndexDeleteClient, RecoveryRowWire,
        };
        use retentiond::io::ArchiveItemId;
        use retentiond::lease::DeleteState;
        use retentiond::serve::Catalog;
        use retentiond::value::EvictionKind;
        use retentiond::{delete::ArchiveDeleteOps, durability::Durability};

        static TEST_COUNTER: AtomicU64 = AtomicU64::new(0);
        const MAX_REQUEST_FRAME: u32 = 64 * 1024;

        fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
            let mut len_buf = [0_u8; 4];
            stream.read_exact(&mut len_buf)?;
            let len = u32::from_le_bytes(len_buf);
            if len > cap {
                return Err(io::Error::other("frame too large"));
            }
            let mut payload = vec![0_u8; len as usize];
            stream.read_exact(&mut payload)?;
            Ok(payload)
        }

        fn write_frame(stream: &mut impl Write, payload: &[u8], cap: u32) -> io::Result<()> {
            if payload.len() > cap as usize {
                return Err(io::Error::other("frame too large"));
            }
            let len = u32::try_from(payload.len())
                .map_err(|_| io::Error::other("frame exceeds u32 length"))?;
            stream.write_all(&len.to_le_bytes())?;
            stream.write_all(payload)?;
            stream.flush()
        }

        fn new_temp_dir() -> PathBuf {
            let unique = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
            let name = format!("retentiond-live-delete-{}-{unique}", std::process::id());
            let dir = std::env::temp_dir().join(name);
            fs::create_dir_all(&dir).expect("create temp dir");
            dir
        }

        #[test]
        fn claim_maps_claimed() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let payload = serde_json::to_vec(&DeleteWireResponse::Claimed {}).expect("encode");
                write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let client = LiveIndexClient::new(shared);
            assert_eq!(
                client.claim_archive_delete(ArchiveItemId(7)),
                retentiond::delete::ClaimResult::Claimed
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn claim_maps_denied() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let payload = serde_json::to_vec(&DeleteWireResponse::ClaimDenied {
                    reason: "leased".to_owned(),
                })
                .expect("encode");
                write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let client = LiveIndexClient::new(shared);
            assert_eq!(
                client.claim_archive_delete(ArchiveItemId(7)),
                retentiond::delete::ClaimResult::Denied {
                    reason: "leased".to_owned()
                }
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn claim_maps_notfound() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let payload = serde_json::to_vec(&DeleteWireResponse::NotFound {}).expect("encode");
                write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let client = LiveIndexClient::new(shared);
            assert_eq!(
                client.claim_archive_delete(ArchiveItemId(7)),
                retentiond::delete::ClaimResult::NotFound
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn claim_transport_error_fails_closed() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                drop(stream);
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let client = LiveIndexClient::new(shared);
            let got = client.claim_archive_delete(ArchiveItemId(7));
            match got {
                retentiond::delete::ClaimResult::Denied { .. } => {}
                other => panic!("expected denied, got {other:?}"),
            }
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn claim_sends_cycle_context() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let req_json = String::from_utf8(payload).expect("utf8");
                assert_eq!(
                    req_json,
                    "{\"cmd\":\"claim_eviction_candidate\",\"id\":7,\"recency_floor_epoch\":12345,\"allow_undurable\":true}"
                );
                let payload = serde_json::to_vec(&DeleteWireResponse::Claimed {}).expect("encode");
                write_frame(&mut stream, &payload, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            shared.set_cycle_context(12345, true);
            let client = LiveIndexClient::new(shared);
            assert_eq!(
                client.claim_archive_delete(ArchiveItemId(7)),
                retentiond::delete::ClaimResult::Claimed
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn mark_deleting_deleted_release_quarantine_ack_ok_else_err() {
            let temp_dir = new_temp_dir();
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let ack = serde_json::to_vec(&DeleteWireResponse::Acked {}).expect("encode");
                let err = serde_json::to_vec(&DeleteWireResponse::Error {
                    message: "nope".to_owned(),
                })
                .expect("encode");

                let (mut stream, _) = listener.accept().expect("accept 1");
                let p1 = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read 1");
                assert_eq!(
                    String::from_utf8(p1).expect("utf8"),
                    "{\"cmd\":\"mark_archive_deleting\",\"id\":7}"
                );
                write_frame(&mut stream, &ack, MAX_REQUEST_FRAME).expect("write 1");

                let (mut stream, _) = listener.accept().expect("accept 2");
                let p2 = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read 2");
                assert_eq!(
                    String::from_utf8(p2).expect("utf8"),
                    "{\"cmd\":\"mark_archive_deleted\",\"id\":7,\"bytes_freed\":99}"
                );
                write_frame(&mut stream, &ack, MAX_REQUEST_FRAME).expect("write 2");

                let (mut stream, _) = listener.accept().expect("accept 3");
                let p3 = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read 3");
                assert_eq!(
                    String::from_utf8(p3).expect("utf8"),
                    "{\"cmd\":\"release_archive_delete_claim\",\"id\":7}"
                );
                write_frame(&mut stream, &ack, MAX_REQUEST_FRAME).expect("write 3");

                let (mut stream, _) = listener.accept().expect("accept 4");
                let p4 = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read 4");
                assert_eq!(
                    String::from_utf8(p4).expect("utf8"),
                    "{\"cmd\":\"quarantine_archive_item\",\"id\":7,\"reason\":\"boom\"}"
                );
                write_frame(&mut stream, &err, MAX_REQUEST_FRAME).expect("write 4");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let client = LiveIndexClient::new(shared);
            assert!(client.mark_deleting(ArchiveItemId(7)).is_ok());
            assert!(client.mark_deleted(ArchiveItemId(7), 99).is_ok());
            assert!(client.release_delete_claim(ArchiveItemId(7)).is_ok());
            assert!(client.quarantine(ArchiveItemId(7), "boom").is_err());
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn eviction_items_maps_and_orders() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive root");
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                assert_eq!(
                    String::from_utf8(payload).expect("utf8"),
                    "{\"cmd\":\"list_eviction_candidates\",\"recency_floor_epoch\":9223372036854775807,\"allow_undurable\":false,\"limit\":256}"
                );
                let response = DeleteWireResponse::EvictionCandidates {
                    items: vec![
                        EvictionCandidateWire {
                            id: 11,
                            path: "RecentClips/older/1".to_owned(),
                            size_bytes: 10,
                            archived_at: 1,
                            folder_class: "RecentClips".to_owned(),
                        },
                        EvictionCandidateWire {
                            id: 99,
                            path: "SentryClips/skip/2".to_owned(),
                            size_bytes: 20,
                            archived_at: 2,
                            folder_class: "SentryClips".to_owned(),
                        },
                        EvictionCandidateWire {
                            id: 12,
                            path: "RecentClips/older/3".to_owned(),
                            size_bytes: 30,
                            archived_at: 3,
                            folder_class: "RecentClips".to_owned(),
                        },
                    ],
                };
                let encoded = serde_json::to_vec(&response).expect("encode");
                write_frame(&mut stream, &encoded, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let catalog = LiveCatalog::new(shared, &archive_root, "/archive/.retention-trash");
            let items = catalog.eviction_items().expect("eviction items");
            assert_eq!(items.len(), 2);
            assert_eq!(items[0].id, ArchiveItemId(11));
            assert_eq!(items[1].id, ArchiveItemId(12));
            assert_eq!(items[0].kind, EvictionKind::RecentMirror);
            assert_eq!(items[1].kind, EvictionKind::RecentMirror);
            assert_eq!(items[0].durability, Durability::Undurable);
            assert_eq!(items[1].durability, Durability::Undurable);
            let req1 = catalog
                .delete_request(ArchiveItemId(11))
                .expect("delete request")
                .expect("exists");
            let req2 = catalog
                .delete_request(ArchiveItemId(12))
                .expect("delete request")
                .expect("exists");
            assert_eq!(
                req1.source_path,
                archive_root.join("RecentClips/older/1").to_string_lossy()
            );
            assert_eq!(
                req2.source_path,
                archive_root.join("RecentClips/older/3").to_string_lossy()
            );
            assert!(
                catalog
                    .delete_request(ArchiveItemId(99))
                    .expect("delete request")
                    .is_none()
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn eviction_items_rejects_escaping_path() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive root");
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let response = DeleteWireResponse::EvictionCandidates {
                    items: vec![EvictionCandidateWire {
                        id: 21,
                        path: "../etc/x".to_owned(),
                        size_bytes: 10,
                        archived_at: 1,
                        folder_class: "RecentClips".to_owned(),
                    }],
                };
                let encoded = serde_json::to_vec(&response).expect("encode");
                write_frame(&mut stream, &encoded, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let catalog = LiveCatalog::new(shared, &archive_root, "/archive/.retention-trash");
            let items = catalog.eviction_items().expect("eviction items");
            assert!(items.is_empty());
            assert!(
                catalog
                    .delete_request(ArchiveItemId(21))
                    .expect("delete request")
                    .is_none()
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn delete_request_miss_is_none() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive root");
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let response = DeleteWireResponse::EvictionCandidates { items: vec![] };
                let encoded = serde_json::to_vec(&response).expect("encode");
                write_frame(&mut stream, &encoded, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let catalog = LiveCatalog::new(shared, &archive_root, "/archive/.retention-trash");
            let _ = catalog.eviction_items().expect("seed cache");
            assert!(
                catalog
                    .delete_request(ArchiveItemId(404))
                    .expect("delete request")
                    .is_none()
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn recovery_rows_maps_state_and_trash() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive root");
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                assert_eq!(
                    String::from_utf8(payload).expect("utf8"),
                    "{\"cmd\":\"list_recovery_rows\"}"
                );
                let response = DeleteWireResponse::RecoveryRows {
                    rows: vec![RecoveryRowWire {
                        id: 5,
                        delete_state: "DELETING".to_owned(),
                        path: "RecentClips/older/5".to_owned(),
                        size_bytes: 42,
                        delete_gen: Some("0000000000000000000000000000000f".to_owned()),
                    }],
                };
                let encoded = serde_json::to_vec(&response).expect("encode");
                write_frame(&mut stream, &encoded, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let catalog = LiveCatalog::new(shared, &archive_root, "/archive/.retention-trash");
            let rows = catalog.recovery_rows().expect("recovery rows");
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].delete_state, DeleteState::Deleting);
            assert_eq!(
                rows[0].source_path,
                archive_root.join("RecentClips/older/5").to_string_lossy()
            );
            assert_eq!(
                rows[0].trash_path,
                "/archive/.retention-trash/5.0000000000000000000000000000000f.deleting"
            );
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);

            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive root");
            let socket_path = temp_dir.join("indexd.sock");
            let listener = UnixListener::bind(&socket_path).expect("bind listener");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept");
                let _payload = read_frame(&mut stream, MAX_REQUEST_FRAME).expect("read request");
                let response = DeleteWireResponse::RecoveryRows {
                    rows: vec![RecoveryRowWire {
                        id: 6,
                        delete_state: "UNKNOWN_STATE".to_owned(),
                        path: "RecentClips/older/6".to_owned(),
                        size_bytes: 1,
                        delete_gen: None,
                    }],
                };
                let encoded = serde_json::to_vec(&response).expect("encode");
                write_frame(&mut stream, &encoded, MAX_REQUEST_FRAME).expect("write response");
            });
            let shared = Rc::new(IndexDeleteClient::new(socket_path));
            let catalog = LiveCatalog::new(shared, &archive_root, "/archive/.retention-trash");
            assert!(catalog.recovery_rows().is_err());
            server.join().expect("join");
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn rename_into_trash_moves_within_jail() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(archive_root.join("src")).expect("src dir");
            fs::create_dir_all(archive_root.join("trash")).expect("trash dir");
            let src = archive_root.join("src/a.txt");
            let dst = archive_root.join("trash/a.txt");
            fs::write(&src, b"x").expect("write src");

            let ops = LiveArchiveDeleteOps::new(&archive_root);
            ops.rename_into_trash(
                src.to_string_lossy().as_ref(),
                dst.to_string_lossy().as_ref(),
            )
            .expect("rename");
            assert!(!src.exists());
            assert!(dst.exists());
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn rename_rejects_outside_root() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            let outside = temp_dir.join("outside");
            fs::create_dir_all(archive_root.join("src")).expect("src dir");
            fs::create_dir_all(&outside).expect("outside dir");
            let src = archive_root.join("src/a.txt");
            let dst = outside.join("a.txt");
            fs::write(&src, b"x").expect("write src");

            let ops = LiveArchiveDeleteOps::new(&archive_root);
            assert!(
                ops.rename_into_trash(
                    src.to_string_lossy().as_ref(),
                    dst.to_string_lossy().as_ref()
                )
                .is_err()
            );
            assert!(src.exists());
            assert!(!dst.exists());
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn recursive_delete_file_and_dir() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(archive_root.join("dir/sub")).expect("dir");
            let file = archive_root.join("dir/file.txt");
            let dir = archive_root.join("dir/sub");
            fs::write(&file, b"x").expect("file");

            let ops = LiveArchiveDeleteOps::new(&archive_root);
            ops.recursive_delete(file.to_string_lossy().as_ref())
                .expect("delete file");
            assert!(!file.exists());
            ops.recursive_delete(dir.to_string_lossy().as_ref())
                .expect("delete dir");
            assert!(!dir.exists());
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn recursive_delete_rejects_escape() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            let outside = temp_dir.join("outside");
            fs::create_dir_all(&archive_root).expect("archive");
            fs::create_dir_all(&outside).expect("outside");
            let target = outside.join("file.txt");
            fs::write(&target, b"x").expect("file");
            let ops = LiveArchiveDeleteOps::new(&archive_root);
            assert!(
                ops.recursive_delete(target.to_string_lossy().as_ref())
                    .is_err()
            );
            assert!(target.exists());
            let _ = fs::remove_dir_all(temp_dir);
        }

        #[test]
        fn exists_true_false() {
            let temp_dir = new_temp_dir();
            let archive_root = temp_dir.join("archive");
            fs::create_dir_all(&archive_root).expect("archive");
            let file = archive_root.join("exists.txt");
            fs::write(&file, b"x").expect("file");
            let missing = archive_root.join("missing.txt");
            let ops = LiveArchiveDeleteOps::new(&archive_root);
            assert!(ops.exists(file.to_string_lossy().as_ref()));
            assert!(!ops.exists(missing.to_string_lossy().as_ref()));
            let _ = fs::remove_dir_all(temp_dir);
        }
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
