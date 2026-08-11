//! Read-only migration discovery for legacy TeslaUSB installations.

use std::fs;
use std::io::Read;
use std::path::Path;
use std::path::PathBuf;
use std::time::UNIX_EPOCH;

use rusqlite::Connection;
use rusqlite::OpenFlags;
use serde::Serialize;
use thiserror::Error;

const SCHEMA_VERSION: u8 = 1;
const DESTINATION_SCHEMA_VERSION: u8 = 1;
const MAX_ITEMS: usize = 64;
const MAX_BLOCKERS: usize = 32;
const MAX_CONFIG_KEYS: usize = 32;
const MAX_CONFIG_LINES: usize = 1024;
const MAX_NORMALIZED_SETTINGS: usize = 64;
const MAX_QUARANTINED_VALUES: usize = 128;
const MAX_SECTION_SETTINGS: usize = 64;
const MAX_VALUE_LEN: usize = 256;
const SQLITE_HEADER: &[u8; 16] = b"SQLite format 3\0";

const SUPPORTED_MAPPING_SCHEMA_VERSIONS: [i64; 8] = [0, 1, 2, 3, 4, 5, 6, 7];

const CANDIDATES: &[Candidate] = &[
    Candidate::new(ItemKind::Config, "config.yaml"),
    Candidate::new(ItemKind::MappingDatabase, "index.sqlite3"),
    Candidate::new(ItemKind::MappingDatabase, "mapping.db"),
    Candidate::new(ItemKind::MappingDatabase, "cloud_sync.db"),
    Candidate::new(ItemKind::MappingDatabase, "var/lib/teslausb/index.sqlite3"),
    Candidate::new(ItemKind::MappingDatabase, "var/lib/teslausb/cloud_sync.db"),
    Candidate::new(ItemKind::ArchiveRoot, "archive"),
    Candidate::new(ItemKind::MediaRoot, "media"),
    Candidate::new(ItemKind::ArchiveRoot, "TeslaCam/RecentClips"),
    Candidate::new(ItemKind::Credential, "secrets/rclone.conf"),
    Candidate::new(ItemKind::Credential, "credentials.json"),
    Candidate::new(ItemKind::Credential, "tokens.json"),
    Candidate::new(ItemKind::DiskImage, "disk.img"),
    Candidate::new(ItemKind::DiskImage, "teslacam.img"),
    Candidate::new(ItemKind::DiskImage, "media.img"),
    Candidate::new(
        ItemKind::ServiceState,
        "etc/systemd/system/teslausb.service",
    ),
    Candidate::new(
        ItemKind::ServiceState,
        "etc/systemd/system/teslausb-webd.service",
    ),
    Candidate::new(
        ItemKind::ServiceState,
        "etc/systemd/system/teslausb-uploader.service",
    ),
    Candidate::new(
        ItemKind::ServiceState,
        "etc/systemd/system/teslausb-wifi.service",
    ),
];

/// Discovery output schema.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DiscoveryReport {
    /// Versioned schema number.
    pub schema: u8,
    /// Redacted root marker; never emits a host path.
    pub legacy_root: String,
    /// True when at least one likely legacy artifact is present.
    pub installation_detected: bool,
    /// Bounded path inventory beneath the root.
    pub items: Vec<DiscoveryItem>,
    /// Explicit blockers discovered during read-only inspection.
    pub blockers: Vec<DiscoveryBlocker>,
    /// Bounded top-level config keys; values are never returned.
    pub config_keys: Vec<String>,
}

/// Read-only conversion plan. It contains section names and destinations, not
/// configuration values.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ConversionPlan {
    /// Versioned conversion-plan schema.
    pub schema: u8,
    /// Underlying read-only discovery report.
    pub source: DiscoveryReport,
    /// Supported section ownership mappings.
    pub mappings: Vec<ConversionMapping>,
    /// Top-level sections not yet supported.
    pub quarantined_sections: Vec<String>,
}

/// Mapping between a legacy top-level section and its B-1 owner.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ConversionMapping {
    /// Legacy config section name.
    pub source_section: String,
    /// B-1 destination owner identifier.
    pub destination_owner: String,
    /// Support status for this section mapping.
    pub status: &'static str,
}

/// Read-only dry-run conversion of supported non-secret scalar values.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DryRunConversionReport {
    /// Versioned dry-run report schema.
    pub schema: u8,
    /// Destination normalization schema version.
    pub destination_schema: u8,
    /// Underlying read-only discovery report.
    pub source: DiscoveryReport,
    /// Supported normalized scalar settings.
    pub normalized_settings: Vec<NormalizedSetting>,
    /// Unknown or unsafe values that were quarantined.
    pub quarantined_values: Vec<QuarantinedValue>,
    /// Dry-run specific bounded warnings.
    pub conversion_blockers: Vec<DiscoveryBlocker>,
}

