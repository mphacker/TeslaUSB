use std::fmt;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use axum::extract::{DefaultBodyLimit, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use teslausb_creds::{
    BlobKeyMaterial, CLOUD_PROVIDER_CREDS_FILENAME, CredentialDocument, CredentialFlow,
    OAuthProvider, TESLA_SALT_FILENAME, decrypt, derive_key, encrypt, normalize_oauth_token,
    read_blob, read_or_create_salt, read_salt, render_rclone_conf, validate_document,
    with_creds_lock, write_blob_atomic,
};
#[cfg(not(test))]
use teslausb_creds::ProcHardwareRoot;
#[cfg(test)]
use teslausb_creds::StaticHardwareRoot;

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
            post(save_cloud_credentials)
                .layer(DefaultBodyLimit::max(CLOUD_CREDENTIALS_BODY_LIMIT)),
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
    let status = tokio::task::spawn_blocking(move || {
        let _guard = guard;
        persist_cloud_credentials(&creds_dir, &req)
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
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(not_configured_state()),
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
) -> Result<CloudCredentialsResp, ApiError> {
    with_creds_lock(creds_dir, || {
        let provider = parse_provider(&req.provider)?;
        let token =
            normalize_oauth_token(req.token.expose()).map_err(|err| normalize_token_error(&err))?;
        let document = CredentialDocument::new(CredentialFlow::OAuth { provider, token });
        let validated = validate_document(&document).map_err(|_| {
            ApiError::bad_request(
                "invalid_token",
                "token is not valid JSON from `rclone authorize`",
            )
        })?;
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
            Some(provider) => Ok::<CloudCredentialsResp, teslausb_creds::CredsError>(
                CloudCredentialsResp {
                    state: "configured",
                    provider: Some(provider.to_owned()),
                    updated_at: Some(updated_at),
                },
            ),
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
    use axum::Router;
    use axum::body::Body;
    use axum::http::header::{CONTENT_TYPE, HOST};
    use axum::http::{Method, Request};
    use rusqlite::Connection;
    use serde_json::{Value, json};
    use tempfile::TempDir;
    use tower::ServiceExt;

    use crate::{Catalog, MediaConfig};

    struct Fixture {
        _dir: TempDir,
        app: Router,
        creds_dir: PathBuf,
    }

    fn fixture() -> Fixture {
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

        let app = crate::router_with_cloud_creds_dir(
            Catalog::open(&db_path).unwrap(),
            static_dir,
            media,
            creds_dir.clone(),
        );
        Fixture {
            _dir: dir,
            app,
            creds_dir,
        }
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
