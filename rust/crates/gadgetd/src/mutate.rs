//! Live image-mutation path for the eject-handoff: loop-mount the backing image,
//! apply a validated [`Mutation`], and tear the mount down durably. Implements
//! [`ImageMutator`] for [`LoopMutator`].
//!
//! Every external command (`losetup`/`mount`/`umount`) runs under a hard
//! timeout so a hang on a corrupt filesystem can never leave the LUN ejected
//! forever. The mount happens under a deterministic per-handoff runtime dir so
//! an interrupted handoff is recoverable by scanning, not by guessing temp
//! paths. On success or failure the implementation reports — via
//! [`MutateError::image_released`] — whether the loop + mount were fully torn
//! down, which the state machine uses to decide whether re-presenting is safe.

use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use crate::handoff::{
    ImageMutator, MutateError, Mutation, Partition, is_protected_dir, validate_rel_path,
};

/// Cap on an `InstallFile` source (media assets are MB-scale; refuse anything
/// that would blow the ~5 s handoff window or the card).
const MAX_INSTALL_BYTES: u64 = 256 * 1024 * 1024;

/// Per-phase command timeouts.
pub(crate) const LOSETUP_TIMEOUT: Duration = Duration::from_secs(5);
pub(crate) const MOUNT_TIMEOUT: Duration = Duration::from_secs(10);
pub(crate) const UMOUNT_TIMEOUT: Duration = Duration::from_secs(10);
/// How long to wait for a just-detached loop to actually clear (`losetup -d`
/// is a deferred/lazy detach; the device lingers briefly, esp. after a `-P`
/// rescan).
pub(crate) const LOOP_CLEAR_TIMEOUT: Duration = Duration::from_secs(5);
/// Poll cadence while waiting for loops to clear.
const LOOP_CLEAR_POLL: Duration = Duration::from_millis(100);
const SYNC_TIMEOUT: Duration = Duration::from_secs(15);

/// Mutates a backing image by loop-mounting it.
///
/// In the two-LUN model each backing image holds exactly ONE partition (MBR
/// `p1`), so a mutator is constructed against a single per-LUN image and always
/// mounts node `p1`. The `Partition` argument to [`ImageMutator::apply`] only
/// names which LUN/image the handoff targets; the caller already selected the
/// matching image when it built this mutator.
pub(crate) struct LoopMutator {
    /// Backing image path for this LUN (`teslacam.img` or `media.img`).
    image: PathBuf,
    /// Runtime root for per-handoff mount dirs (e.g. `/run/teslausb/handoff`).
    runtime_root: PathBuf,
}

impl LoopMutator {
    /// Build a mutator for `image`, mounting under `runtime_root`.
    pub(crate) fn new(image: PathBuf, runtime_root: PathBuf) -> Self {
        Self {
            image,
            runtime_root,
        }
    }
}

impl ImageMutator for LoopMutator {
    fn cleanup_stale(&self) -> io::Result<()> {
        let loops = losetup_for_image(&self.image)?;
        for loopdev in &loops {
            for mountpoint in mounts_for_loop(loopdev)? {
                umount(Path::new(&mountpoint))?;
            }
            losetup_detach(loopdev)?;
        }
        // `losetup -d` detaches lazily (the loop lingers briefly after the `-P`
        // partition scan), so an immediate re-check can wrongly report a leftover
        // loop and make recovery REFUSE to re-present an ejected LUN — including
        // lun.0, the live recording drive (see `recover_lun`). Bounded-wait for the
        // just-detached loops to clear; fail only on a genuinely stuck loop.
        match wait_for_image_loops_clear(&self.image, LOOP_CLEAR_TIMEOUT)? {
            None => Ok(()),
            Some(loops) => Err(io::Error::other(format!(
                "stale loop device(s) {loops:?} still attached to {}",
                self.image.display()
            ))),
        }
    }