/// One normalized setting mapped to a destination key.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct NormalizedSetting {
    /// Source section name.
    pub section: String,
    /// Source key name.
    pub key: String,
    /// Destination owner for this setting.
    pub destination_owner: String,
    /// Destination field/key name.
    pub destination_key: String,
    /// Normalized non-secret scalar value.
    pub value: NormalizedScalar,
}

/// One quarantined source value.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct QuarantinedValue {
    /// Source section name.
    pub section: String,
    /// Source key name.
    pub key: String,
    /// Why the value could not be normalized.
    pub reason: QuarantineReason,
}

/// Why a setting was quarantined during dry-run conversion.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum QuarantineReason {
    /// Section has no defined destination owner.
    UnknownSection,
    /// Key is not in the bounded supported-key list.
    UnsupportedKey,
    /// Value failed validation/range checks.
    InvalidValue,
    /// Key indicates secret material; value is omitted.
    SecretOmitted,
    /// Value is missing or appears non-scalar.
    NonScalarValue,
    /// Top-level scalar key is unsupported for migration.
    TopLevelScalarUnsupported,
}

/// Supported normalized scalar JSON value.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum NormalizedScalar {
    /// Signed integer scalar.
    Integer(i64),
    /// Boolean scalar.
    Bool(bool),
    /// Bounded plain-text scalar.
    Text(String),
}

/// Single discovered path record.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DiscoveryItem {
    /// Inventory kind.
    pub kind: ItemKind,
    /// Path relative to the supplied root.
    pub relative_path: String,
    /// Whether the candidate path exists.
    pub present: bool,
    /// File size when known.
    pub bytes: Option<u64>,
    /// Last-modified epoch seconds when known.
    pub modified_at: Option<i64>,
    /// Optional digest; omitted by default for read-only speed.
    pub sha256: Option<String>,
}

/// Explicit discovery blocker.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DiscoveryBlocker {
    /// Stable blocker code.
    pub code: String,
    /// Severity for the blocker.
    pub severity: BlockerSeverity,
    /// Human-readable diagnostic without secrets.
    pub message: String,
}

/// Discovery item kind.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ItemKind {
    /// config.yaml candidate.
    Config,
    /// Legacy mapping/catalog database candidate.
    MappingDatabase,
    /// Archive root candidate.
    ArchiveRoot,
    /// Media root candidate.
    MediaRoot,
    /// Credential/config candidate.
    Credential,
    /// Car-facing image candidate.
    DiskImage,
    /// Legacy service/systemd state candidate.
    ServiceState,
}

/// Blocker severity.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum BlockerSeverity {
    /// Migration cannot safely continue.
    Error,
    /// Report is bounded/truncated but still usable.
    Warning,
}

/// Discover command errors.
#[derive(Debug, Error)]
pub enum DiscoverError {
    /// Root path cannot be canonicalized.
    #[error("cannot read --root: {0}")]
    RootUnreadable(String),
}

#[derive(Debug, Clone, Copy)]
struct Candidate {
    kind: ItemKind,
    rel: &'static str,
}

impl Candidate {
    const fn new(kind: ItemKind, rel: &'static str) -> Self {
        Self { kind, rel }
    }
}

/// Read-only discovery beneath a canonicalized root.
pub fn discover(root: &Path) -> Result<DiscoveryReport, DiscoverError> {
    let root_canon = fs::canonicalize(root)
        .map_err(|e| DiscoverError::RootUnreadable(format!("{} ({e})", root.display())))?;
    let mut items = Vec::new();
    let mut blockers = Vec::new();
    let mut installation_detected = false;
    let config_keys = inspect_config_keys(root, &root_canon, &mut blockers);
    for candidate in CANDIDATES {
        if items.len() >= MAX_ITEMS {
            push_blocker(
                &mut blockers,
                "report_item_limit_reached",
                BlockerSeverity::Warning,
                "Item inventory limit reached; additional paths were skipped.",
            );
            break;
        }

        let mut item = inspect_candidate(root, &root_canon, candidate, &mut blockers);
        if item.present {
            installation_detected = true;
        }
        item.relative_path = normalize_rel_path(&item.relative_path);
        items.push(item);
    }
    Ok(DiscoveryReport {
        schema: SCHEMA_VERSION,
        legacy_root: "redacted".to_owned(),
        installation_detected,
        items,
        blockers,
        config_keys,
    })
}

