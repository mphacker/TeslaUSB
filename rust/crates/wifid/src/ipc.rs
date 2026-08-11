//! Control-plane IPC for `wifid serve`: a length-prefixed JSON protocol over a
//! Unix domain socket, mirroring `schedulerd`/`gadgetd` framing byte-for-byte.
//!
//! `wifid` owns state mutation and status publication on the control loop;
//! `webd` forwards control requests here. Authorization is by filesystem
//! permission on the socket (mode `0o660`, group-owned) — there is no in-band
//! auth.
//!
//! This module is Unix-only (`std::os::unix`); the daemon runs only on the Pi.

use std::io::{self, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::creds::{CredentialUpdate, validate_ssid, validate_wpa2_passphrase};
use crate::orchestrator::{IpcRequest, IpcResponse};

const MAX_FRAME: u32 = 1 << 20;
const CONN_TIMEOUT: Duration = Duration::from_secs(15);
const SOCKET_HEALTH_INTERVAL: Duration = Duration::from_secs(2);
const ACCEPT_POLL: Duration = Duration::from_millis(100);
/// How long a socket handler waits for the control loop to answer. MUST be less
/// than `CONN_TIMEOUT` so the timeout envelope is written before the conn dies.
const REPLY_TIMEOUT: Duration = Duration::from_secs(10);

/// A request handed from a socket handler thread to the single control loop,
/// with a per-request reply channel. Deliberately derives NO `Debug`: the
/// `Mutate` variant carries a plaintext AP/STA passphrase.
pub(crate) struct IpcJob {
    pub(crate) request: IpcRequest,
    pub(crate) reply: Sender<IpcResponse>,
    /// Instant after which the caller has stopped waiting (its `REPLY_TIMEOUT`
    /// elapsed). The control loop drops an expired job **unapplied** rather than
    /// mutating state the caller was already told timed out — this matters most
    /// for `force_off`, which could otherwise silently take the radio down after
    /// the client reported a timeout.
    pub(crate) deadline: Instant,
}

#[derive(Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum WireRequest {
    GetApStatus,
    SetApMode { mode: crate::creds::ApMode },
    SetApConfig { ssid: String, passphrase: String },
}

/// Validate and lower an untrusted wire request into an `IpcRequest`.
/// On rejection returns `(code, static reason)` — the reason NEVER echoes the
/// submitted value.
fn to_ipc_request(wire: WireRequest) -> Result<IpcRequest, (&'static str, &'static str)> {
    match wire {
        WireRequest::GetApStatus => Ok(IpcRequest::GetStatus),
        WireRequest::SetApMode { mode } => Ok(IpcRequest::Mutate(CredentialUpdate {
            sta_psk: None,
            ap_passphrase: None,
            ap_ssid: None,
            ap_mode: Some(mode),
            clear_sta: false,
        })),
        WireRequest::SetApConfig { ssid, passphrase } => {
            validate_ssid(&ssid).map_err(|m| ("invalid_argument", m))?;
            validate_wpa2_passphrase(&passphrase).map_err(|m| ("invalid_argument", m))?;
            Ok(IpcRequest::Mutate(CredentialUpdate {
                sta_psk: None,
                ap_passphrase: Some(passphrase),
                ap_ssid: Some(ssid),
                ap_mode: None,
                clear_sta: false,
            }))
        }
    }
}

fn submit_and_wait(tx: &Sender<IpcJob>, request: IpcRequest) -> Value {
    let (reply_tx, reply_rx) = std::sync::mpsc::channel::<IpcResponse>();
    if tx
        .send(IpcJob {
            request,
            reply: reply_tx,
            deadline: Instant::now() + REPLY_TIMEOUT,
        })
        .is_err()
    {
        return err_envelope("unavailable", "control loop not running");
    }
    match reply_rx.recv_timeout(REPLY_TIMEOUT) {
        Ok(IpcResponse::Status(status)) => serde_json::to_value(&*status)
            .unwrap_or_else(|_| err_envelope("internal", "status serialisation failed")),
        Ok(IpcResponse::Unavailable) => err_envelope("unavailable", "no status yet"),
        Ok(IpcResponse::Ok) => json!({ "ok": true }),
        Ok(IpcResponse::Err { code, message }) => err_envelope(code, &message),
        Err(_) => err_envelope("timeout", "control loop did not respond"),
    }
}

/// Whether a queued job should still be applied: `true` while the caller is
/// still waiting for its reply, `false` once its [`IpcJob::deadline`] has
/// passed. The control loop uses this to drop expired mutations unapplied.
pub(crate) fn job_is_live(job: &IpcJob) -> bool {
    Instant::now() < job.deadline
}

