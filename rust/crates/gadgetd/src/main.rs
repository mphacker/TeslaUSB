//! `gadgetd` — the guardian of the car-facing write path.
//!
//! This binary owns the kernel `usb_f_mass_storage` gadget backed by TWO
//! single-partition `file=` LUNs: `lun.0` ← `teslacam.img` (the sacred,
//! car-facing `TeslaCam` partition) and `lun.1` ← `media.img` (chimes / lightshow
//! / boombox / music). It provisions the backing images, brings the gadget up
//! at boot, reports status, and tears it down cleanly. Keeping this the only
//! writer of the gadget configfs tree is what preserves the #1 invariant: the
//! car must always be able to write `TeslaCam`, and a Pi crash must look like a
//! clean unplug — never EIO. Splitting media onto its own LUN means a media
//! eject-handoff cycles only `lun.1` and never disturbs the `TeslaCam` LUN.
//!
//! Subcommands:
//! - `provision [--image <p>] [--media-image <p>] [--teslacam-mib <n>]
//!   [--media-mib <n>]` — create the two single-partition exFAT backing images
//!   (idempotent, per image).
//! - `up [--image <p>] [--media-image <p>]` — build the gadget tree and bind the
//!   UDC.
//! - `down [--image <p>] [--media-image <p>]` — unbind and dismantle the gadget
//!   tree.
//! - `status [--image <p>] [--media-image <p>]` — print the current binding.
//! - `serve [--image <p>] [--media-image <p>] [--allow-hot-handoff]` — run the
//!   control daemon: serve the eject-handoff IPC over a Unix socket (gadget
//!   bring-up is owned by a separate unit, so this never disturbs the LUNs).

// systemd captures this binary's stdout/stderr into the journal, so direct
// console output is the intended logging path for the daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

mod config;
mod exec;
#[cfg(unix)]
mod handoff;
#[cfg(unix)]
mod ipc;
#[cfg(unix)]
mod mutate;
mod provision;

use std::path::PathBuf;
use std::process::ExitCode;

use config::GadgetConfig;
use provision::ImagePlan;

/// Default `TeslaCam` (`lun.0`) backing image location on the Pi's data area.
const DEFAULT_IMAGE: &str = "/data/teslausb/teslacam.img";
/// Default media (`lun.1`) backing image location on the Pi's data area.
const DEFAULT_MEDIA_IMAGE: &str = "/data/teslausb/media.img";
/// Default TeslaCam-image size (MiB) when provisioning.
const DEFAULT_TESLACAM_MIB: u64 = 3072;
/// Default media-image size (MiB) when provisioning.
const DEFAULT_MEDIA_MIB: u64 = 1024;
/// Control socket path served by `gadgetd serve`.
#[cfg(unix)]
const DEFAULT_SOCKET: &str = "/run/teslausb/gadgetd.sock";
/// Runtime root for per-handoff mount dirs.
#[cfg(unix)]
const DEFAULT_RUNTIME_ROOT: &str = "/run/teslausb/handoff";

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
    let media_image = opt_flag(args, "--media-image")?
        .map_or_else(|| PathBuf::from(DEFAULT_MEDIA_IMAGE), PathBuf::from);
    let cfg = GadgetConfig::teslausb(image, media_image);

    match command.as_str() {
        "provision" => cmd_provision(args, &cfg),
        "up" => cmd_up(args, &cfg),
        "down" => cmd_down(&cfg),
        "serve" => cmd_serve(args, cfg),
        "status" => {
            cmd_status(&cfg);
            Ok(())
        }
        "--help" | "-h" | "help" => {
            println!("{}", usage());
            Ok(())
        }
        other => Err(format!("unknown command `{other}`\n{}", usage())),
    }
}

#[cfg(unix)]
fn cmd_serve(args: &[String], cfg: GadgetConfig) -> Result<(), String> {
    let allow_hot = args.iter().any(|a| a == "--allow-hot-handoff");
    let socket =
        opt_flag(args, "--socket")?.map_or_else(|| PathBuf::from(DEFAULT_SOCKET), PathBuf::from);
    let runtime_root = PathBuf::from(DEFAULT_RUNTIME_ROOT);
    if allow_hot {
        eprintln!(
            "gadgetd serve: --allow-hot-handoff is set; handoffs may eject while the \
             host is enumerated. This is unsafe on the car until prototype-unknown #2 \
             is measured (SPEC.md §9)."
        );
    }
    ipc::serve(cfg, runtime_root, &socket, allow_hot).map_err(|e| e.to_string())
}