/// Build a read-only section ownership plan from discovery results.
pub fn plan(root: &Path) -> Result<ConversionPlan, DiscoverError> {
    let source = discover(root)?;
    let mut mappings = Vec::new();
    let mut quarantined_sections = Vec::new();
    for section in &source.config_keys {
        if let Some(destination_owner) = destination_owner(section) {
            mappings.push(ConversionMapping {
                source_section: section.clone(),
                destination_owner: destination_owner.to_owned(),
                status: "supported",
            });
        } else {
            quarantined_sections.push(section.clone());
        }
    }
    Ok(ConversionPlan {
        schema: SCHEMA_VERSION,
        source,
        mappings,
        quarantined_sections,
    })
}

/// Build a read-only dry-run conversion report for supported non-secret scalar
/// config settings. No files are written.
pub fn convert(root: &Path) -> Result<DryRunConversionReport, DiscoverError> {
    let source = discover(root)?;
    let root_canon = fs::canonicalize(root)
        .map_err(|e| DiscoverError::RootUnreadable(format!("{} ({e})", root.display())))?;
    let mut normalized_settings = Vec::new();
    let mut quarantined_values = Vec::new();
    let mut conversion_blockers = Vec::new();
    for setting in inspect_config_scalar_settings(root, &root_canon, &mut conversion_blockers) {
        if normalized_settings.len() >= MAX_NORMALIZED_SETTINGS {
            push_blocker(
                &mut conversion_blockers,
                "normalized_setting_limit_reached",
                BlockerSeverity::Warning,
                "Normalized-setting limit reached; additional supported settings were skipped.",
            );
            break;
        }
        if quarantined_values.len() >= MAX_QUARANTINED_VALUES {
            push_blocker(
                &mut conversion_blockers,
                "quarantine_limit_reached",
                BlockerSeverity::Warning,
                "Quarantine limit reached; additional unsupported settings were omitted.",
            );
            break;
        }

        if setting.section == "_root" {
            quarantined_values.push(QuarantinedValue {
                section: "_root".to_owned(),
                key: setting.key,
                reason: QuarantineReason::TopLevelScalarUnsupported,
            });
            continue;
        }

        if is_secret_key(&setting.key) {
            quarantined_values.push(QuarantinedValue {
                section: setting.section,
                key: setting.key,
                reason: QuarantineReason::SecretOmitted,
            });
            continue;
        }

        if destination_owner(&setting.section).is_none() {
            quarantined_values.push(QuarantinedValue {
                section: setting.section,
                key: setting.key,
                reason: QuarantineReason::UnknownSection,
            });
            continue;
        }

        match normalize_supported_setting(&setting) {
            Ok(Some(normalized)) => normalized_settings.push(normalized),
            Ok(None) => quarantined_values.push(QuarantinedValue {
                section: setting.section,
                key: setting.key,
                reason: QuarantineReason::UnsupportedKey,
            }),
            Err(reason) => quarantined_values.push(QuarantinedValue {
                section: setting.section,
                key: setting.key,
                reason,
            }),
        }
    }

    normalized_settings.sort_by(|left, right| {
        (&left.section, &left.key, &left.destination_key).cmp(&(
            &right.section,
            &right.key,
            &right.destination_key,
        ))
    });
    quarantined_values.sort_by(|left, right| {
        (&left.section, &left.key, left.reason).cmp(&(&right.section, &right.key, right.reason))
    });
    Ok(DryRunConversionReport {
        schema: SCHEMA_VERSION,
        destination_schema: DESTINATION_SCHEMA_VERSION,
        source,
        normalized_settings,
        quarantined_values,
        conversion_blockers,
    })
}

fn destination_owner(section: &str) -> Option<&'static str> {
    match section {
        "installation" => Some("setup/system-unit-environment"),
        "disk_images" => Some("gadgetd/provisioning"),
        "setup" => Some("installer/dry-run"),
        "network" | "offline_ap" => Some("wifid/webd"),
        "system" => Some("installer/platform-policy"),
        "web" => Some("webd/spa"),
        "mapping" => Some("indexd"),
        "cloud_archive" => Some("indexd/uploadd/retentiond"),
        _ => None,
    }
}

#[derive(Debug, Clone)]
struct ParsedConfigSetting {
    section: String,
    key: String,
    raw_value: String,
}

