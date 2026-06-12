//! Persistent **read-only** loop-mount of the media image (`media.img`, the
//! `lun.1` backing image) so the web layer can read media files via `std::fs`.
//!
//! The media-write eject-handoff ([`crate::mutate::LoopMutator`]) RW-loop-mounts
//! the SAME image. Two simultaneous mounts of one exFAT volume corrupt it, so
//! this read mount is torn down ([`MediaRoMount::suspend`] via the
//! [`crate::handoff::ReadMountGate`]) *before* a media (P2) RW mutate and
//! re-established afterwards. Establishment is best-effort and never blocks
//! gadget bring-up or a `TeslaCam` (P1) handoff: a media read mount that cannot
//! come up only degrades the web read path, surfaced through
//! [`MediaRoMount::health_snapshot`].

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::Duration;

use crate::handoff::ReadMountGate;
use crate::mutate::{
    LOSETUP_TIMEOUT, MOUNT_TIMEOUT, losetup_detach, losetup_for_image, run_with_timeout, umount,
    wait_for_block,
};

/// Fixed mountpoint for the persistent read-only media mount. The web read path
/// (a later, separate item) reads media files from under here via `std::fs`.
pub(crate) const MEDIA_RO_MOUNT_DIR: &str = "/run/teslausb/media-ro";

/// How long to wait for the loop partition node to appear after attach.
const PARTITION_WAIT: Duration = Duration::from_secs(3);

/// Observable health of the persistent read-only media mount.
#[derive(Debug, Default, Clone)]
pub(crate) struct MediaRoHealth {
    /// `true` once the RO mount is established and not currently suspended.
    pub(crate) mounted: bool,
    /// The most recent establishment/teardown error, if any.
    pub(crate) last_error: Option<String>,
}

/// Owns the persistent read-only mount of the media backing image.
pub(crate) struct MediaRoMount {
    /// Media backing image (`media.img`).
    image: PathBuf,
    /// Fixed mountpoint ([`MEDIA_RO_MOUNT_DIR`]).
    mount_dir: PathBuf,
    /// Health for status reporting (decoupled from the handoff outcome).
    health: Mutex<MediaRoHealth>,
}

impl MediaRoMount {
    /// Build a manager for `image`, mounting at [`MEDIA_RO_MOUNT_DIR`].
    pub(crate) fn new(image: PathBuf) -> Self {
        Self {
            image,
            mount_dir: PathBuf::from(MEDIA_RO_MOUNT_DIR),
            health: Mutex::new(MediaRoHealth::default()),
        }
    }

    /// Idempotently establish the read-only mount. Returns `Ok` if it is already
    /// mounted. On failure the loop attached by this call (if any) is detached.
    ///
    /// # Errors
    /// Returns an error if the loop attach, partition-node wait, or read-only
    /// mount fails.
    pub(crate) fn ensure_mounted(&self) -> io::Result<()> {
        match self.is_mounted() {
            Ok(true) => {
                self.set_mounted();
                return Ok(());
            }
            Ok(false) => {}
            Err(e) => {
                self.set_error(&e);
                return Err(e);
            }
        }
        match self.mount_once() {
            Ok(()) => {
                self.set_mounted();
                Ok(())
            }
            Err(e) => {
                self.set_error(&e);
                Err(e)
            }
        }
    }

    /// Read-only mount once (caller has confirmed we are not already mounted).
    fn mount_once(&self) -> io::Result<()> {
        // Never stack a read mount on an image that already has a loop device:
        // a stale RW loop/mount from an interrupted handoff (whose recovery
        // cleanup failed) would otherwise be mounted a SECOND time here — the
        // exact double-mount that corrupts exFAT. Fail closed; the next
        // recovery/handoff clears the stale loop and a later resume re-mounts.
        if !losetup_for_image(&self.image)?.is_empty() {
            return Err(io::Error::other(format!(
                "{} already has a loop device attached; refusing read-only mount \
                 to avoid a double-mount",
                self.image.display()
            )));
        }

        std::fs::create_dir_all(&self.mount_dir)?;

        let out = run_with_timeout(
            Command::new("losetup").args(losetup_ro_args(&self.image)),
            LOSETUP_TIMEOUT,
        )?;
        let loopdev = String::from_utf8_lossy(&out).trim().to_owned();
        if loopdev.is_empty() {
            return Err(io::Error::other("losetup returned no device"));
        }

        // Each per-LUN image is single-partition (MBR p1).
        let node = format!("{loopdev}p1");
        if let Err(e) = wait_for_block(Path::new(&node), PARTITION_WAIT) {
            return Err(io::Error::other(format!(
                "partition node {node}: {e}{}",
                detach_suffix(&loopdev)
            )));
        }

        if let Err(e) = run_with_timeout(
            Command::new("mount").args(mount_ro_args(&node, &self.mount_dir)),
            MOUNT_TIMEOUT,
        ) {
            return Err(io::Error::other(format!(
                "mount {node} ro: {e}{}",
                detach_suffix(&loopdev)
            )));
        }
        Ok(())
    }