#[cfg(not(unix))]
fn cmd_serve(_args: &[String], _cfg: GadgetConfig) -> Result<(), String> {
    Err("`serve` is only supported on Linux (Unix sockets + loop devices)".to_owned())
}

fn cmd_provision(args: &[String], cfg: &GadgetConfig) -> Result<(), String> {
    let teslacam_mib = parse_flag(args, "--teslacam-mib", DEFAULT_TESLACAM_MIB)?;
    let media_mib = parse_flag(args, "--media-mib", DEFAULT_MEDIA_MIB)?;

    let teslacam = ImagePlan::teslacam(cfg.teslacam_image.clone(), teslacam_mib);
    provision_one(&teslacam, teslacam_mib, "TeslaCam")?;

    let media = ImagePlan::media(cfg.media_image.clone(), media_mib);
    provision_one(&media, media_mib, "media")?;
    Ok(())
}

fn provision_one(plan: &ImagePlan, size_mib: u64, kind: &str) -> Result<(), String> {
    let created = provision::provision_image(plan).map_err(|e| e.to_string())?;
    if created {
        println!(
            "provisioned {kind} image {} ({size_mib} MiB, MBR + 1x exFAT {})",
            plan.image.display(),
            plan.label
        );
    } else {
        println!("{} already exists; left untouched", plan.image.display());
    }
    Ok(())
}

fn cmd_up(args: &[String], cfg: &GadgetConfig) -> Result<(), String> {
    let udc_pref = opt_flag(args, "--udc")?;
    let status = exec::read_status(cfg);

    // SAFETY (#1 invariant): never disturb a gadget that is already bound and
    // serving the host — tearing it down would look like a mid-write yank.
    if status.present && status.bound_udc.is_some() {
        println!(
            "gadget already up: bound {} -> lun.0 {} | lun.1 {}",
            status.bound_udc.as_deref().unwrap_or("?"),
            cfg.teslacam_image.display(),
            cfg.media_image.display()
        );
        return Ok(());
    }

    // A present-but-unbound tree is a half-built or stale leftover (configfs is
    // tmpfs, so this only happens within a boot, never across one). Configfs
    // locks function attributes such as `stall` once the function is linked
    // into a config, so a fresh rebuild — not an in-place rewrite — is required.
    // This is safe precisely because the tree is unbound (not serving the host).
    if status.present {
        exec::execute_ops(&config::plan_tear_down(cfg)).map_err(|e| e.to_string())?;
    }

    let udc = exec::detect_udc(udc_pref.as_deref()).map_err(|e| e.to_string())?;
    let ops = config::plan_bring_up(cfg, &udc);
    exec::execute_ops(&ops).map_err(|e| e.to_string())?;

    // Post-condition: confirm the bind actually took, so a silent failure can
    // never be reported as success for the car-facing write path.
    let after = exec::read_status(cfg);
    if after.bound_udc.as_deref() != Some(udc.as_str()) {
        return Err(format!(
            "bring-up verification failed: UDC reads back as {:?}, expected {udc}",
            after.bound_udc
        ));
    }
    println!(
        "gadget up: bound {udc} -> lun.0 {} | lun.1 {}",
        cfg.teslacam_image.display(),
        cfg.media_image.display()
    );
    Ok(())
}

fn cmd_down(cfg: &GadgetConfig) -> Result<(), String> {
    if !cfg.gadget_dir().exists() {
        println!("gadget already down: nothing to remove");
        return Ok(());
    }
    exec::execute_ops(&config::plan_tear_down(cfg)).map_err(|e| e.to_string())?;

    // Post-condition: the gadget dir must be gone (clean unplug completed).
    if cfg.gadget_dir().exists() {
        return Err("tear-down verification failed: gadget directory still present".to_owned());
    }
    println!("gadget down: unbound and removed");
    Ok(())
}

fn cmd_status(cfg: &GadgetConfig) {
    let status = exec::read_status(cfg);
    println!("present:        {}", status.present);
    println!(
        "bound_udc:      {}",
        status.bound_udc.as_deref().unwrap_or("(unbound)")
    );
    println!(
        "udc_state:      {}",
        status.udc_state.as_deref().unwrap_or("(n/a)")
    );
    println!(
        "lun_file:       {}",
        status.lun_file.as_deref().unwrap_or("(none)")
    );
    println!(
        "media_lun_file: {}",
        status.media_lun_file.as_deref().unwrap_or("(none)")
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
    "usage: gadgetd <provision|up|down|status|serve> [--image <path>] \
[--media-image <path>] [--teslacam-mib <n>] [--media-mib <n>] [--udc <name>] \
[--socket <path>] [--allow-hot-handoff]"
        .to_owned()
}
