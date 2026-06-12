//! Static gadget configuration and the **pure planners** that turn it into an
//! ordered list of configfs operations. The planners take no I/O and are fully
//! unit-tested on any host; the executor in [`crate::exec`] performs the ops on
//! the live device.

use std::path::PathBuf;

/// Default configfs mount point for USB gadgets on Linux.
pub(crate) const DEFAULT_CONFIGFS_ROOT: &str = "/sys/kernel/config/usb_gadget";

/// configfs LUN directory name for the primary (`TeslaCam`) backing image.
pub(crate) const TESLACAM_LUN: u8 = 0;
/// configfs LUN directory name for the media backing image.
pub(crate) const MEDIA_LUN: u8 = 1;

/// USB gadget identity and **two-LUN** mass-storage settings.
///
/// The B-1 appliance presents ONE `mass_storage` function with TWO independent
/// LUNs, each backed by its own single-partition image:
/// - `lun.0` ← `teslacam_image` (MBR + 1 exFAT partition `TESLACAM`), and
/// - `lun.1` ← `media_image`    (MBR + 1 exFAT partition `MEDIA`).
///
/// Two LUNs (rather than one image with two partitions) is what lets a media
/// (p2) eject-handoff cycle `lun.1` **without** clearing the car-facing
/// `lun.0`, so dashcam recording is never interrupted by a media write.
///
/// Defaults mirror `setup-lib/11-gadget.sh` from the legacy stack
/// (`idVendor=0x1d6b` Linux Foundation, `idProduct=0x0104` Multifunction
/// Composite Gadget) so the car sees the same device identity.
// The four bools are independent kernel LUN attributes (removable/ro/nofua/
// stall), each mapping 1:1 to a configfs file — folding them into an enum
// would obscure that direct correspondence.
#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone)]
pub(crate) struct GadgetConfig {
    /// configfs gadget root, e.g. `/sys/kernel/config/usb_gadget`.
    pub(crate) configfs_root: PathBuf,
    /// Gadget directory name under the root (e.g. `teslausb`).
    pub(crate) name: String,
    /// Mass-storage function instance name (e.g. `mass_storage.usb0`).
    pub(crate) function: String,
    /// USB vendor id string written verbatim to configfs (e.g. `0x1d6b`).
    pub(crate) id_vendor: String,
    /// USB product id string (e.g. `0x0104`).
    pub(crate) id_product: String,
    /// Device release bcd (e.g. `0x0100`).
    pub(crate) bcd_device: String,
    /// USB spec bcd (e.g. `0x0200`).
    pub(crate) bcd_usb: String,
    /// Serial number string.
    pub(crate) serial: String,
    /// Manufacturer string.
    pub(crate) manufacturer: String,
    /// Product string.
    pub(crate) product: String,
    /// Configuration label string (e.g. `Config 1`).
    pub(crate) config_label: String,
    /// `MaxPower` in milliamps.
    pub(crate) max_power_ma: u32,
    /// Backing image presented as `lun.0` (MBR + 1 exFAT partition `TESLACAM`).
    /// This is the **sacred** car-facing dashcam LUN — it is the highest-value
    /// invariant and is recovered ahead of (and independently from) `lun.1`.
    pub(crate) teslacam_image: PathBuf,
    /// Backing image presented as `lun.1` (MBR + 1 exFAT partition `MEDIA`).
    /// Cycling this LUN during a media handoff must NEVER disturb `lun.0`.
    pub(crate) media_image: PathBuf,
    /// Whether the LUNs advertise as removable (Tesla expects removable).
    pub(crate) removable: bool,
    /// Whether the media LUN (`lun.1`) is advertised read-only so the car
    /// cannot write media exFAT metadata; `lun.0` is always read-write
    /// because the `TeslaCam` records to it.
    pub(crate) media_read_only: bool,
    /// Disable Force-Unit-Access. **Keep `false`** so host flushes are durable
    /// across an abrupt Pi crash (proven on the bench, 2026-06-08).
    pub(crate) nofua: bool,
    /// Whether the function stalls on unsupported SCSI commands.
    pub(crate) stall: bool,
}

impl GadgetConfig {
    /// Build the production default config for the two backing images.
    pub(crate) fn teslausb(teslacam_image: PathBuf, media_image: PathBuf) -> Self {
        Self {
            configfs_root: PathBuf::from(DEFAULT_CONFIGFS_ROOT),
            name: "teslausb".to_owned(),
            function: "mass_storage.usb0".to_owned(),
            id_vendor: "0x1d6b".to_owned(),
            id_product: "0x0104".to_owned(),
            bcd_device: "0x0100".to_owned(),
            bcd_usb: "0x0200".to_owned(),
            serial: "teslausb-b1".to_owned(),
            manufacturer: "TeslaUSB".to_owned(),
            product: "TeslaUSB B-1".to_owned(),
            config_label: "Config 1".to_owned(),
            max_power_ma: 250,
            teslacam_image,
            media_image,
            removable: true,
            media_read_only: true,
            nofua: false,
            stall: true,
        }
    }

