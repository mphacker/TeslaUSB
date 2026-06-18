use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use axum::Json;
use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use serde::{Deserialize, Serialize};

use crate::AppState;
use crate::error::ApiError;

const ZONEINFO_DIR: &str = "/usr/share/zoneinfo";
const TZIF_MAGIC: &[u8; 4] = b"TZif";

#[derive(Deserialize)]
struct TimezoneRequest {
    timezone: String,
}

#[derive(Serialize)]
struct TimezoneResponse {
    current: Option<String>,
    zones: Vec<String>,
}

#[derive(Serialize)]
struct TimezoneSetResponse {
    current: String,
}

pub(crate) fn routes() -> axum::Router<crate::AppState> {
    Router::new().route("/system/timezone", get(get_timezone).put(set_timezone))
}

async fn get_timezone(State(_): State<AppState>) -> Result<Json<TimezoneResponse>, ApiError> {
    // timedatectl spawns a subprocess and enumerate_zones walks ~600 files; keep
    // that blocking work off the async runtime.
    let dir = zoneinfo_dir();
    let response = tokio::task::spawn_blocking(move || {
        let zones = enumerate_zones(&dir);
        TimezoneResponse {
            current: TimedatectlSetter.current(),
            zones,
        }
    })
    .await
    .map_err(timezone_task_error)?;
    Ok(Json(response))
}

async fn set_timezone(
    State(_): State<AppState>,
    Json(body): Json<TimezoneRequest>,
) -> Result<Json<TimezoneSetResponse>, ApiError> {
    let requested = body.timezone;
    let dir = zoneinfo_dir();
    let current = tokio::task::spawn_blocking(move || {
        put_timezone_with(&TimedatectlSetter, &dir, &requested)
    })
    .await
    .map_err(timezone_task_error)??;
    Ok(Json(TimezoneSetResponse { current }))
}

fn zoneinfo_dir() -> PathBuf {
    std::env::var_os("WEBD_ZONEINFO_DIR")
        .map_or_else(|| PathBuf::from(ZONEINFO_DIR), PathBuf::from)
}

fn timezone_task_error(_: tokio::task::JoinError) -> ApiError {
    ApiError::status(
        StatusCode::INTERNAL_SERVER_ERROR,
        "timezone_task_failed",
        "timezone task failed",
    )
}

fn put_timezone_with(
    setter: &dyn TimezoneSetter,
    base: &Path,
    requested: &str,
) -> Result<String, ApiError> {
    let allowed = enumerate_zones(base);
    let zone = validate_zone(requested, &allowed).map_err(|msg| {
        ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_timezone", msg)
    })?;

    let old = setter.current();
    let _ = writeln!(
        std::io::stderr(),
        "timezone: change requested old={old:?} new={zone}"
    );

    match setter.set(&zone) {
        Ok(()) => {
            let _ = writeln!(std::io::stderr(), "timezone: change succeeded new={zone}");
            Ok(zone)
        }
        Err(msg) => {
            let _ = writeln!(
                std::io::stderr(),
                "timezone: change failed new={zone} err={msg}"
            );
            Err(ApiError::status(
                StatusCode::INTERNAL_SERVER_ERROR,
                "timezone_set_failed",
                msg,
            ))
        }
    }
}

pub(crate) trait TimezoneSetter: Send + Sync {
    fn set(&self, zone: &str) -> Result<(), String>;
    fn current(&self) -> Option<String>;
}

struct TimedatectlSetter;

impl TimezoneSetter for TimedatectlSetter {
    fn set(&self, zone: &str) -> Result<(), String> {
        let output = std::process::Command::new("timedatectl")
            .arg("set-timezone")
            .arg(zone)
            .output()
            .map_err(|_| "timedatectl not found".to_owned())?;

        if output.status.success() {
            return Ok(());
        }

        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        let detail = if stderr.is_empty() { stdout } else { stderr };
        Err(if detail.is_empty() {
            format!("timedatectl exited with status {}", output.status)
        } else {
            detail
        })
    }

    fn current(&self) -> Option<String> {
        let Ok(output) = std::process::Command::new("timedatectl")
            .arg("show")
            .arg("-p")
            .arg("Timezone")
            .arg("--value")
            .output()
        else {
            return current_from_localtime();
        };
        if !output.status.success() {
            return current_from_localtime();
        }
        let value = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        if value.is_empty() {
            current_from_localtime()
        } else {
            Some(value)
        }
    }
}

fn current_from_localtime() -> Option<String> {
    let link = fs::read_link("/etc/localtime").ok()?;
    let candidate = if link.is_absolute() {
        link
    } else {
        PathBuf::from("/etc").join(link)
    };
    let path = fs::canonicalize(candidate).ok()?;
    let zoneinfo = Path::new("/usr/share/zoneinfo");
    let rel = path.strip_prefix(zoneinfo).ok()?;
    Some(rel.to_string_lossy().replace('\\', "/").to_string())
}

fn enumerate_zones(base: &Path) -> Vec<String> {
    let mut zones = Vec::new();
    let mut stack = vec![base.to_path_buf()];

    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };

        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(meta) = fs::metadata(&path) else {
                continue;
            };
            if meta.is_dir() {
                stack.push(path);
                continue;
            }
            if !meta.is_file() || !has_tzif_magic(&path) {
                continue;
            }

            let Ok(rel) = path.strip_prefix(base) else {
                continue;
            };
            let rel_text = rel.to_string_lossy().replace('\\', "/");
            if rel_text.starts_with("posix/") || rel_text.starts_with("right/") {
                continue;
            }
            if rel_text.contains("..") {
                continue;
            }
            if is_excluded_name(&rel_text) {
                continue;
            }
            zones.push(rel_text.to_string());
        }
    }

    zones.sort();
    zones.dedup();
    zones
}

