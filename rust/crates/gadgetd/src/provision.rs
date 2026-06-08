//! Backing-image provisioning: create `disk.img`, lay out an MBR with two
//! exFAT partitions (p1 `TeslaCam`, p2 media), format them, and ensure the
//! `TeslaCam` directory exists so the car enables dashcam recording.
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
/// Smallest partition-1 (`TeslaCam`) we will create.
const MIN_P1_MIB: u64 = 16;
/// Smallest partition-2 (media) we will create.
const MIN_MEDIA_MIB: u64 = 1;

/// Desired on-disk layout for the backing image.
#[derive(Debug, Clone)]
pub(crate) struct PartitionPlan {
    /// Backing image path on the Pi's ext4 data area.
    pub(crate) image: PathBuf,
    /// Total image size in MiB (fully allocated — never sparse).
    pub(crate) size_mib: u64,
    /// exFAT label for partition 1 (`TeslaCam` / dashcam).
    pub(crate) p1_label: String,
    /// End offset of partition 1 in MiB; partition 2 takes the remainder.
    pub(crate) p1_end_mib: u64,
    /// exFAT label for partition 2 (media: chimes / lightshow / boombox).
    pub(crate) p2_label: String,
}

impl PartitionPlan {
    /// A sensible default split for a `size_mib` image: p1 gets all but the
    /// final ~`media_mib` MiB (reserving a 1 MiB MBR-alignment gap at the
    /// front), p2 gets the remainder.
    pub(crate) fn split(image: PathBuf, size_mib: u64, media_mib: u64) -> Self {
        let p1_end = size_mib.saturating_sub(media_mib).max(2);
        Self {
            image,
            size_mib,
            p1_label: "TESLACAM".to_owned(),
            p1_end_mib: p1_end,
            p2_label: "MEDIA".to_owned(),
        }
    }