    /// Backing image for a LUN index (`0` = `TeslaCam`, `1` = MEDIA).
    pub(crate) fn image_for_lun(&self, lun: u8) -> &std::path::Path {
        if lun == MEDIA_LUN {
            &self.media_image
        } else {
            &self.teslacam_image
        }
    }

    /// `<root>/<name>` — the gadget directory.
    pub(crate) fn gadget_dir(&self) -> PathBuf {
        self.configfs_root.join(&self.name)
    }

    /// `<gadget>/functions/<function>`.
    pub(crate) fn function_dir(&self) -> PathBuf {
        self.gadget_dir().join("functions").join(&self.function)
    }

    /// `<function>/lun.<index>`.
    pub(crate) fn lun_dir(&self, lun: u8) -> PathBuf {
        self.function_dir().join(format!("lun.{lun}"))
    }

    /// `<gadget>/configs/c.1`.
    pub(crate) fn config_dir(&self) -> PathBuf {
        self.gadget_dir().join("configs").join("c.1")
    }

    /// `<gadget>/UDC` — writing a UDC name here binds the gadget.
    pub(crate) fn udc_path(&self) -> PathBuf {
        self.gadget_dir().join("UDC")
    }

    fn bool_attr(value: bool) -> String {
        if value {
            "1".to_owned()
        } else {
            "0".to_owned()
        }
    }
}

/// A single reversible configfs mutation. The executor applies these in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ConfigfsOp {
    /// `mkdir -p` the given directory.
    Mkdir(PathBuf),
    /// Write the string (no trailing newline added) to the given attribute file.
    Write(PathBuf, String),
    /// Create a symlink `link -> target` (configfs config<-function wiring).
    Symlink {
        /// The existing function directory the link points at.
        target: PathBuf,
        /// The link path created inside the config directory.
        link: PathBuf,
    },
    /// Remove a symlink.
    Unlink(PathBuf),
    /// `rmdir` an (empty) configfs directory.
    Rmdir(PathBuf),
}

/// Plan the ordered ops that build the gadget tree and bind it to `udc_name`.
///
/// The UDC bind is intentionally **last**: configfs requires the whole gadget
/// to be fully described before a UDC is attached.
pub(crate) fn plan_bring_up(cfg: &GadgetConfig, udc_name: &str) -> Vec<ConfigfsOp> {
    let gadget = cfg.gadget_dir();
    let strings = gadget.join("strings").join("0x409");
    let cfg_dir = cfg.config_dir();
    let cfg_strings = cfg_dir.join("strings").join("0x409");
    let func = cfg.function_dir();
    let lun0 = cfg.lun_dir(TESLACAM_LUN);
    let lun1 = cfg.lun_dir(MEDIA_LUN);

    let mut ops = vec![
        ConfigfsOp::Mkdir(gadget.clone()),
        ConfigfsOp::Write(gadget.join("idVendor"), cfg.id_vendor.clone()),
        ConfigfsOp::Write(gadget.join("idProduct"), cfg.id_product.clone()),
        ConfigfsOp::Write(gadget.join("bcdDevice"), cfg.bcd_device.clone()),
        ConfigfsOp::Write(gadget.join("bcdUSB"), cfg.bcd_usb.clone()),
        ConfigfsOp::Mkdir(strings.clone()),
        ConfigfsOp::Write(strings.join("serialnumber"), cfg.serial.clone()),
        ConfigfsOp::Write(strings.join("manufacturer"), cfg.manufacturer.clone()),
        ConfigfsOp::Write(strings.join("product"), cfg.product.clone()),
        ConfigfsOp::Mkdir(cfg_strings.clone()),
        ConfigfsOp::Write(cfg_strings.join("configuration"), cfg.config_label.clone()),
        ConfigfsOp::Write(cfg_dir.join("MaxPower"), cfg.max_power_ma.to_string()),
        ConfigfsOp::Mkdir(func.clone()),
        ConfigfsOp::Write(func.join("stall"), GadgetConfig::bool_attr(cfg.stall)),
    ];

    // lun.0 (TeslaCam) is auto-created with the function instance; lun.1 (MEDIA)
    // must be explicitly `mkdir`-ed. Both are described BEFORE the UDC bind.
    push_lun_ops(
        &mut ops,
        cfg,
        &lun0,
        cfg.teslacam_image.as_path(),
        false,
        false,
    );
    push_lun_ops(
        &mut ops,
        cfg,
        &lun1,
        cfg.media_image.as_path(),
        true,
        cfg.media_read_only,
    );

    ops.push(ConfigfsOp::Symlink {
        target: func,
        link: cfg_dir.join(&cfg.function),
    });
    // UDC bind is intentionally LAST: the whole gadget (both LUNs) must be fully
    // described before any controller is attached.
    ops.push(ConfigfsOp::Write(cfg.udc_path(), udc_name.to_owned()));

    ops.shrink_to_fit();
    ops
}

