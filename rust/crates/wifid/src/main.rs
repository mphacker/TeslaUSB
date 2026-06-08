//! `wifid` — the disposable `WiFi` service for the `TeslaUSB` B-1 appliance.
//!
//! Provides STA/AP connectivity for cloud upload + the local UI **without ever
//! endangering the car's write path** (`wifid.md`, `SPEC.md` §2 invariant 4):
//!
//! * **STA/AP state machine** ([`link`]) — connect to home `WiFi` when the
//!   gateway is reachable, fall back to a WPA2 onboarding AP otherwise, and
//!   **never run both at once** (reconciled against the live radio state).
//! * **TX rate cap** ([`throttle`]) — a token bucket / `tc` egress limit kept
//!   under the BCM43436 SDIO-deadlock threshold, published to `uploadd` as the
//!   D4 throttle contract.
//! * **Liveness watchdog** ([`watchdog`]) — recover a wedged chip by resetting
//!   the radio (`rmmod`/`modprobe brcmfmac`); a Pi reboot is a last resort gated
//!   on gadgetd's USB write-heartbeat (the single sanctioned non-gadgetd
//!   reboot).
//! * **Credential storage** ([`creds`]) — STA PSK + AP passphrase owned here,
//!   `0600`, never logged, never surfaced to the SPA.
//! * **Status** ([`status`]) — read-only shape for `webd`.
//!
//! The decision/orchestration core is pure and host-tested behind the seams in
//! [`traits`]; the real Linux/hardware I/O lives in [`exec`] as `HARDWARE-GATED`
//! stubs until the Phase-2 spikes (esp. Task 2.6, the TX-cap calibration)
//! validate it on the Pi.

// systemd captures stdout/stderr into the journal, so direct console output is
// the intended logging path for this daemon entrypoint only.
#![allow(clippy::print_stdout, clippy::print_stderr)]

mod config;
mod creds;
mod error;
mod exec;
mod link;
mod orchestrator;
mod status;
mod throttle;
mod traits;
mod watchdog;

use std::process::ExitCode;
use std::time::Duration;

use config::WifidConfig;
use creds::{CredentialStore, CredentialUpdate, Credentials, Secret};
use exec::{
    FileCredentialStore, GadgetHeartbeatSource, HardwareNetworkController,
    HardwareRebootController, MonotonicClock, current_boot_id,
};
use orchestrator::Daemon;

/// Default credential file (Pi-side ext4 data area, never on the Tesla volume).
const DEFAULT_CRED_PATH: &str = "/data/teslausb/wifi-credentials";
/// Control-loop tick interval.
const TICK_INTERVAL: Duration = Duration::from_secs(2);

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("wifid: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let Some(command) = args.first() else {
        return Err(usage());
    };
    let cred_path = opt_flag(args, "--cred-path")?.unwrap_or_else(|| DEFAULT_CRED_PATH.to_owned());

    match command.as_str() {
        "serve" => cmd_serve(&cred_path),
        "status" => cmd_status(&cred_path),
        "check-tx" => cmd_check_tx(args, &cred_path),
        "set-credentials" => cmd_set_credentials(args, &cred_path),
        "config" => {
            cmd_config();
            Ok(())
        }
        "--help" | "-h" | "help" => {
            println!("{}", usage());
            Ok(())
        }
        other => Err(format!("unknown command `{other}`\n{}", usage())),
    }
}

fn build_daemon(
    cred_path: &str,
) -> Result<
    Daemon<
        MonotonicClock,
        HardwareNetworkController,
        GadgetHeartbeatSource,
        HardwareRebootController,
        FileCredentialStore,
    >,
    String,
> {
    let cfg = WifidConfig::default();
    cfg.validate().map_err(|e| format!("invalid config: {e}"))?;
    Daemon::new(
        MonotonicClock::default(),
        HardwareNetworkController,
        GadgetHeartbeatSource,
        HardwareRebootController,
        FileCredentialStore::new(cred_path),
        cfg,
        current_boot_id(),
    )
    .map_err(|e| e.to_string())
}

