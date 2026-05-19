//! Config file loader. YAML on disk; deserialised into `Config`.

use std::path::{Path, PathBuf};

use anyhow::Result;
use serde::Deserialize;

/// Top-level config. Keep field names stable — `setup.sh` writes
/// `/etc/teslausb/teslafat.yaml` against this schema.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Root of the real Linux directory tree backing the
    /// synthesised FAT32 volume. Tesla writes land here as native
    /// files; the indexer/cloud-sync read from here directly.
    pub backing_root: PathBuf,

    /// Total size (in GiB) of the synthesised volume reported to
    /// Tesla. Should match what Tesla expects to see; larger gives
    /// Tesla more headroom but doesn't extend RecentClips
    /// retention (which is time-based).
    pub volume_size_gb: u32,

    /// Volume label shown in Tesla's UI / file managers.
    #[serde(default = "default_label")]
    pub volume_label: String,

    /// Cluster size override, in bytes. If None, auto-computed
    /// from volume_size_gb to match `mkfs.vfat` defaults.
    #[serde(default)]
    pub cluster_size: Option<u32>,

    /// Retention policy for the synthesised view.
    #[serde(default)]
    pub retention: RetentionConfig,

    /// IPC settings.
    #[serde(default)]
    pub ipc: IpcConfig,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetentionConfig {
    /// Hide files in TeslaCam/RecentClips/ older than this many
    /// seconds from Tesla's view. The files stay on disk
    /// indefinitely — a separate cleanup policy in the web UI
    /// decides when to delete them.
    pub recentclips_hide_after_seconds: u64,
}

impl Default for RetentionConfig {
    fn default() -> Self {
        Self {
            recentclips_hide_after_seconds: 3600,
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IpcConfig {
    /// Optional unix-permissions (octal) on the IPC socket. The
    /// web app runs as root, so default 0o600 is fine.
    #[serde(default)]
    pub socket_mode: Option<u32>,
}

fn default_label() -> String {
    "TESLACAM".to_string()
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path)?;
        let cfg: Config = serde_yaml::from_str(&raw)?;
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            self.volume_size_gb >= 4 && self.volume_size_gb <= 2048,
            "volume_size_gb must be 4–2048 (got {})",
            self.volume_size_gb
        );
        if let Some(c) = self.cluster_size {
            anyhow::ensure!(
                c.is_power_of_two() && (512..=131072).contains(&c),
                "cluster_size must be a power of two in [512, 131072] (got {})",
                c
            );
        }
        anyhow::ensure!(
            self.volume_label.len() <= 11,
            "volume_label must be ≤ 11 chars (FAT32 limit)"
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_yaml() {
        let yaml = r#"
backing_root: /var/teslacam
volume_size_gb: 64
"#;
        let cfg: Config = serde_yaml::from_str(yaml).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.volume_size_gb, 64);
        assert_eq!(cfg.volume_label, "TESLACAM");
        assert_eq!(cfg.retention.recentclips_hide_after_seconds, 3600);
    }

    #[test]
    fn rejects_oversize_label() {
        let cfg = Config {
            backing_root: PathBuf::from("/x"),
            volume_size_gb: 64,
            volume_label: "TOOLONGLABEL_AAA".to_string(),
            cluster_size: None,
            retention: Default::default(),
            ipc: Default::default(),
        };
        assert!(cfg.validate().is_err());
    }
}
