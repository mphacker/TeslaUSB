//! The `indexd` control-socket client: framed-JSON request/response transport
//! used by settings write endpoints.

use std::path::PathBuf;
use std::sync::Arc;

use serde_json::Value;

use crate::gadget::TransportError;

pub(crate) trait IndexdClient: Send + Sync {
    fn call(&self, request: Value) -> Result<Value, TransportError>;
}

#[cfg(unix)]
pub(crate) use unix_client::UnixIndexdClient;

#[cfg(not(unix))]
pub(crate) use stub_client::UnavailableIndexdClient;

pub(crate) fn default_client(sock: PathBuf) -> Arc<dyn IndexdClient> {
    #[cfg(unix)]
    {
        Arc::new(UnixIndexdClient::new(sock))
    }
    #[cfg(not(unix))]
    {
        let _ = sock;
        Arc::new(UnavailableIndexdClient)
    }
}

#[cfg(unix)]
mod unix_client {
    use std::io::{self, Read, Write};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::time::Duration;

    use serde_json::Value;

    use super::{IndexdClient, TransportError};

    const MAX_FRAME: u32 = 64 * 1024;
    const CLIENT_TIMEOUT: Duration = Duration::from_secs(15);

    pub(crate) struct UnixIndexdClient {
        sock: PathBuf,
    }

    impl UnixIndexdClient {
        pub(crate) fn new(sock: PathBuf) -> Self {
            Self { sock }
        }
    }

    impl IndexdClient for UnixIndexdClient {
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
            let response = read_frame(&mut stream, MAX_FRAME)
                .map_err(|e| TransportError::Protocol(format!("read: {e}")))?;
            serde_json::from_slice(&response)
                .map_err(|e| TransportError::Protocol(format!("decode: {e}")))
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

    use super::{IndexdClient, TransportError};

    pub(crate) struct UnavailableIndexdClient;

    impl IndexdClient for UnavailableIndexdClient {
        fn call(&self, _request: Value) -> Result<Value, TransportError> {
            Err(TransportError::Unavailable(
                "indexd socket is not available on this platform".to_owned(),
            ))
        }
    }
}
