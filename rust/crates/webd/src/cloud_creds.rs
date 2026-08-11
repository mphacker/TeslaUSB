use std::collections::BTreeMap;
use std::fmt;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use axum::extract::{DefaultBodyLimit, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[cfg(not(test))]
use teslausb_creds::ProcHardwareRoot;
#[cfg(test)]
use teslausb_creds::StaticHardwareRoot;
use teslausb_creds::{
    BlobKeyMaterial, CLOUD_PROVIDER_CREDS_FILENAME, CredentialDocument, CredentialFlow,
    OAuthProvider, TESLA_SALT_FILENAME, decrypt, derive_key, encrypt, normalize_oauth_token,
    read_blob, read_or_create_salt, read_salt, render_rclone_conf, validate_document,
    with_creds_lock, write_blob_atomic,
};

use crate::AppState;
use crate::error::ApiError;

/// Hard request-body ceiling for `POST /api/cloud/credentials` (64 KiB). OAuth
/// token payloads are normally ~1–2 KiB, so this blocks pathological oversized
/// request bodies before parsing.
const CLOUD_CREDENTIALS_BODY_LIMIT: usize = 64 * 1024;

/// The remote name uploadd renders into `/run/teslausb/rclone.conf`
/// (`docs/specs/contracts/cloud-provider-creds.md` §3). Used here only to
/// dry-run the render at save time.
const RENDER_REMOTE_NAME: &str = "teslausb";
const ONEDRIVE_GRAPH_DRIVE_URL: &str = "https://graph.microsoft.com/v1.0/me/drive";
const ONEDRIVE_DISCOVERY_TIMEOUT_SECS: u64 = 20;
const ONEDRIVE_HTTP_STATUS_MARKER: &str = "\n__TESLAUSB_HTTP_STATUS__:";
/// The marker as written *into* the curl config. curl's config parser turns the
/// two-character escape `\n` inside a quoted value into a real newline in the
/// emitted output. A raw newline here instead terminates the quoted value, and
/// curl then rejects the whole config with exit 26 without making the request
/// (verified on device).
const ONEDRIVE_HTTP_STATUS_WRITE_OUT: &str = "\\n__TESLAUSB_HTTP_STATUS__:";
/// Cap on the bytes read from curl during drive discovery. The real
/// `/me/drive` response is ~700 bytes; this bounds webd's memory on a
/// 512 MB device if the response is ever pathologically large.
const ONEDRIVE_DISCOVERY_OUTPUT_LIMIT: usize = 64 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct OnedriveDriveInfo {
    pub(crate) drive_id: String,
    pub(crate) drive_type: String,
}
pub(crate) type OnedriveDiscoverer =
    Arc<dyn Fn(&str) -> Result<OnedriveDriveInfo, ApiError> + Send + Sync>;

pub(crate) fn default_onedrive_discoverer() -> OnedriveDiscoverer {
    Arc::new(discover_onedrive_drive_info_via_curl)
}

#[derive(Clone, Deserialize)]
struct RedactedToken(String);

impl RedactedToken {
    fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for RedactedToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("<redacted>")
    }
}

#[derive(Debug, Deserialize)]
struct SaveCloudCredentialsReq {
    provider: String,
    token: RedactedToken,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct CloudCredentialsResp {
    state: &'static str,
    provider: Option<String>,
    updated_at: Option<i64>,
}

pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/cloud/credentials",
            get(get_cloud_credentials).delete(delete_cloud_credentials),
        )
        .route(
            "/cloud/credentials",
            post(save_cloud_credentials).layer(DefaultBodyLimit::max(CLOUD_CREDENTIALS_BODY_LIMIT)),
        )
}

async fn get_cloud_credentials(
    State(state): State<AppState>,
) -> Result<Json<CloudCredentialsResp>, ApiError> {
    let creds_dir = state.cloud_creds_dir.clone();
    let status = tokio::task::spawn_blocking(move || read_cloud_credentials_state(&creds_dir))
        .await
        .map_err(|_| ApiError::Internal)??;
    Ok(Json(status))
}

async fn save_cloud_credentials(
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(req): Json<SaveCloudCredentialsReq>,
) -> Result<Json<CloudCredentialsResp>, ApiError> {
    if !crate::wifi_mutate::same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin cloud credential mutation refused",
        ));
    }
    let guard = state
        .cloud_creds_mutation
        .clone()
        .try_lock_owned()
        .map_err(|_| {
            ApiError::status(
                StatusCode::CONFLICT,
                "mutation_in_progress",
                "another cloud credential change is in progress",
            )
        })?;
    let creds_dir = state.cloud_creds_dir.clone();
    let onedrive_discoverer = state.onedrive_discoverer.clone();
    let status = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        persist_cloud_credentials(&creds_dir, &req, onedrive_discoverer.as_ref())
    })
    .await
    .map_err(|_| ApiError::Internal)??;
    Ok(Json(status))
}

