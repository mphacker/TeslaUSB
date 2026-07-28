use thiserror::Error;

/// Errors from credential encryption, persistence, and validation.
#[derive(Debug, Error)]
pub enum CredsError {
    /// Blob bytes are malformed.
    #[error("invalid credential blob: {0}")]
    InvalidBlob(&'static str),
    /// Unsupported blob version.
    #[error("unsupported credential blob version: {0}")]
    UnsupportedBlobVersion(u8),
    /// Unsupported KDF identifier.
    #[error("unsupported credential KDF id: {0}")]
    UnsupportedKdfId(u8),
    /// AEAD decrypt failed. Deliberately generic so wrong-machine and tamper do
    /// not leak distinguishable detail.
    #[error("credential decrypt failed")]
    DecryptFailed,
    /// CSPRNG source was unavailable.
    #[error("credential random source unavailable: {0}")]
    Random(String),
    /// I/O failure reading/writing credential files.
    #[error("credential I/O failed: {0}")]
    Io(#[from] std::io::Error),
    /// Serialized credential payload was invalid.
    #[error("credential payload parse failed: {0}")]
    Serde(#[from] serde_json::Error),
    /// `/proc/cpuinfo` does not contain a usable `Serial` field.
    #[error("cpu serial not found in /proc/cpuinfo")]
    CpuSerialMissing,
    /// `/proc/cpuinfo` `Serial` value was malformed.
    #[error("cpu serial is not lowercase hex ascii")]
    CpuSerialMalformed,
    /// `/etc/machine-id` was empty after trimming.
    #[error("machine-id is empty")]
    MachineIdEmpty,
    /// Iteration count must be non-zero.
    #[error("kdf iteration count must be non-zero")]
    InvalidKdfIterations,
    /// Salt bytes had an unexpected length.
    #[error("invalid salt length: expected 32 bytes, got {0}")]
    InvalidSaltLength(usize),
    /// Remote backend type is forbidden or unsupported.
    #[error("backend type is not allowed: {0}")]
    BackendTypeNotAllowed(String),
    /// A backend option key is explicitly forbidden.
    #[error("backend option key is forbidden: {0}")]
    ForbiddenOptionKey(String),
    /// A backend option key is not in the positive allow-list.
    #[error("backend option key is not allowed for type `{backend}`: {key}")]
    UnknownOptionKey {
        /// Normalized backend type.
        backend: String,
        /// Rejected option key.
        key: String,
    },
    /// A backend option value contains a control character.
    #[error("backend option value contains illegal control character: {key}")]
    IllegalValueChar {
        /// Option key whose value contained illegal control data.
        key: String,
    },
    /// A pasted config had more than one section.
    #[error("pasted rclone config must contain exactly one section")]
    MultipleRemoteSections,
    /// A pasted config had no section header.
    #[error("pasted rclone config is missing a section header")]
    MissingRemoteSection,
    /// A pasted config had no `type=` entry.
    #[error("pasted rclone config is missing required key `type`")]
    MissingRemoteType,
    /// A pasted config line was malformed.
    #[error("invalid rclone config line {line}")]
    InvalidConfigLine {
        /// 1-based line number in the pasted config.
        line: usize,
    },
    /// A pasted config repeated a key.
    #[error("duplicate rclone config key: {0}")]
    DuplicateConfigKey(String),
    /// Existing salt file has insecure permissions.
    #[error("salt file permissions are insecure: {mode:o} (expected 600)")]
    InsecureSaltPermissions {
        /// File mode bits (lowest 9 bits).
        mode: u32,
    },
    /// OAuth token text is empty after normalization.
    #[error("oauth token is empty")]
    EmptyOauthToken,
    /// OAuth token text is unexpectedly large.
    #[error("oauth token exceeds size limit")]
    OauthTokenTooLong,
    /// OAuth token must be a JSON object.
    #[error("oauth token must be a JSON object")]
    OauthTokenNotObject,
    /// OAuth token object is missing `access_token`.
    #[error("oauth token must contain non-empty `access_token`")]
    OauthTokenMissingAccessToken,
    /// Render target remote name is invalid.
    #[error("remote name must match [A-Za-z0-9_-]+")]
    InvalidRemoteName,
    /// Render target contains an invalid option key.
    #[error("rclone config key contains illegal characters: {0}")]
    IllegalRenderedKey(String),
    /// Render target contains an invalid value.
    #[error("rclone config value contains illegal characters: {key}")]
    IllegalRenderedValue {
        /// Key whose value was rejected.
        key: String,
    },
}
