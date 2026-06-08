//! `gadgetd` — the guardian of the car-facing write path.
//!
//! This binary owns the kernel `usb_f_mass_storage` gadget backed by a single
//! `file=disk.img` LUN. It provisions the backing image, brings the gadget up
//! at boot, reports status, and tears it down cleanly. Keeping this the only
//! writer of the gadget configfs tree is what preserves the #1 invariant: the
//! car must always be able to write `TeslaCam`, and a Pi crash must look like a
//! clean unplug — never EIO.
//!
//! Subcommands:
//! - `provision --image <p> --size-mib <n> [--media-mib <n>]` — create the
//!   MBR + 2× exFAT backing image (idempotent).
//! - `up [--image <p>]` — build the gadget tree and bind the UDC.
//! - `down [--image <p>]` — unbind and dismantle the gadget tree.
//! - `status [--image <p>]` — print the current binding.

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

mod config;
mod exec;
mod provision;

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use config::GadgetConfig;
use provision::PartitionPlan;

/// Default backing image location on the Pi's data area.
const DEFAULT_IMAGE: &str = "/data/teslausb/disk.img";
/// Default total backing-image size (MiB) when provisioning.
const DEFAULT_SIZE_MIB: u64 = 4096;
/// Default media-partition size (MiB) carved off the tail.
const DEFAULT_MEDIA_MIB: u64 = 1024;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("gadgetd: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let Some(command) = args.first() else {
        return Err(usage());
    };
    let image =
        opt_flag(args, "--image")?.map_or_else(|| PathBuf::from(DEFAULT_IMAGE), PathBuf::from);

    match command.as_str() {
        "provision" => cmd_provision(args, &image),
        "up" => cmd_up(args, image),
        "down" => cmd_down(image),
        "status" => {
            cmd_status(image);
            Ok(())
        }
        "--help" | "-h" | "help" => {
            println!("{}", usage());
            Ok(())
        }
        other => Err(format!("unknown command `{other}`\n{}", usage())),
    }
}

fn cmd_provision(args: &[String], image: &Path) -> Result<(), String> {
    let size_mib = parse_flag(args, "--size-mib", DEFAULT_SIZE_MIB)?;
    let media_mib = parse_flag(args, "--media-mib", DEFAULT_MEDIA_MIB)?;
    let plan = PartitionPlan::split(image.to_path_buf(), size_mib, media_mib);
    let created = provision::provision_image(&plan).map_err(|e| e.to_string())?;
    if created {
        println!(
            "provisioned {} ({size_mib} MiB, MBR + 2x exFAT)",
            image.display()
        );
    } else {
        println!("{} already exists; left untouched", image.display());
    }
    Ok(())
}

fn cmd_up(args: &[String], image: PathBuf) -> Result<(), String> {
    let udc_pref = opt_flag(args, "--udc")?;
    let cfg = GadgetConfig::teslausb(image);
    let status = exec::read_status(&cfg);

    // SAFETY (#1 invariant): never disturb a gadget that is already bound and
    // serving the host — tearing it down would look like a mid-write yank.
    if status.present && status.bound_udc.is_some() {
        println!(
            "gadget already up: bound {} -> {}",
            status.bound_udc.as_deref().unwrap_or("?"),
            cfg.lun_file.display()
        );
        return Ok(());
    }

    // A present-but-unbound tree is a half-built or stale leftover (configfs is
    // tmpfs, so this only happens within a boot, never across one). Configfs
    // locks function attributes such as `stall` once the function is linked
    // into a config, so a fresh rebuild — not an in-place rewrite — is required.
    // This is safe precisely because the tree is unbound (not serving the host).
    if status.present {
        exec::execute_ops(&config::plan_tear_down(&cfg)).map_err(|e| e.to_string())?;
    }

    let udc = exec::detect_udc(udc_pref.as_deref()).map_err(|e| e.to_string())?;
    let ops = config::plan_bring_up(&cfg, &udc);
    exec::execute_ops(&ops).map_err(|e| e.to_string())?;

    // Post-condition: confirm the bind actually took, so a silent failure can
    // never be reported as success for the car-facing write path.
    let after = exec::read_status(&cfg);
    if after.bound_udc.as_deref() != Some(udc.as_str()) {
        return Err(format!(
            "bring-up verification failed: UDC reads back as {:?}, expected {udc}",
            after.bound_udc
        ));
    }
    println!("gadget up: bound {udc} -> {}", cfg.lun_file.display());
    Ok(())
}

fn cmd_down(image: PathBuf) -> Result<(), String> {
    let cfg = GadgetConfig::teslausb(image);
    if !cfg.gadget_dir().exists() {
        println!("gadget already down: nothing to remove");
        return Ok(());
    }
    exec::execute_ops(&config::plan_tear_down(&cfg)).map_err(|e| e.to_string())?;

    // Post-condition: the gadget dir must be gone (clean unplug completed).
    if cfg.gadget_dir().exists() {
        return Err("tear-down verification failed: gadget directory still present".to_owned());
    }
    println!("gadget down: unbound and removed");
    Ok(())
}

fn cmd_status(image: PathBuf) {
    let cfg = GadgetConfig::teslausb(image);
    let status = exec::read_status(&cfg);
    println!("present:   {}", status.present);
    println!(
        "bound_udc: {}",
        status.bound_udc.as_deref().unwrap_or("(unbound)")
    );
    println!(
        "udc_state: {}",
        status.udc_state.as_deref().unwrap_or("(n/a)")
    );
    println!(
        "lun_file:  {}",
        status.lun_file.as_deref().unwrap_or("(none)")
    );
}

fn opt_flag(args: &[String], name: &str) -> Result<Option<String>, String> {
    match args.iter().position(|a| a == name) {
        None => Ok(None),
        Some(i) => args
            .get(i + 1)
            .cloned()
            .map(Some)
            .ok_or_else(|| format!("missing value for {name}")),
    }
}

fn parse_flag(args: &[String], name: &str, default: u64) -> Result<u64, String> {
    match opt_flag(args, name)? {
        None => Ok(default),
        Some(raw) => raw
            .parse::<u64>()
            .map_err(|_| format!("invalid value for {name}: `{raw}`")),
    }
}

fn usage() -> String {
    "usage: gadgetd <provision|up|down|status> [--image <path>] \
[--size-mib <n>] [--media-mib <n>] [--udc <name>]"
        .to_owned()
}