fn inspect_config_scalar_settings(
    root_input: &Path,
    root_canon: &Path,
    blockers: &mut Vec<DiscoveryBlocker>,
) -> Vec<ParsedConfigSetting> {
    let path = root_input.join("config.yaml");
    let Ok(canonical) = fs::canonicalize(&path) else {
        return Vec::new();
    };
    if !canonical.starts_with(root_canon) {
        return Vec::new();
    }
    let Ok(contents) = fs::read_to_string(&path) else {
        return Vec::new();
    };

    let mut current_section: Option<String> = None;
    let mut settings = Vec::new();
    for (line_index, line) in contents.lines().enumerate() {
        if line_index >= MAX_CONFIG_LINES {
            push_blocker(
                blockers,
                "config_line_limit_reached",
                BlockerSeverity::Warning,
                "Config line limit reached; additional settings were skipped.",
            );
            break;
        }
        let trimmed = line.trim_end_matches('\r');
        let compact = trimmed.trim();
        if compact.is_empty() || compact.starts_with('#') {
            continue;
        }

        let indent = trimmed
            .chars()
            .take_while(|ch| *ch == ' ' || *ch == '\t')
            .count();
        if indent == 0 {
            let Some((raw_key, raw_value)) = compact.split_once(':') else {
                continue;
            };
            let key = raw_key.trim();
            if !is_valid_key_token(key) {
                current_section = None;
                continue;
            }
            let value = sanitize_scalar(raw_value);
            if value.is_empty() {
                current_section = Some(key.to_owned());
            } else {
                current_section = None;
                if settings.len() >= MAX_SECTION_SETTINGS {
                    push_blocker(
                        blockers,
                        "config_scalar_limit_reached",
                        BlockerSeverity::Warning,
                        "Config scalar limit reached; additional values were skipped.",
                    );
                    break;
                }
                settings.push(ParsedConfigSetting {
                    section: "_root".to_owned(),
                    key: key.to_owned(),
                    raw_value: value,
                });
            }
            continue;
        }

        let Some(section) = current_section.as_ref() else {
            continue;
        };
        let nested = compact;
        let Some((raw_key, raw_value)) = nested.split_once(':') else {
            continue;
        };
        let key = raw_key.trim();
        if !is_valid_key_token(key) {
            continue;
        }
        if settings.len() >= MAX_SECTION_SETTINGS {
            push_blocker(
                blockers,
                "config_scalar_limit_reached",
                BlockerSeverity::Warning,
                "Config scalar limit reached; additional values were skipped.",
            );
            break;
        }
        let value = sanitize_scalar(raw_value);
        if value.is_empty() {
            settings.push(ParsedConfigSetting {
                section: section.clone(),
                key: key.to_owned(),
                raw_value: String::new(),
            });
            continue;
        }
        settings.push(ParsedConfigSetting {
            section: section.clone(),
            key: key.to_owned(),
            raw_value: value,
        });
    }
    settings
}

fn is_valid_key_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-'))
}

fn sanitize_scalar(raw_value: &str) -> String {
    let mut value = raw_value.trim().to_owned();
    if value.is_empty() {
        return value;
    }
    if let Some(index) = value.find(" #") {
        value.truncate(index);
    }
    value = value.trim().to_owned();
    if let Some(unquoted) = unquote_scalar(&value) {
        return unquoted.trim().to_owned();
    }
    value
}

fn unquote_scalar(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    if bytes.len() < 2 {
        return None;
    }
    let first = bytes[0];
    let last = bytes[bytes.len() - 1];
    if (first == b'"' && last == b'"') || (first == b'\'' && last == b'\'') {
        return Some(value[1..value.len() - 1].to_owned());
    }
    None
}

fn normalize_supported_setting(
    setting: &ParsedConfigSetting,
) -> Result<Option<NormalizedSetting>, QuarantineReason> {
    if setting.raw_value.is_empty() {
        return Err(QuarantineReason::NonScalarValue);
    }
    if setting.raw_value.len() > MAX_VALUE_LEN {
        return Err(QuarantineReason::InvalidValue);
    }
    let section = setting.section.as_str();
    let key = setting.key.as_str();
    let owner = destination_owner(section).unwrap_or_default().to_owned();
    let value = setting.raw_value.as_str();
    let normalized = match (section, key) {
        ("installation", "user") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "system_user".to_owned(),
            value: NormalizedScalar::Text(normalize_user(value)?),
        },
        ("setup", "archive_size_gb") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "archive_size_gb".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 16, 8192)?),
        },
        ("network", "http_port") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "http_port".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 1, 65535)?),
        },
        ("offline_ap", "enabled") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "enabled".to_owned(),
            value: NormalizedScalar::Bool(normalize_bool(value)?),
        },
        ("offline_ap", "channel") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "channel".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 1, 165)?),
        },
        ("web", "port") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "port".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 1, 65535)?),
        },
        ("web", "session_timeout_minutes") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "session_timeout_minutes".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 1, 1440)?),
        },
        ("mapping", "sample_seconds") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "sample_seconds".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 1, 300)?),
        },
        ("mapping", "sentry_speed_mph") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "sentry_speed_mph".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 0, 120)?),
        },
        ("cloud_archive", "reserve_gb") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "reserve_gb".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 0, 4096)?),
        },
        ("cloud_archive", "max_retries") => NormalizedSetting {
            section: setting.section.clone(),
            key: setting.key.clone(),
            destination_owner: owner,
            destination_key: "max_retries".to_owned(),
            value: NormalizedScalar::Integer(normalize_integer(value, 0, 20)?),
        },
        _ => return Ok(None),
    };
    Ok(Some(normalized))
}