async fn delete_cloud_credentials(
    headers: HeaderMap,
    State(state): State<AppState>,
) -> Result<Json<CloudCredentialsResp>, ApiError> {
    if !crate::wifi_mutate::same_origin_ok(&headers) {
        return Err(ApiError::status(
            StatusCode::FORBIDDEN,
            "forbidden_origin",
            "cross-origin cloud credential mutation refused",
        ));
    }
    let guard = state
        .cloud_creds_mutation
        .clone()
        .try_lock_owned()
        .map_err(|_| {
            ApiError::status(
                StatusCode::CONFLICT,
                "mutation_in_progress",
                "another cloud credential change is in progress",
            )
        })?;
    let creds_dir = state.cloud_creds_dir.clone();
    let status = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        with_creds_lock(&creds_dir, || {
            let blob_path = creds_blob_path(&creds_dir);
            match std::fs::remove_file(blob_path) {
                Ok(()) => Ok(not_configured_state()),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    Ok(not_configured_state())
                }
                Err(_) => Err(storage_error()),
            }
        })
        .map_err(|_| storage_error())?
    })
    .await
    .map_err(|_| ApiError::Internal)??;
    Ok(Json(status))
}

fn persist_cloud_credentials(
    creds_dir: &Path,
    req: &SaveCloudCredentialsReq,
    discover_onedrive: &dyn Fn(&str) -> Result<OnedriveDriveInfo, ApiError>,
) -> Result<CloudCredentialsResp, ApiError> {
    with_creds_lock(creds_dir, || {
        let provider = parse_provider(&req.provider)?;
        let token =
            normalize_oauth_token(req.token.expose()).map_err(|err| normalize_token_error(&err))?;
        let mut options = BTreeMap::new();
        if matches!(provider, OAuthProvider::Onedrive) {
            let access_token = extract_onedrive_access_token(&token)?;
            let discovered = discover_onedrive(&access_token)?;
            options.insert("drive_id".to_owned(), discovered.drive_id);
            options.insert("drive_type".to_owned(), discovered.drive_type);
        }
        let document = CredentialDocument::new(CredentialFlow::OAuth {
            provider,
            token,
            options,
        });
        let validated = validate_document(&document).map_err(|err| validate_error(&err))?;
        // The allow-list accepts characters the renderer later refuses (`[`, `]`),
        // so dry-run the render uploadd will perform. Without this the save would
        // report `configured` and uploads would fail silently days later.
        render_rclone_conf(RENDER_REMOTE_NAME, &validated).map_err(|_| {
            ApiError::bad_request(
                "invalid_token",
                "token contains characters that cannot be written to an rclone config",
            )
        })?;

        std::fs::create_dir_all(creds_dir).map_err(|_| storage_error())?;
        let salt_path = salt_path(creds_dir);
        let blob_path = creds_blob_path(creds_dir);
        let salt = read_or_create_salt(&salt_path).map_err(|_| storage_error())?;
        let key = derive_key(
            &active_hardware_root(),
            &salt,
            teslausb_creds::DEFAULT_KDF_ITERS,
        )
        .map_err(|_| storage_error())?;
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = document.to_canonical_bytes().map_err(|_| storage_error())?;
        let blob = encrypt(&plaintext, &material).map_err(|_| storage_error())?;
        write_blob_atomic(&blob_path, &blob).map_err(|_| storage_error())?;
        read_cloud_credentials_state(creds_dir)
    })
    .map_err(|_| storage_error())?
}

fn read_cloud_credentials_state(creds_dir: &Path) -> Result<CloudCredentialsResp, ApiError> {
    let blob_path = creds_blob_path(creds_dir);
    let metadata = match std::fs::metadata(&blob_path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(not_configured_state()),
        Err(_) => return Err(storage_error()),
    };
    let updated_at = metadata
        .modified()
        .ok()
        .and_then(|mtime| mtime.duration_since(UNIX_EPOCH).ok())
        .and_then(|duration| i64::try_from(duration.as_secs()).ok());
    let Some(updated_at) = updated_at else {
        return Err(storage_error());
    };

    let load_result = (|| {
        let blob = read_blob(&blob_path)?;
        let salt = read_salt(&salt_path(creds_dir))?;
        let key = derive_key(
            &active_hardware_root(),
            &salt,
            teslausb_creds::DEFAULT_KDF_ITERS,
        )?;
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = decrypt(&blob, &material)?;
        let document = CredentialDocument::from_bytes(&plaintext)?;
        let provider = oauth_provider_string(&document);
        let _validated = validate_document(&document)?;
        match provider {
            Some(provider) => {
                Ok::<CloudCredentialsResp, teslausb_creds::CredsError>(CloudCredentialsResp {
                    state: "configured",
                    provider: Some(provider.to_owned()),
                    updated_at: Some(updated_at),
                })
            }
            None => Err(teslausb_creds::CredsError::InvalidBlob(
                "credential flow is not oauth",
            )),
        }
    })();

    match load_result {
        Ok(state) => Ok(state),
        // A DELETE can land between the metadata probe above and the read
        // below; if the blob is simply gone, say so rather than crying
        // `unreadable`.
        Err(_) if !blob_path.exists() => Ok(not_configured_state()),
        // Never auto-delete or overwrite unreadable credential blobs: operators
        // must explicitly re-save credentials after media cloning/key mismatch.
        Err(_) => Ok(CloudCredentialsResp {
            state: "unreadable",
            provider: None,
            updated_at: None,
        }),
    }
}

