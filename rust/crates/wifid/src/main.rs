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
mod ipc;
mod link;
mod nmcli;
mod orchestrator;
mod overlay;
mod status;
mod throttle;
mod traits;
mod watchdog;

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::mpsc::RecvTimeoutError;
use std::time::Duration;
use std::time::Instant;

use config::WifidConfig;
use creds::{ApMode, CredentialStore, CredentialUpdate, Credentials, Secret};
use exec::{
    FileCredentialStore, GadgetHeartbeatSource, HardwareRebootController, MonotonicClock,
    current_boot_id,
};
use nmcli::NmcliNetworkController;
use orchestrator::Daemon;
use overlay::Uap0Overlay;

/// Default credential file (Pi-side ext4 data area, never on the Tesla volume).
const DEFAULT_CRED_PATH: &str = "/data/teslausb/wifi-credentials";
/// Control-plane UDS socket (device-only; webd connects here).
const CONTROL_SOCKET: &str = "/run/teslausb/wifid.sock";
/// Control-loop tick interval.
const TICK_INTERVAL: Duration = Duration::from_secs(2);
/// How often the control loop re-logs an *unchanged* status.
///
/// The loop otherwise logs only when the status actually changes. Without this
/// floor a healthy, stable device would go completely silent, making "wifid is
/// fine" indistinguishable from "wifid is wedged" when reading the journal.
///
/// This also sets the sampling resolution for `signal_dbm`, which is
/// deliberately excluded from change detection (see `WifiStatus::log_key`), so
/// it is kept well below the flood threshold rather than as long as possible.
const STATUS_HEARTBEAT: Duration = Duration::from_secs(120);

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
        NmcliNetworkController,
        GadgetHeartbeatSource,
        HardwareRebootController,
        FileCredentialStore,
        Uap0Overlay,
    >,
    String,
> {
    let cfg = WifidConfig::default();
    cfg.validate().map_err(|e| format!("invalid config: {e}"))?;
    let net = NmcliNetworkController::new(cfg.platform.clone());
    Daemon::new(
        MonotonicClock::default(),
        net,
        GadgetHeartbeatSource,
        HardwareRebootController,
        FileCredentialStore::new(cred_path),
        Uap0Overlay::new(cfg.platform.wifi_iface.clone()),
        cfg,
        current_boot_id(),
    )
    .map_err(|e| e.to_string())
}

fn cmd_serve(cred_path: &str) -> Result<(), String> {
    let mut daemon = build_daemon(cred_path)?;
    let (tx, rx) = std::sync::mpsc::channel::<ipc::IpcJob>();
    ipc::spawn_control_server(PathBuf::from(CONTROL_SOCKET), tx);
    println!("wifid serve: control loop starting (tick {TICK_INTERVAL:?})");
    // Log the status on change plus a periodic heartbeat, never once per tick:
    // at a 2s tick that was ~43k lines/day from this daemon alone, which
    // vacuumed every other unit's history out of the journal.
    let mut last_key = None;
    let mut last_emit: Option<Instant> = None;
    loop {
        match daemon.tick() {
            Ok(status) => {
                let key = status.log_key();
                let heartbeat_due = last_emit.is_none_or(|at| at.elapsed() >= STATUS_HEARTBEAT);
                if last_key.as_ref() != Some(&key) || heartbeat_due {
                    if let Ok(json) = serde_json::to_string(&status) {
                        println!("{json}");
                    }
                    last_key = Some(key);
                    last_emit = Some(Instant::now());
                }
            }
            Err(e) => {
                // A failed observation is NOT fatal: `tick` has already
                // published a fail-closed throttle (uploads off) internally, so
                // a transient radio/driver hiccup must never terminate the
                // daemon and hand systemd a restart loop. Log and keep ticking;
                // the next observation re-reconciles from actual radio state.
                // (Off-device, where the hardware-gated executor always errors,
                // this loops harmlessly until interrupted.)
                eprintln!("wifid: tick failed (continuing): {e}");
            }
        }
        // Service IPC jobs until the next tick deadline. IPC problems can never
        // stop the control loop: a dead server thread just falls back to sleep.
        let deadline = Instant::now() + TICK_INTERVAL;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            match rx.recv_timeout(remaining) {
                Ok(job) => {
                    // Drop a job whose caller already timed out rather than
                    // applying a mutation it was told failed (esp. `force_off`).
                    if !ipc::job_is_live(&job) {
                        continue;
                    }
                    let response = daemon.handle_ipc(job.request);
                    // Client may have hung up; dropping the reply is fine.
                    let _ = job.reply.send(response);
                }
                Err(RecvTimeoutError::Timeout) => break,
                Err(RecvTimeoutError::Disconnected) => {
                    std::thread::sleep(remaining);
                    break;
                }
            }
        }
    }
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
    // WPA2 passphrase and SSID, so seed a base before the daemon loads. A
    // genuine read/parse error (not a missing file) is surfaced, not papered
    // over — we must never overwrite a store we failed to read.
    let existing = store.load().map_err(|e| e.to_string())?;
    if existing.is_none() {
        let ap = opt_flag(args, "--ap-pass")?.ok_or_else(|| {
            "no existing credentials: --ap-pass is required to initialise".to_owned()
        })?;
        let ap_ssid = opt_flag(args, "--ap-ssid")?.ok_or_else(|| {
            "no existing credentials: --ap-ssid is required to initialise".to_owned()
        })?;
        creds::validate_wpa2_passphrase(&ap).map_err(str::to_owned)?;
        creds::validate_ssid(&ap_ssid).map_err(str::to_owned)?;
        let base = Credentials {
            sta_psk: None,
            ap_passphrase: Some(Secret::new(ap)),
            ap_ssid: Some(ap_ssid),
            ap_mode: ApMode::Auto,
        };
        debug_assert!(base.ap_provisioned());
        store.store(&base).map_err(|e| e.to_string())?;
    }

    // Route the change through the same path the on-device IPC uses, so the
    // CLI and the daemon share one validated, persisted code path.
    let mut daemon = build_daemon(cred_path)?;
    let update = CredentialUpdate {
        sta_psk: opt_flag(args, "--sta-psk")?,
        ap_passphrase: opt_flag(args, "--ap-pass")?,
        ap_ssid: opt_flag(args, "--ap-ssid")?,
        ap_mode: None,
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
[--bytes <n>] [--sta-psk <psk>] [--ap-pass <passphrase>] [--ap-ssid <ssid>] [--clear-sta]"
        .to_owned()
}
