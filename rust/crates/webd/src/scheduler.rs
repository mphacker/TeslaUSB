//! The `schedulerd` control-socket client: a thin framed-JSON request/response
//! transport, the chime-scheduler counterpart to [`crate::gadget`].
//!
//! `webd` is a **pure proxy** for the chime scheduler — it never owns schedule
//! state. Each REST handler forwards a `cmd`-tagged JSON request over the
//! `schedulerd` Unix domain socket and relays the JSON answer (or maps the
//! `{error:{code,message}}` envelope onto an [`crate::error::ApiError`]). The
//! framing (4-byte LE length prefix + payload) is identical to `gadgetd`'s, so
//! the two clients share the [`crate::gadget::TransportError`] type.
//!
//! Like the gadget client, the real Unix-socket implementation is `#[cfg(unix)]`
//! and a non-Unix dev host gets an always-unavailable stub so the crate still
//! compiles and tests there (handler tests inject a mock instead).

use std::path::PathBuf;

use serde_json::Value;

use crate::gadget::TransportError;

/// A one-shot request/response client for the `schedulerd` control socket. Boxed
/// as `dyn` in [`crate::AppState`] so tests can inject a mock; the blocking
/// socket I/O is offloaded via `spawn_blocking` by the caller.
pub(crate) trait SchedulerClient: Send + Sync {
    /// Send one framed JSON request and return the parsed JSON response.
    fn call(&self, request: Value) -> Result<Value, TransportError>;
}

#[cfg(unix)]
pub(crate) use unix_client::UnixSchedulerClient;

#[cfg(not(unix))]
pub(crate) use stub_client::UnavailableSchedulerClient;

/// Construct the platform default `schedulerd` client: a Unix-socket client on
/// the Pi (Linux), an always-unavailable stub on non-Unix dev hosts.
pub(crate) fn default_client(sock: PathBuf) -> std::sync::Arc<dyn SchedulerClient> {
    #[cfg(unix)]
    {
        std::sync::Arc::new(UnixSchedulerClient::new(sock))
    }
    #[cfg(not(unix))]
    {
        let _ = sock;
        std::sync::Arc::new(UnavailableSchedulerClient)
    }
}

#[cfg(unix)]
mod unix_client {
    use std::io::{self, Read, Write};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::time::Duration;

    use serde_json::Value;

    use super::{SchedulerClient, TransportError};

    /// Maximum accepted frame size (matches `schedulerd`'s `MAX_FRAME`).
    const MAX_FRAME: u32 = 1 << 20;
    /// Socket read/write timeout. Scheduler ops are quick (a file write at most).
    const CLIENT_TIMEOUT: Duration = Duration::from_secs(15);

    /// A `schedulerd` control-socket client over a Unix domain socket.
    pub(crate) struct UnixSchedulerClient {
        sock: PathBuf,
    }

    impl UnixSchedulerClient {
        pub(crate) fn new(sock: PathBuf) -> Self {
            Self { sock }
        }
    }

    impl SchedulerClient for UnixSchedulerClient {
        fn call(&self, request: Value) -> Result<Value, TransportError> {
            let payload = serde_json::to_vec(&request)
                .map_err(|e| TransportError::Protocol(e.to_string()))?;

            let mut stream = UnixStream::connect(&self.sock).map_err(|e| {
                TransportError::Unavailable(format!("connect {}: {e}", self.sock.display()))
            })?;
            stream.set_read_timeout(Some(CLIENT_TIMEOUT)).ok();
            stream.set_write_timeout(Some(CLIENT_TIMEOUT)).ok();

            write_frame(&mut stream, &payload)
                .map_err(|e| TransportError::Unavailable(format!("write: {e}")))?;
            let resp = read_frame(&mut stream, MAX_FRAME)
                .map_err(|e| TransportError::Protocol(format!("read: {e}")))?;
            serde_json::from_slice(&resp)
                .map_err(|e| TransportError::Protocol(format!("decode: {e}")))
        }
    }

    /// Read a length-prefixed frame (4-byte LE length, then the payload).
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

    /// Write a length-prefixed frame.
    fn write_frame(stream: &mut impl Write, payload: &[u8]) -> io::Result<()> {
        let len = u32::try_from(payload.len())
            .map_err(|_| io::Error::other("request exceeds u32 length"))?;
        stream.write_all(&len.to_le_bytes())?;
        stream.write_all(payload)?;
        stream.flush()
    }
}

#[cfg(not(unix))]
mod stub_client {
    use serde_json::Value;

    use super::{SchedulerClient, TransportError};

    /// A no-op client for non-Unix build hosts: `schedulerd`'s Unix socket does
    /// not exist there, so every call reports the service as unavailable. The
    /// `webd` binary only runs on the Pi (Linux); this keeps the dev host
    /// compiling and the handler tests injecting a mock instead.
    pub(crate) struct UnavailableSchedulerClient;

    impl SchedulerClient for UnavailableSchedulerClient {
        fn call(&self, _request: Value) -> Result<Value, TransportError> {
            Err(TransportError::Unavailable(
                "schedulerd socket is not available on this platform".to_owned(),
            ))
        }
    }
}