fn oauth_provider_string(document: &CredentialDocument) -> Option<&'static str> {
    match &document.flow {
        CredentialFlow::OAuth { provider, .. } => Some(match provider {
            OAuthProvider::Drive => "drive",
            OAuthProvider::Onedrive => "onedrive",
            OAuthProvider::Dropbox => "dropbox",
        }),
        _ => None,
    }
}

fn parse_provider(raw: &str) -> Result<OAuthProvider, ApiError> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "drive" => Ok(OAuthProvider::Drive),
        "onedrive" => Ok(OAuthProvider::Onedrive),
        "dropbox" => Ok(OAuthProvider::Dropbox),
        _ => Err(ApiError::bad_request(
            "invalid_provider",
            "provider must be one of: drive, onedrive, dropbox",
        )),
    }
}

fn normalize_token_error(err: &teslausb_creds::CredsError) -> ApiError {
    let message = match err {
        teslausb_creds::CredsError::EmptyOauthToken => "token is empty",
        teslausb_creds::CredsError::OauthTokenTooLong => "token exceeds 8192-byte limit",
        _ => "token is not valid JSON from `rclone authorize`",
    };
    ApiError::bad_request("invalid_token", message)
}

fn validate_error(err: &teslausb_creds::CredsError) -> ApiError {
    match err {
        teslausb_creds::CredsError::InvalidOnedriveDriveType => ApiError::bad_request(
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned unsupported driveType; expected personal, business, or documentLibrary",
        ),
        teslausb_creds::CredsError::EmptyOnedriveDriveId
        | teslausb_creds::CredsError::OnedriveDriveIdTooLong
        | teslausb_creds::CredsError::IllegalOnedriveDriveId => ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned an invalid drive id",
        ),
        _ => ApiError::bad_request(
            "invalid_token",
            "token is not valid JSON from `rclone authorize`",
        ),
    }
}

fn extract_onedrive_access_token(token_json: &str) -> Result<String, ApiError> {
    let token = serde_json::from_str::<Value>(token_json)
        .ok()
        .and_then(|value| {
            value
                .get("access_token")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|token| !token.is_empty())
                .map(str::to_owned)
        })
        .ok_or_else(|| {
            ApiError::bad_request(
                "invalid_token",
                "token is missing non-empty access_token; re-run `rclone authorize \"onedrive\"` and paste the full JSON token",
            )
        })?;
    // Security: this value is interpolated into a curl config fed on stdin.
    // A raw newline would terminate the quoted `header =` line and let the
    // caller inject arbitrary curl directives (e.g. `output = <path>`, which
    // writes the response body to that path). Fail closed — a real OAuth
    // access token never contains control bytes.
    if token.bytes().any(|byte| byte <= 0x1f || byte == 0x7f) {
        return Err(ApiError::bad_request(
            "invalid_token",
            "access_token contains control characters; re-run `rclone authorize \"onedrive\"` and paste the token exactly as printed",
        ));
    }
    Ok(token)
}

fn build_onedrive_curl_config(access_token: &str) -> String {
    let mut auth_header = String::from("Authorization: ");
    auth_header.push_str("Bearer ");
    auth_header.push_str(access_token);
    let escaped_auth = escape_curl_config_value(&auth_header);
    format!(
        "url = \"{ONEDRIVE_GRAPH_DRIVE_URL}\"\nheader = \"{escaped_auth}\"\nsilent\nshow-error\nmax-time = {ONEDRIVE_DISCOVERY_TIMEOUT_SECS}\nmax-filesize = {ONEDRIVE_DISCOVERY_OUTPUT_LIMIT}\nwrite-out = \"{ONEDRIVE_HTTP_STATUS_WRITE_OUT}%{{http_code}}\"\n",
    )
}