    fn apply(&self, partition: Partition, mutation: &Mutation) -> Result<(), MutateError> {
        // Attach the loop; before any mount, a failure means the image is still
        // released (the eject already closed the kernel's fd).
        let loopdev = match losetup_attach(&self.image) {
            Ok(d) => d,
            Err(e) => return Err(released_err(format!("losetup attach: {e}"))),
        };

        // Each per-LUN image is single-partition (MBR p1).
        let node = format!("{loopdev}p1");
        if let Err(e) = wait_for_block(Path::new(&node), Duration::from_secs(3)) {
            let _ = losetup_detach(&loopdev);
            return Err(released_err(format!("partition node {node}: {e}")));
        }

        let mnt = self.runtime_root.join(format!("h-{}", std::process::id()));
        if let Err(e) = std::fs::create_dir_all(&mnt) {
            let _ = losetup_detach(&loopdev);
            return Err(released_err(format!("mkdir mountpoint: {e}")));
        }

        if let Err(e) = mount_exfat(&node, &mnt) {
            let _ = std::fs::remove_dir(&mnt);
            let _ = losetup_detach(&loopdev);
            return Err(released_err(format!("mount {node}: {e}")));
        }

        // Mounted: from here we must umount + detach before reporting released.
        let op_result = apply_op(&mnt, mutation);

        // Durability: flush the filesystem before unmount (exFAT has no journal).
        sync_all();

        let umount_ok = umount(&mnt).is_ok();
        let detach_ok = losetup_detach(&loopdev).is_ok();
        if partition == Partition::P2 && detach_ok {
            if let Ok(Some(loops)) = wait_for_image_loops_clear(&self.image, LOOP_CLEAR_TIMEOUT) {
                eprintln!(
                    "gadgetd mutate: media loop detach still settling; image {} still has \
                     loop device(s) {loops:?} after {LOOP_CLEAR_TIMEOUT:?}",
                    self.image.display()
                );
            }
        }
        let _ = std::fs::remove_dir(&mnt);
        let image_released = umount_ok && detach_ok;

        match op_result {
            Ok(()) if image_released => Ok(()),
            Ok(()) => Err(MutateError {
                detail: format!(
                    "op applied but teardown incomplete (umount_ok={umount_ok}, \
                     detach_ok={detach_ok})"
                ),
                image_released,
            }),
            Err(e) => Err(MutateError {
                detail: e.to_string(),
                image_released,
            }),
        }
    }
}

/// Build a `MutateError` for a failure that occurred while the image was still
/// released (no mount held).
fn released_err(detail: String) -> MutateError {
    MutateError {
        detail,
        image_released: true,
    }
}

/// Apply the validated op against the freshly-mounted partition root.
fn apply_op(mnt: &Path, mutation: &Mutation) -> io::Result<()> {
    match mutation {
        Mutation::DeletePath { rel_path } => {
            let target = resolve_within(mnt, rel_path)?;
            let meta = std::fs::symlink_metadata(&target)?;
            if meta.is_dir() {
                std::fs::remove_dir_all(&target)
            } else {
                std::fs::remove_file(&target)
            }
        }
        Mutation::DeletePaths { rel_paths } => delete_files(mnt, rel_paths),
        Mutation::InstallFile {
            rel_path,
            source_path,
        } => install_file(mnt, rel_path, Path::new(source_path)),
        Mutation::RemoveEmptyDir { rel_path } => remove_empty_dir(mnt, rel_path),
    }
}