fn normalize_integer(value: &str, min: i64, max: i64) -> Result<i64, QuarantineReason> {
    let parsed = value
        .parse::<i64>()
        .map_err(|_| QuarantineReason::InvalidValue)?;
    if parsed < min || parsed > max {
        return Err(QuarantineReason::InvalidValue);
    }
    Ok(parsed)
}

fn normalize_bool(value: &str) -> Result<bool, QuarantineReason> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "1" | "on" => Ok(true),
        "false" | "no" | "0" | "off" => Ok(false),
        _ => Err(QuarantineReason::InvalidValue),
    }
}

fn normalize_user(value: &str) -> Result<String, QuarantineReason> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > 32 {
        return Err(QuarantineReason::InvalidValue);
    }
    let normalized = trimmed.to_ascii_lowercase();
    if normalized
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_'))
    {
        Ok(normalized)
    } else {
        Err(QuarantineReason::InvalidValue)
    }
}

fn is_secret_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    lower.contains("password")
        || lower.contains("token")
        || lower.contains("secret")
        || lower.contains("private_key")
        || lower == "apikey"
        || lower == "api_key"
        || lower.ends_with("_password")
        || lower.ends_with("_secret")
}

fn inspect_config_keys(
    root_input: &Path,
    root_canon: &Path,
    blockers: &mut Vec<DiscoveryBlocker>,
) -> Vec<String> {
    let path = root_input.join("config.yaml");
    let Ok(canonical) = fs::canonicalize(&path) else {
        return Vec::new();
    };
    if !canonical.starts_with(root_canon) {
        return Vec::new();
    }
    let Ok(contents) = fs::read_to_string(&path) else {
        return Vec::new();
    };
    let mut keys = Vec::new();
    for line in contents.lines().take(512) {
        if line.starts_with([' ', '\t']) || line.trim_start().starts_with('#') {
            continue;
        }
        let Some((key, _)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty()
            || key.len() > 64
            || !key.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '-')
            })
        {
            continue;
        }
        if !keys.iter().any(|existing| existing == key) {
            keys.push(key.to_owned());
        }
        if keys.len() == MAX_CONFIG_KEYS {
            push_blocker(
                blockers,
                "config_key_limit_reached",
                BlockerSeverity::Warning,
                "Config key inventory limit reached; additional keys were skipped.",
            );
            break;
        }
    }
    keys
}

fn inspect_candidate(
    root_input: &Path,
    root_canon: &Path,
    candidate: &Candidate,
    blockers: &mut Vec<DiscoveryBlocker>,
) -> DiscoveryItem {
    let joined = root_input.join(candidate.rel);
    let metadata = match fs::metadata(&joined) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return DiscoveryItem {
                kind: candidate.kind,
                relative_path: candidate.rel.to_owned(),
                present: false,
                bytes: None,
                modified_at: None,
                sha256: None,
            };
        }
        Err(error) => {
            push_blocker(
                blockers,
                "unreadable_file",
                BlockerSeverity::Error,
                &format!(
                    "Cannot access {}: {}.",
                    normalize_rel_path(candidate.rel),
                    sanitize_io_error(&error)
                ),
            );
            return DiscoveryItem {
                kind: candidate.kind,
                relative_path: candidate.rel.to_owned(),
                present: true,
                bytes: None,
                modified_at: None,
                sha256: None,
            };
        }
    };

    match fs::canonicalize(&joined) {
        Ok(path) => {
            if !path.starts_with(root_canon) {
                push_blocker(
                    blockers,
                    "path_outside_root",
                    BlockerSeverity::Error,
                    &format!(
                        "Resolved path for {} escapes the discovery root.",
                        normalize_rel_path(candidate.rel)
                    ),
                );
            }
        }
        Err(error) => {
            push_blocker(
                blockers,
                "unreadable_file",
                BlockerSeverity::Error,
                &format!(
                    "Cannot resolve {}: {}.",
                    normalize_rel_path(candidate.rel),
                    sanitize_io_error(&error)
                ),
            );
        }
    }

    if let Err(error) = check_readability(&joined, metadata.is_dir()) {
        push_blocker(
            blockers,
            "unreadable_file",
            BlockerSeverity::Error,
            &format!(
                "Cannot read {}: {}.",
                normalize_rel_path(candidate.rel),
                sanitize_io_error(&error)
            ),
        );
    } else if candidate.kind == ItemKind::MappingDatabase {
        inspect_mapping_database(&joined, candidate.rel, blockers);
    }

    DiscoveryItem {
        kind: candidate.kind,
        relative_path: candidate.rel.to_owned(),
        present: true,
        bytes: if metadata.is_file() {
            Some(metadata.len())
        } else {
            None
        },
        modified_at: metadata.modified().ok().and_then(to_epoch_seconds),
        sha256: None,
    }
}

