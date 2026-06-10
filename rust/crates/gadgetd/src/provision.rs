//! Backing-image provisioning: create a single-partition exFAT backing image
//! and (for the `TeslaCam` image) ensure the `TeslaCam` directory exists so the
//! car enables dashcam recording.
//!
//! The two-LUN gadget is backed by TWO independent single-partition images —
//! `teslacam.img` (label `TESLACAM`, seeded with `TeslaCam/`) on `lun.0` and
//! `media.img` (label `MEDIA`) on `lun.1`. Each image holds exactly ONE MBR
//! partition so that a media (`lun.1`) eject-handoff cycles only its own LUN
//! and never disturbs the car-facing `TeslaCam` LUN.
//!
//! The deterministic command-argument builders are pure and unit-tested; the
//! orchestrator shells out to the standard `util-linux` / `exfatprogs` tools
//! (which exist on the Pi) and is integration-tested on hardware.

use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

/// 1 MiB front-of-disk gap for MBR alignment.
const RESERVE_MIB: u64 = 1;
/// Smallest usable `TeslaCam` partition we will create.
const MIN_TESLACAM_MIB: u64 = 16;
/// Smallest usable media partition we will create.
const MIN_MEDIA_MIB: u64 = 1;

/// Desired on-disk layout for ONE single-partition backing image.
#[derive(Debug, Clone)]
pub(crate) struct ImagePlan {
    /// Backing image path on the Pi's ext4 data area.
    pub(crate) image: PathBuf,
    /// Total image size in MiB (fully allocated — never sparse).
    pub(crate) size_mib: u64,
    /// exFAT volume label for the single partition.
    pub(crate) label: String,
    /// Whether to seed a top-level `TeslaCam/` directory (`TeslaCam` image only).
    pub(crate) seed_teslacam: bool,
    /// Smallest usable partition size (MiB) tolerated for this image.
    min_usable_mib: u64,
}

impl ImagePlan {
    /// The `TeslaCam` (`lun.0`) image: single `TESLACAM` exFAT partition seeded
    /// with a top-level `TeslaCam/` directory so the car records immediately.
    pub(crate) fn teslacam(image: PathBuf, size_mib: u64) -> Self {
        Self {
            image,
            size_mib,
            label: "TESLACAM".to_owned(),
            seed_teslacam: true,
            min_usable_mib: MIN_TESLACAM_MIB,
        }
    }

    /// The media (`lun.1`) image: single `MEDIA` exFAT partition, no seed.
    pub(crate) fn media(image: PathBuf, size_mib: u64) -> Self {
        Self {
            image,
            size_mib,
            label: "MEDIA".to_owned(),
            seed_teslacam: false,
            min_usable_mib: MIN_MEDIA_MIB,
        }
    }

    /// Reject layouts that would produce an unusable or partial image (a zero
    /// or out-of-range partition). Sizes are validated up front so a bad CLI
    /// value never reaches `fallocate`/`parted`.
    ///
    /// # Errors
    /// Returns an error describing the first constraint violated.
    fn validate(&self) -> io::Result<()> {
        let usable = self.size_mib.saturating_sub(RESERVE_MIB);
        if self.size_mib <= RESERVE_MIB || usable < self.min_usable_mib {
            return Err(io::Error::other(format!(
                "{} image too small ({} MiB total; need >= {} MiB usable after a {} MiB MBR gap)",
                self.label, self.size_mib, self.min_usable_mib, RESERVE_MIB
            )));
        }
        Ok(())
    }
}

/// `fallocate -l <size>MiB <image>` — fully allocate (not sparse) so car
/// writes never depend on ext4 free space.
pub(crate) fn fallocate_args(image: &Path, size_mib: u64) -> Vec<String> {
    vec![
        "-l".to_owned(),
        format!("{size_mib}MiB"),
        image.to_string_lossy().into_owned(),
    ]
}

/// `parted -s <image> mklabel msdos`.
pub(crate) fn parted_label_args(image: &Path) -> Vec<String> {
    vec![
        "-s".to_owned(),
        image.to_string_lossy().into_owned(),
        "mklabel".to_owned(),
        "msdos".to_owned(),
    ]
}

