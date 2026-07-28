use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::blob::BlobKeyMaterial;
use crate::error::CredsError;
use crate::hardware_root::{StaticHardwareRoot, derive_key, parse_cpuinfo_serial};
use crate::schema::{CredentialDocument, CredentialFlow, CredentialValue, NasCredentials, OAuthProvider};
// Storage-permission tests are unix-only; the crate itself compiles on Windows so
// webd (which depends on it) can be built by the SPA UAT harness there.
#[cfg(unix)]
use crate::storage::{CLOUD_PROVIDER_CREDS_FILENAME, TESLA_SALT_FILENAME, file_mode};
use crate::validate::{
    normalize_oauth_token, parse_single_remote_conf, render_rclone_conf, validate_document,
    validate_options_map,
};
use crate::{DEFAULT_KDF_ITERS, decrypt, encrypt, encrypt_with_nonce_for_test};
#[cfg(unix)]
use crate::{read_blob, read_or_create_salt, write_blob_atomic};
use zeroize::Zeroizing;

const TEST_SALT: [u8; 32] = [
    0x32, 0x8b, 0x18, 0x74, 0x7a, 0x2f, 0x98, 0x44, 0xb2, 0x9d, 0x33, 0x2d, 0xb0, 0x11, 0x9a,
    0xc7, 0x6d, 0x9b, 0xe5, 0x03, 0x61, 0x0e, 0x2f, 0x87, 0xb9, 0xae, 0x44, 0x3c, 0xd8, 0x10,
    0xfe, 0x09,
];
const TEST_NONCE: [u8; 12] = [
    0xb4, 0x4f, 0x0c, 0x96, 0x72, 0x27, 0xaa, 0x55, 0x3e, 0x19, 0x44, 0x8f,
];
const KAT_BLOB_HEX: &str = "54555342435244310101000927c0328b18747a2f9844b29d332db0119ac76d9be503610e2f87b9ae443cd810fe09b44f0c967227aa553e19448f03b3afeba9c3696a7c56c48255d65600ed7c4faadb1150792ac805ab7c6e5f1c62f3c19013220192e06e453a997265f4f43b68de2855548f3260f221637d676c40a083449bd814980508bb68fe0144689ef331b05573aec99cf0b9896ad6b725f80bb2b62d8c298fa3f67e2b507a67a668a38cc4275cb1ce227f982026e1cdf1fd88da14c034b96e45";

