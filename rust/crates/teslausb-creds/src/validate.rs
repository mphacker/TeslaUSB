use std::collections::BTreeMap;

use serde_json::Value;

use crate::error::CredsError;
use crate::schema::{
    CredentialDocument, CredentialFlow, CredentialValue, NasCredentials, OAuthProvider,
    S3StyleProvider,
};

/// Allowed remote types.
pub const ALLOWED_BACKEND_TYPES: &[&str] = &[
    "sftp",
    "webdav",
    "smb",
    "ftp",
    "s3",
    "b2",
    "wasabi",
    "azureblob",
    "swift",
    "drive",
    "onedrive",
    "dropbox",
];

const REJECTED_BACKEND_TYPES: &[&str] = &[
    "crypt", "union", "chunker", "local", "http", "alias", "cache",
];

const SFTP_ALLOWED_KEYS: &[&str] = &["host", "user", "port", "pass"];
const WEBDAV_ALLOWED_KEYS: &[&str] = &["url", "vendor", "user", "pass", "bearer_token"];
const SMB_ALLOWED_KEYS: &[&str] = &["host", "user", "pass", "domain", "port"];
const FTP_ALLOWED_KEYS: &[&str] = &["host", "user", "pass", "port", "tls", "explicit_tls"];
const S3_ALLOWED_KEYS: &[&str] = &[
    "provider",
    "access_key_id",
    "secret_access_key",
    "region",
    "endpoint",
];
const B2_ALLOWED_KEYS: &[&str] = &["account", "key", "endpoint"];
const AZUREBLOB_ALLOWED_KEYS: &[&str] = &["account", "key", "endpoint", "sas_url"];
const SWIFT_ALLOWED_KEYS: &[&str] = &["user", "key", "auth", "tenant", "region"];
const DRIVE_ALLOWED_KEYS: &[&str] = &["token"];
const ONEDRIVE_ALLOWED_KEYS: &[&str] = &["token", "drive_id", "drive_type"];
const DROPBOX_ALLOWED_KEYS: &[&str] = &["token"];

/// Sanitized single-remote output used by P3 render logic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedRemote {
    /// Normalized backend type (`wasabi` is normalized to `s3`).
    pub backend_type: String,
    /// Positive-allow-listed backend options.
    pub options: BTreeMap<String, String>,
}

/// Parsed single-remote `rclone.conf` content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedRemoteConfig {
    /// Backend type from `type=...`.
    pub backend_type: String,
    /// Parsed option keys/values (excluding `type`).
    pub options: BTreeMap<String, String>,
}

/// Normalize pasted `rclone authorize` output to a compact JSON token string.
///
/// The returned JSON is serialized by `serde_json::to_string`, which escapes
/// control characters in string values. This guarantees the token cannot contain
/// literal newlines or raw `[`/`]` section markers that could inject extra INI
/// sections when later rendered into an `rclone.conf` entry.
///
/// # Errors
///
/// Returns [`CredsError`] when the token is empty, too large, not a JSON object,
/// or missing a non-empty `access_token`.
pub fn normalize_oauth_token(raw: &str) -> Result<String, CredsError> {
    let mut text = raw.trim();
    if let Some((_, rest)) = text.rsplit_once("--->") {
        text = rest;
    }
    if let Some((before, _)) = text.split_once("<---") {
        text = before;
    }
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err(CredsError::EmptyOauthToken);
    }
    if trimmed.len() > 8_192 {
        return Err(CredsError::OauthTokenTooLong);
    }
    let value = serde_json::from_str::<Value>(trimmed)?;
    let Some(object) = value.as_object() else {
        return Err(CredsError::OauthTokenNotObject);
    };
    let has_access_token = object
        .get("access_token")
        .and_then(Value::as_str)
        .is_some_and(|token| !token.is_empty());
    if !has_access_token {
        return Err(CredsError::OauthTokenMissingAccessToken);
    }
    Ok(serde_json::to_string(&value)?)
}

/// Render a single `rclone.conf` remote section from a validated remote.
///
/// # Errors
///
/// Returns [`CredsError`] if `remote_name` is invalid, or if any rendered key or
/// value contains illegal bytes for INI emission.
pub fn render_rclone_conf(
    remote_name: &str,
    remote: &ValidatedRemote,
) -> Result<String, CredsError> {
    if !is_valid_remote_name(remote_name) {
        return Err(CredsError::InvalidRemoteName);
    }
    if has_illegal_render_bytes(&remote.backend_type) {
        return Err(CredsError::IllegalRenderedValue {
            key: "type".to_owned(),
        });
    }
    let mut out = String::new();
    out.push('[');
    out.push_str(remote_name);
    out.push_str("]\n");
    out.push_str("type = ");
    out.push_str(&remote.backend_type);
    out.push('\n');
    for (key, value) in &remote.options {
        if !is_valid_render_key(key) {
            return Err(CredsError::IllegalRenderedKey(key.clone()));
        }
        if has_illegal_render_bytes(value) {
            return Err(CredsError::IllegalRenderedValue { key: key.clone() });
        }
        out.push_str(key);
        out.push_str(" = ");
        out.push_str(value);
        out.push('\n');
    }
    Ok(out)
}