/// `parted -s <image> mkpart primary <start> <end>` (units in MiB or `100%`).
pub(crate) fn parted_mkpart_args(image: &Path, start: &str, end: &str) -> Vec<String> {
    vec![
        "-s".to_owned(),
        image.to_string_lossy().into_owned(),
        "mkpart".to_owned(),
        "primary".to_owned(),
        start.to_owned(),
        end.to_owned(),
    ]
}

/// `mkfs.exfat -L <label> <device>`.
pub(crate) fn mkfs_exfat_args(device: &Path, label: &str) -> Vec<String> {
    vec![
        "-L".to_owned(),
        label.to_owned(),
        device.to_string_lossy().into_owned(),
    ]
}

/// `sfdisk --part-type <image> <part_num> 7` — set MBR partition type to
/// `0x07` (HPFS/NTFS/exFAT), which Tesla and Windows recognise for exFAT.
pub(crate) fn sfdisk_parttype_args(image: &Path, part_num: u8) -> Vec<String> {
    vec![
        "--part-type".to_owned(),
        image.to_string_lossy().into_owned(),
        part_num.to_string(),
        "7".to_owned(),
    ]
}

fn run(program: &str, args: &[String]) -> io::Result<String> {
    let output = Command::new(program).args(args).output()?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(io::Error::other(format!(
            "`{program} {}` failed ({}): {}",
            args.join(" "),
            output.status,
            stderr.trim()
        )))
    }
}

/// Provision a single-partition backing image to [`ImagePlan`] if it does not
/// already exist. Idempotent: returns `Ok(false)` (no work) when the image is
/// present.
///
/// The image is built at a sibling `*.partial` path and atomically renamed
/// into place only after every step succeeds, so a failure or crash never
/// leaves a half-formatted image that a later run would mistake for valid.
///
/// # Errors
/// Returns an error if the layout is invalid, the target is currently exported
/// by a bound gadget, or any provisioning command fails.
pub(crate) fn provision_image(plan: &ImagePlan) -> io::Result<bool> {
    plan.validate()?;
    if plan.image.exists() {
        return Ok(false);
    }
    // #1-invariant guard: never touch a backing file the car is actively using.
    if crate::exec::image_is_exported(Path::new(crate::config::DEFAULT_CONFIGFS_ROOT), &plan.image)
    {
        return Err(io::Error::other(format!(
            "refusing to provision {}: it is exported by a bound gadget",
            plan.image.display()
        )));
    }
    if let Some(parent) = plan.image.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }

    let tmp = partial_path(&plan.image);
    let _ = std::fs::remove_file(&tmp); // discard any stale partial

    let result = build_image(plan, &tmp);
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
        result?;
    }
    std::fs::rename(&tmp, &plan.image)?;
    Ok(true)
}

fn build_image(plan: &ImagePlan, tmp: &Path) -> io::Result<()> {
    run("fallocate", &fallocate_args(tmp, plan.size_mib))?;
    run("parted", &parted_label_args(tmp))?;
    // One partition spanning the whole image after the 1 MiB alignment gap.
    run("parted", &parted_mkpart_args(tmp, "1MiB", "100%"))?;

    let loop_dev = run(
        "losetup",
        &[
            "-fP".to_owned(),
            "--show".to_owned(),
            tmp.to_string_lossy().into_owned(),
        ],
    )?;
    let loop_dev = loop_dev.trim().to_owned();
    let formatted = format_and_seed(plan, &loop_dev);
    let detached = run("losetup", &["-d".to_owned(), loop_dev.clone()]);
    formatted?;
    detached?; // a leaked loop device must fail provisioning, not be ignored

    run("sfdisk", &sfdisk_parttype_args(tmp, 1))?;
    Ok(())
}

/// Sibling `<image>.partial` path (preserves the full filename, unlike
/// `Path::with_extension`).
fn partial_path(image: &Path) -> PathBuf {
    let mut s = image.as_os_str().to_owned();
    s.push(".partial");
    PathBuf::from(s)
}

