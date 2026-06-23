//! Unix-socket RPC server for `retentiond → indexd` archive registration.

use std::collections::HashSet;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use rusqlite::Connection;

use crate::db::ingest::{
    ArchiveAngleRegistration, ArchiveRegistration, ArchiveUnitRegistration, register_archived_clip,
    register_quarantined_clip,
};
use crate::model::FolderClass;
use crate::proto::{RegisterArchivedClip, Request, Response, read_request, write_response};

/// Start the indexd registration server thread.
///
/// # Errors
///
/// Returns an error if socket setup/bind fails.
pub fn spawn(
    conn: &Arc<Mutex<Connection>>,
    socket_path: &Path,
    io_timeout: Duration,
) -> io::Result<thread::JoinHandle<()>> {
    let listener = bind_listener(socket_path)?;
    let conn = Arc::clone(conn);
    thread::Builder::new()
        .name("indexd-rpc".to_owned())
        .spawn(move || serve(&listener, &conn, io_timeout))
}

fn bind_listener(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    Ok(listener)
}

fn serve(listener: &UnixListener, conn: &Arc<Mutex<Connection>>, io_timeout: Duration) {
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let _ = handle_connection(stream, conn, io_timeout);
            }
            Err(_) => continue,
        }
    }
}

fn handle_connection(
    mut stream: UnixStream,
    conn: &Arc<Mutex<Connection>>,
    io_timeout: Duration,
) -> io::Result<()> {
    stream.set_read_timeout(Some(io_timeout))?;
    stream.set_write_timeout(Some(io_timeout))?;

    let response = match read_request(&mut stream) {
        Ok(request) => match request {
            Request::RegisterArchivedClip(payload) => {
                match handle_register_archived_clip(conn, &payload) {
                    Ok((clip_id, archive_item_id)) => Response::Ok {
                        clip_id,
                        archive_item_id,
                    },
                    Err(message) => Response::Error { message },
                }
            }
            Request::RegisterQuarantinedArchive(payload) => {
                match handle_register_quarantined_clip(conn, &payload) {
                    Ok((clip_id, archive_item_id)) => Response::Ok {
                        clip_id,
                        archive_item_id,
                    },
                    Err(message) => Response::Error { message },
                }
            }
            Request::SetPref { key, value } => match handle_set_pref(conn, &key, &value) {
                Ok(()) => Response::PrefSet { key },
                Err(message) => Response::Error { message },
            },
        },
        Err(e) => Response::Error {
            message: format!("invalid request: {e}"),
        },
    };
    let _ = write_response(&mut stream, &response);
    Ok(())
}

fn handle_register_archived_clip(
    conn: &Arc<Mutex<Connection>>,
    payload: &RegisterArchivedClip,
) -> Result<(i64, i64), String> {
    let registration = build_registration(payload)?;
    let mut locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    register_archived_clip(&mut locked, &registration).map_err(|e| e.to_string())
}

fn handle_register_quarantined_clip(
    conn: &Arc<Mutex<Connection>>,
    payload: &RegisterArchivedClip,
) -> Result<(i64, i64), String> {
    let registration = build_registration(payload)?;
    let mut locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    register_quarantined_clip(&mut locked, &registration).map_err(|e| e.to_string())
}

fn handle_set_pref(conn: &Arc<Mutex<Connection>>, key: &str, value: &str) -> Result<(), String> {
    let locked = conn
        .lock()
        .map_err(|_| "index database mutex is poisoned".to_owned())?;
    crate::db::mutations::set_pref(&locked, key, value).map_err(|e| e.to_string())
}

fn build_registration(payload: &RegisterArchivedClip) -> Result<ArchiveRegistration, String> {
    validate_payload(payload)?;
    let folder_class = parse_folder_class(&payload.folder_class)?;
    Ok(ArchiveRegistration {
        canonical_key: payload.canonical_key.clone(),
        folder_class,
        partition: payload.partition.clone(),
        started_at: payload.started_at,
        ended_at: payload.ended_at,
        duration_s: seconds_opt_to_f64(payload.duration_s, "duration_s")?,
        archive: ArchiveUnitRegistration {
            path: payload.archive.path.clone(),
            size_bytes: payload.archive.size_bytes,
            file_count: payload.archive.file_count,
            archived_at: payload.archive.archived_at,
        },
        angles: payload
            .angles
            .iter()
            .map(|a| {
                Ok(ArchiveAngleRegistration {
                    camera: a.camera.clone(),
                    file_ref: a.file_ref.clone(),
                    offset_ms: a.offset_ms,
                    duration_s: seconds_opt_to_f64(a.duration_s, "angles.duration_s")?,
                    size_bytes: a.size_bytes,
                })
            })
            .collect::<Result<Vec<_>, String>>()?,
    })
}

