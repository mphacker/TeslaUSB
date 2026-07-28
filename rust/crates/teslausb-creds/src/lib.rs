//! Hardware-bound credential storage and validation for `TeslaUSB` cloud sync.
//!
//! This crate implements the P2 contract from
//! `docs/specs/contracts/cloud-provider-creds.md`: the frozen AEAD blob framing,
//! PBKDF2 hardware-root key derivation, atomic on-disk persistence, and strict
//! backend/key validation that emits a sanitized in-memory [`ValidatedRemote`].

mod blob;
mod error;
mod hardware_root;
mod schema;
mod storage;
mod validate;

pub use blob::{
    BlobKeyMaterial, ParsedBlobHeader, decrypt, decrypt_with_parsed_header, encrypt,
    encrypt_with_nonce_for_test,
};
pub use error::CredsError;
pub use hardware_root::{DOMAIN_SEPARATOR, HardwareRoot, ProcHardwareRoot, StaticHardwareRoot, derive_key};
pub use schema::{
    CredentialDocument, CredentialFlow, CredentialValue, NasCredentials, OAuthProvider,
    S3StyleProvider,
};
pub use storage::{
    CLOUD_PROVIDER_CREDS_FILENAME, TESLA_SALT_FILENAME, read_blob, read_or_create_salt, read_salt,
    write_blob_atomic,
};
pub use validate::{
    ALLOWED_BACKEND_TYPES, ValidatedRemote, normalize_oauth_token, parse_single_remote_conf,
    render_rclone_conf, validate_document, validate_options_map,
};

/// Current credential schema version.
pub const CREDENTIAL_SCHEMA_VERSION: u8 = 1;
/// Frozen blob magic bytes.
pub const BLOB_MAGIC: &[u8; 8] = b"TUSBCRD1";
/// Frozen blob version.
pub const BLOB_VERSION: u8 = 1;
/// PBKDF2-SHA256 KDF id.
pub const KDF_ID_PBKDF2_SHA256: u8 = 1;
/// Default PBKDF2 iteration count.
pub const DEFAULT_KDF_ITERS: u32 = 600_000;
/// Credential salt length in bytes.
pub const SALT_LEN: usize = 32;
/// AES-GCM nonce length in bytes.
pub const NONCE_LEN: usize = 12;
/// AES-GCM tag length in bytes.
pub const TAG_LEN: usize = 16;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests;
