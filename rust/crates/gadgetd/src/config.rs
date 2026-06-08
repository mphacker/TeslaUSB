//! Static gadget configuration and the **pure planners** that turn it into an
//! ordered list of configfs operations. The planners take no I/O and are fully
//! unit-tested on any host; the executor in [`crate::exec`] performs the ops on
//! the live device.

use std::path::PathBuf;

/// Default configfs mount point for USB gadgets on Linux.
pub(crate) const DEFAULT_CONFIGFS_ROOT: &str = "/sys/kernel/config/usb_gadget";

/// USB gadget identity and single-LUN mass-storage settings.
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
    /// Backing image file presented as the LUN (`file=disk.img`).
    pub(crate) lun_file: PathBuf,
    /// Whether the LUN advertises as removable (Tesla expects removable).
    pub(crate) removable: bool,
    /// Whether the LUN is read-only.
    pub(crate) read_only: bool,
    /// Disable Force-Unit-Access. **Keep `false`** so host flushes are durable
    /// across an abrupt Pi crash (proven on the bench, 2026-06-08).
    pub(crate) nofua: bool,
    /// Whether the function stalls on unsupported SCSI commands.
    pub(crate) stall: bool,
}

impl GadgetConfig {
    /// Build the production default config for the given backing image.
    pub(crate) fn teslausb(lun_file: PathBuf) -> Self {
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
            lun_file,
            removable: true,
            read_only: false,
            nofua: false,
            stall: true,
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

    /// `<function>/lun.0`.
    pub(crate) fn lun_dir(&self) -> PathBuf {
        self.function_dir().join("lun.0")
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
    let lun = cfg.lun_dir();

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
        ConfigfsOp::Write(lun.join("cdrom"), GadgetConfig::bool_attr(false)),
        ConfigfsOp::Write(lun.join("ro"), GadgetConfig::bool_attr(cfg.read_only)),
        ConfigfsOp::Write(lun.join("nofua"), GadgetConfig::bool_attr(cfg.nofua)),
        ConfigfsOp::Write(
            lun.join("removable"),
            GadgetConfig::bool_attr(cfg.removable),
        ),
        ConfigfsOp::Write(
            lun.join("file"),
            cfg.lun_file.to_string_lossy().into_owned(),
        ),
        ConfigfsOp::Symlink {
            target: func,
            link: cfg_dir.join(&cfg.function),
        },
        ConfigfsOp::Write(cfg.udc_path(), udc_name.to_owned()),
    ];
    ops.shrink_to_fit();
    ops
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
        let mut cfg = GadgetConfig::teslausb(PathBuf::from("/data/disk.img"));
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
    fn bring_up_points_lun_at_backing_image() {
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let has_lun_file = ops.iter().any(|op| {
            matches!(op, ConfigfsOp::Write(p, v)
                if p.ends_with("lun.0/file") && v == "/data/disk.img")
        });
        assert!(has_lun_file, "lun.0/file must point at the backing image");
    }

    #[test]
    fn bring_up_keeps_fua_enabled_for_durability() {
        // nofua must be written "0" so the host's flushes are honoured.
        let ops = plan_bring_up(&test_cfg("/cfgroot"), "udc0");
        let nofua = ops.iter().find_map(|op| match op {
            ConfigfsOp::Write(p, v) if p.ends_with("lun.0/nofua") => Some(v.clone()),
            _ => None,
        });
        assert_eq!(
            nofua.as_deref(),
            Some("0"),
            "nofua must be 0 (FUA honoured)"
        );
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
}