fn check_readability(path: &Path, is_dir: bool) -> Result<(), std::io::Error> {
    if is_dir {
        let mut iter = fs::read_dir(path)?;
        let _ = iter.next();
        Ok(())
    } else {
        let _ = fs::File::open(path)?;
        Ok(())
    }
}

fn inspect_mapping_database(
    path: &Path,
    relative_path: &str,
    blockers: &mut Vec<DiscoveryBlocker>,
) {
    let mut header = [0_u8; 16];
    match fs::File::open(path).and_then(|mut file| file.read_exact(&mut header)) {
        Ok(()) => {}
        Err(error) => {
            if error.kind() == std::io::ErrorKind::UnexpectedEof {
                push_blocker(
                    blockers,
                    "unsupported_mapping_database",
                    BlockerSeverity::Error,
                    &format!(
                        "Mapping database at {} is not a supported SQLite database.",
                        normalize_rel_path(relative_path)
                    ),
                );
            } else {
                push_blocker(
                    blockers,
                    "unreadable_file",
                    BlockerSeverity::Error,
                    &format!(
                        "Cannot read {}: {}.",
                        normalize_rel_path(relative_path),
                        sanitize_io_error(&error)
                    ),
                );
            }
            return;
        }
    }

    if &header != SQLITE_HEADER {
        push_blocker(
            blockers,
            "unsupported_mapping_database",
            BlockerSeverity::Error,
            &format!(
                "Mapping database at {} is not a supported SQLite database.",
                normalize_rel_path(relative_path)
            ),
        );
        return;
    }

    let conn = match Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(conn) => conn,
        Err(_) => {
            push_blocker(
                blockers,
                "unknown_mapping_database",
                BlockerSeverity::Error,
                &format!(
                    "Mapping database at {} could not be opened read-only.",
                    normalize_rel_path(relative_path)
                ),
            );
            return;
        }
    };

    if !has_known_mapping_tables(&conn) {
        push_blocker(
            blockers,
            "unknown_mapping_database",
            BlockerSeverity::Error,
            &format!(
                "Mapping database at {} does not match recognized table markers.",
                normalize_rel_path(relative_path)
            ),
        );
        return;
    }

    let user_version = read_user_version(&conn);
    match user_version {
        None => push_blocker(
            blockers,
            "unknown_mapping_database",
            BlockerSeverity::Error,
            &format!(
                "Mapping database at {} has no readable schema marker.",
                normalize_rel_path(relative_path)
            ),
        ),
        Some(version) if !SUPPORTED_MAPPING_SCHEMA_VERSIONS.contains(&version) => push_blocker(
            blockers,
            "unsupported_mapping_schema",
            BlockerSeverity::Error,
            &format!(
                "Mapping database schema {} at {} is not supported.",
                version,
                normalize_rel_path(relative_path)
            ),
        ),
        Some(_) => {}
    }
}

fn has_known_mapping_tables(conn: &Connection) -> bool {
    let sql = "SELECT name FROM sqlite_master WHERE type='table'";
    let mut statement = match conn.prepare(sql) {
        Ok(statement) => statement,
        Err(_) => return false,
    };
    let rows = match statement.query_map([], |row| row.get::<_, String>(0)) {
        Ok(rows) => rows,
        Err(_) => return false,
    };

    rows.flatten().any(|name| {
        matches!(
            name.as_str(),
            "clips" | "trips" | "events" | "clip_waypoints" | "front_parse_attempts"
        )
    })
}

fn read_user_version(conn: &Connection) -> Option<i64> {
    conn.query_row("PRAGMA user_version;", [], |row| row.get::<_, i64>(0))
        .ok()
}

fn push_blocker(
    blockers: &mut Vec<DiscoveryBlocker>,
    code: &str,
    severity: BlockerSeverity,
    message: &str,
) {
    if blockers.len() >= MAX_BLOCKERS {
        let has_marker = blockers
            .iter()
            .any(|entry| entry.code == "report_blocker_limit_reached");
        if !has_marker {
            blockers.push(DiscoveryBlocker {
                code: "report_blocker_limit_reached".to_owned(),
                severity: BlockerSeverity::Warning,
                message: "Blocker list limit reached; additional blockers were omitted.".to_owned(),
            });
        }
        return;
    }
    blockers.push(DiscoveryBlocker {
        code: code.to_owned(),
        severity,
        message: message.to_owned(),
    });
}