fn discover_onedrive_drive_info_via_curl(
    access_token: &str,
) -> Result<OnedriveDriveInfo, ApiError> {
    let curl_config = build_onedrive_curl_config(access_token);
    let mut child = Command::new("curl")
        .arg("--config")
        .arg("-")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                ApiError::status(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "onedrive_discovery_failed",
                    "curl is required for OneDrive drive discovery but is not installed",
                )
            } else {
                ApiError::status(
                    StatusCode::BAD_GATEWAY,
                    "onedrive_discovery_failed",
                    "failed to start curl for OneDrive drive discovery",
                )
            }
        })?;
    {
        let mut stdin = child.stdin.take().ok_or_else(|| {
            ApiError::status(
                StatusCode::BAD_GATEWAY,
                "onedrive_discovery_failed",
                "failed to open curl stdin for OneDrive drive discovery",
            )
        })?;
        stdin.write_all(curl_config.as_bytes()).map_err(|_| {
            ApiError::status(
                StatusCode::BAD_GATEWAY,
                "onedrive_discovery_failed",
                "failed to send request to curl for OneDrive drive discovery",
            )
        })?;
    }

    let mut stdout_pipe = child.stdout.take().ok_or_else(|| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "failed to open curl output for OneDrive drive discovery",
        )
    })?;
    // Drain stdout before waiting, and stop at a fixed cap so an oversized
    // response cannot exhaust memory on the device.
    let Ok(Some(stdout_bytes)) = read_capped(&mut stdout_pipe, ONEDRIVE_DISCOVERY_OUTPUT_LIMIT)
    else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned an unreadable or oversized response",
        ));
    };
    drop(stdout_pipe);
    let status = child.wait().map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "failed while waiting for curl during OneDrive drive discovery",
        )
    })?;
    if !status.success() {
        return Err(onedrive_curl_exit_error(status.code()));
    }

    let stdout = String::from_utf8(stdout_bytes).map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned non-UTF-8 output",
        )
    })?;
    let (body, http_code) = split_onedrive_curl_output(&stdout)?;
    if http_code != 200 {
        return Err(onedrive_http_status_error(http_code));
    }
    parse_onedrive_drive_body(body)
}

fn onedrive_curl_exit_error(code: Option<i32>) -> ApiError {
    let message = match code {
        Some(28) => {
            "OneDrive drive discovery timed out after 20 seconds; check network access and retry"
                .to_owned()
        }
        Some(63) => "OneDrive drive discovery response exceeded the size limit".to_owned(),
        Some(code) => format!(
            "curl failed during OneDrive drive discovery (exit code {code}); check network access and retry"
        ),
        None => "curl terminated unexpectedly during OneDrive drive discovery".to_owned(),
    };
    ApiError::status(
        StatusCode::BAD_GATEWAY,
        "onedrive_discovery_failed",
        message,
    )
}

fn read_capped(reader: &mut impl Read, limit: usize) -> std::io::Result<Option<Vec<u8>>> {
    let cap = u64::try_from(limit).unwrap_or(u64::MAX).saturating_add(1);
    let mut buf = Vec::new();
    reader.take(cap).read_to_end(&mut buf)?;
    if buf.len() > limit {
        return Ok(None);
    }
    Ok(Some(buf))
}

fn split_onedrive_curl_output(output: &str) -> Result<(&str, u16), ApiError> {
    let (body, status_part) = output
        .rsplit_once(ONEDRIVE_HTTP_STATUS_MARKER)
        .ok_or_else(|| {
            ApiError::status(
                StatusCode::BAD_GATEWAY,
                "onedrive_discovery_failed",
                "OneDrive drive discovery did not return an HTTP status",
            )
        })?;
    let http_code = status_part.trim().parse::<u16>().map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned an unreadable HTTP status",
        )
    })?;
    Ok((body, http_code))
}

fn onedrive_http_status_error(http_code: u16) -> ApiError {
    match http_code {
        401 => ApiError::bad_request(
            "onedrive_discovery_failed",
            "OneDrive token appears expired or invalid (Microsoft Graph returned HTTP 401); re-run `rclone authorize \"onedrive\"` and save again",
        ),
        403 => ApiError::bad_request(
            "onedrive_discovery_failed",
            "Microsoft Graph returned HTTP 403 while discovering OneDrive drive metadata; re-run `rclone authorize \"onedrive\"` and make sure the account can access OneDrive",
        ),
        404 => ApiError::bad_request(
            "onedrive_discovery_failed",
            "Microsoft Graph returned HTTP 404 while discovering OneDrive drive metadata; verify the account has an initialized OneDrive",
        ),
        400..=499 => ApiError::bad_request(
            "onedrive_discovery_failed",
            format!(
                "Microsoft Graph returned HTTP {http_code} while discovering OneDrive drive metadata"
            ),
        ),
        _ => ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            format!(
                "Microsoft Graph returned HTTP {http_code} while discovering OneDrive drive metadata"
            ),
        ),
    }
}

