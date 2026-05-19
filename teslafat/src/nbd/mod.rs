//! NBD (Network Block Device) server.
//!
//! Implements the **newstyle** NBD handshake + transmission
//! protocol over a Unix socket. The kernel NBD client (configured
//! via `nbd-client -unix /run/teslafat/nbd.sock /dev/nbd0`)
//! connects, completes handshake, then issues block read/write
//! requests against `/dev/nbd0`. The kernel `g_mass_storage`
//! gadget is in turn pointed at `/dev/nbd0`, so Tesla's SCSI
//! reads/writes flow:
//!
//! Tesla → g_mass_storage → /dev/nbd0 → nbd-client → unix socket
//!       → teslafat (this module) → BlockBackend trait
//!
//! Protocol references:
//!   - https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md
//!
//! Limitations of this implementation (deliberate):
//!   - Single export, no name (caller passes `""`)
//!   - No TLS (Unix socket only — kernel client doesn't TLS)
//!   - No structured replies (simple replies are enough)
//!   - No multi-conn (only one kernel client connects)

pub mod handshake;
pub mod transmission;

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use tokio::net::UnixListener;
use tracing::{debug, info, warn};

use crate::backend::BlockBackend;
use crate::Shutdown;

pub use transmission::Command;

/// Serve NBD on the given Unix socket path until shutdown.
pub async fn serve<B: BlockBackend + 'static>(
    socket: PathBuf,
    backend: Arc<B>,
    shutdown: Shutdown,
) -> Result<()> {
    let listener = UnixListener::bind(&socket)?;
    info!(socket = %socket.display(), "NBD listening");

    // Permissions: 0o600 — only root (the nbd-client systemd unit
    // and `g_mass_storage` script) needs access.
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(&socket, perms)?;

    loop {
        tokio::select! {
            _ = shutdown.recv() => {
                info!("NBD server shutting down");
                break;
            }
            accept = listener.accept() => {
                match accept {
                    Ok((stream, _)) => {
                        debug!("NBD client connected");
                        let backend = backend.clone();
                        let shutdown = shutdown.clone();
                        tokio::spawn(async move {
                            if let Err(e) =
                                handle_client(stream, backend, shutdown).await
                            {
                                warn!(error = %e, "NBD client session ended with error");
                            } else {
                                debug!("NBD client session ended cleanly");
                            }
                        });
                    }
                    Err(e) => {
                        warn!(error = %e, "accept failed");
                    }
                }
            }
        }
    }

    // Best-effort: remove the socket file on shutdown.
    let _ = std::fs::remove_file(&socket);
    Ok(())
}

async fn handle_client<B: BlockBackend + 'static>(
    mut stream: tokio::net::UnixStream,
    backend: Arc<B>,
    shutdown: Shutdown,
) -> Result<()> {
    let export_size = backend.size();
    handshake::run(&mut stream, export_size).await?;
    transmission::run(&mut stream, backend, shutdown).await?;
    Ok(())
}