fn is_excluded_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    if let Some(ext) = Path::new(&lower).extension().and_then(|e| e.to_str()) {
        if matches!(ext, "tab" | "zi" | "list") {
            return true;
        }
    }
    matches!(
        lower.as_str(),
        "leapseconds"
            | "leap-seconds.list"
            | "tzdata.zi"
            | "iso3166.tab"
            | "zone.tab"
            | "zone1970.tab"
            | "posixrules"
    )
}

fn has_tzif_magic(path: &Path) -> bool {
    let Ok(mut file) = fs::File::open(path) else {
        return false;
    };
    let mut bytes = [0_u8; 4];
    file.read_exact(&mut bytes).is_ok() && bytes == *TZIF_MAGIC
}

fn validate_zone(requested: &str, allowed: &[String]) -> Result<String, &'static str> {
    if requested.is_empty() {
        return Err("timezone is required");
    }
    if requested.contains('\0') {
        return Err("timezone contains invalid bytes");
    }
    if requested.contains("..") || requested.starts_with('/') {
        return Err("timezone is invalid");
    }
    if !allowed.iter().any(|zone| zone == requested) {
        return Err("timezone is not allowed");
    }
    Ok(requested.to_owned())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::tempdir;

    use super::{enumerate_zones, validate_zone};

    #[test]
    fn enumerate_zones_filters_fake_and_special_files() {
        let dir = tempdir().expect("tempdir");
        fs::create_dir_all(dir.path().join("America")).expect("America");
        fs::create_dir_all(dir.path().join("Europe")).expect("Europe");
        fs::create_dir_all(dir.path().join("posix/America")).expect("posix");

        write_tzif(dir.path().join("America/New_York"));
        write_tzif(dir.path().join("Europe/Paris"));
        write_tzif(dir.path().join("posix/America/New_York"));
        write_tzif(dir.path().join("posixrules"));
        fs::write(dir.path().join("zone.tab"), b"x").expect("zone.tab");
        fs::write(dir.path().join("leapseconds"), b"x").expect("leapseconds");
        fs::write(dir.path().join("README"), b"x").expect("README");

        let zones = enumerate_zones(dir.path());

        assert_eq!(zones, vec!["America/New_York", "Europe/Paris"]);
    }

    #[test]
    fn validate_zone_accepts_and_rejects_expected_values() {
        let allowed = vec!["America/New_York".to_owned(), "Europe/Paris".to_owned()];

        assert_eq!(
            validate_zone("America/New_York", &allowed),
            Ok("America/New_York".to_owned())
        );
        assert_eq!(
            validate_zone("../etc/passwd", &allowed),
            Err("timezone is invalid")
        );
        assert_eq!(
            validate_zone("/etc/passwd", &allowed),
            Err("timezone is invalid")
        );
        assert_eq!(
            validate_zone("America/Bogus", &allowed),
            Err("timezone is not allowed")
        );
        assert_eq!(validate_zone("", &allowed), Err("timezone is required"));
        assert_eq!(
            validate_zone("A\0B", &allowed),
            Err("timezone contains invalid bytes")
        );
    }

    #[test]
    fn put_timezone_with_records_success_and_errors() {
        use super::{TimezoneSetter, put_timezone_with};
        use crate::error::ApiError;
        use axum::http::StatusCode;
        use std::sync::Mutex;

        #[derive(Default)]
        struct FakeSetter {
            calls: Mutex<Vec<String>>,
            current: Mutex<Option<String>>,
            fail: bool,
        }

        impl TimezoneSetter for FakeSetter {
            fn set(&self, zone: &str) -> Result<(), String> {
                self.calls.lock().unwrap().push(zone.to_owned());
                if self.fail {
                    return Err("boom".to_owned());
                }
                Ok(())
            }

            fn current(&self) -> Option<String> {
                self.current.lock().unwrap().clone()
            }
        }

        let dir = tempdir().expect("tempdir");
        write_tzif(dir.path().join("America/New_York"));
        let base = dir.path().to_path_buf();

        let fake = FakeSetter::default();
        let result = put_timezone_with(&fake, &base, "America/New_York");
        assert!(result.as_ref().is_ok_and(|zone| zone == "America/New_York"));
        assert_eq!(fake.calls.lock().unwrap().as_slice(), ["America/New_York"]);

        let fake = FakeSetter::default();
        let result = put_timezone_with(&fake, &base, "../etc/passwd");
        assert!(matches!(
            result,
            Err(ApiError::Status { status, code, .. })
                if status == StatusCode::UNPROCESSABLE_ENTITY && code == "invalid_timezone"
        ));
        assert!(fake.calls.lock().unwrap().is_empty());

        let fake = FakeSetter {
            fail: true,
            ..FakeSetter::default()
        };
        let result = put_timezone_with(&fake, &base, "America/New_York");
        assert!(matches!(
            result,
            Err(ApiError::Status { status, code, .. })
                if status == StatusCode::INTERNAL_SERVER_ERROR && code == "timezone_set_failed"
        ));
        assert_eq!(fake.calls.lock().unwrap().as_slice(), ["America/New_York"]);
    }

    fn write_tzif(path: std::path::PathBuf) {
        fs::create_dir_all(path.parent().expect("parent")).expect("parent");
        fs::write(path, b"TZif2\0\0\0\0").expect("tzif");
    }
}
