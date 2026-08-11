//! The `uploadd` control-socket client: framed-JSON request/response transport
//! for read-only cloud uploader status.

use std::path::PathBuf;

use serde_json::Value;

use crate::gadget::TransportError;

/// One-shot request/response client for `uploadd` control IPC.
pub(crate) trait UploaddClient: Send + Sync {
    /// Send one framed JSON request and return the parsed JSON response.
    fn call(&self, request: Value) -> Result<Value, TransportError>;
}

#[cfg(unix)]
pub(crate) use unix_client::UnixUploaddClient;

#[cfg(not(unix))]
pub(crate) use stub_client::UnavailableUploaddClient;

/// Construct the platform default `uploadd` client.
pub(crate) fn default_client(sock: PathBuf) -> std::sync::Arc<dyn UploaddClient> {
    #[cfg(unix)]
    {
        std::sync::Arc::new(UnixUploaddClient::new(sock))
    }
    #[cfg(not(unix))]
    {
        let _ = sock;
        std::sync::Arc::new(UnavailableUploaddClient)
    }
}

#[cfg(unix)]
mod unix_client {
    use std::io::{self, Read, Write};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::time::Duration;

    use serde_json::Value;

    use super::{TransportError, UploaddClient};

    const MAX_FRAME: u32 = 64 * 1024;
    const CLIENT_TIMEOUT: Duration = Duration::from_secs(15);

    pub(crate) struct UnixUploaddClient {
        sock: PathBuf,
    }

    impl UnixUploaddClient {
        pub(crate) fn new(sock: PathBuf) -> Self {
            Self { sock }
        }
    }

    impl UploaddClient for UnixUploaddClient {
        fn call(&self, request: Value) -> Result<Value, TransportError> {
            let payload = serde_json::to_vec(&request)
                .map_err(|err| TransportError::Protocol(err.to_string()))?;
            let mut stream = UnixStream::connect(&self.sock).map_err(|err| {
                TransportError::Unavailable(format!("connect {}: {err}", self.sock.display()))
            })?;
            stream.set_read_timeout(Some(CLIENT_TIMEOUT)).ok();
            stream.set_write_timeout(Some(CLIENT_TIMEOUT)).ok();
            write_frame(&mut stream, &payload)
                .map_err(|err| TransportError::Unavailable(format!("write: {err}")))?;
            let response = read_frame(&mut stream, MAX_FRAME)
                .map_err(|err| TransportError::Protocol(format!("read: {err}")))?;
            serde_json::from_slice(&response)
                .map_err(|err| TransportError::Protocol(format!("decode: {err}")))
        }
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
            .map_err(|_| io::Error::other("request exceeds u32 length"))?;
        stream.write_all(&len.to_le_bytes())?;
        stream.write_all(payload)?;
        stream.flush()
    }
}

#[cfg(not(unix))]
mod stub_client {
    use serde_json::Value;

    use super::{TransportError, UploaddClient};

    pub(crate) struct UnavailableUploaddClient;

    impl UploaddClient for UnavailableUploaddClient {
        fn call(&self, _request: Value) -> Result<Value, TransportError> {
            Err(TransportError::Unavailable(
                "uploadd socket is not available on this platform".to_owned(),
            ))
        }
    }
}