fn parse_folder_class(raw: &str) -> Result<FolderClass, String> {
    match raw {
        "RecentClips" => Ok(FolderClass::RecentClips),
        "SavedClips" => Ok(FolderClass::SavedClips),
        "SentryClips" => Ok(FolderClass::SentryClips),
        "TeslaTrackMode" => Ok(FolderClass::TeslaTrackMode),
        other => Err(format!("invalid folder_class: {other}")),
    }
}

fn validate_payload(payload: &RegisterArchivedClip) -> Result<(), String> {
    if payload.canonical_key.is_empty() {
        return Err("canonical_key must be non-empty".to_owned());
    }
    if payload.partition.is_empty() {
        return Err("partition must be non-empty".to_owned());
    }
    if let Some(duration_s) = payload.duration_s {
        if duration_s < 0 {
            return Err("duration_s must be >= 0".to_owned());
        }
    }
    validate_rel_path(&payload.archive.path, "archive.path")?;
    if payload.archive.size_bytes < 0 {
        return Err("archive.size_bytes must be >= 0".to_owned());
    }
    if payload.archive.file_count < 1 {
        return Err("archive.file_count must be >= 1".to_owned());
    }
    if payload.angles.is_empty() {
        return Err("register_archived_clip requires at least one angle".to_owned());
    }

    let mut seen_cameras: HashSet<&str> = HashSet::new();
    for angle in &payload.angles {
        if !is_allowed_camera(&angle.camera) {
            return Err(format!("invalid camera: {}", angle.camera));
        }
        if !seen_cameras.insert(angle.camera.as_str()) {
            return Err(format!("duplicate camera: {}", angle.camera));
        }
        validate_rel_path(&angle.file_ref, "angles.file_ref")?;
        if angle.offset_ms < 0 {
            return Err("angles.offset_ms must be >= 0".to_owned());
        }
        if angle.size_bytes < 0 {
            return Err("angles.size_bytes must be >= 0".to_owned());
        }
        if let Some(duration_s) = angle.duration_s {
            if duration_s < 0 {
                return Err("angles.duration_s must be >= 0".to_owned());
            }
        }
    }
    Ok(())
}

fn is_allowed_camera(camera: &str) -> bool {
    matches!(
        camera,
        "front" | "back" | "left_repeater" | "right_repeater" | "left" | "right"
    )
}

fn validate_rel_path(path: &str, field: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err(format!("{field} must be non-empty"));
    }
    if path.starts_with('/') || path.starts_with('\\') {
        return Err(format!("{field} must be archive-root-relative"));
    }
    if path
        .split(['/', '\\'])
        .any(|segment| segment.is_empty() || segment == "." || segment == "..")
    {
        return Err(format!("{field} must not contain empty/dot path segments"));
    }
    Ok(())
}

fn seconds_opt_to_f64(value: Option<i64>, field: &str) -> Result<Option<f64>, String> {
    value
        .map(|seconds| seconds_to_f64(seconds, field))
        .transpose()
}

fn seconds_to_f64(value: i64, field: &str) -> Result<f64, String> {
    const MAX_EXACT_INT_IN_F64: u64 = 9_007_199_254_740_992;
    if value.unsigned_abs() > MAX_EXACT_INT_IN_F64 {
        return Err(format!("{field} exceeds exact f64 integer range"));
    }
    value
        .to_string()
        .parse::<f64>()
        .map_err(|e| format!("failed to convert {field}: {e}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::unwrap_used)]

    use super::{parse_folder_class, validate_payload};
    use crate::proto::{ArchiveAngle, ArchiveUnit, RegisterArchivedClip};

    fn payload() -> RegisterArchivedClip {
        RegisterArchivedClip {
            canonical_key: "slot0:TeslaCam/RecentClips/2026-06-19/clip-a".to_owned(),
            folder_class: "RecentClips".to_owned(),
            partition: "slot0".to_owned(),
            started_at: 1_718_805_600,
            ended_at: 1_718_805_660,
            duration_s: Some(60),
            archive: ArchiveUnit {
                path: "archive/2026-06-19/clip-a".to_owned(),
                size_bytes: 4096,
                file_count: 4,
                archived_at: 1_718_805_700,
            },
            angles: vec![ArchiveAngle {
                camera: "front".to_owned(),
                file_ref: "archive/2026-06-19/clip-a/front.mp4".to_owned(),
                offset_ms: 0,
                duration_s: Some(60),
                size_bytes: 1024,
            }],
        }
    }

    #[test]
    fn archived_clips_folder_class_is_rejected() {
        assert!(parse_folder_class("ArchivedClips").is_err());
    }

    #[test]
    fn validation_rejects_duplicate_camera() {
        let mut request = payload();
        let angle = request.angles.first().cloned().unwrap();
        request.angles.push(angle);
        assert!(validate_payload(&request).is_err());
    }

    #[test]
    fn validation_accepts_valid_payload() {
        assert!(validate_payload(&payload()).is_ok());
    }
}