/// Emit the attribute writes for one LUN. `mkdir_first` is `true` for any LUN
/// beyond `lun.0` (which the kernel auto-creates with the function instance).
fn push_lun_ops(
    ops: &mut Vec<ConfigfsOp>,
    cfg: &GadgetConfig,
    lun: &std::path::Path,
    image: &std::path::Path,
    mkdir_first: bool,
    read_only: bool,
) {
    if mkdir_first {
        ops.push(ConfigfsOp::Mkdir(lun.to_path_buf()));
    }
    ops.push(ConfigfsOp::Write(
        lun.join("cdrom"),
        GadgetConfig::bool_attr(false),
    ));
    ops.push(ConfigfsOp::Write(
        lun.join("ro"),
        GadgetConfig::bool_attr(read_only),
    ));
    ops.push(ConfigfsOp::Write(
        lun.join("nofua"),
        GadgetConfig::bool_attr(cfg.nofua),
    ));
    ops.push(ConfigfsOp::Write(
        lun.join("removable"),
        GadgetConfig::bool_attr(cfg.removable),
    ));
    ops.push(ConfigfsOp::Write(
        lun.join("file"),
        image.to_string_lossy().into_owned(),
    ));
}

/// Plan the ordered ops that unbind and dismantle the gadget tree.
///
/// Order is the reverse of bring-up: unbind the UDC first (so the host sees a
/// clean disconnect), drop the symlink, then remove directories leaf-first.
///
/// The unbind writes a newline (matching `echo "" > UDC`) rather than a
/// zero-length buffer: `std::fs::write` performs no `write(2)` syscall for an
/// empty slice, so a guaranteed-non-empty payload keeps the unbind explicit and
/// portable across kernels.
pub(crate) fn plan_tear_down(cfg: &GadgetConfig) -> Vec<ConfigfsOp> {
    let gadget = cfg.gadget_dir();
    let strings = gadget.join("strings").join("0x409");
    let cfg_dir = cfg.config_dir();
    let cfg_strings = cfg_dir.join("strings").join("0x409");
    let func = cfg.function_dir();

    vec![
        ConfigfsOp::Write(cfg.udc_path(), "\n".to_owned()),
        ConfigfsOp::Unlink(cfg_dir.join(&cfg.function)),
        // lun.1 was created by us, so it must be removed before the function dir;
        // lun.0 is auto-removed when the function instance is destroyed.
        ConfigfsOp::Rmdir(cfg.lun_dir(MEDIA_LUN)),
        ConfigfsOp::Rmdir(cfg_strings),
        ConfigfsOp::Rmdir(cfg_dir),
        ConfigfsOp::Rmdir(func),
        ConfigfsOp::Rmdir(strings),
        ConfigfsOp::Rmdir(gadget),
    ]
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{ConfigfsOp, GadgetConfig, plan_bring_up, plan_tear_down};
    use std::path::PathBuf;

    fn test_cfg(root: &str) -> GadgetConfig {
        let mut cfg = GadgetConfig::teslausb(
            PathBuf::from("/data/teslacam.img"),
            PathBuf::from("/data/media.img"),
        );
        cfg.configfs_root = PathBuf::from(root);
        cfg.name = "teslausb".to_owned();
        cfg
    }

    #[test]
    fn bring_up_writes_udc_last() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "3f980000.usb");
        let last = ops.last().expect("non-empty plan");
        match last {
            ConfigfsOp::Write(path, value) => {
                assert!(
                    path.ends_with("teslausb/UDC"),
                    "last op binds UDC, got {path:?}"
                );
                assert_eq!(value, "3f980000.usb");
            }
            other => panic!("expected UDC write last, got {other:?}"),
        }
    }

    #[test]
    fn bring_up_creates_gadget_dir_first() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        match ops.first().expect("non-empty plan") {
            ConfigfsOp::Mkdir(path) => assert_eq!(path, &PathBuf::from("/cfgroot/teslausb")),
            other => panic!("expected gadget mkdir first, got {other:?}"),
        }
    }

    #[test]
    fn bring_up_points_lun0_at_teslacam_image() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let has_lun_file = ops.iter().any(|op| {
            matches!(op, ConfigfsOp::Write(p, v)
                if p.ends_with("lun.0/file") && v == "/data/teslacam.img")
        });
        assert!(has_lun_file, "lun.0/file must point at the TeslaCam image");
    }

    #[test]
    fn bring_up_points_lun1_at_media_image() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let has_lun_file = ops.iter().any(|op| {
            matches!(op, ConfigfsOp::Write(p, v)
                if p.ends_with("lun.1/file") && v == "/data/media.img")
        });
        assert!(has_lun_file, "lun.1/file must point at the MEDIA image");
    }

    #[test]
    fn bring_up_creates_media_lun_dir_before_binding() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let mkdir_idx = ops.iter().position(
            |op| matches!(op, ConfigfsOp::Mkdir(p) if p.ends_with("mass_storage.usb0/lun.1")),
        );
        let udc_idx = ops
            .iter()
            .position(|op| matches!(op, ConfigfsOp::Write(p, _) if p.ends_with("teslausb/UDC")));
        let (Some(m), Some(u)) = (mkdir_idx, udc_idx) else {
            panic!("expected both a lun.1 mkdir and a UDC write");
        };
        assert!(m < u, "lun.1 must be created before the UDC bind");
    }

    #[test]
    fn bring_up_does_not_mkdir_lun0() {
        // lun.0 is auto-created with the function instance; an explicit mkdir
        // would fail (EEXIST/EBUSY) on the live kernel.
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let mkdir_lun0 = ops
            .iter()
            .any(|op| matches!(op, ConfigfsOp::Mkdir(p) if p.ends_with("lun.0")));
        assert!(!mkdir_lun0, "lun.0 must NOT be explicitly mkdir-ed");
    }

    #[test]
    fn bring_up_keeps_fua_enabled_for_durability() {
        // nofua must be written "0" on BOTH LUNs so host flushes are honoured.
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        for lun in ["lun.0/nofua", "lun.1/nofua"] {
            let nofua = ops.iter().find_map(|op| match op {
                ConfigfsOp::Write(p, v) if p.ends_with(lun) => Some(v.clone()),
                _ => None,
            });
            assert_eq!(
                nofua.as_deref(),
                Some("0"),
                "{lun} must be 0 (FUA honoured)"
            );
        }
    }

    #[test]
    fn bring_up_marks_media_lun_read_only() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let expected = GadgetConfig::bool_attr(true);
        let ro = ops.iter().find_map(|op| match op {
            ConfigfsOp::Write(p, v) if p.ends_with("lun.1/ro") => Some(v),
            _ => None,
        });
        assert_eq!(ro, Some(&expected));
    }

    #[test]
    fn bring_up_keeps_teslacam_lun_writable() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let expected = GadgetConfig::bool_attr(false);
        let ro = ops.iter().find_map(|op| match op {
            ConfigfsOp::Write(p, v) if p.ends_with("lun.0/ro") => Some(v),
            _ => None,
        });
        assert_eq!(ro, Some(&expected));
    }

    #[test]
    fn bring_up_wires_function_into_config() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let wired = ops.iter().any(|op| {
            matches!(op, ConfigfsOp::Symlink { target, link }
            if target.ends_with("functions/mass_storage.usb0")
                && link.ends_with("configs/c.1/mass_storage.usb0"))
        });
        assert!(wired, "function must be symlinked into the config");
    }

    #[test]
    fn tear_down_unbinds_udc_first() {
        let ops = plan_tear_down(&test_cfg("/cfgroot"));
        match ops.first().expect("non-empty plan") {
            ConfigfsOp::Write(path, value) => {
                assert!(path.ends_with("teslausb/UDC"));
                assert!(value.trim().is_empty(), "unbind writes blank (newline) UDC");
                assert!(
                    !value.is_empty(),
                    "payload must be non-empty so write(2) fires"
                );
            }
            other => panic!("expected UDC unbind first, got {other:?}"),
        }
    }

    #[test]
    fn tear_down_removes_gadget_dir_last() {
        let ops = plan_tear_down(&test_cfg("/cfgroot"));
        match ops.last().expect("non-empty plan") {
            ConfigfsOp::Rmdir(path) => assert_eq!(path, &PathBuf::from("/cfgroot/teslausb")),
            other => panic!("expected gadget rmdir last, got {other:?}"),
        }
    }

    #[test]
    fn tear_down_removes_media_lun_before_function() {
        let ops = plan_tear_down(&test_cfg("/cfgroot"));
        let lun1_idx = ops
            .iter()
            .position(|op| matches!(op, ConfigfsOp::Rmdir(p) if p.ends_with("lun.1")));
        let func_idx = ops.iter().position(
            |op| matches!(op, ConfigfsOp::Rmdir(p) if p.ends_with("functions/mass_storage.usb0")),
        );
        let (Some(l), Some(f)) = (lun1_idx, func_idx) else {
            panic!("expected both a lun.1 rmdir and a function rmdir");
        };
        assert!(l < f, "lun.1 must be removed before the function dir");
    }
}
