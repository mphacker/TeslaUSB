use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::os::fd::AsRawFd;

use crate::{CredsError, SALT_LEN};

/// Filename for the encrypted credential blob.
pub const CLOUD_PROVIDER_CREDS_FILENAME: &str = "cloud_provider_creds.bin";
/// Filename for the credential KDF salt.
pub const TESLA_SALT_FILENAME: &str = "tesla_salt.bin";

/// Read `tesla_salt.bin`, creating it once if absent.
///
/// New files are generated as 32 CSPRNG bytes, mode `0600`.
///
/// # Errors
///
/// Returns [`CredsError`] when RNG or I/O fails, or when an existing file is not
/// exactly 32 bytes.
pub fn read_or_create_salt(path: &Path) -> Result<[u8; SALT_LEN], CredsError> {
    match read_salt(path) {
        Ok(salt) => Ok(salt),
        Err(CredsError::Io(err)) if err.kind() == std::io::ErrorKind::NotFound => {
            create_salt_file(path)
        }
        Err(other) => Err(other),
    }
}

/// Read an existing `tesla_salt.bin` file.
///
/// # Errors
///
/// Returns [`CredsError`] if the file cannot be read or is not 32 bytes.
pub fn read_salt(path: &Path) -> Result<[u8; SALT_LEN], CredsError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mode = std::fs::metadata(path)?.permissions().mode() & 0o777;
        if mode & 0o077 != 0 {
            return Err(CredsError::InsecureSaltPermissions { mode });
        }
    }
    let bytes = std::fs::read(path)?;
    if bytes.len() != SALT_LEN {
        return Err(CredsError::InvalidSaltLength(bytes.len()));
    }
    let mut salt = [0_u8; SALT_LEN];
    salt.copy_from_slice(&bytes);
    Ok(salt)
}

/// Read an encrypted credential blob file.
///
/// # Errors
///
/// Returns [`CredsError`] if the file cannot be read.
pub fn read_blob(path: &Path) -> Result<Vec<u8>, CredsError> {
    Ok(std::fs::read(path)?)
}

/// Atomically write an encrypted credential blob:
/// sibling temp file (`0600`) → file `fsync` → rename → directory `fsync`.
///
/// # Errors
///
/// Returns [`CredsError`] if any write/sync/rename step fails.
pub fn write_blob_atomic(path: &Path, blob: &[u8]) -> Result<(), CredsError> {
    write_atomic(path, blob)
}

/// Run `action` while holding an exclusive credential lock for `creds_dir`.
///
/// On Unix, this acquires an exclusive `flock` on `<creds_dir>/.creds.lock`,
/// creating the lock file with mode `0600` when absent. On non-Unix build
/// hosts, this is a no-op lock used only so Windows builds still compile.
///
/// # Errors
///
/// Returns [`CredsError`] if lock setup or acquisition fails.
pub fn with_creds_lock<T>(creds_dir: &Path, action: impl FnOnce() -> T) -> Result<T, CredsError> {
    #[cfg(unix)]
    let _lock = CredsLockGuard::acquire(creds_dir)?;
    #[cfg(not(unix))]
    let _ = creds_dir;

    let result = action();
    Ok(result)
}

#[cfg(unix)]
struct CredsLockGuard {
    file: File,
}

#[cfg(unix)]
impl CredsLockGuard {
    fn acquire(creds_dir: &Path) -> Result<Self, CredsError> {
        use std::os::unix::fs::OpenOptionsExt;

        std::fs::create_dir_all(creds_dir)?;
        let lock_path = creds_dir.join(".creds.lock");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(lock_path)?;
        lock_file_exclusive(&file)?;
        Ok(Self { file })
    }
}

#[cfg(unix)]
impl Drop for CredsLockGuard {
    fn drop(&mut self) {
        let _ = unlock_file(&self.file);
    }
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn lock_file_exclusive(file: &File) -> Result<(), CredsError> {
    // SAFETY: `flock` is called with a valid fd owned by `file`; operation is
    // synchronous and does not outlive the file handle.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
    if rc == 0 {
        Ok(())
    } else {
        Err(CredsError::Io(std::io::Error::last_os_error()))
    }
}

#[cfg(unix)]
#[allow(unsafe_code)]
fn unlock_file(file: &File) -> Result<(), CredsError> {
    // SAFETY: same fd validity guarantees as lock acquisition.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    if rc == 0 {
        Ok(())
    } else {
        Err(CredsError::Io(std::io::Error::last_os_error()))
    }
}

fn create_salt_file(path: &Path) -> Result<[u8; SALT_LEN], CredsError> {
    let parent = parent_dir(path);
    std::fs::create_dir_all(parent)?;

    let mut salt = [0_u8; SALT_LEN];
    getrandom::getrandom(&mut salt).map_err(|err| CredsError::Random(err.to_string()))?;

    let temp = temp_path(path)?;
    let mut file = open_secure_temp(&temp)?;
    file.write_all(&salt)?;
    file.sync_all()?;
    drop(file);

    match std::fs::hard_link(&temp, path) {
        Ok(()) => {
            let _ = std::fs::remove_file(&temp);
            sync_dir(parent)?;
            Ok(salt)
        }
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
            let _ = std::fs::remove_file(&temp);
            read_salt(path)
        }
        Err(err) => {
            let _ = std::fs::remove_file(&temp);
            Err(CredsError::Io(err))
        }
    }
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), CredsError> {
    let parent = parent_dir(path);
    std::fs::create_dir_all(parent)?;
    let temp = temp_path(path)?;
    let mut file = open_secure_temp(&temp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temp, path)?;
    sync_dir(parent)?;
    Ok(())
}

fn open_secure_temp(path: &Path) -> Result<File, CredsError> {
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    // The 0600 creation mode is the security control on the real (Linux) target.
    // Gated only so the crate still COMPILES on a Windows dev host — webd depends
    // on it, and the SPA UAT harness builds webd natively there.
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;

        opts.mode(0o600);
    }
    Ok(opts.open(path)?)
}

fn parent_dir(path: &Path) -> &Path {
    path.parent().unwrap_or_else(|| Path::new("."))
}

fn sync_dir(path: &Path) -> Result<(), CredsError> {
    let dir = File::open(path)?;
    dir.sync_all()?;
    Ok(())
}

fn temp_path(path: &Path) -> Result<PathBuf, CredsError> {
    let name = path
        .file_name()
        .ok_or(CredsError::InvalidBlob("blob path has no file name"))?;
    let mut rand = [0_u8; 4];
    getrandom::getrandom(&mut rand).map_err(|err| CredsError::Random(err.to_string()))?;
    let temp_name = format!(
        ".{}.{}.tmp{:02x}{:02x}{:02x}{:02x}",
        name.to_string_lossy(),
        std::process::id(),
        rand[0],
        rand[1],
        rand[2],
        rand[3]
    );
    Ok(path.with_file_name(temp_name))
}

#[cfg(all(test, unix))]
pub(crate) fn file_mode(path: &Path) -> Result<u32, CredsError> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = std::fs::metadata(path)?;
    Ok(metadata.permissions().mode() & 0o777)
}