fn sanitize_io_error(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => "permission denied",
        std::io::ErrorKind::NotFound => "not found",
        std::io::ErrorKind::InvalidData => "invalid data",
        _ => "I/O error",
    }
}

fn to_epoch_seconds(timestamp: std::time::SystemTime) -> Option<i64> {
    let secs = timestamp.duration_since(UNIX_EPOCH).ok()?.as_secs();
    i64::try_from(secs).ok()
}

fn normalize_rel_path(path: &str) -> String {
    path.replace('\\', "/")
}

/// Parse a supported command into a canonical root path for discovery.
pub fn parse_discover_root(args: &[String]) -> Result<PathBuf, String> {
    parse_command_root(args, "discover")
}

/// Parse the read-only conversion-plan command.
pub fn parse_plan_root(args: &[String]) -> Result<PathBuf, String> {
    parse_command_root(args, "plan")
}

/// Parse the read-only conversion dry-run command.
pub fn parse_convert_root(args: &[String]) -> Result<PathBuf, String> {
    parse_command_root(args, "convert")
}

fn parse_command_root(args: &[String], expected_command: &str) -> Result<PathBuf, String> {
    let Some(command) = args.first().map(String::as_str) else {
        return Err(usage());
    };
    if command != expected_command {
        return Err(format!("unknown command `{command}`\n{}", usage()));
    }
    let mut index = 1_usize;
    let mut root: Option<PathBuf> = None;
    while index < args.len() {
        match args[index].as_str() {
            "--root" => {
                let Some(value) = args.get(index + 1) else {
                    return Err("missing value for --root".to_owned());
                };
                root = Some(PathBuf::from(value));
                index += 2;
            }
            "--help" | "-h" | "help" => return Err(usage()),
            flag if flag.starts_with("--") => {
                return Err(format!("unknown flag `{flag}`\n{}", usage()));
            }
            unexpected => {
                return Err(format!("unexpected argument `{unexpected}`\n{}", usage()));
            }
        }
    }
    root.ok_or_else(|| format!("{expected_command} requires --root <path>"))
}