/// Validate a credential document and emit a sanitized in-memory remote.
///
/// # Errors
///
/// Returns [`CredsError`] when type/key allow-lists reject any value.
pub fn validate_document(document: &CredentialDocument) -> Result<ValidatedRemote, CredsError> {
    match &document.flow {
        CredentialFlow::OAuth {
            provider,
            token,
            options,
        } => {
            let backend_type = match provider {
                OAuthProvider::Drive => "drive",
                OAuthProvider::Onedrive => "onedrive",
                OAuthProvider::Dropbox => "dropbox",
            };
            let mut rendered_options = BTreeMap::new();
            rendered_options.insert("token".to_owned(), token.clone());
            for (key, value) in options {
                if key.trim().eq_ignore_ascii_case("token") {
                    return Err(CredsError::ReservedOauthOptionKey);
                }
                rendered_options.insert(key.clone(), value.clone());
            }
            validate_options_map(backend_type, &rendered_options)
        }
        CredentialFlow::S3Style {
            provider,
            access_key,
            secret,
            region,
            endpoint,
        } => {
            let mut options = BTreeMap::new();
            match provider {
                S3StyleProvider::S3 => {
                    options.insert("provider".to_owned(), "AWS".to_owned());
                    options.insert("access_key_id".to_owned(), access_key.clone());
                    options.insert("secret_access_key".to_owned(), secret.clone());
                    if let Some(region) = normalize_non_empty(region.as_deref()) {
                        options.insert("region".to_owned(), region);
                    }
                    if let Some(endpoint) = normalize_non_empty(endpoint.as_deref()) {
                        options.insert("endpoint".to_owned(), endpoint);
                    }
                    validate_options_map("s3", &options)
                }
                S3StyleProvider::Wasabi => {
                    options.insert("provider".to_owned(), "Wasabi".to_owned());
                    options.insert("access_key_id".to_owned(), access_key.clone());
                    options.insert("secret_access_key".to_owned(), secret.clone());
                    if let Some(region) = normalize_non_empty(region.as_deref()) {
                        options.insert("region".to_owned(), region);
                    }
                    if let Some(endpoint) = normalize_non_empty(endpoint.as_deref()) {
                        options.insert("endpoint".to_owned(), endpoint);
                    }
                    validate_options_map("wasabi", &options)
                }
                S3StyleProvider::B2 => {
                    options.insert("account".to_owned(), access_key.clone());
                    options.insert("key".to_owned(), secret.clone());
                    if let Some(endpoint) = normalize_non_empty(endpoint.as_deref()) {
                        options.insert("endpoint".to_owned(), endpoint);
                    }
                    validate_options_map("b2", &options)
                }
            }
        }
        CredentialFlow::NasCustom { creds } => match creds {
            NasCredentials::Typed {
                backend_type,
                options,
            } => {
                let rendered_options = options
                    .iter()
                    .map(|(k, v)| (k.clone(), normalize_trimmed(v)))
                    .collect::<BTreeMap<_, _>>();
                validate_options_map(backend_type, &rendered_options)
            }
            NasCredentials::PastedRcloneConf { rclone_conf } => {
                let parsed = parse_single_remote_conf(rclone_conf)?;
                validate_options_map(&parsed.backend_type, &parsed.options)
            }
        },
    }
}

/// Validate backend type + options against the positive allow-list.
///
/// # Errors
///
/// Returns [`CredsError`] on forbidden/unknown type or key.
pub fn validate_options_map(
    backend_type: &str,
    options: &BTreeMap<String, String>,
) -> Result<ValidatedRemote, CredsError> {
    let normalized_type = normalize_backend_type(backend_type)?;
    let allowed_keys = allowed_keys_for(&normalized_type)?;

    let mut sanitized = BTreeMap::new();
    for (key, raw_value) in options {
        let normalized_key = key.trim().to_ascii_lowercase();
        if normalized_key.is_empty() {
            return Err(CredsError::ForbiddenOptionKey(key.clone()));
        }
        if is_forbidden_key(&normalized_key) {
            return Err(CredsError::ForbiddenOptionKey(normalized_key));
        }
        if !allowed_keys.iter().any(|item| *item == normalized_key) {
            return Err(CredsError::UnknownOptionKey {
                backend: normalized_type.clone(),
                key: normalized_key,
            });
        }
        let sanitized_value = sanitize_option_value(&normalized_key, raw_value)?;
        sanitized.insert(normalized_key, sanitized_value);
    }

    if backend_type.trim().eq_ignore_ascii_case("wasabi") {
        sanitized.insert("provider".to_owned(), "Wasabi".to_owned());
    }

    Ok(ValidatedRemote {
        backend_type: normalized_type,
        options: sanitized,
    })
}