#[test]
fn aead_round_trip() {
    let root = StaticHardwareRoot::new("00000000deadbeef", "f0f1f2f3f4f5f6f7f8f9aaaabbbbcccc");
    let key = derive_key(&root, &TEST_SALT, DEFAULT_KDF_ITERS).unwrap();
    let blob_key = BlobKeyMaterial {
        key,
        salt: TEST_SALT,
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let plaintext = b"teslausb cloud creds payload";
    let blob = encrypt(plaintext, &blob_key).unwrap();
    let decrypted = decrypt(&blob, &blob_key).unwrap();
    assert_eq!(decrypted.as_slice(), plaintext);
}

#[test]
fn wrong_machine_decrypt_fails_closed() {
    let root_a = StaticHardwareRoot::new("00000000deadbeef", "machine-a-0123456789abcdef");
    let root_b = StaticHardwareRoot::new("00000000feedface", "machine-a-0123456789abcdef");
    let key_a = derive_key(&root_a, &TEST_SALT, DEFAULT_KDF_ITERS).unwrap();
    let key_b = derive_key(&root_b, &TEST_SALT, DEFAULT_KDF_ITERS).unwrap();
    let blob_key_a = BlobKeyMaterial {
        key: key_a,
        salt: TEST_SALT,
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let blob_key_b = BlobKeyMaterial {
        key: key_b,
        salt: TEST_SALT,
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let blob = encrypt_with_nonce_for_test(b"secret", &blob_key_a, TEST_NONCE).unwrap();
    let err = decrypt(&blob, &blob_key_b).unwrap_err();
    assert!(matches!(err, CredsError::DecryptFailed));
}

#[test]
fn kat_vector_matches_committed_blob_hex() {
    let root = StaticHardwareRoot::new(
        "00000000cafebabe",
        "1234567890abcdef1234567890abcdef",
    );
    let key = derive_key(&root, &TEST_SALT, DEFAULT_KDF_ITERS).unwrap();
    let blob_key = BlobKeyMaterial {
        key,
        salt: TEST_SALT,
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let doc = CredentialDocument::new(CredentialFlow::OAuth {
        provider: crate::schema::OAuthProvider::Drive,
        token: "{\"access_token\":\"abc\",\"expiry\":\"2026-01-01T00:00:00Z\"}".to_owned(),
    });
    let plaintext = doc.to_canonical_bytes().unwrap();
    let blob = encrypt_with_nonce_for_test(&plaintext, &blob_key, TEST_NONCE).unwrap();
    let blob_hex = hex_encode(&blob);
    assert_eq!(blob_hex, KAT_BLOB_HEX);
}

#[test]
fn aad_tamper_and_kdf_downgrade_are_rejected() {
    let root = StaticHardwareRoot::new("00000000cafebabe", "machine-id-0011223344556677");
    let key = derive_key(&root, &TEST_SALT, DEFAULT_KDF_ITERS).unwrap();
    let blob_key = BlobKeyMaterial {
        key,
        salt: TEST_SALT,
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let blob = encrypt_with_nonce_for_test(b"payload", &blob_key, TEST_NONCE).unwrap();

    let mut tampered_header = blob.clone();
    let tampered_byte = tampered_header.get_mut(20).unwrap();
    *tampered_byte ^= 0x40;
    let tamper_err = decrypt(&tampered_header, &blob_key).unwrap_err();
    assert!(matches!(tamper_err, CredsError::DecryptFailed));

    let mut downgraded_iters = blob;
    let current = u32::from_be_bytes(downgraded_iters.get(10..14).unwrap().try_into().unwrap());
    let lowered = current.saturating_sub(1).max(1);
    let lowered_bytes = lowered.to_be_bytes();
    downgraded_iters
        .get_mut(10..14)
        .unwrap()
        .copy_from_slice(&lowered_bytes);
    let downgrade_err = decrypt(&downgraded_iters, &blob_key).unwrap_err();
    assert!(matches!(downgrade_err, CredsError::DecryptFailed));
}

#[test]
fn type_allow_list_rejects_crypt_union_local_http() {
    let empty = BTreeMap::<String, String>::new();
    for rejected in ["crypt", "union", "local", "http"] {
        let err = validate_options_map(rejected, &empty).unwrap_err();
        assert!(matches!(err, CredsError::BackendTypeNotAllowed(_)));
    }
}

#[test]
fn per_key_allow_list_rejects_banned_keys_and_multisection_paste() {
    let mut webdav = BTreeMap::new();
    webdav.insert("url".to_owned(), "https://dav.example".to_owned());
    webdav.insert("bearer_token_command".to_owned(), "cat /etc/token".to_owned());
    let err = validate_options_map("webdav", &webdav).unwrap_err();
    assert!(matches!(err, CredsError::ForbiddenOptionKey(_)));

    let mut s3_command = BTreeMap::new();
    s3_command.insert("provider".to_owned(), "AWS".to_owned());
    s3_command.insert("access_key_id".to_owned(), "AKIA".to_owned());
    s3_command.insert("secret_access_key".to_owned(), "SECRET".to_owned());
    s3_command.insert("session_command".to_owned(), "do-eval".to_owned());
    let command_err = validate_options_map("s3", &s3_command).unwrap_err();
    assert!(matches!(command_err, CredsError::ForbiddenOptionKey(_)));

    let mut s3_env = BTreeMap::new();
    s3_env.insert("provider".to_owned(), "AWS".to_owned());
    s3_env.insert("access_key_id".to_owned(), "AKIA".to_owned());
    s3_env.insert("secret_access_key".to_owned(), "SECRET".to_owned());
    s3_env.insert("env_auth".to_owned(), "true".to_owned());
    let env_err = validate_options_map("s3", &s3_env).unwrap_err();
    assert!(matches!(env_err, CredsError::ForbiddenOptionKey(_)));

    let config = "[one]\ntype = s3\nprovider = AWS\naccess_key_id = a\nsecret_access_key = b\n[two]\ntype = s3\n";
    let parse_err = parse_single_remote_conf(config).unwrap_err();
    assert!(matches!(parse_err, CredsError::MultipleRemoteSections));
}

#[test]
fn wasabi_normalizes_to_s3_with_provider() {
    let mut options = BTreeMap::new();
    options.insert("access_key_id".to_owned(), "AKIA".to_owned());
    options.insert("secret_access_key".to_owned(), "SECRET".to_owned());
    let validated = validate_options_map("wasabi", &options).unwrap();
    assert_eq!(validated.backend_type, "s3");
    assert_eq!(
        validated.options.get("provider").map(String::as_str),
        Some("Wasabi")
    );
}

#[test]
fn pasted_config_unknown_key_rejects_whole_remote() {
    let conf = "[teslausb]\ntype = webdav\nurl = https://dav.example\nuser = alice\npass = secret\nheaders = X-A:1\n";
    let doc = CredentialDocument::new(CredentialFlow::NasCustom {
        creds: NasCredentials::PastedRcloneConf {
            rclone_conf: conf.to_owned(),
        },
    });
    let err = validate_document(&doc).unwrap_err();
    assert!(matches!(err, CredsError::ForbiddenOptionKey(_)));
}

#[test]
fn option_values_reject_control_char_injection() {
    let mut typed_options = BTreeMap::new();
    typed_options.insert(
        "url".to_owned(),
        CredentialValue::String(
            "https://dav.example\nbearer_token_command = sh -c 'echo pwned'".to_owned(),
        ),
    );
    let typed_doc = CredentialDocument::new(CredentialFlow::NasCustom {
        creds: NasCredentials::Typed {
            backend_type: "webdav".to_owned(),
            options: typed_options,
        },
    });
    let typed_err = validate_document(&typed_doc).unwrap_err();
    assert!(matches!(
        typed_err,
        CredsError::IllegalValueChar { ref key } if key == "url"
    ));

    let oauth_doc = CredentialDocument::new(CredentialFlow::OAuth {
        provider: crate::schema::OAuthProvider::Drive,
        token: "{\"access_token\":\"abc\"}\nbearer_token_command = sh -c 'echo pwned'".to_owned(),
    });
    let oauth_err = validate_document(&oauth_doc).unwrap_err();
    assert!(matches!(
        oauth_err,
        CredsError::IllegalValueChar { ref key } if key == "token"
    ));

    let s3_secret_doc = CredentialDocument::new(CredentialFlow::S3Style {
        provider: crate::schema::S3StyleProvider::S3,
        access_key: "AKIA".to_owned(),
        secret: "SECRET\nsession_command = sh -c 'echo pwned'".to_owned(),
        region: None,
        endpoint: None,
    });
    let s3_secret_err = validate_document(&s3_secret_doc).unwrap_err();
    assert!(matches!(
        s3_secret_err,
        CredsError::IllegalValueChar { ref key } if key == "secret_access_key"
    ));

    let s3_endpoint_doc = CredentialDocument::new(CredentialFlow::S3Style {
        provider: crate::schema::S3StyleProvider::S3,
        access_key: "AKIA".to_owned(),
        secret: "SECRET".to_owned(),
        region: None,
        endpoint: Some("https://s3.example\nsession_command = sh -c 'echo pwned'".to_owned()),
    });
    let s3_endpoint_err = validate_document(&s3_endpoint_doc).unwrap_err();
    assert!(matches!(
        s3_endpoint_err,
        CredsError::IllegalValueChar { ref key } if key == "endpoint"
    ));
}

#[test]
fn normalize_oauth_token_strips_markers_and_compacts_json() {
    let wrapped = r#"
Paste the following into your remote machine --->
{"access_token":"ya29.token","token_type":"Bearer","refresh_token":"refresh-token","expiry":"2026-01-01T00:00:00Z"}
<---End paste
"#;
    let normalized = normalize_oauth_token(wrapped).unwrap();
    assert_eq!(
        normalized,
        normalize_oauth_token(
            r#"{"access_token":"ya29.token","token_type":"Bearer","refresh_token":"refresh-token","expiry":"2026-01-01T00:00:00Z"}"#
        )
        .unwrap()
    );
}

#[test]
fn normalize_oauth_token_round_trips_bare_json() {
    let bare =
        r#"{"access_token":"abc","token_type":"Bearer","refresh_token":"def","expiry":"2026-01-01T00:00:00Z"}"#;
    let expected = serde_json::to_string(&serde_json::from_str::<serde_json::Value>(bare).unwrap())
        .unwrap();
    assert_eq!(normalize_oauth_token(bare).unwrap(), expected);
}

#[test]
fn normalize_oauth_token_rejects_missing_access_token_and_non_objects() {
    let missing_access = r#"{"token_type":"Bearer"}"#;
    let err = normalize_oauth_token(missing_access).unwrap_err();
    assert!(matches!(err, CredsError::OauthTokenMissingAccessToken));

    let array_err = normalize_oauth_token("[1,2,3]").unwrap_err();
    assert!(matches!(array_err, CredsError::OauthTokenNotObject));

    let string_err = normalize_oauth_token("\"hello\"").unwrap_err();
    assert!(matches!(string_err, CredsError::OauthTokenNotObject));
}

#[test]
fn normalize_oauth_token_rejects_empty_and_oversized_input() {
    let empty_err = normalize_oauth_token(" \n\t ").unwrap_err();
    assert!(matches!(empty_err, CredsError::EmptyOauthToken));

    let oversized = format!(r#"{{"access_token":"{}"}}"#, "x".repeat(9_000));
    let oversize_err = normalize_oauth_token(&oversized).unwrap_err();
    assert!(matches!(oversize_err, CredsError::OauthTokenTooLong));
}

#[test]
fn oauth_token_normalization_and_rendering_block_ini_injection() {
    let injected_multiline = "{\"access_token\":\"x\"}\n[evil]\ntype = local\n";
    let injected_in_string =
        "{\"access_token\":\"x\n[evil]\ntype = local\n\",\"token_type\":\"Bearer\"}";

    let multiline_err = normalize_oauth_token(injected_multiline).unwrap_err();
    assert!(matches!(multiline_err, CredsError::Serde(_)));
    let string_err = normalize_oauth_token(injected_in_string).unwrap_err();
    assert!(matches!(string_err, CredsError::Serde(_)));

    let accepted_tokens = [
        r#"{"access_token":"x","token_type":"Bearer","refresh_token":"r"}"#,
        r#"{"access_token":"x","token_type":"Bearer","scope":"Files.ReadWrite"}"#,
        "Paste the following into your remote machine --->\n{\"access_token\":\"x\",\"token_type\":\"Bearer\"}\n<---End paste",
    ];

    for raw in accepted_tokens {
        let token = normalize_oauth_token(raw).unwrap();
        let document = CredentialDocument::new(CredentialFlow::OAuth {
            provider: OAuthProvider::Onedrive,
            token,
        });
        let validated = validate_document(&document).unwrap();
        let rendered = render_rclone_conf("teslausb", &validated).unwrap();
        let parsed = parse_single_remote_conf(&rendered).unwrap();
        let reparsed = validate_options_map(&parsed.backend_type, &parsed.options).unwrap();
        assert_eq!(reparsed, validated);
    }
}

#[test]
fn multiline_valid_json_token_is_compacted_before_rendering() {
    // JSON allows raw newlines *between* tokens, so a pasted token can be valid
    // JSON and still span several lines (pretty-printed, or copied out of a
    // file). Emitted verbatim those newlines would start new config lines.
    // Compact re-serialization in `normalize_oauth_token` is what prevents it.
    let pretty = "{\n  \"access_token\": \"x\",\n  \"token_type\": \"Bearer\"\n}";

    let token = normalize_oauth_token(pretty).unwrap();
    assert!(!token.contains('\n'), "normalized token still spans lines");

    let document = CredentialDocument::new(CredentialFlow::OAuth {
        provider: OAuthProvider::Onedrive,
        token,
    });
    let validated = validate_document(&document).unwrap();
    let rendered = render_rclone_conf("teslausb", &validated).unwrap();

    let section_lines = rendered
        .lines()
        .filter(|line| line.starts_with('['))
        .count();
    assert_eq!(section_lines, 1, "rendered config has {section_lines} sections");

    let parsed = parse_single_remote_conf(&rendered).unwrap();
    let reparsed = validate_options_map(&parsed.backend_type, &parsed.options).unwrap();
    assert_eq!(reparsed, validated);
}

#[test]
fn token_containing_brackets_is_refused_at_render_time() {
    // A JSON array inside the token would put `[...]` into the config, which
    // reads back as a second remote section. The allow-list lets it through
    // (brackets are not control characters), so the renderer is the layer that
    // refuses it — it fails closed rather than emitting the line. Real rclone
    // OAuth tokens are flat string maps, so this rejects nothing legitimate.
    let token = normalize_oauth_token("{\"access_token\":\"x\",\"a\":\n[\"evil\"]}").unwrap();
    let document = CredentialDocument::new(CredentialFlow::OAuth {
        provider: OAuthProvider::Onedrive,
        token,
    });

    let validated = validate_document(&document).unwrap();
    let err = render_rclone_conf("teslausb", &validated).unwrap_err();
    assert!(matches!(err, CredsError::IllegalRenderedValue { .. }));
}

#[test]
fn render_rclone_conf_rejects_invalid_remote_name() {
    let validated = validate_options_map(
        "onedrive",
        &BTreeMap::from([(
            "token".to_owned(),
            normalize_oauth_token(r#"{"access_token":"abc"}"#).unwrap(),
        )]),
    )
    .unwrap();
    let err = render_rclone_conf("teslausb bad", &validated).unwrap_err();
    assert!(matches!(err, CredsError::InvalidRemoteName));
}

#[test]
fn option_values_trim_and_accept_safe_values() {
    let mut options = BTreeMap::new();
    options.insert("url".to_owned(), "https://dav.example".to_owned());
    let validated = validate_options_map("webdav", &options).unwrap();
    assert_eq!(
        validated.options.get("url").map(String::as_str),
        Some("https://dav.example")
    );

    let mut trailing = BTreeMap::new();
    trailing.insert("url".to_owned(), "https://dav.example \n".to_owned());
    let trailing_validated = validate_options_map("webdav", &trailing).unwrap();
    assert_eq!(
        trailing_validated.options.get("url").map(String::as_str),
        Some("https://dav.example")
    );
}

#[test]
fn typed_map_is_canonical_and_stable() {
    let mut options = BTreeMap::new();
    options.insert("port".to_owned(), CredentialValue::Int(22));
    options.insert("host".to_owned(), CredentialValue::String("nas.local".to_owned()));
    options.insert("tls".to_owned(), CredentialValue::Bool(false));
    let doc = CredentialDocument::new(CredentialFlow::NasCustom {
        creds: NasCredentials::Typed {
            backend_type: "sftp".to_owned(),
            options,
        },
    });
    let bytes1 = doc.to_canonical_bytes().unwrap();
    let bytes2 = doc.to_canonical_bytes().unwrap();
    assert_eq!(bytes1, bytes2);
}

#[cfg(unix)]
#[test]
fn salt_create_once_and_blob_atomic_io() {
    let base = test_data_path("storage");
    std::fs::create_dir_all(&base).unwrap();
    let salt_path = base.join(TESLA_SALT_FILENAME);
    let blob_path = base.join(CLOUD_PROVIDER_CREDS_FILENAME);

    let salt1 = read_or_create_salt(&salt_path).unwrap();
    let salt2 = read_or_create_salt(&salt_path).unwrap();
    assert_eq!(salt1, salt2);
    assert_eq!(file_mode(&salt_path).unwrap(), 0o600);

    write_blob_atomic(&blob_path, b"blob-1").unwrap();
    assert_eq!(read_blob(&blob_path).unwrap(), b"blob-1");
    assert_eq!(file_mode(&blob_path).unwrap(), 0o600);

    let _ = std::fs::remove_file(blob_path);
    let _ = std::fs::remove_file(salt_path);
    let _ = std::fs::remove_dir(base);
}

#[cfg(unix)]
#[test]
fn read_salt_rejects_insecure_permissions() {
    use std::os::unix::fs::PermissionsExt;

    let base = test_data_path("salt-perms");
    std::fs::create_dir_all(&base).unwrap();
    let salt_path = base.join(TESLA_SALT_FILENAME);
    let salt = read_or_create_salt(&salt_path).unwrap();
    std::fs::write(&salt_path, salt).unwrap();

    let insecure = std::fs::Permissions::from_mode(0o644);
    std::fs::set_permissions(&salt_path, insecure).unwrap();
    let insecure_err = read_or_create_salt(&salt_path).unwrap_err();
    assert!(matches!(
        insecure_err,
        CredsError::InsecureSaltPermissions { mode } if mode == 0o644
    ));

    let secure = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(&salt_path, secure).unwrap();
    let secure_read = read_or_create_salt(&salt_path).unwrap();
    assert_eq!(secure_read, salt);

    let _ = std::fs::remove_file(salt_path);
    let _ = std::fs::remove_dir(base);
}

#[test]
fn cpuinfo_serial_parser_uses_full_lowercase_hex() {
    let cpuinfo = "Processor\t: ARMv7 Processor rev 5\nSerial\t\t: 00000000ABCDEF12\n";
    let serial = parse_cpuinfo_serial(cpuinfo).unwrap();
    assert_eq!(serial, "00000000abcdef12");
}

#[test]
fn invalid_config_line_error_redacts_secret_content() {
    let secret = "super-secret-token";
    let conf = format!("[teslausb]\ntype = webdav\nurl = https://dav.example\nbad {secret}\n");
    let err = parse_single_remote_conf(&conf).unwrap_err();
    assert!(matches!(err, CredsError::InvalidConfigLine { line: 4 }));
    let rendered = err.to_string();
    let debug_rendered = format!("{err:?}");
    assert!(!rendered.contains(secret));
    assert!(!debug_rendered.contains(secret));
}

#[test]
fn blob_key_material_debug_redacts_key() {
    let blob_key = BlobKeyMaterial {
        key: Zeroizing::new([
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
            24, 25, 26, 27, 28, 29, 30, 31, 32,
        ]),
        salt: [0_u8; 32],
        kdf_iters: DEFAULT_KDF_ITERS,
    };
    let rendered = format!("{blob_key:?}");
    assert!(rendered.contains("redacted"));
    assert!(!rendered.contains("1, 2, 3, 4"));
}

fn test_data_path(tag: &str) -> PathBuf {
    let mut path = std::env::var_os("CARGO_TARGET_DIR").map_or_else(
        || {
            let mut fallback = std::env::current_dir().unwrap();
            fallback.push("target");
            fallback
        },
        PathBuf::from,
    );
    path.push("teslausb-creds-tests");
    path.push(format!(
        "{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    path
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(&mut out, "{byte:02x}");
    }
    out
}
