//! Strict Host/Origin/Sec-Fetch-Site same-origin evidence checks for non-GET
//! mutation routes.

use axum::http::header::HOST;
use axum::http::{HeaderMap, HeaderName, StatusCode};
use teslausb_core::durable_mutation::validate_non_get_same_origin;

use crate::error::ApiError;

/// Require strict same-origin evidence for a non-GET mutation request.
pub(crate) fn require_strict_same_origin(
    headers: &HeaderMap,
    forbidden_message: &'static str,
) -> Result<(), ApiError> {
    let host = single_header(headers, &HOST).ok_or_else(|| forbidden(forbidden_message))?;
    let origin_name = HeaderName::from_static("origin");
    let origin =
        single_header(headers, &origin_name).ok_or_else(|| forbidden(forbidden_message))?;
    let sec_fetch_name = HeaderName::from_static("sec-fetch-site");
    let sec_fetch_site = single_header(headers, &sec_fetch_name);
    validate_non_get_same_origin(host, Some(origin), sec_fetch_site)
        .map_err(|_| forbidden(forbidden_message))
}

fn single_header<'a>(headers: &'a HeaderMap, name: &HeaderName) -> Option<&'a str> {
    let values: Vec<_> = headers.get_all(name).iter().collect();
    if values.len() != 1 {
        return None;
    }
    values[0].to_str().ok()
}

fn forbidden(message: &'static str) -> ApiError {
    ApiError::status(StatusCode::FORBIDDEN, "forbidden_origin", message)
}
