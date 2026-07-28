use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::{CREDENTIAL_SCHEMA_VERSION, CredsError};

/// Versioned credential document encrypted into `cloud_provider_creds.bin`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CredentialDocument {
    /// Schema version. Current value is [`CREDENTIAL_SCHEMA_VERSION`].
    pub version: u8,
    /// Credential flow payload.
    #[serde(flatten)]
    pub flow: CredentialFlow,
}

impl CredentialDocument {
    /// Build a v1 credential document.
    #[must_use]
    pub fn new(flow: CredentialFlow) -> Self {
        Self {
            version: CREDENTIAL_SCHEMA_VERSION,
            flow,
        }
    }

    /// Serialize using canonical, stable JSON (`serde_json::to_vec` over
    /// deterministic field + map ordering).
    ///
    /// # Errors
    ///
    /// Returns [`CredsError`] if serialization fails.
    pub fn to_canonical_bytes(&self) -> Result<Vec<u8>, CredsError> {
        Ok(serde_json::to_vec(self)?)
    }

    /// Parse a serialized credential document.
    ///
    /// # Errors
    ///
    /// Returns [`CredsError`] if JSON is invalid.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, CredsError> {
        Ok(serde_json::from_slice(bytes)?)
    }
}

/// Supported credential flows.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "flow", rename_all = "snake_case")]
pub enum CredentialFlow {
    /// OAuth providers (Drive, `OneDrive`, Dropbox) with a token blob.
    OAuth {
        /// OAuth-backed remote provider.
        provider: OAuthProvider,
        /// Opaque token JSON/string from `rclone authorize`.
        token: String,
        /// Provider-specific rclone options required to build a working remote
        /// (`OneDrive` needs `drive_id` + `drive_type`). Empty for providers
        /// needing none.
        #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
        options: BTreeMap<String, String>,
    },
    /// S3-style key/secret credentials (S3, B2, Wasabi).
    S3Style {
        /// S3-style provider.
        provider: S3StyleProvider,
        /// Access key/account id.
        access_key: String,
        /// Secret key.
        secret: String,
        /// Optional region.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        region: Option<String>,
        /// Optional endpoint.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        endpoint: Option<String>,
    },
    /// NAS/custom backend credentials.
    NasCustom {
        /// Typed form or pasted single-remote config.
        #[serde(flatten)]
        creds: NasCredentials,
    },
}

/// OAuth providers accepted by this schema.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OAuthProvider {
    /// Google Drive.
    Drive,
    /// Microsoft `OneDrive`.
    Onedrive,
    /// Dropbox.
    Dropbox,
}

/// S3-style providers accepted by this schema.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum S3StyleProvider {
    /// Standard `s3` backend.
    S3,
    /// Backblaze `b2` backend.
    B2,
    /// Wasabi (normalized to `s3 + provider=Wasabi`).
    Wasabi,
}

/// NAS/custom input forms.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "input", rename_all = "snake_case")]
pub enum NasCredentials {
    /// Typed backend + typed option map.
    Typed {
        /// Backend type.
        backend_type: String,
        /// Backend options from the form.
        options: BTreeMap<String, CredentialValue>,
    },
    /// Single remote `rclone.conf` text pasted by the operator.
    PastedRcloneConf {
        /// Raw config content.
        rclone_conf: String,
    },
}

/// Typed backend option value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CredentialValue {
    /// String value.
    String(String),
    /// Boolean value.
    Bool(bool),
    /// Integer value.
    Int(i64),
}

impl CredentialValue {
    /// Render this value to the string form expected by rclone config keys.
    #[must_use]
    pub fn as_string(&self) -> String {
        match self {
            Self::String(value) => value.clone(),
            Self::Bool(value) => value.to_string(),
            Self::Int(value) => value.to_string(),
        }
    }
}
