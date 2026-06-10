//! `POST /api/chimes` + `DELETE /api/chimes/{id}` — the lock-chime media
//! feature, the first concrete consumer of the generic p2-media install/remove
//! primitive in [`crate::route`].
//!
//! Tesla's MEDIA (p2) partition holds a single lock chime as `LockChime.wav` at
//! the partition root, so the chime is a single-slot asset: install overwrites
//! it, and remove deletes it. The destination `rel_path` is the fixed constant
//! [`CHIME_REL_PATH`] — never the client-supplied filename — so the upload can
//! never steer the write outside its slot.
//!
//! The handlers here are deliberately thin: they validate the upload (size +
//! WAV container shape, fail-closed BEFORE any staging or gadget round-trip),
//! then delegate to [`crate::route::run_install`] / [`crate::route::run_remove`]
//! which own staging, the `gadgetd` handoff, cleanup, and the `job_status`
//! lifecycle. Adding the next media feature (lightshow, boombox, …) is the same
//! shape: a thin validate-then-`run_install` handler with its own `kind` and
//! fixed `rel_path`.

use axum::Json;
use axum::extract::multipart::MultipartError;
use axum::extract::{Multipart, Path, State};
use axum::http::StatusCode;
use serde_json::Value;

use crate::AppState;
use crate::dto::ChimesDto;
use crate::error::ApiError;

/// The MEDIA partition wire index (`gadgetd` `Partition::P2`).
const PARTITION_MEDIA: u8 = 2;

/// The fixed destination path of the lock chime at the p2 root (Tesla
/// convention). Never derived from the upload filename.
const CHIME_REL_PATH: &str = "LockChime.wav";

/// The single-slot chime id accepted by `DELETE /api/chimes/{id}`.
const CHIME_ID: &str = "LockChime";

/// The multipart form field carrying the WAV bytes.
const FIELD_NAME: &str = "file";

/// Maximum accepted lock-chime size (1 MiB). Tesla lock chimes are short PCM
/// clips far under this; the cap is enforced incrementally while reading the
/// upload so a hostile client cannot force an unbounded in-memory buffer.
const CHIME_MAX_BYTES: usize = 1024 * 1024;

/// Hard request-body ceiling applied as an axum `DefaultBodyLimit` layer on the
/// route (8 MiB). Defense-in-depth above the 1 MiB logical cap: it bounds the
/// total decoded multipart body (including the drain of any unexpected fields)
/// while leaving the 1 MiB per-field guard as the binding oversize signal — so a
/// realistically-too-large chime (1–8 MiB) is reported as `422 chime_too_large`
/// rather than a generic body-limit rejection. A body above this ceiling is a
/// `400 invalid_multipart` backstop.
pub(crate) const CHIME_BODY_LIMIT: usize = 8 * 1024 * 1024;

/// `GET /api/chimes`: report the installed lock chime (read-only).
///
/// Reads the `media_entries` catalog (populated by the scannerd→indexd media
/// inventory) and returns `{installed: {...}}` or `{installed: null}`. This is
/// the safe, always-on counterpart to the operator-gated install/remove: it
/// never touches the live USB LUN, only the read-only catalog. A catalog that
/// predates the media inventory degrades to `{installed: null}` rather than a
/// `5xx`, so the SPA's media page stays clean.
pub(crate) async fn list_chimes(
    State(state): State<AppState>,
) -> Result<Json<ChimesDto>, ApiError> {
    let installed = crate::route::read(state.catalog, crate::query::installed_chime).await?;
    Ok(Json(ChimesDto { installed }))
}

/// `POST /api/chimes`: install a lock chime onto the MEDIA partition.
///
/// Accepts `multipart/form-data` with a single `file` field holding a finished
/// 16-bit PCM WAV (mono/stereo, 44.1 or 48 kHz). The bytes are validated, then
/// installed at the fixed [`CHIME_REL_PATH`] via the car-handoff. On success the
/// response is `200 {handoff_id, state:"done"}`; progress is observable on
/// `GET /api/jobs`.
pub(crate) async fn install_chime(
    State(state): State<AppState>,
    multipart: Multipart,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let bytes = read_chime_upload(multipart).await?;
    validate_lock_chime_wav(&bytes)
        .map_err(|msg| ApiError::status(StatusCode::UNPROCESSABLE_ENTITY, "invalid_wav", msg))?;
    crate::route::run_install(
        state,
        "chime_install",
        PARTITION_MEDIA,
        CHIME_REL_PATH,
        bytes,
    )
    .await
}

/// `DELETE /api/chimes/{id}`: remove the installed lock chime.
///
/// The single-slot id must equal [`CHIME_ID`] (else `404`). Removal is a
/// `delete_paths` handoff for the fixed [`CHIME_REL_PATH`]; it is idempotent, so
/// removing an already-absent chime still reports `200 {handoff_id,
/// state:"done"}`.
pub(crate) async fn remove_chime(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    if id != CHIME_ID {
        return Err(ApiError::NotFound);
    }
    crate::route::run_remove(state, "chime_remove", PARTITION_MEDIA, CHIME_REL_PATH).await
}

