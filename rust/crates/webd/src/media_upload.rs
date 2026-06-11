//! Shared upload helpers for the five toybox media categories (Boombox, Music,
//! `LightShow`, `LicensePlate`, Wraps). Each category has its own handler module
//! (`boombox`, `music`, `lightshows`, `plates`, `wraps`) that calls these
//! helpers for file-reading, filename sanitisation, and format validation.

use axum::extract::Multipart;
use axum::http::StatusCode;
use serde::Deserialize;

use crate::error::ApiError;

/// Maximum number of files accepted in one bulk-delete request. A single
/// `gadgetd` `delete_paths` handoff carries the whole set; this bound keeps the
/// request (and the resulting mutation) sane on a small appliance.
pub(crate) const MAX_BULK_DELETE: usize = 100;

/// Request body for the bulk-delete endpoints (`POST /api/<category>/bulk-delete`).
///
/// Each entry is a bare file name (e.g. `horn.wav`), never a path — the handler
/// reconstructs the partition-relative path under the category directory, so a
/// client can never address a file outside its own category.
#[derive(Debug, Deserialize)]
pub(crate) struct BulkDeleteRequest {
    /// The bare file names to delete.
    pub names: Vec<String>,
}

/// Sanitise a batch of bare file names into partition-root-relative paths under
/// `dir` (e.g. `Boombox/horn.wav`). Rejects an empty batch (`400 empty_batch`),
/// an over-cap batch (`422 batch_too_large`), and any individual name that
/// fails [`sanitise_filename`] (path traversal, non-ASCII, embedded separators
/// — those collapse to the last component, which is then validated). Duplicate
/// names are de-duplicated so the resulting `delete_paths` set is minimal.
pub(crate) fn plan_bulk_delete(dir: &str, names: &[String]) -> Result<Vec<String>, ApiError> {
    if names.is_empty() {
        return Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "empty_batch",
            "expected at least one file name".to_owned(),
        ));
    }
    if names.len() > MAX_BULK_DELETE {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "batch_too_large",
            format!("at most {MAX_BULK_DELETE} files may be deleted at once"),
        ));
    }
    let mut rel_paths: Vec<String> = Vec::with_capacity(names.len());
    for raw in names {
        let name = sanitise_filename(raw)?;
        let rel_path = format!("{dir}/{name}");
        if !rel_paths.contains(&rel_path) {
            rel_paths.push(rel_path);
        }
    }
    Ok(rel_paths)
}

/// Read a single `file` multipart field, enforcing an incremental byte cap.
///
/// Unknown extra fields are drained and ignored (consistent with the chimes
/// handler). A missing `file` field is `400 missing_file`; exceeding `max_bytes`
/// is `422 file_too_large`.
pub(crate) async fn read_file_upload(
    mut multipart: Multipart,
    field_name: &str,
    max_bytes: usize,
) -> Result<(String, Vec<u8>), ApiError> {
    let mut file_bytes: Option<Vec<u8>> = None;
    let mut file_name: Option<String> = None;

    while let Some(field) = multipart.next_field().await.map_err(|e| {
        ApiError::status(
            StatusCode::BAD_REQUEST,
            "invalid_multipart",
            format!("multipart error: {e}"),
        )
    })? {
        let name = field.name().unwrap_or("").to_owned();
        if name != field_name {
            // Drain unknown fields.
            let _ = field.bytes().await;
            continue;
        }
        if file_bytes.is_some() {
            return Err(ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_multipart",
                "duplicate 'file' field".to_owned(),
            ));
        }
        let fname = field
            .file_name()
            .map_or_else(|| "upload".to_owned(), str::to_owned);
        let mut buf = Vec::with_capacity(4096);
        let mut stream = field;
        while let Some(chunk) = stream.chunk().await.map_err(|e| {
            ApiError::status(
                StatusCode::BAD_REQUEST,
                "invalid_multipart",
                format!("read error: {e}"),
            )
        })? {
            if buf.len() + chunk.len() > max_bytes {
                return Err(ApiError::status(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "file_too_large",
                    format!("file exceeds {max_bytes} bytes"),
                ));
            }
            buf.extend_from_slice(&chunk);
        }
        file_bytes = Some(buf);
        file_name = Some(fname);
    }

    match (file_bytes, file_name) {
        (Some(bytes), Some(name)) => Ok((name, bytes)),
        _ => Err(ApiError::status(
            StatusCode::BAD_REQUEST,
            "missing_file",
            "expected a 'file' multipart field".to_owned(),
        )),
    }
}

/// Sanitise the upload filename into a safe single-component name.
///
/// Extracts the last path component (browsers may send a full local path) and
/// validates it. Allows ASCII letters, digits, spaces, underscores, dashes,
/// and dots. Rejects:
/// * Empty after extracting the last path component.
/// * Longer than 255 bytes.
/// * Any NUL or path-traversal component (`.` or `..` exactly).
/// * Non-ASCII characters (Tesla's exFAT drivers have spotty Unicode support).
///
/// Returns the sanitised filename, or an `Err(ApiError::status(422, …))`.
pub(crate) fn sanitise_filename(raw: &str) -> Result<String, ApiError> {
    // Accept the last component only (in case the browser sends a full path).
    let base = raw.rsplit(['/', '\\']).next().unwrap_or(raw).trim();

    if base.is_empty() || base == "." || base == ".." {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_filename",
            "filename must not be empty or a path-traversal component".to_owned(),
        ));
    }
    if base.len() > 255 {
        return Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_filename",
            "filename exceeds 255 bytes".to_owned(),
        ));
    }
    for ch in base.chars() {
        if !ch.is_ascii() {
            return Err(ApiError::status(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_filename",
                "filename must contain only ASCII characters".to_owned(),
            ));
        }
        if matches!(ch, '\0' | '/' | '\\') {
            return Err(ApiError::status(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_filename",
                format!("filename contains forbidden character: {ch:?}"),
            ));
        }
    }
    Ok(base.to_owned())
}