/// CLI usage text.
pub fn usage() -> String {
    "usage: teslausb-migrate <discover|plan|convert> --root <path>".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::io::Write;

    use tempfile::TempDir;

    #[test]
    fn discover_reports_items_without_absolute_paths() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(fixture.path().join("config.yaml"), b"mode: test\n")?;
        fs::create_dir_all(fixture.path().join("archive"))?;
        fs::write(fixture.path().join("disk.img"), b"not-an-image")?;

        let report = discover(fixture.path())?;
        let json = serde_json::to_string(&report)?;
        let fixture_path = fixture.path().to_string_lossy().into_owned();

        assert!(report.installation_detected);
        assert!(json.contains("\"legacy_root\":\"redacted\""));
        assert!(!json.contains(&fixture_path));
        assert!(
            report
                .items
                .iter()
                .any(|item| item.relative_path == "config.yaml" && item.present)
        );
        assert_eq!(report.config_keys, vec!["mode"]);
        Ok(())
    }

    #[test]
    fn discover_flags_non_sqlite_mapping_db() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(fixture.path().join("mapping.db"), b"plain-text")?;

        let report = discover(fixture.path())?;
        let has_blocker = report
            .blockers
            .iter()
            .any(|blocker| blocker.code == "unsupported_mapping_database");
        assert!(has_blocker);
        Ok(())
    }

    #[test]
    fn discover_includes_legacy_service_state() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        let service = fixture
            .path()
            .join("etc/systemd/system/teslausb-webd.service");
        fs::create_dir_all(service.parent().expect("service parent"))?;
        fs::write(&service, b"[Unit]\nDescription=legacy webd\n")?;

        let report = discover(fixture.path())?;
        assert!(report.items.iter().any(|item| {
            item.kind == ItemKind::ServiceState
                && item.relative_path == "etc/systemd/system/teslausb-webd.service"
                && item.present
        }));
        Ok(())
    }

    #[test]
    fn plan_maps_supported_sections_and_quarantines_unknowns()
    -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"mapping:\nunknown_section:\n",
        )?;

        let conversion = plan(fixture.path())?;
        assert_eq!(conversion.mappings[0].destination_owner, "indexd");
        assert_eq!(conversion.quarantined_sections, vec!["unknown_section"]);
        Ok(())
    }

    #[test]
    fn discover_flags_unsupported_mapping_schema() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        let path = fixture.path().join("index.sqlite3");
        create_sqlite_fixture(&path)?;

        let report = discover(fixture.path())?;
        let has_blocker = report
            .blockers
            .iter()
            .any(|blocker| blocker.code == "unsupported_mapping_schema");
        assert!(has_blocker);
        Ok(())
    }

    #[test]
    fn convert_normalizes_supported_non_secret_scalars() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"installation:\n  user: TesLa\nnetwork:\n  http_port: 8080\noffline_ap:\n  enabled: yes\nweb:\n  port: 8081\nmapping:\n  sample_seconds: 5\ncloud_archive:\n  reserve_gb: 12\n",
        )?;

        let report = convert(fixture.path())?;
        assert!(report.quarantined_values.is_empty());
        assert!(report.normalized_settings.iter().any(|setting| {
            setting.section == "installation"
                && setting.key == "user"
                && setting.destination_key == "system_user"
                && setting.value == NormalizedScalar::Text("tesla".to_owned())
        }));
        assert!(report.normalized_settings.iter().any(|setting| {
            setting.section == "network"
                && setting.key == "http_port"
                && setting.value == NormalizedScalar::Integer(8080)
        }));
        assert!(report.normalized_settings.iter().any(|setting| {
            setting.section == "offline_ap"
                && setting.key == "enabled"
                && setting.value == NormalizedScalar::Bool(true)
        }));
        Ok(())
    }

    #[test]
    fn convert_quarantines_invalid_scalar_values() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"network:\n  http_port: 70000\nmapping:\n  sample_seconds: fast\n",
        )?;

        let report = convert(fixture.path())?;
        assert!(report.normalized_settings.is_empty());
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "network"
                && entry.key == "http_port"
                && entry.reason == QuarantineReason::InvalidValue
        }));
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "mapping"
                && entry.key == "sample_seconds"
                && entry.reason == QuarantineReason::InvalidValue
        }));
        Ok(())
    }

    #[test]
    fn convert_omits_secret_values_from_report() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"network:\n  wifi_password: super-secret-value\n",
        )?;

        let report = convert(fixture.path())?;
        let json = serde_json::to_string(&report)?;
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "network"
                && entry.key == "wifi_password"
                && entry.reason == QuarantineReason::SecretOmitted
        }));
        assert!(!json.contains("super-secret-value"));
        Ok(())
    }

    #[test]
    fn convert_quarantines_unknown_and_unsafe_entries() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"mystery:\n  custom: 1\nweb:\n  unsupported_toggle: true\nmapping:\n  sentry_speed_mph:\nmode: legacy\n",
        )?;

        let report = convert(fixture.path())?;
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "mystery"
                && entry.key == "custom"
                && entry.reason == QuarantineReason::UnknownSection
        }));
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "web"
                && entry.key == "unsupported_toggle"
                && entry.reason == QuarantineReason::UnsupportedKey
        }));
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "mapping"
                && entry.key == "sentry_speed_mph"
                && entry.reason == QuarantineReason::NonScalarValue
        }));
        assert!(report.quarantined_values.iter().any(|entry| {
            entry.section == "_root"
                && entry.key == "mode"
                && entry.reason == QuarantineReason::TopLevelScalarUnsupported
        }));
        Ok(())
    }

    #[test]
    fn convert_is_repeatable_for_unchanged_inputs() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        fs::write(
            fixture.path().join("config.yaml"),
            b"web:\n  port: 8080\nmapping:\n  sample_seconds: 4\n",
        )?;

        let first = convert(fixture.path())?;
        let second = convert(fixture.path())?;
        assert_eq!(first, second);
        Ok(())
    }

    fn create_sqlite_fixture(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
        let conn = Connection::open(path)?;
        conn.execute_batch(
            "PRAGMA user_version = 999;
             CREATE TABLE clips (id INTEGER PRIMARY KEY);
             CREATE TABLE trips (id INTEGER PRIMARY KEY);",
        )?;
        drop(conn);

        let mut file = File::options().read(true).write(true).open(path)?;
        let mut header = [0_u8; 16];
        file.read_exact(&mut header)?;
        if &header != SQLITE_HEADER {
            return Err("sqlite header mismatch".into());
        }
        file.flush()?;
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn discover_reports_path_escape_blocker() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = TempDir::new()?;
        let path = fixture.path().join("credentials.json");
        let outside = TempDir::new()?;
        std::os::unix::fs::symlink(outside.path(), &path)?;

        let report = discover(fixture.path())?;
        let has_escape = report
            .blockers
            .iter()
            .any(|blocker| blocker.code == "path_outside_root");
        assert!(has_escape);
        Ok(())
    }
}