    /// Reject layouts that would produce an unusable or partial image (a zero
    /// or out-of-range partition). Sizes are validated up front so a bad CLI
    /// value never reaches `fallocate`/`parted`.
    ///
    /// # Errors
    /// Returns an error describing the first constraint violated.
    fn validate(&self) -> io::Result<()> {
        let media_mib = self.size_mib.saturating_sub(self.p1_end_mib);
        if media_mib < MIN_MEDIA_MIB {
            return Err(io::Error::other(format!(
                "media partition too small ({media_mib} MiB; need >= {MIN_MEDIA_MIB})"
            )));
        }
        if self.p1_end_mib <= RESERVE_MIB
            || self.p1_end_mib.saturating_sub(RESERVE_MIB) < MIN_P1_MIB
        {
            return Err(io::Error::other(format!(
                "TeslaCam partition too small (p1 end {} MiB; need >= {} MiB usable)",
                self.p1_end_mib,
                RESERVE_MIB + MIN_P1_MIB
            )));
        }
        if self.p1_end_mib >= self.size_mib {
            return Err(io::Error::other(format!(
                "partition layout exceeds image ({} MiB end vs {} MiB total)",
                self.p1_end_mib, self.size_mib
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

/// Provision the backing image to [`PartitionPlan`] if it does not already
/// exist. Idempotent: returns `Ok(false)` (no work) when the image is present.
///
/// The image is built at a sibling `*.partial` path and atomically renamed
/// into place only after every step succeeds, so a failure or crash never
/// leaves a half-formatted image that a later run would mistake for valid.
///
/// # Errors
/// Returns an error if the layout is invalid, the target is currently exported
/// by a bound gadget, or any provisioning command fails.
pub(crate) fn provision_image(plan: &PartitionPlan) -> io::Result<bool> {
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

fn build_image(plan: &PartitionPlan, tmp: &Path) -> io::Result<()> {
    run("fallocate", &fallocate_args(tmp, plan.size_mib))?;
    run("parted", &parted_label_args(tmp))?;
    let p1_end = format!("{}MiB", plan.p1_end_mib);
    run("parted", &parted_mkpart_args(tmp, "1MiB", &p1_end))?;
    run("parted", &parted_mkpart_args(tmp, &p1_end, "100%"))?;

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
    run("sfdisk", &sfdisk_parttype_args(tmp, 2))?;
    Ok(())
}

/// Sibling `<image>.partial` path (preserves the full filename, unlike
/// `Path::with_extension`).
fn partial_path(image: &Path) -> PathBuf {
    let mut s = image.as_os_str().to_owned();
    s.push(".partial");
    PathBuf::from(s)
}

fn format_and_seed(plan: &PartitionPlan, loop_dev: &str) -> io::Result<()> {
    let p1 = PathBuf::from(format!("{loop_dev}p1"));
    let p2 = PathBuf::from(format!("{loop_dev}p2"));
    wait_for_path(&p1)?;
    wait_for_path(&p2)?;
    run("mkfs.exfat", &mkfs_exfat_args(&p1, &plan.p1_label))?;
    run("mkfs.exfat", &mkfs_exfat_args(&p2, &plan.p2_label))?;

    // Ensure the TeslaCam directory exists on p1 (the car needs it to record).
    // A unique mountpoint avoids colliding with a concurrent or crashed run.
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
        PartitionPlan, fallocate_args, mkfs_exfat_args, parted_label_args, parted_mkpart_args,
        sfdisk_parttype_args,
    };
    use std::path::{Path, PathBuf};

    fn plan() -> PartitionPlan {
        PartitionPlan::split(PathBuf::from("/data/disk.img"), 4096, 1024)
    }

    #[test]
    fn split_reserves_media_at_tail() {
        let p = plan();
        assert_eq!(
            p.p1_end_mib, 3072,
            "p1 ends where the 1 GiB media tail begins"
        );
        assert_eq!(p.p1_label, "TESLACAM");
        assert_eq!(p.p2_label, "MEDIA");
    }

    #[test]
    fn fallocate_uses_full_size_in_mib() {
        assert_eq!(
            fallocate_args(Path::new("/data/disk.img"), 4096),
            vec!["-l", "4096MiB", "/data/disk.img"]
        );
    }

    #[test]
    fn valid_layout_passes_validation() {
        assert!(plan().validate().is_ok());
    }

    #[test]
    fn rejects_media_partition_larger_than_image() {
        // media >= size leaves p1_end clamped to 2 -> media partition vanishes.
        let p = PartitionPlan::split(PathBuf::from("/data/d.img"), 64, 64);
        assert!(p.validate().is_err(), "media >= size must be rejected");
    }

    #[test]
    fn rejects_tiny_image() {
        let p = PartitionPlan::split(PathBuf::from("/data/d.img"), 8, 1);
        assert!(
            p.validate().is_err(),
            "p1 below the minimum must be rejected"
        );
    }

    #[test]
    fn rejects_zero_media() {
        let p = PartitionPlan::split(PathBuf::from("/data/d.img"), 4096, 0);
        assert!(
            p.validate().is_err(),
            "zero media partition must be rejected"
        );
    }

    #[test]
    fn partial_path_keeps_full_filename() {
        assert_eq!(
            super::partial_path(Path::new("/data/disk.img")),
            PathBuf::from("/data/disk.img.partial")
        );
    }

    #[test]
    fn parted_label_is_msdos() {
        let args = parted_label_args(Path::new("/data/disk.img"));
        assert_eq!(args, vec!["-s", "/data/disk.img", "mklabel", "msdos"]);
    }

    #[test]
    fn mkpart_passes_start_and_end() {
        let args = parted_mkpart_args(Path::new("/data/disk.img"), "1MiB", "3072MiB");
        assert_eq!(
            args,
            vec![
                "-s",
                "/data/disk.img",
                "mkpart",
                "primary",
                "1MiB",
                "3072MiB"
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
        let args = sfdisk_parttype_args(Path::new("/data/disk.img"), 2);
        assert_eq!(args, vec!["--part-type", "/data/disk.img", "2", "7"]);
    }
}
