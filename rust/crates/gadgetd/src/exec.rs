//! Live-device executor: applies [`ConfigfsOp`]s to the kernel's configfs,
//! auto-detects the UDC, and reads back gadget status. All side effects live
//! here; the planners in [`crate::config`] stay pure.

use std::io;
use std::path::{Path, PathBuf};

use crate::config::{ConfigfsOp, GadgetConfig};

/// Directory the kernel lists available USB Device Controllers under.
const UDC_SYSFS: &str = "/sys/class/udc";

/// Apply an ordered op list to configfs, tolerating already-applied state so
/// bring-up/tear-down are idempotent.
///
/// # Errors
/// Returns the first non-idempotent I/O error encountered.
pub(crate) fn execute_ops(ops: &[ConfigfsOp]) -> io::Result<()> {
    for op in ops {
        apply(op)?;
    }
    Ok(())
}

fn apply(op: &ConfigfsOp) -> io::Result<()> {
    match op {
        ConfigfsOp::Mkdir(path) => match std::fs::create_dir_all(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => Ok(()),
            Err(e) => Err(annotate(&e, "mkdir", path)),
        },
        ConfigfsOp::Write(path, value) => {
            std::fs::write(path, value).map_err(|e| annotate(&e, "write", path))
        }
        ConfigfsOp::Symlink { target, link } => match make_symlink(target, link) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => Ok(()),
            Err(e) => Err(annotate(&e, "symlink", link)),
        },
        ConfigfsOp::Unlink(path) => match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(annotate(&e, "unlink", path)),
        },
        ConfigfsOp::Rmdir(path) => match std::fs::remove_dir(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(annotate(&e, "rmdir", path)),
        },
    }
}

fn annotate(err: &io::Error, verb: &str, path: &Path) -> io::Error {
    io::Error::new(
        err.kind(),
        format!("{verb} {} failed: {err}", path.display()),
    )
}

#[cfg(unix)]
fn make_symlink(target: &Path, link: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(not(unix))]
fn make_symlink(_target: &Path, _link: &Path) -> io::Result<()> {
    Err(io::Error::other(
        "symlinks are only supported on the unix target",
    ))
}

/// Resolve the UDC name to bind to.
///
/// - If `preferred` is given, it must exist under [`UDC_SYSFS`].
/// - Otherwise, exactly one UDC must be present (the normal Pi Zero 2 W case,
///   `3f980000.usb`). Zero or multiple controllers fail closed so we never bind
///   the wrong (e.g. dummy) controller silently.
///
/// # Errors
/// Returns an error if the requested UDC is absent, no UDC exists, or the
/// choice is ambiguous.
pub(crate) fn detect_udc(preferred: Option<&str>) -> io::Result<String> {
    let mut names: Vec<String> = std::fs::read_dir(UDC_SYSFS)?
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    names.sort();

    if let Some(want) = preferred {
        return if names.iter().any(|n| n == want) {
            Ok(want.to_owned())
        } else {
            Err(io::Error::other(format!(
                "requested UDC `{want}` not found under {UDC_SYSFS} (have: {names:?})"
            )))
        };
    }

    match names.as_slice() {
        [] => Err(io::Error::other(format!("no UDC found under {UDC_SYSFS}"))),
        [only] => Ok(only.clone()),
        many => Err(io::Error::other(format!(
            "multiple UDCs present ({many:?}); pass --udc to choose one"
        ))),
    }
}

/// Best-effort check: is `image` currently exported as a LUN by a *bound*
/// gadget? Used to refuse provisioning/mounting an in-use backing file
/// (protects the #1 invariant — never touch the live write path).
pub(crate) fn image_is_exported(configfs_root: &Path, image: &Path) -> bool {
    let target = canonical_or_owned(image);
    let Ok(gadgets) = std::fs::read_dir(configfs_root) else {
        return false;
    };
    for gadget in gadgets.filter_map(Result::ok) {
        let gdir = gadget.path();
        let bound = read_trimmed(&gdir.join("UDC")).is_some_and(|u| !u.is_empty());
        if !bound {
            continue;
        }
        if gadget_exports(&gdir, &target) {
            return true;
        }
    }
    false
}