/// Parse a pasted single-remote `rclone.conf`.
///
/// # Errors
///
/// Returns [`CredsError`] if the config has more than one section or malformed
/// key/value lines.
pub fn parse_single_remote_conf(conf: &str) -> Result<ParsedRemoteConfig, CredsError> {
    let mut section_name: Option<String> = None;
    let mut options = BTreeMap::new();

    for (line_index, line_text) in conf.lines().enumerate() {
        let line = line_index + 1;
        let trimmed = line_text.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with(';') {
            continue;
        }
        if trimmed.starts_with('[') && trimmed.ends_with(']') {
            let current_name = trimmed
                .strip_prefix('[')
                .and_then(|x| x.strip_suffix(']'))
                .map(str::trim)
                .ok_or(CredsError::InvalidConfigLine { line })?;
            if current_name.is_empty() {
                return Err(CredsError::InvalidConfigLine { line });
            }
            if section_name.is_some() {
                return Err(CredsError::MultipleRemoteSections);
            }
            section_name = Some(current_name.to_owned());
            continue;
        }
        if section_name.is_none() {
            return Err(CredsError::MissingRemoteSection);
        }
        let (key, value) = trimmed
            .split_once('=')
            .ok_or(CredsError::InvalidConfigLine { line })?;
        let normalized_key = key.trim().to_ascii_lowercase();
        if normalized_key.is_empty() {
            return Err(CredsError::InvalidConfigLine { line });
        }
        if options
            .insert(normalized_key.clone(), value.trim().to_owned())
            .is_some()
        {
            return Err(CredsError::DuplicateConfigKey(normalized_key));
        }
    }

    if section_name.is_none() {
        return Err(CredsError::MissingRemoteSection);
    }
    let backend_type = options
        .remove("type")
        .ok_or(CredsError::MissingRemoteType)?;
    Ok(ParsedRemoteConfig {
        backend_type,
        options,
    })
}

fn normalize_backend_type(raw: &str) -> Result<String, CredsError> {
    let normalized = raw.trim().to_ascii_lowercase();
    if REJECTED_BACKEND_TYPES
        .iter()
        .any(|blocked| *blocked == normalized)
    {
        return Err(CredsError::BackendTypeNotAllowed(normalized));
    }
    if !ALLOWED_BACKEND_TYPES
        .iter()
        .any(|allowed| *allowed == normalized)
    {
        return Err(CredsError::BackendTypeNotAllowed(normalized));
    }
    if normalized == "wasabi" {
        return Ok("s3".to_owned());
    }
    Ok(normalized)
}

fn allowed_keys_for(backend_type: &str) -> Result<&'static [&'static str], CredsError> {
    match backend_type {
        "sftp" => Ok(SFTP_ALLOWED_KEYS),
        "webdav" => Ok(WEBDAV_ALLOWED_KEYS),
        "smb" => Ok(SMB_ALLOWED_KEYS),
        "ftp" => Ok(FTP_ALLOWED_KEYS),
        "s3" => Ok(S3_ALLOWED_KEYS),
        "b2" => Ok(B2_ALLOWED_KEYS),
        "azureblob" => Ok(AZUREBLOB_ALLOWED_KEYS),
        "swift" => Ok(SWIFT_ALLOWED_KEYS),
        "drive" => Ok(DRIVE_ALLOWED_KEYS),
        "onedrive" => Ok(ONEDRIVE_ALLOWED_KEYS),
        "dropbox" => Ok(DROPBOX_ALLOWED_KEYS),
        other => Err(CredsError::BackendTypeNotAllowed(other.to_owned())),
    }
}

fn is_forbidden_key(key: &str) -> bool {
    key == "command"
        || key.ends_with("_command")
        || key.ends_with("_helper")
        || key == "env_auth"
        || key == "headers"
        || key.contains("header")
        || key.starts_with("--")
        || key.starts_with("rc")
        || key == "unix_socket"
        || key.ends_with("_socket")
        || key == "file"
        || key.ends_with("_file")
        || key.ends_with("_path")
}

fn normalize_trimmed(value: &CredentialValue) -> String {
    value.as_string().trim().to_owned()
}

fn sanitize_option_value(key: &str, raw_value: &str) -> Result<String, CredsError> {
    let value = raw_value.trim().to_owned();
    if value.bytes().any(|byte| byte <= 0x1f || byte == 0x7f) {
        return Err(CredsError::IllegalValueChar {
            key: key.to_owned(),
        });
    }
    if key == "drive_type"
        && value != "personal"
        && value != "business"
        && value != "documentLibrary"
    {
        return Err(CredsError::InvalidOnedriveDriveType);
    }
    if key == "drive_id" {
        if value.is_empty() {
            return Err(CredsError::EmptyOnedriveDriveId);
        }
        if value.len() > 256 {
            return Err(CredsError::OnedriveDriveIdTooLong);
        }
        if value
            .bytes()
            .any(|byte| !(0x21..=0x7e).contains(&byte) || byte == b'[' || byte == b']')
        {
            return Err(CredsError::IllegalOnedriveDriveId);
        }
    }
    Ok(value)
}

fn normalize_non_empty(value: Option<&str>) -> Option<String> {
    value
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
}

fn is_valid_remote_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn is_valid_render_key(key: &str) -> bool {
    !key.is_empty()
        && key
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn has_illegal_render_bytes(value: &str) -> bool {
    value
        .bytes()
        .any(|byte| byte <= 0x1f || byte == 0x7f || byte == b'[' || byte == b']')
}
