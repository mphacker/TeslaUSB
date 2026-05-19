//! IPC envelope + payload types for the `teslafat` daemon's
//! control socket.
//!
//! Format-agnostic: every type derives `serde::Serialize` and
//! `serde::Deserialize` but the encoder choice (JSON, length-
//! prefixed, `MessagePack`, etc.) is a transport-layer decision that
//! lives outside this crate. Phase 1.5 wires the actual Unix-socket
//! transport in the `teslafat` binary; this module exists to pin
//! the on-the-wire vocabulary independently of the transport so the
//! worker (`teslausb-worker`) and the daemon (`teslafat`) can be
//! built and tested in isolation.
//!
//! ## Forward compatibility
//!
//! IPC types deliberately **do not** use
//! `#[serde(deny_unknown_fields)]`. A newer server may add fields
//! to a response that an older client must safely ignore; that's
//! the entire point of the versioned envelope (see
//! [`PROTOCOL_VERSION`] and [`Envelope::validate`]). Major-version
//! breaks bump `PROTOCOL_VERSION` and rely on `Envelope::validate`
//! to reject the mismatch at the boundary. Compare with the
//! binary's TOML config loader, which **does** use
//! `deny_unknown_fields` because a typo from an operator should be
//! a load error, not a silent ignore.

use serde::{Deserialize, Serialize};

/// Wire-protocol major version transmitted in every [`Envelope`].
///
/// Bumped only on breaking changes (a field changes type, an enum
/// variant is removed, a default semantic flips). Adding optional
/// fields or new enum variants does **not** bump this number — the
/// peer's `serde` derive tolerates them per the forward-compat
/// contract documented at the module level.
pub const PROTOCOL_VERSION: u8 = 1;

/// Errors surfaced from this module's typed helpers (currently only
/// [`Envelope::validate`]).
///
/// Per charter §"Rust standards" — typed errors at the library
/// boundary via `thiserror`; `anyhow` is reserved for the binary
/// outer layer.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum IpcError {
    /// An [`Envelope`]'s `version` field did not match
    /// [`PROTOCOL_VERSION`].
    #[error("unsupported IPC protocol version: got {got}, expected {expected}")]
    UnsupportedVersion {
        /// Version that arrived on the wire.
        got: u8,
        /// Version this build of `teslausb-core` speaks
        /// (`PROTOCOL_VERSION`).
        expected: u8,
    },
}

/// Versioned envelope wrapping every request/response payload.
///
/// The `id` field is a correlation handle: clients pick a fresh
/// value per request and match it against the response. Servers
/// MUST echo the client-supplied `id` unchanged.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Envelope<T> {
    /// Protocol major version. See [`PROTOCOL_VERSION`].
    pub version: u8,
    /// Client-chosen correlation id. Servers echo this back in the
    /// response so async clients can match completions.
    pub id: u64,
    /// The wrapped payload — typically [`Request`] or [`Response`].
    pub payload: T,
}

impl<T> Envelope<T> {
    /// Construct an envelope with the current [`PROTOCOL_VERSION`].
    pub const fn new(id: u64, payload: T) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            id,
            payload,
        }
    }

    /// Reject envelopes whose `version` field does not match
    /// [`PROTOCOL_VERSION`]. Call this at the wire boundary
    /// immediately after deserialization.
    ///
    /// # Errors
    ///
    /// Returns [`IpcError::UnsupportedVersion`] with both the
    /// observed and expected versions so the operator can diagnose
    /// peer-skew from a single log line.
    pub const fn validate(&self) -> Result<(), IpcError> {
        if self.version == PROTOCOL_VERSION {
            Ok(())
        } else {
            Err(IpcError::UnsupportedVersion {
                got: self.version,
                expected: PROTOCOL_VERSION,
            })
        }
    }
}

/// A request sent from the worker (or `teslactl`) to the daemon.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Request {
    /// Ask the daemon for its current state. Empty request body.
    Status,
    /// Apply a batch of clip-retention changes (hide / unhide /
    /// extend a hold-until timestamp).
    RetentionUpdate {
        /// Per-clip retention changes to apply atomically.
        updates: Vec<RetentionUpdate>,
    },
    /// Force Tesla to re-enumerate the USB device (simulated
    /// unplug/replug). No body — applies to the LUN this socket
    /// serves.
    InvalidateCache,
}