fn cmd_serve(cred_path: &str) -> Result<(), String> {
    let mut daemon = build_daemon(cred_path)?;
    println!("wifid serve: control loop starting (tick {TICK_INTERVAL:?})");
    loop {
        // Drain any pending admin commands (delivered over IPC on the device;
        // none off-device). Referenced here so the command path is wired.
        for cmd in drain_admin_commands() {
            if let Err(e) = daemon.handle_command(cmd) {
                eprintln!("wifid: admin command failed: {e}");
            }
        }
        match daemon.tick() {
            Ok(status) => {
                if let Ok(json) = serde_json::to_string(&status) {
                    println!("{json}");
                }
            }
            Err(e) => {
                // On the host (no radio) observation is HARDWARE-GATED: report
                // and stop rather than spin on a guaranteed error.
                eprintln!("wifid: tick failed (expected off-device): {e}");
                return Ok(());
            }
        }
        std::thread::sleep(TICK_INTERVAL);
    }
}

/// On the device this is fed by the control UDS; off-device there are none.
fn drain_admin_commands() -> Vec<orchestrator::AdminCommand> {
    Vec::new()
}

fn cmd_status(cred_path: &str) -> Result<(), String> {
    let mut daemon = build_daemon(cred_path)?;
    if let Err(e) = daemon.tick() {
        eprintln!("wifid: live observation unavailable off-device: {e}");
    }
    match daemon.status() {
        Some(status) => {
            let json = serde_json::to_string_pretty(&status).map_err(|e| e.to_string())?;
            println!("{json}");
            Ok(())
        }
        None => Err("no status available yet (off-device)".to_owned()),
    }
}

fn cmd_check_tx(args: &[String], cred_path: &str) -> Result<(), String> {
    let bytes = parse_u64_flag(args, "--bytes", 0)?;
    let mut daemon = build_daemon(cred_path)?;
    let allowed = daemon.admit_tx(bytes);
    println!("admit_tx({bytes}) = {allowed}");
    Ok(())
}

fn cmd_set_credentials(args: &[String], cred_path: &str) -> Result<(), String> {
    let store = FileCredentialStore::new(cred_path);
    // First run: there is no credential file yet. The AP must always have a
    // WPA2 passphrase, so seed a base from --ap-pass before the daemon loads.
    if store.load().is_err() {
        let ap = opt_flag(args, "--ap-pass")?.ok_or_else(|| {
            "no existing credentials: --ap-pass is required to initialise".to_owned()
        })?;
        creds::validate_wpa2_passphrase(&ap).map_err(str::to_owned)?;
        let base = Credentials {
            sta_psk: None,
            ap_passphrase: Secret::new(ap),
        };
        store.store(&base).map_err(|e| e.to_string())?;
    }

    // Route the change through the same path the on-device IPC uses, so the
    // CLI and the daemon share one validated, persisted code path.
    let mut daemon = build_daemon(cred_path)?;
    let update = CredentialUpdate {
        sta_psk: opt_flag(args, "--sta-psk")?,
        ap_passphrase: opt_flag(args, "--ap-pass")?,
        clear_sta: args.iter().any(|a| a == "--clear-sta"),
    };
    daemon
        .handle_command(orchestrator::AdminCommand::UpdateCredentials(update))
        .map_err(|e| e.to_string())?;
    println!("wifid: credentials updated");
    Ok(())
}

fn cmd_config() {
    let cfg = WifidConfig::default();
    println!("{cfg:#?}");
    match cfg.validate() {
        Ok(()) => println!("config: valid"),
        Err(e) => println!("config: INVALID ({e})"),
    }
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

fn parse_u64_flag(args: &[String], name: &str, default: u64) -> Result<u64, String> {
    match opt_flag(args, name)? {
        None => Ok(default),
        Some(raw) => raw
            .parse::<u64>()
            .map_err(|_| format!("invalid value for {name}: `{raw}`")),
    }
}

fn usage() -> String {
    "usage: wifid <serve|status|check-tx|set-credentials|config> [--cred-path <path>] \
[--bytes <n>] [--sta-psk <psk>] [--ap-pass <passphrase>] [--clear-sta]"
        .to_owned()
}