/// Remove the **empty** directory at `rel_path`, then walk UP and remove any
/// now-empty ancestors, stopping at the first non-empty, protected, or absent
/// directory. This is the orphan-directory cleanup that complements
/// [`delete_files`]: after a folder's files are deleted (file-only, never
/// recursive) the empty exFAT directory lingers, so this prunes it.
///
/// SAFETY — this can NEVER delete a file:
/// - It uses `remove_dir` (empty-only). A directory containing any entry returns
///   an error and is left completely untouched; we treat that as success and
///   stop (best-effort prune).
/// - Each candidate is re-jailed every iteration: `symlink_metadata` (a symlink
///   or non-dir stops the walk), then `canonicalize` + `starts_with(mnt_canon)`
///   (a resolved path escaping the mount is a hard error), and the mount root
///   itself is never a candidate.
/// - Protected directories ([`is_protected_dir`]: top-level partition roots and
///   the TeslaCam structural roots) stop the walk before removal.
///
/// An already-absent directory (`NotFound`) is success — a retried prune is safe.
/// Any other `remove_dir` error (e.g. `DirectoryNotEmpty`) ends the walk without
/// failing the handoff: leaving an empty directory behind is harmless and must
/// not poison a delete that already succeeded.
fn remove_empty_dir(mnt: &Path, rel_path: &str) -> io::Result<()> {
    let mnt_canon = std::fs::canonicalize(mnt)?;
    let rel = validate_rel_path(rel_path).map_err(io::Error::other)?;

    // Components to walk up from the deepest. `validate_rel_path` guarantees a
    // relative, `.`/`..`-free path, so this is just the cleaned segments.
    let mut comps: Vec<String> = rel.split('/').map(str::to_owned).collect();

    while !comps.is_empty() {
        let current_rel = comps.join("/");
        // Stop before touching any protected/structural directory.
        if is_protected_dir(&current_rel) {
            break;
        }

        let joined = mnt_canon.join(&current_rel);
        // Stat WITHOUT following symlinks: a symlink (or anything not a real
        // directory) is not something we prune.
        match std::fs::symlink_metadata(&joined) {
            Ok(meta) => {
                if !meta.is_dir() {
                    break;
                }
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                // Already gone (orphan repaired or never existed) — keep walking
                // up in case an ancestor is now empty too.
                comps.pop();
                continue;
            }
            Err(e) => return Err(e),
        }

        // Re-jail the resolved real path: never the mount root, never an escape.
        let canon = std::fs::canonicalize(&joined)?;
        if canon == mnt_canon {
            break;
        }
        if !canon.starts_with(&mnt_canon) {
            return Err(io::Error::other(format!(
                "resolved path {} escapes the mount",
                canon.display()
            )));
        }
        // Defence-in-depth on the untrusted exFAT (which cannot itself hold
        // symlinks, but gadgetd does not trust that): require the canonical path
        // to equal the lexical path. If an intermediate symlink made resolution
        // diverge, `is_protected_dir`'s lexical check would no longer match the
        // directory `remove_dir` actually targets — so refuse and stop.
        let expected = mnt_canon.join(&current_rel);
        if canon != expected {
            break;
        }

        match std::fs::remove_dir(&canon) {
            Ok(()) => {
                comps.pop();
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                comps.pop();
            }
            // Non-empty (or any other error): leave it and stop. Best-effort.
            Err(_) => break,
        }
    }
    Ok(())
}