/// Read the single `file` field from the multipart body, enforcing the size cap
/// incrementally. A missing `file` field is a `400`; a second `file` field is a
/// `400`; exceeding [`CHIME_MAX_BYTES`] is a `422`. Unknown fields are drained
/// and ignored. All reads happen BEFORE any staging or gadget round-trip.
async fn read_chime_upload(mut multipart: Multipart) -> Result<Vec<u8>, ApiError> {
    let mut found: Option<Vec<u8>> = None;
    while let Some(mut field) = multipart.next_field().await.map_err(map_multipart_err)? {
        let is_target = field.name() == Some(FIELD_NAME);
        if !is_target {
            // Drain unknown fields so the stream stays well-formed.
            while field.chunk().await.map_err(map_multipart_err)?.is_some() {}
            continue;
        }
        if found.is_some() {
            return Err(ApiError::bad_request(
                "duplicate_field",
                "multiple `file` fields in upload",
            ));
        }
        let mut buf: Vec<u8> = Vec::new();
        while let Some(chunk) = field.chunk().await.map_err(map_multipart_err)? {
            let projected = buf.len().saturating_add(chunk.len());
            if projected > CHIME_MAX_BYTES {
                return Err(ApiError::status(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "chime_too_large",
                    format!("lock chime exceeds the {CHIME_MAX_BYTES}-byte limit"),
                ));
            }
            buf.extend_from_slice(&chunk);
        }
        found = Some(buf);
    }
    found.ok_or_else(|| ApiError::bad_request("upload_required", "missing `file` upload field"))
}

/// Map a multipart decode error (malformed body or the body-limit backstop) to
/// a `400`. The normal oversize path trips the `422` logical cap first; this is
/// the hard-limit / protocol-error fallback.
#[allow(clippy::needless_pass_by_value)] // by-value matches `Result::map_err`'s FnOnce
fn map_multipart_err(err: MultipartError) -> ApiError {
    ApiError::bad_request("invalid_multipart", format!("malformed upload: {err}"))
}

/// Validate a lock-chime WAV: RIFF/WAVE container, a PCM `fmt ` chunk with
/// mono/stereo channels at 44.1/48 kHz and 16-bit samples (with the derived
/// `byte_rate`/`block_align` cross-checked so a hand-crafted header cannot lie),
/// and a non-empty `data` chunk. All offset math is checked; no slice indexing.
fn validate_lock_chime_wav(bytes: &[u8]) -> Result<(), String> {
    if bytes.len() < 12 {
        return Err("file too small to be a WAV".to_owned());
    }
    if bytes.get(0..4) != Some(b"RIFF") {
        return Err("missing RIFF header".to_owned());
    }
    if bytes.get(8..12) != Some(b"WAVE") {
        return Err("missing WAVE form type".to_owned());
    }

    let mut offset = 12usize;
    let mut fmt_seen = false;
    let mut data_non_empty = false;
    while let Some(header) = bytes.get(offset..offset.saturating_add(8)) {
        let chunk_id = header.get(0..4).ok_or("truncated chunk id")?;
        let size_slice = header.get(4..8).ok_or("truncated chunk size")?;
        let size_arr: [u8; 4] = size_slice.try_into().map_err(|_| "bad chunk size")?;
        let chunk_size = u32::from_le_bytes(size_arr) as usize;

        let body_start = offset.saturating_add(8);
        let body_end = body_start
            .checked_add(chunk_size)
            .ok_or("chunk size overflow")?;
        let body = bytes
            .get(body_start..body_end)
            .ok_or("chunk body exceeds file")?;

        if chunk_id == b"fmt " {
            validate_fmt_chunk(body)?;
            fmt_seen = true;
        } else if chunk_id == b"data" && !body.is_empty() {
            data_non_empty = true;
        }

        // Chunks are word-aligned: an odd-sized body is followed by a pad byte.
        let consumed = 8usize
            .checked_add(chunk_size)
            .and_then(|v| v.checked_add(chunk_size & 1))
            .ok_or("chunk advance overflow")?;
        offset = offset.checked_add(consumed).ok_or("offset overflow")?;
    }

    if !fmt_seen {
        return Err("missing fmt chunk".to_owned());
    }
    if !data_non_empty {
        return Err("missing or empty data chunk".to_owned());
    }
    Ok(())
}

