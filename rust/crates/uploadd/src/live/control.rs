//! Read-only control socket for `uploadd` (`get_status`).
//!
//! This is the minimum safe control-channel slice needed before cloud mutations:
//! a framed-JSON Unix socket with a single typed `get_status` verb. It exposes
//! only non-secret daemon state.

use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Default Unix socket path for the `uploadd` control API.
pub(crate) const DEFAULT_CONTROL_SOCKET: &str = "/run/teslausb/uploadd.sock";
const MAX_FRAME: u32 = 64 * 1024;
const CONN_TIMEOUT: Duration = Duration::from_secs(15);

/// Shared, non-secret uploader status exposed to `webd`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct StatusSnapshot {
    /// Whether upload credentials/config are available.
    pub configured: bool,
    /// Non-secret provider/backend type (`drive`, `onedrive`, `s3`, ...), when known.
    pub provider_type: Option<String>,
    /// Current daemon state (`starting`, `running`, `idle_no_destination`, `stopping`).
    pub uploader_state: String,
    /// Current sync-now capability (`unsupported` in this read-only slice).
    pub sync_now_state: String,
}

impl Default for StatusSnapshot {
    fn default() -> Self {
        Self {
            configured: false,
            provider_type: None,
            uploader_state: "starting".to_owned(),
            sync_now_state: "unsupported".to_owned(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum WireRequest {
    GetStatus,
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum WireResponse {
    UploaddStatus {
        configured: bool,
        provider_type: Option<String>,
        uploader_state: String,
        sync_now_state: String,
    },
    Error {
        message: String,
    },
}

/// Spawn the `uploadd` control-socket server thread.
///
/// # Errors
///
/// Returns an error if socket setup/bind fails.
pub(crate) fn spawn_control_server(
    socket_path: PathBuf,
    status: Arc<Mutex<StatusSnapshot>>,
) -> io::Result<thread::JoinHandle<()>> {
    let listener = bind_listener(&socket_path)?;
    thread::Builder::new()
        .name("uploadd-control".to_owned())
        .spawn(move || serve(listener, status))
}

fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => return Err(err),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    Ok(listener)
}

fn serve(listener: UnixListener, status: Arc<Mutex<StatusSnapshot>>) {
    for incoming in listener.incoming() {
        let Ok(stream) = incoming else {
            continue;
        };
        let _ = handle_conn(stream, &status);
    }
}

fn handle_conn(mut stream: UnixStream, status: &Arc<Mutex<StatusSnapshot>>) -> io::Result<()> {
    stream.set_read_timeout(Some(CONN_TIMEOUT))?;
    stream.set_write_timeout(Some(CONN_TIMEOUT))?;
    let request_payload = match read_frame(&mut stream, MAX_FRAME) {
        Ok(payload) => payload,
        Err(err) => {
            let _ = write_response(
                &mut stream,
                &WireResponse::Error {
                    message: format!("bad request frame: {err}"),
                },
            );
            return Ok(());
        }
    };
    let response = match serde_json::from_slice::<WireRequest>(&request_payload) {
        Ok(WireRequest::GetStatus) => {
            let snapshot = status
                .lock()
                .map_err(|_| io::Error::other("status mutex poisoned"))?
                .clone();
            WireResponse::UploaddStatus {
                configured: snapshot.configured,
                provider_type: snapshot.provider_type,
                uploader_state: snapshot.uploader_state,
                sync_now_state: snapshot.sync_now_state,
            }
        }
        Err(err) => WireResponse::Error {
            message: format!("bad request: {err}"),
        },
    };
    write_response(&mut stream, &response)
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

fn write_frame(stream: &mut impl Write, payload: &[u8], cap: u32) -> io::Result<()> {
    if payload.len() > cap as usize {
        return Err(io::Error::other(format!(
            "frame too large: {} > {}",
            payload.len(),
            cap
        )));
    }
    let len =
        u32::try_from(payload.len()).map_err(|_| io::Error::other("frame exceeds u32 length"))?;
    stream.write_all(&len.to_le_bytes())?;
    stream.write_all(payload)?;
    stream.flush()
}

fn write_response(stream: &mut impl Write, response: &WireResponse) -> io::Result<()> {
    let payload = serde_json::to_vec(response).map_err(io::Error::other)?;
    write_frame(stream, &payload, MAX_FRAME)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod tests {
    use std::os::unix::net::UnixStream;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use serde_json::{Value, json};

    use super::{StatusSnapshot, WireResponse, read_frame, spawn_control_server, write_frame};

    static NEXT_ID: AtomicU64 = AtomicU64::new(0);

    struct TempSocket {
        socket: PathBuf,
        dir: PathBuf,
    }

    impl TempSocket {
        fn new(tag: &str) -> Self {
            let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
            let dir = std::env::temp_dir()
                .join(format!("uploadd-control-{tag}-{}-{id}", std::process::id()));
            std::fs::create_dir_all(&dir).expect("create temp dir");
            Self {
                socket: dir.join("uploadd.sock"),
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
            if path.exists() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("socket never became ready: {}", path.display());
    }

    fn request(path: &Path, payload: &Value) -> Value {
        let mut stream = UnixStream::connect(path).expect("connect");
        let bytes = serde_json::to_vec(payload).expect("encode");
        write_frame(&mut stream, &bytes, 64 * 1024).expect("write frame");
        let response = read_frame(&mut stream, 64 * 1024).expect("read frame");
        serde_json::from_slice(&response).expect("decode response")
    }

    #[test]
    fn get_status_returns_snapshot() {
        let fixture = TempSocket::new("get-status");
        let status = Arc::new(Mutex::new(StatusSnapshot {
            configured: true,
            provider_type: Some("drive".to_owned()),
            uploader_state: "running".to_owned(),
            sync_now_state: "unsupported".to_owned(),
        }));
        let _server = spawn_control_server(fixture.socket.clone(), status).expect("spawn");
        wait_for_socket(&fixture.socket);

        let response = request(&fixture.socket, &json!({ "cmd": "get_status" }));
        assert_eq!(response["status"], "uploadd_status");
        assert_eq!(response["configured"], true);
        assert_eq!(response["provider_type"], "drive");
        assert_eq!(response["uploader_state"], "running");
        assert_eq!(response["sync_now_state"], "unsupported");
    }

    #[test]
    fn unknown_command_returns_error_envelope() {
        let fixture = TempSocket::new("unknown-cmd");
        let status = Arc::new(Mutex::new(StatusSnapshot::default()));
        let _server = spawn_control_server(fixture.socket.clone(), status).expect("spawn");
        wait_for_socket(&fixture.socket);

        let response = request(&fixture.socket, &json!({ "cmd": "nope" }));
        assert_eq!(response["status"], "error");
        assert!(
            response["message"]
                .as_str()
                .is_some_and(|value| value.contains("unknown variant"))
        );
    }

    #[test]
    fn response_variant_serializes_with_uploadd_status_tag() {
        let encoded = serde_json::to_value(WireResponse::UploaddStatus {
            configured: true,
            provider_type: Some("s3".to_owned()),
            uploader_state: "running".to_owned(),
            sync_now_state: "unsupported".to_owned(),
        })
        .expect("encode");
        assert_eq!(encoded["status"], "uploadd_status");
    }
}