/// A response returned by the daemon for a [`Request`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Response {
    /// Reply to [`Request::Status`].
    Status(StatusBody),
    /// Reply to [`Request::RetentionUpdate`] — summarises which
    /// entries applied and which failed.
    RetentionAck {
        /// Number of updates the daemon successfully applied.
        applied: u32,
        /// Updates the daemon could not apply, each with a human-
        /// readable reason.
        failed: Vec<RetentionFailure>,
    },
    /// Reply to [`Request::InvalidateCache`] — UDC rebind kicked off.
    InvalidateAck,
    /// Generic failure response carrying a typed [`ErrorCode`].
    Error(ErrorBody),
}

/// Body of a [`Response::Status`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StatusBody {
    /// The LUN this daemon serves (0 = `TeslaCam`, 1 = `LightShow`).
    pub lun_id: u8,
    /// Daemon lifecycle state.
    pub state: DaemonState,
    /// FAT32 volume label currently presented to Tesla.
    pub volume_label: String,
    /// Size of the synthesized volume in bytes.
    pub volume_size_bytes: u64,
    /// Seconds since the daemon started serving.
    pub uptime_seconds: u64,
}

/// Daemon lifecycle state reported in [`StatusBody::state`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DaemonState {
    /// Mounting backing storage / building synthesized geometry.
    Initializing,
    /// Bound to the UDC and serving NBD reads/writes.
    Serving,
    /// UDC unbound; flushing pending writes before stop.
    Draining,
    /// Drained and idle, awaiting shutdown.
    Stopped,
}

/// One entry in a [`Request::RetentionUpdate`] batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RetentionUpdate {
    /// Clip path relative to the FAT volume root
    /// (e.g. `TeslaCam/RecentClips/2025-12-31_23-59-59-front.mp4`).
    pub clip_path: String,
    /// Retention action to apply.
    pub action: RetentionAction,
}

/// What the worker wants the daemon to do with a clip.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RetentionAction {
    /// Hide the clip from Tesla (mark deleted in FAT, keep backing
    /// data intact for the worker to copy).
    Hide,
    /// Reveal a previously hidden clip.
    Unhide,
    /// Extend the auto-prune deadline; the daemon will not delete
    /// the clip until at least this absolute Unix-epoch second.
    Extend {
        /// Hold until this Unix-epoch second (UTC).
        until_unix_seconds: u64,
    },
}

/// One entry in [`Response::RetentionAck::failed`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RetentionFailure {
    /// Clip path that could not be updated (matches the input).
    pub clip_path: String,
    /// Human-readable reason — operator logs read this verbatim.
    pub reason: String,
}

/// Body of a [`Response::Error`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorBody {
    /// Machine-readable error category.
    pub code: ErrorCode,
    /// Human-readable detail. May include peer-supplied data.
    pub message: String,
}