fn parse_onedrive_drive_body(body: &str) -> Result<OnedriveDriveInfo, ApiError> {
    let value = serde_json::from_str::<Value>(body).map_err(|_| {
        ApiError::status(
            StatusCode::BAD_GATEWAY,
            "onedrive_discovery_failed",
            "OneDrive drive discovery returned malformed JSON from Microsoft Graph",
        )
    })?;
    let drive_id = value
        .get("id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| {
            ApiError::status(
                StatusCode::BAD_GATEWAY,
                "onedrive_discovery_failed",
                "OneDrive drive discovery response is missing top-level `id`",
            )
        })?;
    let drive_type = value
        .get("driveType")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|drive_type| !drive_type.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| {
            ApiError::status(
                StatusCode::BAD_GATEWAY,
                "onedrive_discovery_failed",
                "OneDrive drive discovery response is missing top-level `driveType`",
            )
        })?;
    Ok(OnedriveDriveInfo {
        drive_id,
        drive_type,
    })
}

fn escape_curl_config_value(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn creds_blob_path(base_dir: &Path) -> PathBuf {
    base_dir.join(CLOUD_PROVIDER_CREDS_FILENAME)
}

fn salt_path(base_dir: &Path) -> PathBuf {
    base_dir.join(TESLA_SALT_FILENAME)
}

fn not_configured_state() -> CloudCredentialsResp {
    CloudCredentialsResp {
        state: "not_configured",
        provider: None,
        updated_at: None,
    }
}

fn storage_error() -> ApiError {
    ApiError::status(
        StatusCode::INTERNAL_SERVER_ERROR,
        "credentials_io_failed",
        "failed to access cloud credential storage",
    )
}

#[cfg(not(test))]
fn active_hardware_root() -> ProcHardwareRoot {
    ProcHardwareRoot
}

#[cfg(test)]
fn active_hardware_root() -> StaticHardwareRoot {
    StaticHardwareRoot::new("00000000deadbeef", "webd-tests-machine-id")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use axum::Router;
    use axum::body::Body;
    use axum::http::header::{CONTENT_TYPE, HOST};
    use axum::http::{Method, Request};
    use rusqlite::Connection;
    use serde_json::{Value, json};
    use tempfile::TempDir;
    use tower::ServiceExt;

    use crate::{Catalog, MediaConfig};

    /// Regression: the write-out marker constant starts with a real newline for
    /// *parsing* curl's output. Emitting that raw newline into the curl config
    /// terminated the quoted `write-out` value, and curl then rejected the whole
    /// config with exit 26 and never made the request -- discovery could not work
    /// for any token. The seam-injected unit tests never exercised the real curl
    /// path, so only a live device probe caught it. Every config line must be a
    /// complete directive with balanced quotes.
    #[test]
    fn onedrive_curl_config_lines_are_complete_directives() {
        let config = build_onedrive_curl_config("tok");
        for line in config.lines() {
            assert_eq!(
                line.matches('"').count() % 2,
                0,
                "unbalanced quotes (value broken across lines): {line:?}"
            );
        }
        let Some(write_out) = config.lines().find(|line| line.starts_with("write-out")) else {
            panic!("config must carry a write-out directive");
        };
        assert!(
            write_out.ends_with("%{http_code}\""),
            "write-out directive was truncated: {write_out:?}"
        );
    }

    #[test]
    fn onedrive_status_marker_config_form_decodes_to_parse_form() {
        assert_eq!(
            ONEDRIVE_HTTP_STATUS_WRITE_OUT.replace("\\n", "\n"),
            ONEDRIVE_HTTP_STATUS_MARKER,
            "config-side and parse-side markers drifted"
        );
    }

    #[test]
    fn read_capped_accepts_output_exactly_at_the_limit() {
        let payload = vec![b'a'; 32];
        let capped = read_capped(&mut payload.as_slice(), 32).unwrap();
        assert_eq!(capped, Some(payload));
    }

    #[test]
    fn read_capped_rejects_output_one_byte_over_the_limit() {
        let payload = vec![b'a'; 33];
        let capped = read_capped(&mut payload.as_slice(), 32).unwrap();
        assert_eq!(capped, None, "oversized output must not be buffered");
    }

    /// A newline inside `access_token` would break out of the quoted `header =`
    /// line in the curl config we feed on stdin, letting an unauthenticated
    /// caller inject arbitrary curl directives (verified: an injected
    /// `output = <path>` makes curl write the response body to that path).
    /// `normalize_oauth_token` only requires a non-empty `access_token`, so the
    /// control byte survives into the extracted string. Reject it at the source.
    #[test]
    fn onedrive_access_token_with_control_bytes_is_rejected() {
        for raw in [
            "x\noutput = /home/pi/pwned",
            "x\r\nupload-file = /etc/shadow",
            "x\ttab",
            "x\u{0000}nul",
        ] {
            let token_json = serde_json::to_string(&json!({ "access_token": raw })).unwrap();
            let normalized = teslausb_creds::normalize_oauth_token(&token_json).unwrap();
            let err = extract_onedrive_access_token(&normalized).unwrap_err();
            assert!(
                matches!(&err, ApiError::BadRequest { code, .. } if *code == "invalid_token"),
                "raw {raw:?} was not rejected"
            );
        }
    }

    #[test]
    fn onedrive_access_token_escaping_leaves_no_config_breakout() {
        let escaped = escape_curl_config_value("a\"b\\c");
        assert!(!escaped.contains('\n'), "escaped value must not contain LF");
        assert_eq!(escaped, "a\\\"b\\\\c");
    }

    struct Fixture {
        _dir: TempDir,
        app: Router,
        creds_dir: PathBuf,
    }

    fn fixture() -> Fixture {
        fixture_with_onedrive_discoverer(Arc::new(|_| {
            Ok(OnedriveDriveInfo {
                drive_id: "fixture-drive-id".to_owned(),
                drive_type: "personal".to_owned(),
            })
        }))
    }

    fn fixture_with_onedrive_discoverer(onedrive_discoverer: OnedriveDiscoverer) -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("catalog.db");
        {
            let mut conn = Connection::open(&db_path).unwrap();
            conn.execute_batch("PRAGMA foreign_keys=ON;").unwrap();
            indexd::db::apply_migrations(&mut conn).unwrap();
        }

        let static_dir = dir.path().join("static");
        std::fs::create_dir_all(&static_dir).unwrap();
        std::fs::write(static_dir.join("index.html"), "<!doctype html>shell").unwrap();

        let archive = dir.path().join("archive");
        let cache = dir.path().join("cache");
        std::fs::create_dir_all(&archive).unwrap();
        std::fs::create_dir_all(&cache).unwrap();
        let media = MediaConfig::new(archive, cache);
        let creds_dir = dir.path().join("cloud-creds");

        let app = crate::router_with_cloud_creds_dir_and_onedrive_discoverer(
            Catalog::open(&db_path).unwrap(),
            static_dir,
            media,
            creds_dir.clone(),
            onedrive_discoverer,
        );
        Fixture {
            _dir: dir,
            app,
            creds_dir,
        }
    }

    fn read_stored_document(creds_dir: &Path) -> CredentialDocument {
        let blob = read_blob(&creds_blob_path(creds_dir)).unwrap();
        let salt = read_salt(&salt_path(creds_dir)).unwrap();
        let key = derive_key(
            &active_hardware_root(),
            &salt,
            teslausb_creds::DEFAULT_KDF_ITERS,
        )
        .unwrap();
        let material = BlobKeyMaterial {
            key,
            salt,
            kdf_iters: teslausb_creds::DEFAULT_KDF_ITERS,
        };
        let plaintext = decrypt(&blob, &material).unwrap();
        CredentialDocument::from_bytes(&plaintext).unwrap()
    }

    async fn request_json(
        app: &Router,
        method: Method,
        uri: &str,
        host: Option<&str>,
        body: Option<Value>,
    ) -> (StatusCode, Vec<u8>, Value) {
        let mut builder = Request::builder().method(method).uri(uri);
        if let Some(host) = host {
            builder = builder.header(HOST, host);
        }
        let request = if let Some(body) = body {
            builder
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap()
        } else {
            builder.body(Body::empty()).unwrap()
        };
        let response = app.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec();
        let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, bytes, value)
    }

    #[test]
    fn debug_redacts_token() {
        let token = "secret-token-never-log";
        let req: SaveCloudCredentialsReq =
            serde_json::from_value(json!({ "provider": "onedrive", "token": token })).unwrap();
        let rendered = format!("{req:?}");
        assert!(rendered.contains("<redacted>"));
        assert!(!rendered.contains(token));
    }

    #[tokio::test]
    async fn get_empty_directory_reports_not_configured() {
        let fx = fixture();
        let (status, _, body) =
            request_json(&fx.app, Method::GET, "/api/cloud/credentials", None, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(
            body,
            json!({ "state": "not_configured", "provider": null, "updated_at": null })
        );
    }

    #[tokio::test]
    async fn post_get_delete_cycle_works_for_marker_wrapped_onedrive_token() {
        let fx = fixture();
        let wrapped = "Paste the following into your remote machine --->\n{\"access_token\":\"ya29.token\",\"token_type\":\"Bearer\",\"refresh_token\":\"refresh-token\",\"expiry\":\"2026-01-01T00:00:00Z\"}\n<---End paste";
        let (post_status, _, post_body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({ "provider": "onedrive", "token": wrapped })),
        )
        .await;
        assert_eq!(post_status, StatusCode::OK);
        assert_eq!(post_body["state"], "configured");
        assert_eq!(post_body["provider"], "onedrive");
        assert!(post_body["updated_at"].is_number());

        let (get_status, _, get_body) =
            request_json(&fx.app, Method::GET, "/api/cloud/credentials", None, None).await;
        assert_eq!(get_status, StatusCode::OK);
        assert_eq!(get_body["state"], "configured");
        assert_eq!(get_body["provider"], "onedrive");
        assert!(get_body["updated_at"].is_number());

        let (delete_status, _, delete_body) = request_json(
            &fx.app,
            Method::DELETE,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            None,
        )
        .await;
        assert_eq!(delete_status, StatusCode::OK);
        assert_eq!(delete_body["state"], "not_configured");

        let (final_get_status, _, final_get_body) =
            request_json(&fx.app, Method::GET, "/api/cloud/credentials", None, None).await;
        assert_eq!(final_get_status, StatusCode::OK);
        assert_eq!(
            final_get_body,
            json!({ "state": "not_configured", "provider": null, "updated_at": null })
        );
    }

    #[tokio::test]
    async fn onedrive_save_persists_discovered_drive_id_and_drive_type() {
        let fx = fixture_with_onedrive_discoverer(Arc::new(|_| {
            Ok(OnedriveDriveInfo {
                drive_id: "3AD8F5FDB3ED7D27".to_owned(),
                drive_type: "personal".to_owned(),
            })
        }));

        let (status, _, _) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({
                "provider": "onedrive",
                "token": r#"{"access_token":"ya29.token"}"#,
            })),
        )
        .await;
        assert_eq!(status, StatusCode::OK);

        let document = read_stored_document(&fx.creds_dir);
        let CredentialFlow::OAuth {
            provider, options, ..
        } = document.flow
        else {
            panic!("expected oauth credential flow");
        };
        assert_eq!(provider, OAuthProvider::Onedrive);
        assert_eq!(
            options.get("drive_id"),
            Some(&"3AD8F5FDB3ED7D27".to_owned())
        );
        assert_eq!(options.get("drive_type"), Some(&"personal".to_owned()));
    }

    #[test]
    fn nested_onedrive_ids_keep_top_level_drive_id() {
        let graph_body = r#"{
            "@odata.context":"https://graph.microsoft.com/v1.0/$metadata#drives/$entity",
            "createdDateTime":"2024-01-01T00:00:00Z",
            "description":"",
            "id":"TOP-LEVEL-ID",
            "name":"OneDrive",
            "driveType":"personal",
            "createdBy":{"user":{"id":"nested-created-by"}},
            "lastModifiedBy":{"user":{"id":"nested-last-modified"}},
            "owner":{"user":{"id":"nested-owner"}}
        }"#;
        let parsed = parse_onedrive_drive_body(graph_body).unwrap();
        assert_eq!(parsed.drive_id, "TOP-LEVEL-ID");
        assert_eq!(parsed.drive_type, "personal");
    }

    #[tokio::test]
    async fn onedrive_drive_type_accepts_business_and_document_library() {
        for drive_type in ["business", "documentLibrary"] {
            let fx = fixture_with_onedrive_discoverer({
                let drive_type = drive_type.to_owned();
                Arc::new(move |_| {
                    Ok(OnedriveDriveInfo {
                        drive_id: "3AD8F5FDB3ED7D27".to_owned(),
                        drive_type: drive_type.clone(),
                    })
                })
            });
            let (status, _, _) = request_json(
                &fx.app,
                Method::POST,
                "/api/cloud/credentials",
                Some("cybertruckusb.local"),
                Some(json!({
                    "provider": "onedrive",
                    "token": r#"{"access_token":"ya29.token"}"#,
                })),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
        }
    }

    #[tokio::test]
    async fn onedrive_unrecognized_drive_type_is_rejected() {
        let fx = fixture_with_onedrive_discoverer(Arc::new(|_| {
            Ok(OnedriveDriveInfo {
                drive_id: "3AD8F5FDB3ED7D27".to_owned(),
                drive_type: "team".to_owned(),
            })
        }));
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({
                "provider": "onedrive",
                "token": r#"{"access_token":"ya29.token"}"#,
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "onedrive_discovery_failed");
        assert!(!fx.creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME).exists());
    }

    #[tokio::test]
    async fn onedrive_discovery_non_200_returns_error_and_does_not_persist() {
        let fx = fixture_with_onedrive_discoverer(Arc::new(|_| {
            Err(ApiError::bad_request(
                "onedrive_discovery_failed",
                "OneDrive token appears expired or invalid (Microsoft Graph returned HTTP 401); re-run `rclone authorize \"onedrive\"` and save again",
            ))
        }));
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({
                "provider": "onedrive",
                "token": r#"{"access_token":"ya29.token"}"#,
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "onedrive_discovery_failed");
        assert!(!fx.creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME).exists());
    }

    #[tokio::test]
    async fn onedrive_discovery_malformed_body_returns_error_and_does_not_persist() {
        let fx = fixture_with_onedrive_discoverer(Arc::new(|_| {
            parse_onedrive_drive_body("this is not json")
        }));
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({
                "provider": "onedrive",
                "token": r#"{"access_token":"ya29.token"}"#,
            })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_GATEWAY);
        assert_eq!(body["error"]["code"], "onedrive_discovery_failed");
        assert!(!fx.creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME).exists());
    }

    #[tokio::test]
    async fn non_onedrive_oauth_persists_with_empty_options_without_discovery() {
        let discover_calls = Arc::new(AtomicUsize::new(0));
        let fx = fixture_with_onedrive_discoverer({
            let discover_calls = Arc::clone(&discover_calls);
            Arc::new(move |_| {
                discover_calls.fetch_add(1, Ordering::SeqCst);
                Ok(OnedriveDriveInfo {
                    drive_id: "unused".to_owned(),
                    drive_type: "personal".to_owned(),
                })
            })
        });

        for provider in ["drive", "dropbox"] {
            let (status, _, _) = request_json(
                &fx.app,
                Method::POST,
                "/api/cloud/credentials",
                Some("cybertruckusb.local"),
                Some(json!({
                    "provider": provider,
                    "token": r#"{"access_token":"ya29.token"}"#,
                })),
            )
            .await;
            assert_eq!(status, StatusCode::OK);
            let document = read_stored_document(&fx.creds_dir);
            let CredentialFlow::OAuth { options, .. } = document.flow else {
                panic!("expected oauth credential flow");
            };
            assert!(options.is_empty());
        }

        assert_eq!(discover_calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn responses_never_echo_token() {
        let fx = fixture();
        let token = "token-never-leak-7d6a8a99";
        let token_json = format!(r#"{{"access_token":"{token}"}}"#);
        let (post_status, post_bytes, _) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({ "provider": "drive", "token": token_json })),
        )
        .await;
        assert_eq!(post_status, StatusCode::OK);
        let post_text = String::from_utf8_lossy(&post_bytes);
        assert!(!post_text.contains(token));

        let (get_status, get_bytes, _) =
            request_json(&fx.app, Method::GET, "/api/cloud/credentials", None, None).await;
        assert_eq!(get_status, StatusCode::OK);
        let get_text = String::from_utf8_lossy(&get_bytes);
        assert!(!get_text.contains(token));
    }

    #[tokio::test]
    async fn post_rejects_non_oauth_providers() {
        let fx = fixture();
        for provider in ["crypt", "s3"] {
            let (status, _, body) = request_json(
                &fx.app,
                Method::POST,
                "/api/cloud/credentials",
                Some("cybertruckusb.local"),
                Some(json!({ "provider": provider, "token": r#"{"access_token":"x"}"# })),
            )
            .await;
            assert_eq!(status, StatusCode::BAD_REQUEST);
            assert_eq!(body["error"]["code"], "invalid_provider");
        }
    }

    #[tokio::test]
    async fn post_rejects_junk_token_without_echoing_input() {
        let fx = fixture();
        let junk = "this is definitely not oauth json";
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({ "provider": "dropbox", "token": junk })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "invalid_token");
        let message = body["error"]["message"].as_str().unwrap();
        assert!(!message.contains(junk));
    }

    #[tokio::test]
    async fn post_rejects_a_token_that_could_not_be_rendered() {
        // `[`/`]` pass the option allow-list but the renderer refuses them. The
        // save has to fail here: reporting `configured` and only failing later,
        // inside uploadd, would look like a broken upload with no visible cause.
        let fx = fixture();
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({
                "provider": "onedrive",
                "token": r#"{"access_token":"x","extra":["a"]}"#,
            })),
        )
        .await;

        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "invalid_token");
        assert!(
            !fx.creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME).exists(),
            "rejected credential must not be written"
        );
    }

    #[tokio::test]
    async fn unreadable_blob_is_reported_and_not_deleted() {
        let fx = fixture();
        std::fs::create_dir_all(&fx.creds_dir).unwrap();
        let blob_path = fx.creds_dir.join(CLOUD_PROVIDER_CREDS_FILENAME);
        std::fs::write(&blob_path, [1_u8, 2, 3, 4, 5]).unwrap();

        let (status, _, body) =
            request_json(&fx.app, Method::GET, "/api/cloud/credentials", None, None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["state"], "unreadable");
        assert!(blob_path.exists());
    }

    #[tokio::test]
    async fn oversized_token_is_rejected() {
        let fx = fixture();
        let token = format!(r#"{{"access_token":"{}"}}"#, "x".repeat(9_000));
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("cybertruckusb.local"),
            Some(json!({ "provider": "onedrive", "token": token })),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body["error"]["code"], "invalid_token");
    }

    #[tokio::test]
    async fn cross_origin_host_is_forbidden() {
        let fx = fixture();
        let (status, _, body) = request_json(
            &fx.app,
            Method::POST,
            "/api/cloud/credentials",
            Some("evil.com"),
            Some(json!({ "provider": "drive", "token": r#"{"access_token":"x"}"# })),
        )
        .await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(body["error"]["code"], "forbidden_origin");
    }
}
