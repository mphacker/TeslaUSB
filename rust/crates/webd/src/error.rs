//! The uniform error envelope (`{"error": {"code", "message"}}`, contract
//! D2 §1).
#![allow(clippy::module_name_repetitions)]

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::Serialize;

/// A request-level API error rendered as the D2 error envelope. Internal
/// failures never leak the underlying database message to the client.
#[derive(Debug)]
pub(crate) enum ApiError {
    /// `400` — a malformed query parameter.
    BadRequest {
        /// Machine-readable error code.
        code: &'static str,
        /// Human-readable message.
        message: String,
    },
    /// `404` — the addressed resource does not exist.
    NotFound,
    /// A mutation-path error carrying an explicit status, machine code, and
    /// message (used by the car-delete handoff route for `409`/`422`/`500`/
    /// `501`/`502`/`503`). Internal failures still route through [`Self::Internal`]
    /// so a DB message never leaks.
    Status {
        /// The HTTP status to emit.
        status: StatusCode,
        /// Machine-readable error code.
        code: &'static str,
        /// Human-readable message (safe to surface; never a raw DB error).
        message: String,
    },
    /// `500` — an unexpected internal failure (DB error, task join). The
    /// detail is kept server-side and never serialized.
    Internal,
}

impl ApiError {
    /// A `400` for an invalid query parameter.
    pub(crate) fn bad_request(code: &'static str, message: impl Into<String>) -> Self {
        Self::BadRequest {
            code,
            message: message.into(),
        }
    }

    /// A mutation-path error with an explicit status.
    pub(crate) fn status(
        status: StatusCode,
        code: &'static str,
        message: impl Into<String>,
    ) -> Self {
        Self::Status {
            status,
            code,
            message: message.into(),
        }
    }
}

/// The serialized body shape: `{"error": {"code", "message"}}`.
#[derive(Serialize)]
struct ErrorBody {
    error: ErrorDetail,
}

#[derive(Serialize)]
struct ErrorDetail {
    code: &'static str,
    message: String,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code, message) = match self {
            Self::BadRequest { code, message } => (StatusCode::BAD_REQUEST, code, message),
            Self::NotFound => (
                StatusCode::NOT_FOUND,
                "not_found",
                "resource not found".to_owned(),
            ),
            Self::Status {
                status,
                code,
                message,
            } => (status, code, message),
            Self::Internal => (
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal",
                "internal server error".to_owned(),
            ),
        };
        let body = ErrorBody {
            error: ErrorDetail { code, message },
        };
        (status, Json(body)).into_response()
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(_: rusqlite::Error) -> Self {
        Self::Internal
    }
}