/// Categories surfaced via [`ErrorBody::code`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    /// `Envelope::validate` rejected the envelope. See
    /// [`IpcError::UnsupportedVersion`] for the typed counterpart.
    UnsupportedVersion,
    /// Payload could not be deserialised as a known [`Request`]
    /// variant.
    UnknownMessageType,
    /// Payload deserialised but failed a semantic check (e.g.
    /// retention `until_unix_seconds` in the past).
    InvalidPayload,
    /// Daemon-side failure (I/O, busy LUN, etc.). The peer should
    /// retry per its own backoff policy.
    Internal,
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_test::{Token, assert_tokens};

    #[test]
    fn protocol_version_is_one_at_phase_one_two() {
        assert_eq!(PROTOCOL_VERSION, 1);
    }

    #[test]
    fn envelope_new_uses_current_protocol_version() {
        let env = Envelope::new(42, Request::Status);
        assert_eq!(env.version, PROTOCOL_VERSION);
        assert_eq!(env.id, 42);
        assert_eq!(env.payload, Request::Status);
    }

    #[test]
    fn envelope_validate_accepts_current_version() {
        let env = Envelope::new(1, Request::Status);
        assert_eq!(env.validate(), Ok(()));
    }

    #[test]
    fn envelope_validate_rejects_other_version() {
        let env = Envelope {
            version: 2,
            id: 1,
            payload: Request::Status,
        };
        assert_eq!(
            env.validate(),
            Err(IpcError::UnsupportedVersion {
                got: 2,
                expected: PROTOCOL_VERSION,
            }),
        );
    }

    #[test]
    fn envelope_validate_error_message_is_actionable() {
        let err = IpcError::UnsupportedVersion {
            got: 9,
            expected: 1,
        };
        let rendered = err.to_string();
        assert!(rendered.contains("got 9"), "missing got: {rendered}");
        assert!(
            rendered.contains("expected 1"),
            "missing expected: {rendered}"
        );
    }

    #[test]
    fn status_body_round_trips_via_tokens() {
        let body = StatusBody {
            lun_id: 0,
            state: DaemonState::Serving,
            volume_label: "TESLACAM".to_owned(),
            volume_size_bytes: 64 * 1024 * 1024 * 1024,
            uptime_seconds: 3600,
        };
        assert_tokens(
            &body,
            &[
                Token::Struct {
                    name: "StatusBody",
                    len: 5,
                },
                Token::Str("lun_id"),
                Token::U8(0),
                Token::Str("state"),
                Token::UnitVariant {
                    name: "DaemonState",
                    variant: "SERVING",
                },
                Token::Str("volume_label"),
                Token::Str("TESLACAM"),
                Token::Str("volume_size_bytes"),
                Token::U64(64 * 1024 * 1024 * 1024),
                Token::Str("uptime_seconds"),
                Token::U64(3600),
                Token::StructEnd,
            ],
        );
    }

    #[test]
    fn retention_update_round_trips_via_tokens() {
        let upd = RetentionUpdate {
            clip_path: "TeslaCam/SavedClips/2026-01-01_00-00-00".to_owned(),
            action: RetentionAction::Extend {
                until_unix_seconds: 1_700_000_000,
            },
        };
        // RetentionAction uses `#[serde(tag = "type")]` so the
        // Extend variant serialises as a Struct with the tag
        // flattened in as a field, not as a StructVariant.
        assert_tokens(
            &upd,
            &[
                Token::Struct {
                    name: "RetentionUpdate",
                    len: 2,
                },
                Token::Str("clip_path"),
                Token::Str("TeslaCam/SavedClips/2026-01-01_00-00-00"),
                Token::Str("action"),
                Token::Struct {
                    name: "RetentionAction",
                    len: 2,
                },
                Token::Str("type"),
                Token::Str("EXTEND"),
                Token::Str("until_unix_seconds"),
                Token::U64(1_700_000_000),
                Token::StructEnd,
                Token::StructEnd,
            ],
        );
    }

    #[test]
    fn request_status_serialises_as_tagged_unit_variant() {
        // Request uses `#[serde(tag = "type")]`; the unit variant
        // Status serialises as a single-field Struct, not as a
        // top-level Map. assert_tokens (which checks both
        // directions) is the right helper.
        assert_tokens(
            &Request::Status,
            &[
                Token::Struct {
                    name: "Request",
                    len: 1,
                },
                Token::Str("type"),
                Token::Str("STATUS"),
                Token::StructEnd,
            ],
        );
    }

    #[test]
    fn request_retention_update_carries_payload_via_tag() {
        let req = Request::RetentionUpdate {
            updates: vec![RetentionUpdate {
                clip_path: "a.mp4".to_owned(),
                action: RetentionAction::Hide,
            }],
        };
        // serde_json round-trip verifies the same wire shape we'd
        // actually use over the socket.
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("\"type\":\"RETENTION_UPDATE\""), "{json}");
        assert!(json.contains("\"clip_path\":\"a.mp4\""), "{json}");
        assert!(json.contains("\"type\":\"HIDE\""), "{json}");
        let parsed: Request = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, req);
    }

    #[test]
    fn response_status_serialises_with_nested_body() {
        let resp = Response::Status(StatusBody {
            lun_id: 1,
            state: DaemonState::Draining,
            volume_label: "LIGHTSHOW".to_owned(),
            volume_size_bytes: 32 * 1024 * 1024 * 1024,
            uptime_seconds: 7200,
        });
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"type\":\"STATUS\""), "{json}");
        assert!(json.contains("\"lun_id\":1"), "{json}");
        assert!(json.contains("\"state\":\"DRAINING\""), "{json}");
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, resp);
    }

    #[test]
    fn response_retention_ack_carries_counts_and_failures() {
        let resp = Response::RetentionAck {
            applied: 7,
            failed: vec![RetentionFailure {
                clip_path: "b.mp4".to_owned(),
                reason: "not found".to_owned(),
            }],
        };
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"type\":\"RETENTION_ACK\""), "{json}");
        assert!(json.contains("\"applied\":7"), "{json}");
        assert!(json.contains("\"reason\":\"not found\""), "{json}");
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, resp);
    }

    #[test]
    fn response_invalidate_ack_is_tagged_unit_variant() {
        let resp = Response::InvalidateAck;
        let json = serde_json::to_string(&resp).unwrap();
        assert_eq!(json, "{\"type\":\"INVALIDATE_ACK\"}");
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, resp);
    }

    #[test]
    fn response_error_carries_typed_code_and_message() {
        let resp = Response::Error(ErrorBody {
            code: ErrorCode::UnsupportedVersion,
            message: "got 2, expected 1".to_owned(),
        });
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"code\":\"UNSUPPORTED_VERSION\""), "{json}");
        assert!(json.contains("\"message\":\"got 2, expected 1\""), "{json}");
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, resp);
    }

    #[test]
    fn envelope_round_trips_end_to_end_via_json() {
        // The wire format the transport layer is most likely to
        // pick (length-prefixed JSON over Unix socket); the actual
        // framing is out of scope for this module, but the payload
        // shape isn't.
        let env = Envelope::new(
            1234,
            Request::RetentionUpdate {
                updates: vec![
                    RetentionUpdate {
                        clip_path: "TeslaCam/RecentClips/x.mp4".to_owned(),
                        action: RetentionAction::Hide,
                    },
                    RetentionUpdate {
                        clip_path: "TeslaCam/SavedClips/y.mp4".to_owned(),
                        action: RetentionAction::Extend {
                            until_unix_seconds: 1_800_000_000,
                        },
                    },
                ],
            },
        );
        let json = serde_json::to_string(&env).unwrap();
        let parsed: Envelope<Request> = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, env);
        assert_eq!(parsed.validate(), Ok(()));
    }

    #[test]
    fn unknown_fields_are_tolerated_for_forward_compat() {
        // Older clients receiving a future server response with an
        // added field MUST parse cleanly. This documents the
        // deliberate absence of `deny_unknown_fields` on wire types.
        let future = r#"{
            "type": "STATUS",
            "lun_id": 0,
            "state": "SERVING",
            "volume_label": "TESLACAM",
            "volume_size_bytes": 1024,
            "uptime_seconds": 1,
            "new_field_added_in_future_version": "ok"
        }"#;
        let parsed: Response = serde_json::from_str(future).unwrap();
        assert!(
            matches!(&parsed, Response::Status(body) if body.lun_id == 0),
            "expected Status with lun_id=0, got {parsed:?}",
        );
    }

    #[test]
    fn unknown_request_type_is_rejected_at_deserialise_time() {
        // Defensive: even with forward-compat, a totally unknown
        // tag value is a hard failure. The Phase 1.5+ server will
        // map this to `Response::Error(UnknownMessageType)`.
        let bogus = r#"{"type":"DELETE_ALL_THE_THINGS"}"#;
        let res: Result<Request, _> = serde_json::from_str(bogus);
        assert!(res.is_err(), "unknown variant should fail to parse");
    }
}