/// Delete a set of individual files within the single mount (one eject), using
/// an **all-or-nothing preflight** so a stale or corrupt request can never cause
/// a partial, ambiguous delete:
///
/// 1. Resolve + jail + stat every path (regular-file-only; reject dirs, mount
///    root, escapes, duplicates) BEFORE removing anything.
/// 2. Decide on the whole set (fail closed on the ambiguous middle):
///    - every target already absent → idempotent success (a retried delete);
///    - every target present        → delete the whole set;
///    - a *mix* of present + absent  → refuse and delete nothing (stale catalog
///      or a partial prior delete; requires a rescan, not a guess).
fn delete_files(mnt: &Path, rel_paths: &[String]) -> io::Result<()> {
    let mnt_canon = std::fs::canonicalize(mnt)?;
    let mut present: Vec<PathBuf> = Vec::new();
    let mut seen: std::collections::BTreeSet<PathBuf> = std::collections::BTreeSet::new();
    let mut absent = 0_usize;

    for rel_path in rel_paths {
        let rel = validate_rel_path(rel_path).map_err(io::Error::other)?;
        let joined = mnt_canon.join(&rel);
        match std::fs::canonicalize(&joined) {
            Ok(canon) => {
                if canon == mnt_canon {
                    return Err(io::Error::other("refusing to operate on the mount root"));
                }
                if !canon.starts_with(&mnt_canon) {
                    return Err(io::Error::other(format!(
                        "resolved path {} escapes the mount",
                        canon.display()
                    )));
                }
                let meta = std::fs::symlink_metadata(&canon)?;
                if !meta.is_file() {
                    return Err(io::Error::other(
                        "refusing to delete a non-regular file (clip delete removes files only)",
                    ));
                }
                if !seen.insert(canon.clone()) {
                    return Err(io::Error::other("duplicate path in delete set"));
                }
                present.push(canon);
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => absent += 1,
            Err(e) => return Err(e),
        }
    }

    if present.is_empty() {
        // Every target already gone: a retried/duplicate delete is safe.
        return Ok(());
    }
    if absent > 0 {
        return Err(io::Error::other(format!(
            "clip in inconsistent state: {absent} of {} target files already absent; \
             refusing partial delete (rescan required)",
            rel_paths.len()
        )));
    }
    for path in &present {
        std::fs::remove_file(path)?;
    }
    Ok(())
}

/// Resolve a validated relative path under `mnt` and prove it stays within the
/// mount after canonicalization (defence against symlink/TOCTOU escape on the
/// untrusted exFAT). The path must exist (used by delete).
fn resolve_within(mnt: &Path, rel_path: &str) -> io::Result<PathBuf> {
    let rel = validate_rel_path(rel_path).map_err(io::Error::other)?;
    let mnt_canon = std::fs::canonicalize(mnt)?;
    let joined = mnt_canon.join(&rel);
    let canon = std::fs::canonicalize(&joined)?;
    if canon == mnt_canon {
        return Err(io::Error::other("refusing to operate on the mount root"));
    }
    if !canon.starts_with(&mnt_canon) {
        return Err(io::Error::other(format!(
            "resolved path {} escapes the mount",
            canon.display()
        )));
    }
    Ok(canon)
}

/// Copy a staged source file into the partition via temp + atomic rename.
fn install_file(mnt: &Path, rel_path: &str, source: &Path) -> io::Result<()> {
    let rel = validate_rel_path(rel_path).map_err(io::Error::other)?;

    // Open the source with O_NOFOLLOW so a swapped-in symlink can't redirect us,
    // and require a regular file within the size cap (TOCTOU/special-file guard).
    let src = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(source)
        .map_err(|e| io::Error::other(format!("open source {}: {e}", source.display())))?;
    let src_meta = src.metadata()?;
    if !src_meta.is_file() {
        return Err(io::Error::other("source is not a regular file"));
    }
    if src_meta.len() > MAX_INSTALL_BYTES {
        return Err(io::Error::other(format!(
            "source exceeds {MAX_INSTALL_BYTES} byte install cap"
        )));
    }

    let mnt_canon = std::fs::canonicalize(mnt)?;
    let dest = mnt_canon.join(&rel);
    let parent = dest
        .parent()
        .ok_or_else(|| io::Error::other("destination has no parent"))?;
    // A freshly-formatted media image has no category subfolders (e.g. `Chimes/`,
    // `Wraps/`), so the destination's parent may not exist yet. `rel` is validated
    // relative and `..`/`.`-free (`validate_rel_path`), so `mnt_canon.join(rel)`
    // stays lexically inside the mount and `create_dir_all` only ever materialises
    // directories under `mnt_canon`. Without this, a first-ever install into a new
    // category fails the `canonicalize(parent)` below with ENOENT.
    std::fs::create_dir_all(parent)?;
    // The destination's parent must be a real directory (not a swapped-in symlink)
    // and must resolve inside the mount (defence against a TOCTOU/symlink escape).
    if !std::fs::symlink_metadata(parent)?.is_dir() {
        return Err(io::Error::other("destination parent is not a directory"));
    }
    let parent_canon = std::fs::canonicalize(parent)?;
    if !parent_canon.starts_with(&mnt_canon) {
        return Err(io::Error::other("destination escapes the mount"));
    }

    let tmp = parent_canon.join(format!(".gadgetd-install-{}.partial", std::process::id()));
    let mut reader = src;
    {
        let mut writer = std::fs::File::create(&tmp)?;
        io::copy(&mut reader, &mut writer)?;
        writer.sync_all()?;
    }
    std::fs::rename(&tmp, &dest)?;
    Ok(())
}

/// `losetup -fP --show <image>` → loop device path.
fn losetup_attach(image: &Path) -> io::Result<String> {
    let out = run_with_timeout(
        Command::new("losetup").arg("-fP").arg("--show").arg(image),
        LOSETUP_TIMEOUT,
    )?;
    let dev = String::from_utf8_lossy(&out).trim().to_owned();
    if dev.is_empty() {
        return Err(io::Error::other("losetup returned no device"));
    }
    Ok(dev)
}

/// `losetup -d <loopdev>`.
pub(crate) fn losetup_detach(loopdev: &str) -> io::Result<()> {
    run_with_timeout(
        Command::new("losetup").arg("-d").arg(loopdev),
        LOSETUP_TIMEOUT,
    )
    .map(|_| ())
}

/// `losetup -j <image>` → the loop devices currently backing `image`.
pub(crate) fn losetup_for_image(image: &Path) -> io::Result<Vec<String>> {
    let out = run_with_timeout(
        Command::new("losetup").arg("-j").arg(image),
        LOSETUP_TIMEOUT,
    )?;
    let text = String::from_utf8_lossy(&out);
    Ok(text
        .lines()
        .filter_map(|line| line.split(':').next())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
        .collect())
}

/// Testable core: poll `probe` until it yields an empty list or the deadline
/// passes. Returns `Ok(None)` when cleared, `Ok(Some(remaining))` if still
/// non-empty at timeout. `sleep` and `elapsed` are injected so tests need no
/// real time or real `losetup`. A probe error propagates.
///
/// The deadline is a soft bound: total wall time can exceed `timeout` by up to
/// one `interval` plus one `probe` duration (the deadline is re-checked only at
/// the top of each iteration). That overrun is immaterial for this best-effort
/// settle and keeps the never-double-mount guarantee (it never returns `None`
/// while a loop is still attached).
fn poll_loops_clear<P, S, E>(
    mut probe: P,
    timeout: Duration,
    interval: Duration,
    mut elapsed: E,
    mut sleep: S,
) -> io::Result<Option<Vec<String>>>
where
    P: FnMut() -> io::Result<Vec<String>>,
    S: FnMut(Duration),
    E: FnMut() -> Duration,
{
    loop {
        let loops = probe()?;
        if loops.is_empty() {
            return Ok(None);
        }
        if elapsed() >= timeout {
            return Ok(Some(loops));
        }
        sleep(interval);
    }
}

/// Real-world wrapper: wait up to `timeout` for `image` to have no loop
/// devices. `Ok(None)` = cleared; `Ok(Some(loops))` = still attached at
/// timeout.
pub(crate) fn wait_for_image_loops_clear(
    image: &Path,
    timeout: Duration,
) -> io::Result<Option<Vec<String>>> {
    let start = Instant::now();
    poll_loops_clear(
        || losetup_for_image(image),
        timeout,
        LOOP_CLEAR_POLL,
        || start.elapsed(),
        std::thread::sleep,
    )
}

/// Mount points whose source device is a partition of `loopdev`, parsed from
/// `/proc/self/mountinfo`.
fn mounts_for_loop(loopdev: &str) -> io::Result<Vec<String>> {
    let info = std::fs::read_to_string("/proc/self/mountinfo")?;
    let mut found = Vec::new();
    for line in info.lines() {
        let Some((left, right)) = line.split_once(" - ") else {
            continue;
        };
        let left_fields: Vec<&str> = left.split_whitespace().collect();
        let right_fields: Vec<&str> = right.split_whitespace().collect();
        // mountinfo: ... [4]=mountpoint ... ` - ` fstype source superopts
        let (Some(mountpoint), Some(source)) = (left_fields.get(4), right_fields.get(1)) else {
            continue;
        };
        if source.starts_with(loopdev) {
            found.push(unescape_mountinfo(mountpoint));
        }
    }
    Ok(found)
}

/// mountinfo escapes spaces/tabs/newlines as octal (`\040` etc.); decode them.
fn unescape_mountinfo(raw: &str) -> String {
    let mut decoded = String::with_capacity(raw.len());
    let mut bytes = raw.bytes().peekable();
    while let Some(b) = bytes.next() {
        if b == b'\\' {
            // Try to read exactly three following octal digits.
            let mut octal = String::with_capacity(3);
            while octal.len() < 3 {
                match bytes.peek() {
                    Some(&d) if d.is_ascii_digit() => {
                        octal.push(d as char);
                        bytes.next();
                    }
                    _ => break,
                }
            }
            if octal.len() == 3 {
                if let Ok(code) = u8::from_str_radix(&octal, 8) {
                    decoded.push(code as char);
                    continue;
                }
            }
            // Not a valid escape: emit the backslash and any consumed digits.
            decoded.push('\\');
            decoded.push_str(&octal);
            continue;
        }
        decoded.push(b as char);
    }
    decoded
}

/// `mount -t exfat <node> <mountpoint>`.
fn mount_exfat(node: &str, mountpoint: &Path) -> io::Result<()> {
    run_with_timeout(
        Command::new("mount")
            .arg("-t")
            .arg("exfat")
            .arg(node)
            .arg(mountpoint),
        MOUNT_TIMEOUT,
    )
    .map(|_| ())
}

/// `umount <mountpoint>`.
pub(crate) fn umount(mountpoint: &Path) -> io::Result<()> {
    run_with_timeout(Command::new("umount").arg(mountpoint), UMOUNT_TIMEOUT).map(|_| ())
}

/// Flush all filesystem buffers before unmount (exFAT has no journal). The file
/// data is already `fsync`'d via `writer.sync_all()` and `umount` flushes the
/// mounted fs; this global `sync` is belt-and-braces to push the backing
/// `disk.img` pages toward stable storage. Best-effort: a `sync` failure does
/// not by itself indicate data loss, so it is logged by the caller's flow only
/// via the subsequent umount verification.
fn sync_all() {
    let _ = run_with_timeout(&mut Command::new("sync"), SYNC_TIMEOUT);
}

/// Wait until `path` is a block device or the deadline passes.
pub(crate) fn wait_for_block(path: &Path, timeout: Duration) -> io::Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if path.exists() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(io::Error::other("timed out waiting for block device"));
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Run `cmd` to completion under `timeout`, killing it on overrun. Returns
/// stdout on success; maps a non-zero exit or timeout to an error.
pub(crate) fn run_with_timeout(cmd: &mut Command, timeout: Duration) -> io::Result<Vec<u8>> {
    use std::process::Stdio;
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            if let Some(mut o) = child.stdout.take() {
                io::Read::read_to_end(&mut o, &mut stdout)?;
            }
            if let Some(mut e) = child.stderr.take() {
                io::Read::read_to_end(&mut e, &mut stderr)?;
            }
            if status.success() {
                return Ok(stdout);
            }
            return Err(io::Error::other(format!(
                "command failed ({status}): {}",
                String::from_utf8_lossy(&stderr).trim()
            )));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::other("command timed out"));
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{
        delete_files, install_file, poll_loops_clear, remove_empty_dir, run_with_timeout,
        unescape_mountinfo,
    };
    use std::cell::Cell;
    use std::io;
    use std::path::PathBuf;
    use std::process::Command;
    use std::time::Duration;

    /// Create a fresh, unique temp directory for a single test.
    fn temp_mnt(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "gadgetd-delfiles-{tag}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn touch(root: &std::path::Path, rel: &str) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, b"x").unwrap();
    }

    #[test]
    fn delete_files_removes_a_whole_present_set() {
        let mnt = temp_mnt("present");
        let a = "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-front.mp4";
        let b = "TeslaCam/SavedClips/2026-06-01_20-10-04/2026-06-01_20-10-04-back.mp4";
        touch(&mnt, a);
        touch(&mnt, b);
        delete_files(&mnt, &[a.to_owned(), b.to_owned()]).expect("all present deletes");
        assert!(!mnt.join(a).exists());
        assert!(!mnt.join(b).exists());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn delete_files_is_idempotent_when_all_absent() {
        let mnt = temp_mnt("absent");
        let a = "TeslaCam/SavedClips/e/2026-06-01_20-10-04-front.mp4";
        // Nothing created: a retried delete of an already-gone clip must succeed.
        delete_files(&mnt, &[a.to_owned()]).expect("all absent is idempotent success");
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn delete_files_refuses_a_mixed_set_and_deletes_nothing() {
        let mnt = temp_mnt("mixed");
        let present = "TeslaCam/SavedClips/e/2026-06-01_20-10-04-front.mp4";
        let absent = "TeslaCam/SavedClips/e/2026-06-01_20-10-04-back.mp4";
        touch(&mnt, present);
        let err = delete_files(&mnt, &[present.to_owned(), absent.to_owned()])
            .expect_err("mixed present/absent is refused");
        assert!(err.to_string().contains("inconsistent state"));
        // Fail closed: the present file must NOT have been deleted.
        assert!(mnt.join(present).exists());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn install_file_creates_missing_category_parent() {
        // Regression: a first-ever install into a new category folder (e.g.
        // `Chimes/`) on a freshly-formatted media image must auto-create the
        // parent rather than failing `canonicalize(parent)` with ENOENT.
        let mnt = temp_mnt("install-mkparent");
        let src = mnt.join("source.wav");
        std::fs::write(&src, b"RIFFchimebytes").unwrap();
        install_file(&mnt, "Chimes/testchime.wav", &src)
            .expect("install auto-creates the missing Chimes/ parent");
        let dest = mnt.join("Chimes/testchime.wav");
        assert!(dest.is_file());
        assert_eq!(std::fs::read(&dest).unwrap(), b"RIFFchimebytes");
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn install_file_still_rejects_traversal() {
        // The new create_dir_all must not weaken the jail: a `..` path is refused
        // before anything is created.
        let mnt = temp_mnt("install-traversal");
        let src = mnt.join("source.wav");
        std::fs::write(&src, b"x").unwrap();
        install_file(&mnt, "../escape.wav", &src).expect_err("traversal is refused");
        assert!(!mnt.parent().unwrap().join("escape.wav").exists());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn delete_files_refuses_a_directory_target() {
        let mnt = temp_mnt("dir");
        let dir_rel = "TeslaCam/SavedClips/2026-06-01_20-10-04";
        std::fs::create_dir_all(mnt.join(dir_rel)).unwrap();
        let err = delete_files(&mnt, &[dir_rel.to_owned()]).expect_err("a directory is refused");
        assert!(err.to_string().contains("non-regular file"));
        assert!(mnt.join(dir_rel).exists());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn delete_files_refuses_a_duplicate_path() {
        let mnt = temp_mnt("dup");
        let a = "TeslaCam/SavedClips/e/2026-06-01_20-10-04-front.mp4";
        touch(&mnt, a);
        let err = delete_files(&mnt, &[a.to_owned(), a.to_owned()])
            .expect_err("duplicate present path is refused");
        assert!(err.to_string().contains("duplicate"));
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_removes_an_empty_dir() {
        let mnt = temp_mnt("red-empty");
        std::fs::create_dir_all(mnt.join("Music/Artist/Album")).unwrap();
        remove_empty_dir(&mnt, "Music/Artist/Album").expect("empty dir pruned");
        assert!(!mnt.join("Music/Artist/Album").exists());
        // The now-empty parent is pruned too, but the protected top-level stays.
        assert!(!mnt.join("Music/Artist").exists());
        assert!(mnt.join("Music").exists());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_never_touches_a_non_empty_dir_or_its_file() {
        let mnt = temp_mnt("red-nonempty");
        touch(&mnt, "Music/Artist/Album/song.mp3");
        remove_empty_dir(&mnt, "Music/Artist/Album").expect("best-effort, non-fatal");
        // The directory and its file MUST be untouched (empty-only remove_dir).
        assert!(mnt.join("Music/Artist/Album/song.mp3").is_file());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_stops_at_a_non_empty_ancestor() {
        let mnt = temp_mnt("red-ancestor");
        // Album is empty; Artist also holds a sibling file → walk stops at Artist.
        std::fs::create_dir_all(mnt.join("Music/Artist/Album")).unwrap();
        touch(&mnt, "Music/Artist/other.mp3");
        remove_empty_dir(&mnt, "Music/Artist/Album").expect("prunes only the empty leaf");
        assert!(!mnt.join("Music/Artist/Album").exists());
        assert!(mnt.join("Music/Artist").is_dir());
        assert!(mnt.join("Music/Artist/other.mp3").is_file());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_is_idempotent_when_absent() {
        let mnt = temp_mnt("red-absent");
        std::fs::create_dir_all(mnt.join("Music")).unwrap();
        // The target never existed: a retried prune (orphan already gone) is OK.
        remove_empty_dir(&mnt, "Music/Gone/Deeper").expect("absent dir is success");
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_refuses_to_prune_protected_top_level() {
        let mnt = temp_mnt("red-toplevel");
        std::fs::create_dir_all(mnt.join("Music")).unwrap();
        // Even though Music/ is empty, a single top-level component is protected.
        remove_empty_dir(&mnt, "Music").expect("protected break is non-fatal");
        assert!(mnt.join("Music").is_dir());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_refuses_teslacam_structural_root() {
        let mnt = temp_mnt("red-structural");
        std::fs::create_dir_all(mnt.join("TeslaCam/SavedClips")).unwrap();
        remove_empty_dir(&mnt, "TeslaCam/SavedClips").expect("structural break is non-fatal");
        assert!(mnt.join("TeslaCam/SavedClips").is_dir());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn remove_empty_dir_refuses_traversal() {
        let mnt = temp_mnt("red-traversal");
        let escape = mnt.parent().unwrap().join("escape-target");
        std::fs::create_dir_all(&escape).unwrap();
        remove_empty_dir(&mnt, "../escape-target").expect_err("traversal is refused");
        assert!(escape.is_dir());
        std::fs::remove_dir_all(&mnt).ok();
        std::fs::remove_dir_all(&escape).ok();
    }

    #[test]
    fn remove_empty_dir_refuses_symlinked_path_component() {
        // An intermediate symlink that diverges the canonical path from the
        // lexical path must be refused (so the lexical protected-dir check can
        // never disagree with the directory remove_dir actually targets).
        use std::os::unix::fs::symlink;
        let mnt = temp_mnt("red-symlink");
        // A real, empty victim directory the symlink resolves into.
        std::fs::create_dir_all(mnt.join("Other/Album")).unwrap();
        // Music/link -> <mnt>/Other ; request prune of Music/link/Album.
        std::fs::create_dir_all(mnt.join("Music")).unwrap();
        symlink(mnt.join("Other"), mnt.join("Music/link")).unwrap();
        remove_empty_dir(&mnt, "Music/link/Album").expect("symlink mismatch break is non-fatal");
        // The victim directory MUST be untouched: resolution diverged → refused.
        assert!(mnt.join("Other/Album").is_dir());
        std::fs::remove_dir_all(&mnt).ok();
    }

    #[test]
    fn unescape_mountinfo_decodes_octal_spaces() {
        assert_eq!(unescape_mountinfo("/run/a\\040b"), "/run/a b");
        assert_eq!(unescape_mountinfo("/plain/path"), "/plain/path");
    }

    #[cfg(unix)]
    #[test]
    fn run_with_timeout_returns_stdout_on_success() {
        let out = run_with_timeout(
            Command::new("/bin/echo").arg("hello"),
            Duration::from_secs(5),
        )
        .expect("echo runs");
        assert_eq!(String::from_utf8_lossy(&out).trim(), "hello");
    }

    #[cfg(unix)]
    #[test]
    fn run_with_timeout_kills_an_overrunning_command() {
        let err = run_with_timeout(
            Command::new("/bin/sleep").arg("10"),
            Duration::from_millis(200),
        )
        .expect_err("should time out");
        assert!(err.to_string().contains("timed out"));
    }

    #[test]
    fn poll_loops_clear_returns_none_when_loops_clear_before_timeout() {
        let probe_calls = Cell::new(0_u8);
        let elapsed_calls = Cell::new(0_u8);
        let result = poll_loops_clear(
            || {
                let n = probe_calls.get();
                probe_calls.set(n + 1);
                if n < 2 {
                    Ok(vec!["/dev/loop0".to_owned()])
                } else {
                    Ok(Vec::new())
                }
            },
            Duration::from_secs(5),
            Duration::from_millis(100),
            || {
                let n = elapsed_calls.get();
                elapsed_calls.set(n + 1);
                Duration::from_millis(u64::from(n) * 10)
            },
            |_| {},
        )
        .expect("probe succeeds");
        assert!(result.is_none());
        assert_eq!(probe_calls.get(), 3);
    }

    #[test]
    fn poll_loops_clear_times_out_when_loops_persist() {
        let elapsed_calls = Cell::new(0_u8);
        let result = poll_loops_clear(
            || Ok(vec!["/dev/loop7".to_owned()]),
            Duration::from_millis(250),
            Duration::from_millis(100),
            || {
                let n = elapsed_calls.get();
                elapsed_calls.set(n + 1);
                match n {
                    0 => Duration::from_millis(0),
                    1 => Duration::from_millis(100),
                    _ => Duration::from_millis(300),
                }
            },
            |_| {},
        )
        .expect("probe succeeds");
        assert_eq!(result, Some(vec!["/dev/loop7".to_owned()]));
    }

    #[test]
    fn poll_loops_clear_propagates_probe_error() {
        let err = poll_loops_clear(
            || Err(io::Error::other("probe failed")),
            Duration::from_secs(1),
            Duration::from_millis(100),
            || Duration::from_millis(0),
            |_| {},
        )
        .expect_err("probe error should propagate");
        assert!(err.to_string().contains("probe failed"));
    }
}