/// Spawn the detached IPC server thread. A bind failure is logged and the
/// thread exits; the control loop keeps running (recording is never affected).
pub(crate) fn spawn_control_server(socket_path: PathBuf, tx: Sender<IpcJob>) {
    std::thread::spawn(move || {
        if let Err(e) = serve(&socket_path, &tx) {
            eprintln!("wifid ipc: control server exited: {e}");
        }
    });
}

fn serve(socket_path: &Path, tx: &Sender<IpcJob>) -> io::Result<()> {
    let mut listener = bind_listener(socket_path)?;
    listener.set_nonblocking(true)?;

    let mut last_health = Instant::now();
    loop {
        match listener.accept() {
            Ok((stream, _addr)) => {
                stream.set_nonblocking(false)?;
                let tx = tx.clone();
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(&stream, &tx) {
                        eprintln!("wifid ipc: connection error: {e}");
                    }
                });
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(ACCEPT_POLL);
            }
            Err(e) => eprintln!("wifid ipc: accept error: {e}"),
        }

        if last_health.elapsed() >= SOCKET_HEALTH_INTERVAL {
            last_health = Instant::now();
            if !socket_path_healthy(socket_path) {
                eprintln!(
                    "wifid ipc: control socket {} vanished; re-binding",
                    socket_path.display()
                );
                match bind_listener(socket_path) {
                    Ok(l) => {
                        l.set_nonblocking(true)?;
                        listener = l;
                    }
                    Err(e) => eprintln!("wifid ipc: re-bind failed: {e}"),
                }
            }
        }
    }
}

fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    println!("wifid serve: listening on {}", socket_path.display());
    Ok(listener)
}

fn socket_path_healthy(socket_path: &Path) -> bool {
    std::fs::symlink_metadata(socket_path)
        .map(|m| m.file_type().is_socket())
        .unwrap_or(false)
}

fn handle_conn(stream: &UnixStream, tx: &Sender<IpcJob>) -> io::Result<()> {
    stream.set_read_timeout(Some(CONN_TIMEOUT))?;
    stream.set_write_timeout(Some(CONN_TIMEOUT))?;
    let mut reader = stream;
    let payload = read_frame(&mut reader, MAX_FRAME)?;
    let response = match serde_json::from_slice::<WireRequest>(&payload) {
        Ok(wire) => match to_ipc_request(wire) {
            Ok(request) => submit_and_wait(tx, request),
            Err((code, message)) => err_envelope(code, message),
        },
        Err(e) => err_envelope("bad_request", &format!("bad request: {e}")),
    };
    let bytes = serde_json::to_vec(&response).map_err(io::Error::other)?;
    let mut writer = stream;
    write_frame(&mut writer, &bytes)
}

fn read_frame(stream: &mut impl Read, cap: u32) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf);
    if len > cap {
        return Err(io::Error::other(format!("frame too large: {len} > {cap}")));
    }
    let mut payload = vec![0u8; len as usize];
    stream.read_exact(&mut payload)?;
    Ok(payload)
}

fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
    let len = u32::try_from(payload.len())
        .map_err(|_| io::Error::other("response exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

fn err_envelope(code: &str, message: &str) -> Value {
    json!({ "error": { "code": code, "message": message } })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::os::unix::net::UnixStream;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::mpsc::{self, Receiver};
    use std::time::{Duration, Instant};

    use serde_json::{Value, json};

    use super::{
        IpcJob, MAX_FRAME, WireRequest, job_is_live, read_frame, socket_path_healthy,
        spawn_control_server, to_ipc_request, write_frame,
    };
    use crate::config::WifidConfig;
    use crate::creds::ApMode;
    use crate::link::{LinkMode, LinkObservation};
    use crate::orchestrator::{IpcRequest, IpcResponse};
    use crate::status::{ApStatus, WifiStatus};
    use crate::throttle::{ThrottleInputs, ThrottlePublisher};

    static NEXT_ID: AtomicU64 = AtomicU64::new(0);

    struct TempSocket {
        socket: PathBuf,
        dir: PathBuf,
    }

    impl TempSocket {
        fn new(tag: &str) -> Self {
            let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
            let dir =
                std::env::temp_dir().join(format!("wifid-ipc-{tag}-{}-{id}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            Self {
                socket: dir.join("wifid.sock"),
                dir,
            }
        }
    }

    impl Drop for TempSocket {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.socket);
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn wait_for_socket(path: &Path) {
        for _ in 0..200 {
            if socket_path_healthy(path) {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("socket never became ready: {}", path.display());
    }

    fn connect(path: &Path) -> UnixStream {
        for _ in 0..50 {
            if let Ok(stream) = UnixStream::connect(path) {
                return stream;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("connect failed: {}", path.display());
    }

    fn spawn_server(tag: &str) -> (TempSocket, Receiver<IpcJob>) {
        let fixture = TempSocket::new(tag);
        let (tx, rx) = mpsc::channel::<IpcJob>();
        spawn_control_server(fixture.socket.clone(), tx);
        wait_for_socket(&fixture.socket);
        (fixture, rx)
    }

    fn write_frame_test(stream: &mut UnixStream, payload: &[u8]) {
        write_frame(stream, payload).expect("write request frame");
    }

    fn read_frame_test(stream: &mut UnixStream) -> Vec<u8> {
        read_frame(stream, MAX_FRAME).expect("read response frame")
    }

    fn call_json(socket: &Path, request: &Value) -> (Value, Vec<u8>) {
        call_raw(
            socket,
            &serde_json::to_vec(request).expect("serialise request"),
        )
    }

    fn call_raw(socket: &Path, payload: &[u8]) -> (Value, Vec<u8>) {
        let mut stream = connect(socket);
        write_frame_test(&mut stream, payload);
        let raw = read_frame_test(&mut stream);
        let value = serde_json::from_slice(&raw).expect("parse response json");
        (value, raw)
    }

    fn build_status() -> WifiStatus {
        let cfg = WifidConfig::default();
        let mut publisher = ThrottlePublisher::new(&cfg.throttle);
        let throttle = publisher.update(ThrottleInputs {
            link_mode: LinkMode::Sta,
            sta_link_up: true,
            chip_recovering: false,
            near_deadlock: false,
            tc_applied: true,
            ap_overlay_active: false,
            ap_cap_applied: false,
        });
        WifiStatus::new(
            LinkMode::Sta,
            &LinkObservation {
                sta_configured: true,
                sta_running: true,
                ap_running: false,
                ap_fallback_suppressed: false,
                mutation_hold: false,
                associated: true,
                carrier_up: true,
                gateway_reachable: true,
                ap_has_clients: false,
                signal_dbm: Some(-55),
                sta_channel: Some(6),
            },
            throttle,
            false,
            ApStatus {
                mode: ApMode::Auto,
                active: false,
                ssid: Some("MyAccessPoint".to_owned()),
                client_count: 0,
                ip: None,
            },
        )
    }

    #[test]
    fn get_ap_status_lowers_to_get_status() {
        let lowered = to_ipc_request(WireRequest::GetApStatus);
        assert!(matches!(lowered, Ok(IpcRequest::GetStatus)));
    }

    #[test]
    fn set_ap_mode_lowers_to_mutate_mode_only() {
        let lowered = to_ipc_request(WireRequest::SetApMode {
            mode: ApMode::ForceOn,
        })
        .unwrap();
        match lowered {
            IpcRequest::Mutate(update) => {
                assert_eq!(update.ap_mode, Some(ApMode::ForceOn));
                assert!(update.sta_psk.is_none());
                assert!(update.ap_passphrase.is_none());
                assert!(update.ap_ssid.is_none());
                assert!(!update.clear_sta);
            }
            IpcRequest::GetStatus => panic!("wrong lowering"),
        }
    }

    #[test]
    fn set_ap_config_valid_lowers_to_mutate() {
        let lowered = to_ipc_request(WireRequest::SetApConfig {
            ssid: "MyAccessPoint".to_owned(),
            passphrase: "supersecretpass99".to_owned(),
        })
        .unwrap();
        match lowered {
            IpcRequest::Mutate(update) => {
                assert_eq!(update.ap_ssid.as_deref(), Some("MyAccessPoint"));
                assert_eq!(update.ap_passphrase.as_deref(), Some("supersecretpass99"));
                assert!(update.sta_psk.is_none());
                assert!(update.ap_mode.is_none());
                assert!(!update.clear_sta);
            }
            IpcRequest::GetStatus => panic!("wrong lowering"),
        }
    }

    #[test]
    fn set_ap_config_one_char_ssid_is_rejected() {
        let lowered = to_ipc_request(WireRequest::SetApConfig {
            ssid: " ".to_owned(),
            passphrase: "supersecretpass99".to_owned(),
        });
        assert!(matches!(lowered, Err(("invalid_argument", _))));
    }

    #[test]
    fn set_ap_config_short_passphrase_is_rejected() {
        let lowered = to_ipc_request(WireRequest::SetApConfig {
            ssid: "MyAccessPoint".to_owned(),
            passphrase: "1234".to_owned(),
        });
        assert!(matches!(lowered, Err(("invalid_argument", _))));
    }

    #[test]
    fn expired_job_is_dropped_but_a_waiting_job_is_live() {
        let (reply, _rx) = mpsc::channel();
        let expired = IpcJob {
            request: IpcRequest::GetStatus,
            reply: reply.clone(),
            deadline: Instant::now()
                .checked_sub(Duration::from_millis(1))
                .expect("instant underflow"),
        };
        assert!(
            !job_is_live(&expired),
            "a past-deadline job must be dropped"
        );
        let live = IpcJob {
            request: IpcRequest::GetStatus,
            reply,
            deadline: Instant::now() + Duration::from_secs(5),
        };
        assert!(
            job_is_live(&live),
            "a job within its deadline must be applied"
        );
    }

    #[test]
    fn set_ap_mode_round_trip_returns_ok() {
        let (fixture, rx) = spawn_server("set-mode");
        let responder = std::thread::spawn(move || {
            if let Ok(job) = rx.recv_timeout(Duration::from_secs(2)) {
                assert!(matches!(job.request, IpcRequest::Mutate(_)));
                let _ = job.reply.send(IpcResponse::Ok);
            }
        });
        let (resp, _raw) = call_json(
            &fixture.socket,
            &json!({ "cmd": "set_ap_mode", "mode": "force_on" }),
        );
        assert_eq!(resp, json!({ "ok": true }));
        responder.join().unwrap();
    }

    #[test]
    fn set_ap_config_round_trip_does_not_echo_secret() {
        let (fixture, rx) = spawn_server("set-config");
        let responder = std::thread::spawn(move || {
            if let Ok(job) = rx.recv_timeout(Duration::from_secs(2)) {
                assert!(matches!(job.request, IpcRequest::Mutate(_)));
                let _ = job.reply.send(IpcResponse::Ok);
            }
        });
        let passphrase = "supersecretpass99";
        let (resp, raw) = call_json(
            &fixture.socket,
            &json!({
                "cmd": "set_ap_config",
                "ssid": "MyAccessPoint",
                "passphrase": passphrase
            }),
        );
        assert_eq!(resp, json!({ "ok": true }));
        assert!(
            !String::from_utf8_lossy(&raw).contains(passphrase),
            "response echoed a secret"
        );
        responder.join().unwrap();
    }

    #[test]
    fn set_ap_config_invalid_argument_is_pre_enqueue_and_secret_safe() {
        let (fixture, rx) = spawn_server("reject-pre-enqueue");
        let responder = std::thread::spawn(move || rx.recv_timeout(Duration::from_millis(500)));
        let (resp, raw) = call_json(
            &fixture.socket,
            &json!({
                "cmd": "set_ap_config",
                "ssid": "MyAccessPoint",
                "passphrase": "short"
            }),
        );
        let code = resp
            .get("error")
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str);
        assert_eq!(code, Some("invalid_argument"));
        assert!(
            !String::from_utf8_lossy(&raw).contains("short"),
            "response echoed rejected passphrase"
        );
        assert!(matches!(
            responder.join().unwrap(),
            Err(std::sync::mpsc::RecvTimeoutError::Timeout)
        ));
    }

    #[test]
    fn malformed_json_returns_bad_request() {
        let (fixture, _rx) = spawn_server("bad-json");
        let (resp, _raw) = call_raw(&fixture.socket, b"not json");
        let code = resp
            .get("error")
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str);
        assert_eq!(code, Some("bad_request"));
    }

    #[test]
    fn get_ap_status_round_trip_returns_status_shape() {
        let (fixture, rx) = spawn_server("status");
        let responder = std::thread::spawn(move || {
            if let Ok(job) = rx.recv_timeout(Duration::from_secs(2)) {
                assert!(matches!(job.request, IpcRequest::GetStatus));
                let _ = job
                    .reply
                    .send(IpcResponse::Status(Box::new(build_status())));
            }
        });
        let (resp, raw) = call_json(&fixture.socket, &json!({ "cmd": "get_ap_status" }));
        assert!(resp.get("mode").is_some());
        assert!(resp.get("ap").is_some());
        let raw_text = String::from_utf8_lossy(&raw);
        assert!(!raw_text.contains("passphrase"));
        assert!(!raw_text.contains("password"));
        responder.join().unwrap();
    }
}