/// Check that `filename`'s extension (case-insensitive) is one of `allowed`.
///
/// Returns `Err(422 invalid_extension)` when the check fails.
pub(crate) fn check_extension(filename: &str, allowed: &[&str]) -> Result<(), ApiError> {
    let ext = filename
        .rsplit('.')
        .next()
        .map(str::to_ascii_lowercase)
        .unwrap_or_default();
    if allowed.iter().any(|a| *a == ext.as_str()) {
        return Ok(());
    }
    Err(ApiError::status(
        StatusCode::UNPROCESSABLE_ENTITY,
        "invalid_extension",
        format!(
            "file extension '.{ext}' not accepted; allowed: {}",
            allowed.join(", ")
        ),
    ))
}

/// Verify `bytes` begins with the PNG magic signature (`\x89PNG\r\n\x1a\n`).
///
/// Returns `Err(422 invalid_png)` when the magic does not match.
pub(crate) fn validate_png_magic(bytes: &[u8]) -> Result<(), ApiError> {
    const PNG_MAGIC: &[u8] = b"\x89PNG\r\n\x1a\n";
    if bytes.starts_with(PNG_MAGIC) {
        Ok(())
    } else {
        Err(ApiError::status(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_png",
            "file does not start with a valid PNG signature".to_owned(),
        ))
    }
}

// ── tests ─────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::{check_extension, sanitise_filename, validate_png_magic};

    #[test]
    fn sanitise_accepts_normal_names() {
        assert_eq!(sanitise_filename("horn.wav").unwrap(), "horn.wav");
        assert_eq!(sanitise_filename("my-show.fseq").unwrap(), "my-show.fseq");
        assert_eq!(sanitise_filename("track 1.mp3").unwrap(), "track 1.mp3");
    }

    #[test]
    fn sanitise_strips_leading_path_component() {
        // Browser may send a full local path on some OS/browser combos.
        assert_eq!(
            sanitise_filename("/home/user/horn.wav").unwrap(),
            "horn.wav"
        );
        assert_eq!(
            sanitise_filename("C:\\Users\\me\\wrap.png").unwrap(),
            "wrap.png"
        );
    }

    #[test]
    fn sanitise_rejects_traversal() {
        assert!(sanitise_filename("..").is_err());
        assert!(sanitise_filename(".").is_err());
        assert!(sanitise_filename("").is_err());
    }

    #[test]
    fn sanitise_strips_embedded_slash_to_last_component() {
        // "sub/file.wav" → strips to "file.wav" (safe; no path component stored).
        assert_eq!(sanitise_filename("sub/file.wav").unwrap(), "file.wav");
    }

    #[test]
    fn sanitise_rejects_non_ascii() {
        assert!(sanitise_filename("téléchargement.wav").is_err());
    }

    #[test]
    fn check_extension_accepts_known_ext() {
        check_extension("horn.wav", &["wav", "mp3"]).unwrap();
        check_extension("song.MP3", &["mp3"]).unwrap(); // case-insensitive
    }

    #[test]
    fn check_extension_rejects_unknown_ext() {
        assert!(check_extension("song.ogg", &["wav", "mp3"]).is_err());
    }

    #[test]
    fn png_magic_accepted() {
        let mut data = b"\x89PNG\r\n\x1a\n".to_vec();
        data.extend_from_slice(&[0u8; 16]);
        validate_png_magic(&data).unwrap();
    }

    #[test]
    fn png_magic_rejected_for_non_png() {
        assert!(validate_png_magic(b"JFIF").is_err());
    }

    #[test]
    fn plan_bulk_delete_maps_and_dedupes() {
        let paths =
            super::plan_bulk_delete("Boombox", &["a.wav".to_owned(), "a.wav".to_owned()]).unwrap();
        assert_eq!(paths, vec!["Boombox/a.wav".to_owned()]);

        let paths =
            super::plan_bulk_delete("Music", &["x.mp3".to_owned(), "y.flac".to_owned()]).unwrap();
        assert_eq!(
            paths,
            vec!["Music/x.mp3".to_owned(), "Music/y.flac".to_owned()]
        );
    }

    #[test]
    fn plan_bulk_delete_rejects_empty_and_oversize() {
        assert!(super::plan_bulk_delete("Boombox", &[]).is_err());
        let many: Vec<String> = (0..=super::MAX_BULK_DELETE)
            .map(|i| format!("f{i}.wav"))
            .collect();
        assert!(super::plan_bulk_delete("Boombox", &many).is_err());
    }

    #[test]
    fn plan_bulk_delete_rejects_bad_name() {
        assert!(super::plan_bulk_delete("Boombox", &["..".to_owned()]).is_err());
        // An embedded path collapses to its last component (never escapes `dir`).
        let paths = super::plan_bulk_delete("Boombox", &["../../etc/horn.wav".to_owned()]).unwrap();
        assert_eq!(paths, vec!["Boombox/horn.wav".to_owned()]);
    }
}