    /// Tear down the read-only mount so the RW mutate can take the image. Real
    /// `umount` (never lazy) followed by detaching every loop backing the image;
    /// then verify nothing remains attached. `Ok` if there was nothing to do.
    /// Health is updated on every path (success clears it; any failure records
    /// it) so a partial teardown can never leave a stale `mounted=true`.
    ///
    /// # Errors
    /// Returns an error if `umount`/detach fails or a loop is still attached —
    /// the caller must then REFUSE the write to avoid a double-mount.
    fn release(&self) -> io::Result<()> {
        match self.release_inner() {
            Ok(()) => {
                if let Ok(mut h) = self.health.lock() {
                    h.mounted = false;
                    h.last_error = None;
                }
                Ok(())
            }
            Err(e) => {
                self.set_error(&e);
                Err(e)
            }
        }
    }

    /// The raw teardown steps; health is managed by [`Self::release`].
    fn release_inner(&self) -> io::Result<()> {
        if self.is_mounted()? {
            umount(&self.mount_dir)?;
        }
        for loopdev in losetup_for_image(&self.image)? {
            losetup_detach(&loopdev)?;
        }
        if !losetup_for_image(&self.image)?.is_empty() {
            return Err(io::Error::other(format!(
                "loop devices still attached to {} after suspend",
                self.image.display()
            )));
        }
        Ok(())
    }

    /// `(mounted, mount_dir, last_error)` for `gadget_status`.
    pub(crate) fn health_snapshot(&self) -> (bool, String, Option<String>) {
        let dir = self.mount_dir.to_string_lossy().into_owned();
        match self.health.lock() {
            Ok(h) => (h.mounted, dir, h.last_error.clone()),
            Err(_) => (false, dir, Some("health lock poisoned".to_owned())),
        }
    }

    /// Is [`Self::mount_dir`] currently a mountpoint?
    fn is_mounted(&self) -> io::Result<bool> {
        let info = std::fs::read_to_string("/proc/self/mountinfo")?;
        Ok(mountinfo_has_mount_dir(
            &info,
            &self.mount_dir.to_string_lossy(),
        ))
    }

    fn set_mounted(&self) {
        if let Ok(mut h) = self.health.lock() {
            h.mounted = true;
            h.last_error = None;
        }
    }

    fn set_error(&self, e: &io::Error) {
        if let Ok(mut h) = self.health.lock() {
            h.mounted = false;
            h.last_error = Some(e.to_string());
        }
    }
}

impl ReadMountGate for MediaRoMount {
    fn suspend(&self) -> io::Result<()> {
        self.release()
    }

    fn resume(&self) -> io::Result<()> {
        self.ensure_mounted()
    }
}

/// Detach `loopdev` during error cleanup, returning a message suffix that
/// surfaces a detach failure (a leaked loop device) in the caller's error.
fn detach_suffix(loopdev: &str) -> String {
    losetup_detach(loopdev)
        .err()
        .map(|d| format!("; loop {loopdev} detach ALSO failed: {d}"))
        .unwrap_or_default()
}

/// `losetup -rfP --show <image>` — attach a READ-ONLY loop with a partition scan.
fn losetup_ro_args(image: &Path) -> Vec<OsString> {
    vec![
        OsString::from("-rfP"),
        OsString::from("--show"),
        image.as_os_str().to_owned(),
    ]
}

/// `mount -t exfat -o ro <node> <dir>` — read-only exFAT mount.
fn mount_ro_args(node: &str, dir: &Path) -> Vec<OsString> {
    vec![
        OsString::from("-t"),
        OsString::from("exfat"),
        OsString::from("-o"),
        OsString::from("ro"),
        OsString::from(node),
        dir.as_os_str().to_owned(),
    ]
}

/// True iff `dir` is a mountpoint (mountinfo field index 4, before the ` - `).
fn mountinfo_has_mount_dir(mountinfo: &str, dir: &str) -> bool {
    mountinfo.lines().any(|line| {
        line.split_once(" - ")
            .and_then(|(left, _)| left.split_whitespace().nth(4))
            .is_some_and(|mp| mp == dir)
    })
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{losetup_ro_args, mount_ro_args, mountinfo_has_mount_dir};
    use std::ffi::OsString;
    use std::path::Path;

    #[test]
    fn losetup_ro_args_request_readonly_partitioned_show() {
        let args = losetup_ro_args(Path::new("/data/teslausb/media.img"));
        assert_eq!(
            args,
            vec![
                OsString::from("-rfP"),
                OsString::from("--show"),
                OsString::from("/data/teslausb/media.img"),
            ]
        );
    }

    #[test]
    fn mount_ro_args_request_readonly_exfat() {
        let args = mount_ro_args("/dev/loop7p1", Path::new("/run/teslausb/media-ro"));
        assert_eq!(
            args,
            vec![
                OsString::from("-t"),
                OsString::from("exfat"),
                OsString::from("-o"),
                OsString::from("ro"),
                OsString::from("/dev/loop7p1"),
                OsString::from("/run/teslausb/media-ro"),
            ]
        );
    }

    #[test]
    fn mountinfo_detects_present_mount_dir() {
        let info =
            "36 35 7:0 / /run/teslausb/media-ro ro,relatime shared:1 - exfat /dev/loop7p1 ro\n";
        assert!(mountinfo_has_mount_dir(info, "/run/teslausb/media-ro"));
    }

    #[test]
    fn mountinfo_absent_mount_dir_is_false() {
        let info = "36 35 7:0 / /some/other ro,relatime - exfat /dev/loop7p1 ro\n";
        assert!(!mountinfo_has_mount_dir(info, "/run/teslausb/media-ro"));
    }

    #[test]
    fn mountinfo_empty_is_false() {
        assert!(!mountinfo_has_mount_dir("", "/run/teslausb/media-ro"));
    }
}
