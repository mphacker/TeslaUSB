//! IPC vocabulary for the `teslafat` daemon's control socket.
//!
//! Format-agnostic wire types live in [`messages`]. See the module
//! doc there for the forward-compat contract (no
//! `deny_unknown_fields`, version field validated at the boundary).

pub mod messages;

pub use messages::{
    DaemonState, Envelope, ErrorBody, ErrorCode, IpcError, PROTOCOL_VERSION, Request, Response,
    RetentionAction, RetentionFailure, RetentionUpdate, StatusBody,
};