fn gadget_exports(gadget_dir: &Path, target: &Path) -> bool {
    let functions = gadget_dir.join("functions");
    let Ok(funcs) = std::fs::read_dir(&functions) else {
        return false;
    };
    for func in funcs.filter_map(Result::ok) {
        let Ok(luns) = std::fs::read_dir(func.path()) else {
            continue;
        };
        for lun in luns.filter_map(Result::ok) {
            let file_attr = lun.path().join("file");
            if let Some(exported) = read_trimmed(&file_attr) {
                if !exported.is_empty() && canonical_or_owned(Path::new(&exported)) == target {
                    return true;
                }
            }
        }
    }
    false
}

fn canonical_or_owned(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Snapshot of the gadget's current binding, read from configfs/sysfs.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct GadgetStatus {
    /// Whether the gadget directory exists in configfs.
    pub(crate) present: bool,
    /// UDC name the gadget is bound to, if any (empty file ⇒ unbound).
    pub(crate) bound_udc: Option<String>,
    /// Controller state (`configured` when the host has enumerated it).
    pub(crate) udc_state: Option<String>,
    /// Backing file the LUN currently exports.
    pub(crate) lun_file: Option<String>,
}

/// Read the current [`GadgetStatus`] for `cfg` from the live filesystem.
pub(crate) fn read_status(cfg: &GadgetConfig) -> GadgetStatus {
    let present = cfg.gadget_dir().exists();
    let bound_udc = read_trimmed(&cfg.udc_path()).filter(|s| !s.is_empty());
    let udc_state = bound_udc
        .as_ref()
        .and_then(|udc| read_trimmed(&PathBuf::from(UDC_SYSFS).join(udc).join("state")));
    let lun_file = read_trimmed(&cfg.lun_dir().join("file")).filter(|s| !s.is_empty());
    GadgetStatus {
        present,
        bound_udc,
        udc_state,
        lun_file,
    }
}

fn read_trimmed(path: &Path) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_owned())
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{apply, read_status};
    use crate::config::{ConfigfsOp, GadgetConfig};
    use std::path::PathBuf;

    #[test]
    fn mkdir_then_write_then_rmdir_roundtrips_on_a_temp_tree() {
        let base = std::env::temp_dir().join(format!("gadgetd-exec-{}", std::process::id()));
        let dir = base.join("g");
        let file = dir.join("idVendor");

        apply(&ConfigfsOp::Mkdir(dir.clone())).expect("mkdir");
        // Mkdir is idempotent.
        apply(&ConfigfsOp::Mkdir(dir.clone())).expect("mkdir twice");
        apply(&ConfigfsOp::Write(file.clone(), "0x1d6b".to_owned())).expect("write");
        assert_eq!(std::fs::read_to_string(&file).expect("read"), "0x1d6b");

        // Unlink/Rmdir of absent paths are tolerated.
        apply(&ConfigfsOp::Unlink(dir.join("missing"))).expect("unlink missing ok");
        std::fs::remove_file(&file).expect("cleanup file");
        apply(&ConfigfsOp::Rmdir(dir.clone())).expect("rmdir");
        apply(&ConfigfsOp::Rmdir(dir)).expect("rmdir missing ok");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn status_of_absent_gadget_is_empty() {
        let mut cfg = GadgetConfig::teslausb(PathBuf::from("/data/disk.img"));
        cfg.configfs_root = std::env::temp_dir().join("gadgetd-status-absent");
        let st = read_status(&cfg);
        assert!(!st.present);
        assert_eq!(st.bound_udc, None);
        assert_eq!(st.lun_file, None);
    }
}