/// Validate the body of a PCM `fmt ` chunk (≥16 bytes).
fn validate_fmt_chunk(body: &[u8]) -> Result<(), String> {
    if body.len() < 16 {
        return Err("fmt chunk too small".to_owned());
    }
    let read_u16 = |start: usize| -> Result<u16, String> {
        let slice = body
            .get(start..start.saturating_add(2))
            .ok_or("truncated fmt field")?;
        let arr: [u8; 2] = slice.try_into().map_err(|_| "bad fmt field")?;
        Ok(u16::from_le_bytes(arr))
    };
    let read_u32 = |start: usize| -> Result<u32, String> {
        let slice = body
            .get(start..start.saturating_add(4))
            .ok_or("truncated fmt field")?;
        let arr: [u8; 4] = slice.try_into().map_err(|_| "bad fmt field")?;
        Ok(u32::from_le_bytes(arr))
    };

    let audio_format = read_u16(0)?;
    let channels = read_u16(2)?;
    let sample_rate = read_u32(4)?;
    let byte_rate = read_u32(8)?;
    let block_align = read_u16(12)?;
    let bits = read_u16(14)?;

    if audio_format != 1 {
        return Err("only PCM (format 1) lock chimes are supported".to_owned());
    }
    if channels != 1 && channels != 2 {
        return Err("lock chime must be mono or stereo".to_owned());
    }
    if sample_rate != 44_100 && sample_rate != 48_000 {
        return Err("lock chime sample rate must be 44.1 or 48 kHz".to_owned());
    }
    if bits != 16 {
        return Err("lock chime must be 16-bit PCM".to_owned());
    }

    let expected_block = channels.checked_mul(bits / 8).ok_or("bad block align")?;
    if block_align != expected_block {
        return Err("fmt block_align inconsistent with channels/bits".to_owned());
    }
    let expected_byte_rate = sample_rate
        .checked_mul(u32::from(expected_block))
        .ok_or("byte rate overflow")?;
    if byte_rate != expected_byte_rate {
        return Err("fmt byte_rate inconsistent with rate/channels/bits".to_owned());
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic, clippy::indexing_slicing)]
mod tests {
    use super::{CHIME_MAX_BYTES, validate_lock_chime_wav};

    /// Build a minimal valid 16-bit PCM WAV with `data_len` bytes of audio.
    fn wav(channels: u16, sample_rate: u32, bits: u16, data_len: usize) -> Vec<u8> {
        let block_align = channels * (bits / 8);
        let byte_rate = sample_rate * u32::from(block_align);
        let mut v = Vec::new();
        v.extend_from_slice(b"RIFF");
        v.extend_from_slice(&u32::try_from(36 + data_len).unwrap().to_le_bytes());
        v.extend_from_slice(b"WAVE");
        v.extend_from_slice(b"fmt ");
        v.extend_from_slice(&16u32.to_le_bytes());
        v.extend_from_slice(&1u16.to_le_bytes()); // PCM
        v.extend_from_slice(&channels.to_le_bytes());
        v.extend_from_slice(&sample_rate.to_le_bytes());
        v.extend_from_slice(&byte_rate.to_le_bytes());
        v.extend_from_slice(&block_align.to_le_bytes());
        v.extend_from_slice(&bits.to_le_bytes());
        v.extend_from_slice(b"data");
        v.extend_from_slice(&u32::try_from(data_len).unwrap().to_le_bytes());
        v.extend(std::iter::repeat_n(0u8, data_len));
        v
    }

    #[test]
    fn accepts_mono_44k_16bit() {
        assert!(validate_lock_chime_wav(&wav(1, 44_100, 16, 8)).is_ok());
    }

    #[test]
    fn accepts_stereo_48k_16bit() {
        assert!(validate_lock_chime_wav(&wav(2, 48_000, 16, 16)).is_ok());
    }

    #[test]
    fn rejects_non_riff() {
        let mut v = wav(1, 44_100, 16, 8);
        v[0] = b'X';
        assert!(validate_lock_chime_wav(&v).is_err());
    }

    #[test]
    fn rejects_non_pcm() {
        let mut v = wav(1, 44_100, 16, 8);
        // audio_format lives 2 bytes into the fmt body (offset 20 overall).
        v[20] = 3; // IEEE float
        assert!(validate_lock_chime_wav(&v).is_err());
    }

    #[test]
    fn rejects_8bit() {
        assert!(validate_lock_chime_wav(&wav(1, 44_100, 8, 8)).is_err());
    }

    #[test]
    fn rejects_odd_sample_rate() {
        assert!(validate_lock_chime_wav(&wav(1, 32_000, 16, 8)).is_err());
    }

    #[test]
    fn rejects_empty_data_chunk() {
        assert!(validate_lock_chime_wav(&wav(1, 44_100, 16, 0)).is_err());
    }

    #[test]
    fn rejects_truncated_header() {
        assert!(validate_lock_chime_wav(b"RIFF").is_err());
    }

    #[test]
    fn cap_is_one_mib() {
        assert_eq!(CHIME_MAX_BYTES, 1024 * 1024);
    }
}
