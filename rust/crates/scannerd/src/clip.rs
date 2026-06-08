//! Clip identity from Tesla's filename convention.
//!
//! Tesla names every recording
//! `YYYY-MM-DD_HH-MM-SS-<camera>.mp4`, e.g.
//! `2026-06-01_20-10-04-front.mp4`. All camera angles of one recording
//! share the 19-character timestamp prefix, so that prefix groups a
//! clip and the suffix names the angle. The camera token is kept
//! **verbatim** — scannerd does not normalize or interpret it (that is
//! `indexd`'s job); it only needs a stable grouping key.

/// A parsed Tesla clip filename.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClipName {
    /// The 19-char `YYYY-MM-DD_HH-MM-SS` timestamp prefix — the
    /// grouping key shared by every angle of one recording.
    pub timestamp: String,
    /// The camera token verbatim (e.g. `front`, `left_repeater`), or
    /// `None` if the name had no `-camera` suffix.
    pub camera: Option<String>,
}

/// Length of the `YYYY-MM-DD_HH-MM-SS` timestamp prefix.
const TIMESTAMP_LEN: usize = 19;

/// Parse a Tesla clip filename, or `None` if it is not an `.mp4` with a
/// valid timestamp prefix.
#[must_use]
pub fn parse_clip_name(name: &str) -> Option<ClipName> {
    let stem = name.strip_suffix(".mp4")?;
    if stem.len() < TIMESTAMP_LEN {
        return None;
    }
    let (timestamp, rest) = stem.split_at(TIMESTAMP_LEN);
    if !is_timestamp(timestamp) {
        return None;
    }
    let camera = match rest.strip_prefix('-') {
        Some(cam) if !cam.is_empty() => Some(cam.to_owned()),
        _ if rest.is_empty() => None,
        // Anything else (no separator, trailing dash) is not a clip.
        _ => return None,
    };
    Some(ClipName {
        timestamp: timestamp.to_owned(),
        camera,
    })
}

/// Validate the `YYYY-MM-DD_HH-MM-SS` shape: digits in the right places
/// and the exact separator layout. Cheap structural check only.
fn is_timestamp(s: &str) -> bool {
    // Positions of the fixed separators within the prefix.
    const DASH: [usize; 2] = [4, 7];
    const UNDERSCORE: usize = 10;
    const COLON_AS_DASH: [usize; 2] = [13, 16];
    let b = s.as_bytes();
    if b.len() != TIMESTAMP_LEN {
        return false;
    }
    for (i, &ch) in b.iter().enumerate() {
        let ok = if DASH.contains(&i) || COLON_AS_DASH.contains(&i) {
            ch == b'-'
        } else if i == UNDERSCORE {
            ch == b'_'
        } else {
            ch.is_ascii_digit()
        };
        if !ok {
            return false;
        }
    }
    true
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn parses_front_angle() {
        let p = parse_clip_name("2026-06-01_20-10-04-front.mp4").unwrap();
        assert_eq!(p.timestamp, "2026-06-01_20-10-04");
        assert_eq!(p.camera.as_deref(), Some("front"));
    }

    #[test]
    fn keeps_multi_token_camera_verbatim() {
        let p = parse_clip_name("2026-06-01_20-10-04-left_repeater.mp4").unwrap();
        assert_eq!(p.camera.as_deref(), Some("left_repeater"));
    }

    #[test]
    fn all_angles_share_timestamp_key() {
        let a = parse_clip_name("2026-06-01_20-10-04-front.mp4").unwrap();
        let b = parse_clip_name("2026-06-01_20-10-04-back.mp4").unwrap();
        assert_eq!(a.timestamp, b.timestamp);
        assert_ne!(a.camera, b.camera);
    }

    #[test]
    fn rejects_non_mp4() {
        assert!(parse_clip_name("event.json").is_none());
        assert!(parse_clip_name("thumb.png").is_none());
    }

    #[test]
    fn rejects_bad_timestamp() {
        assert!(parse_clip_name("not-a-timestamp-here-x.mp4").is_none());
        assert!(parse_clip_name("2026/06/01_20-10-04-front.mp4").is_none());
    }
}