fn format_and_seed(plan: &ImagePlan, loop_dev: &str) -> io::Result<()> {
    let p1 = PathBuf::from(format!("{loop_dev}p1"));
    wait_for_path(&p1)?;
    run("mkfs.exfat", &mkfs_exfat_args(&p1, &plan.label))?;

    if !plan.seed_teslacam {
        return Ok(());
    }

    // Ensure the TeslaCam directory exists on the TeslaCam image (the car needs
    // it to record). A unique mountpoint avoids colliding with a concurrent or
    // crashed run.
    let mnt = std::env::temp_dir().join(format!("gadgetd-provision-{}", std::process::id()));
    std::fs::create_dir_all(&mnt)?;
    run(
        "mount",
        &[
            "-t".to_owned(),
            "exfat".to_owned(),
            p1.to_string_lossy().into_owned(),
            mnt.to_string_lossy().into_owned(),
        ],
    )?;
    let seed = std::fs::create_dir_all(mnt.join("TeslaCam"));
    let unmounted = run("umount", &[mnt.to_string_lossy().into_owned()]);
    let _ = std::fs::remove_dir(&mnt);
    seed?;
    unmounted?; // a left-mounted partition must fail, not silently succeed
    Ok(())
}

/// Poll for a device node to appear (loop partition nodes can lag `losetup -P`).
fn wait_for_path(path: &Path) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !path.exists() {
        if Instant::now() >= deadline {
            return Err(io::Error::other(format!(
                "partition node {} did not appear within 5s",
                path.display()
            )));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        ImagePlan, fallocate_args, mkfs_exfat_args, parted_label_args, parted_mkpart_args,
        sfdisk_parttype_args,
    };
    use std::path::{Path, PathBuf};

    fn teslacam_plan() -> ImagePlan {
        ImagePlan::teslacam(PathBuf::from("/data/teslacam.img"), 3072)
    }

    fn media_plan() -> ImagePlan {
        ImagePlan::media(PathBuf::from("/data/media.img"), 1024)
    }

    #[test]
    fn teslacam_plan_labels_and_seeds() {
        let p = teslacam_plan();
        assert_eq!(p.label, "TESLACAM");
        assert!(p.seed_teslacam, "the TeslaCam image must seed TeslaCam/");
    }

    #[test]
    fn media_plan_labels_and_does_not_seed() {
        let p = media_plan();
        assert_eq!(p.label, "MEDIA");
        assert!(!p.seed_teslacam, "the media image must not seed TeslaCam/");
    }

    #[test]
    fn fallocate_uses_full_size_in_mib() {
        assert_eq!(
            fallocate_args(Path::new("/data/teslacam.img"), 3072),
            vec!["-l", "3072MiB", "/data/teslacam.img"]
        );
    }

    #[test]
    fn valid_layouts_pass_validation() {
        assert!(teslacam_plan().validate().is_ok());
        assert!(media_plan().validate().is_ok());
    }

    #[test]
    fn rejects_tiny_teslacam_image() {
        let p = ImagePlan::teslacam(PathBuf::from("/data/t.img"), 8);
        assert!(
            p.validate().is_err(),
            "a TeslaCam image below the minimum must be rejected"
        );
    }

    #[test]
    fn rejects_image_with_no_usable_space() {
        let p = ImagePlan::media(PathBuf::from("/data/m.img"), 1);
        assert!(
            p.validate().is_err(),
            "an image that is all MBR gap must be rejected"
        );
    }

    #[test]
    fn partial_path_keeps_full_filename() {
        assert_eq!(
            super::partial_path(Path::new("/data/teslacam.img")),
            PathBuf::from("/data/teslacam.img.partial")
        );
    }

    #[test]
    fn parted_label_is_msdos() {
        let args = parted_label_args(Path::new("/data/teslacam.img"));
        assert_eq!(args, vec!["-s", "/data/teslacam.img", "mklabel", "msdos"]);
    }

    #[test]
    fn mkpart_spans_whole_image() {
        let args = parted_mkpart_args(Path::new("/data/teslacam.img"), "1MiB", "100%");
        assert_eq!(
            args,
            vec![
                "-s",
                "/data/teslacam.img",
                "mkpart",
                "primary",
                "1MiB",
                "100%"
            ]
        );
    }

    #[test]
    fn mkfs_sets_label() {
        let args = mkfs_exfat_args(Path::new("/dev/loop0p1"), "TESLACAM");
        assert_eq!(args, vec!["-L", "TESLACAM", "/dev/loop0p1"]);
    }

    #[test]
    fn parttype_is_exfat_0x07() {
        let args = sfdisk_parttype_args(Path::new("/data/media.img"), 1);
        assert_eq!(args, vec!["--part-type", "/data/media.img", "1", "7"]);
    }
}
