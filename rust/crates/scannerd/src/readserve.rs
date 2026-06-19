//! Dedicated `ReadFile` socket server for raw clip windows.

use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use scannerd::boot::{ExfatParams, parse_boot_sector};
use scannerd::mbr::parse_mbr;
use scannerd::proto::{
    ClipIdentity, MAX_READ_LEN, MAX_REQUEST_FRAME, ReadFileHeader, ReadFileRequest, read_frame,
    write_frame,
};
use scannerd::reader::BlockReader;
use scannerd::volume::Volume;
use scannerd::walk::{FileRecord, resolve_file_by_components};

use crate::io::PreadReader;

/// Default dedicated read socket path.
pub const DEFAULT_READ_SOCKET: &str = "/run/teslausb/scannerd-read.sock";
/// Maximum concurrent `ReadFile` connections.
const MAX_CONNECTIONS: usize = 4;
/// Read timeout for one request frame.
const READ_TIMEOUT: Duration = Duration::from_secs(120);
/// Write timeout for one response.
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);
/// Maximum accepted raw/decoded path length.
const MAX_PATH_LEN: usize = 1024;
/// Maximum accepted path component count.
const MAX_COMPONENTS: usize = 32;
/// Slot used for live TeslaCam reads.
const SLOT0: u8 = 0;

/// Start the read server on a dedicated thread.
///
/// # Errors
///
/// Returns an error when socket setup/bind fails or thread spawn fails.
pub fn start(reader: Arc<PreadReader>, socket_path: &Path) -> io::Result<JoinHandle<()>> {
    let listener = bind_listener(socket_path)?;
    let path_text = socket_path.display().to_string();
    let handle = thread::Builder::new()
        .name("scannerd-readserve".to_owned())
        .spawn(move || run_listener(&listener, &reader))
        .map_err(|e| io::Error::other(format!("spawn readserve: {e}")))?;
    println!("scannerd read serve: listening on {path_text}");
    Ok(handle)
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

fn run_listener(listener: &UnixListener, reader: &Arc<PreadReader>) {
    let active = Arc::new(AtomicUsize::new(0));
    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let current = active.fetch_add(1, Ordering::AcqRel);
                if current >= MAX_CONNECTIONS {
                    active.fetch_sub(1, Ordering::AcqRel);
                    let _ = write_error_and_close(stream, "too many read connections");
                    continue;
                }
                let active_guard = Arc::clone(&active);
                let reader = Arc::clone(reader);
                let _handle = thread::spawn(move || {
                    let _counter = ActiveCounter::new(active_guard);
                    if let Err(e) = handle_conn(stream, reader.as_ref()) {
                        eprintln!("scannerd read serve: connection ended: {e}");
                    }
                });
            }
            Err(e) => eprintln!("scannerd read serve: accept error: {e}"),
        }
    }
}

struct ActiveCounter {
    active: Arc<AtomicUsize>,
}

impl ActiveCounter {
    fn new(active: Arc<AtomicUsize>) -> Self {
        Self { active }
    }
}

impl Drop for ActiveCounter {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::AcqRel);
    }
}

fn write_error_and_close(mut stream: UnixStream, message: &str) -> io::Result<()> {
    stream.set_write_timeout(Some(WRITE_TIMEOUT))?;
    write_response(
        &mut stream,
        ReadReply {
            header: ReadFileHeader::Error {
                message: message.to_owned(),
            },
            bytes: None,
        },
    )
}

fn handle_conn<R: BlockReader + ?Sized>(mut stream: UnixStream, reader: &R) -> io::Result<()> {
    stream.set_read_timeout(Some(READ_TIMEOUT))?;
    stream.set_write_timeout(Some(WRITE_TIMEOUT))?;

    let payload = read_frame(&mut stream, MAX_REQUEST_FRAME)?;
    let request: ReadFileRequest = match serde_json::from_slice(&payload) {
        Ok(req) => req,
        Err(e) => {
            return write_response(
                &mut stream,
                ReadReply {
                    header: ReadFileHeader::Error {
                        message: format!("invalid request json: {e}"),
                    },
                    bytes: None,
                },
            );
        }
    };

    let reply = process_request(reader, &request);
    write_response(&mut stream, reply)
}

struct ReadReply {
    header: ReadFileHeader,
    bytes: Option<Vec<u8>>,
}

fn write_response(stream: &mut impl Write, reply: ReadReply) -> io::Result<()> {
    let header_json = serde_json::to_vec(&reply.header).map_err(io::Error::other)?;
    write_frame(stream, &header_json)?;
    if let Some(bytes) = reply.bytes {
        let len_u32 = u32::try_from(bytes.len())
            .map_err(|_| io::Error::other("raw tail length exceeds u32"))?;
        stream.write_all(&len_u32.to_le_bytes())?;
        stream.write_all(&bytes)?;
        stream.flush()?;
    }
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn process_request<R: BlockReader + ?Sized>(reader: &R, request: &ReadFileRequest) -> ReadReply {
    let path_components = match validate_request_path(&request.path) {
        Ok(components) => components,
        Err(message) => {
            return ReadReply {
                header: ReadFileHeader::Error { message },
                bytes: None,
            };
        }
    };

    let resolved = match resolve_file(reader, &path_components) {
        Ok(found) => found,
        Err(message) => {
            return ReadReply {
                header: ReadFileHeader::Error { message },
                bytes: None,
            };
        }
    };
    let Some(resolved) = resolved else {
        return ReadReply {
            header: ReadFileHeader::NotFound,
            bytes: None,
        };
    };

    let identity = ClipIdentity {
        first_cluster: resolved.record.first_cluster,
        total_size: resolved.record.data_length,
        name_hash: resolved.record.name_hash,
    };
    if let Some(handle) = request.handle {
        if handle != identity {
            return ReadReply {
                header: ReadFileHeader::Changed,
                bytes: None,
            };
        }
    }

    let readable_size = resolved
        .record
        .valid_data_length
        .min(resolved.record.data_length);
    if request.offset > readable_size {
        return ReadReply {
            header: ReadFileHeader::OutOfRange,
            bytes: None,
        };
    }

    let take = u64::from(request.len)
        .min(u64::from(MAX_READ_LEN))
        .min(readable_size.saturating_sub(request.offset));
    let Ok(take_usize) = usize::try_from(take) else {
        return ReadReply {
            header: ReadFileHeader::Error {
                message: "requested length exceeds usize".to_owned(),
            },
            bytes: None,
        };
    };
    let bytes = if take_usize == 0 {
        Vec::new()
    } else {
        match read_window(reader, &resolved, request.offset, take_usize) {
            Ok(window) => window,
            Err(message) => {
                return ReadReply {
                    header: ReadFileHeader::Error { message },
                    bytes: None,
                };
            }
        }
    };

    if bytes.len() != take_usize {
        return ReadReply {
            header: ReadFileHeader::Error {
                message: format!("short read: expected {take_usize} got {}", bytes.len()),
            },
            bytes: None,
        };
    }

    let post_resolved = match resolve_file(reader, &path_components) {
        Ok(Some(found)) => found,
        Ok(None) => {
            return ReadReply {
                header: ReadFileHeader::Changed,
                bytes: None,
            };
        }
        Err(message) => {
            return ReadReply {
                header: ReadFileHeader::Error { message },
                bytes: None,
            };
        }
    };
    let post_identity = ClipIdentity {
        first_cluster: post_resolved.record.first_cluster,
        total_size: post_resolved.record.data_length,
        name_hash: post_resolved.record.name_hash,
    };
    if post_identity != identity {
        return ReadReply {
            header: ReadFileHeader::Changed,
            bytes: None,
        };
    }

    let Ok(byte_len) = u32::try_from(bytes.len()) else {
        return ReadReply {
            header: ReadFileHeader::Error {
                message: "raw tail length exceeds u32".to_owned(),
            },
            bytes: None,
        };
    };
    let eof = request.offset.saturating_add(take) >= readable_size;
    ReadReply {
        header: ReadFileHeader::Ok {
            identity,
            readable_size,
            eof,
            byte_len,
        },
        bytes: Some(bytes),
    }
}

struct ResolvedFile {
    params: ExfatParams,
    record: FileRecord,
}

fn read_window<R: BlockReader + ?Sized>(
    reader: &R,
    resolved: &ResolvedFile,
    offset: u64,
    len: usize,
) -> Result<Vec<u8>, String> {
    let volume = Volume::new(reader, resolved.params);
    let readable_size = resolved
        .record
        .valid_data_length
        .min(resolved.record.data_length);
    volume
        .read_file_window(
            resolved.record.first_cluster,
            resolved.record.no_fat_chain,
            readable_size,
            offset,
            len,
        )
        .map_err(|e| format!("read window failed: {e}"))
}

fn resolve_file<R: BlockReader + ?Sized>(
    reader: &R,
    path_components: &[String],
) -> Result<Option<ResolvedFile>, String> {
    let Some(params) = parse_slot0(reader)? else {
        return Ok(None);
    };
    let volume = Volume::new(reader, params);
    let found = resolve_file_by_components(&volume, SLOT0, path_components)
        .map_err(|e| format!("resolve failed: {e}"))?;
    Ok(found.map(|record| ResolvedFile { params, record }))
}

fn parse_slot0<R: BlockReader + ?Sized>(reader: &R) -> Result<Option<ExfatParams>, String> {
    let partitions = parse_mbr(reader).map_err(|e| format!("mbr parse failed: {e}"))?;
    let slot0 = partitions.into_iter().find(|entry| entry.slot == SLOT0);
    let Some(slot0) = slot0 else {
        return Ok(None);
    };
    if !slot0.is_exfat() {
        return Ok(None);
    }
    let params = parse_boot_sector(reader, slot0.start_lba)
        .map_err(|e| format!("boot parse failed for slot0: {e}"))?;
    Ok(Some(params))
}

fn validate_request_path(path: &str) -> Result<Vec<String>, String> {
    validate_path_layout(path)?;
    let decoded = percent_decode_once(path)?;
    validate_path_layout(&decoded)
}

fn validate_path_layout(path: &str) -> Result<Vec<String>, String> {
    if path.is_empty() {
        return Err("path is empty".to_owned());
    }
    if path.len() > MAX_PATH_LEN {
        return Err("path is too long".to_owned());
    }
    if path.starts_with('/') {
        return Err("path must be relative".to_owned());
    }
    if path.contains('\0') {
        return Err("path contains NUL".to_owned());
    }
    if path.contains('\\') {
        return Err("path contains backslash".to_owned());
    }

    let components: Vec<&str> = path.split('/').collect();
    if components.is_empty() {
        return Err("path has no components".to_owned());
    }
    if components.len() > MAX_COMPONENTS {
        return Err("path has too many components".to_owned());
    }
    let mut normalized = Vec::with_capacity(components.len());
    for component in components {
        if component.is_empty() {
            return Err("path contains empty component".to_owned());
        }
        if component == "." || component == ".." {
            return Err("path contains reserved component".to_owned());
        }
        if component.encode_utf16().count() > 255 {
            return Err("path component exceeds 255 utf16 code units".to_owned());
        }
        normalized.push(component.to_owned());
    }
    Ok(normalized)
}

fn percent_decode_once(path: &str) -> Result<String, String> {
    let mut out = Vec::with_capacity(path.len());
    let mut iter = path.as_bytes().iter().copied();
    while let Some(byte) = iter.next() {
        if byte == b'%' {
            let Some(hi_raw) = iter.next() else {
                return Err("path has invalid percent escape".to_owned());
            };
            let Some(lo_raw) = iter.next() else {
                return Err("path has invalid percent escape".to_owned());
            };
            let hi = hex_value(hi_raw).ok_or_else(|| "path has invalid percent escape".to_owned())?;
            let lo = hex_value(lo_raw).ok_or_else(|| "path has invalid percent escape".to_owned())?;
            out.push((hi << 4) | lo);
        } else {
            out.push(byte);
        }
    }
    String::from_utf8(out).map_err(|_| "percent-decoded path is not valid utf-8".to_owned())
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
#[allow(
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::panic,
    clippy::indexing_slicing,
    clippy::too_many_lines,
    clippy::struct_excessive_bools
)]
mod tests {
    use super::{ReadReply, process_request};
    use scannerd::proto::{ClipIdentity, ReadFileHeader, ReadFileRequest};
    use scannerd::reader::{BlockReader, ReaderError, SliceReader};
    use std::sync::atomic::{AtomicBool, Ordering};
    use teslausb_core::fs::exfat::directory::{
        FileAttributes, FileEntrySetParams, FileTimestamps, encode_file_entry_set,
    };
    use teslausb_core::fs::exfat::upcase_table::UpcaseTable;

    const TEST_FILE_NAME: &str = "2026-06-19_10-00-00-front.mp4";
    const TEST_FILE_PATH: &str = "TeslaCam/RecentClips/2026-06-19_10-00-00-front.mp4";
    const START_LBA: u32 = 1;
    const CLUSTER_SIZE: usize = 512;
    const FILE_CLUSTER: u32 = 6;

    #[test]
    fn targeted_descent_reads_nested_clip_without_walking_broken_sibling_tree() {
        let bytes = b"abcdefghijklmnopqrstuvwxyz".to_vec();
        let reader = fixture_reader(&FixtureConfig {
            file_bytes: bytes.clone(),
            data_length: bytes.len() as u64,
            add_broken_root_dir: true,
            ..FixtureConfig::default_with_size(bytes.len() as u64)
        });

        let first = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 3,
                len: 5,
                handle: None,
            },
        );
        let identity = expect_ok(first, b"defgh", false, bytes.len() as u64);

        let final_window = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 20,
                len: 100,
                handle: Some(identity),
            },
        );
        let _ = expect_ok(final_window, b"uvwxyz", true, bytes.len() as u64);
    }

    #[test]
    fn path_jail_rejects_invalid_inputs_and_case_insensitive_path_resolves() {
        let reader = fixture_reader(&FixtureConfig::default_with_size(6));

        for bad_path in [
            "TeslaCam/../RecentClips/x.mp4".to_owned(),
            "/TeslaCam/RecentClips/x.mp4".to_owned(),
            "TeslaCam\\RecentClips\\x.mp4".to_owned(),
            format!("TeslaCam/Recent\0Clips/{TEST_FILE_NAME}"),
            "a".repeat(1025),
        ] {
            let reply = process_request(
                &reader,
                &ReadFileRequest {
                    path: bad_path,
                    offset: 0,
                    len: 4,
                    handle: None,
                },
            );
            assert!(matches!(reply.header, ReadFileHeader::Error { .. }));
        }

        let mixed_case = process_request(
            &reader,
            &ReadFileRequest {
                path: "teslacam/recentclips/2026-06-19_10-00-00-front.mp4".to_owned(),
                offset: 0,
                len: 3,
                handle: None,
            },
        );
        let _ = expect_ok(mixed_case, b"abc", false, 6);
    }

    #[test]
    fn returns_not_found_for_missing_path_and_directory() {
        let reader = fixture_reader(&FixtureConfig::default_with_size(6));

        let missing = process_request(
            &reader,
            &ReadFileRequest {
                path: "TeslaCam/RecentClips/missing.mp4".to_owned(),
                offset: 0,
                len: 2,
                handle: None,
            },
        );
        assert!(matches!(missing.header, ReadFileHeader::NotFound));

        let directory = process_request(
            &reader,
            &ReadFileRequest {
                path: "TeslaCam/RecentClips".to_owned(),
                offset: 0,
                len: 2,
                handle: None,
            },
        );
        assert!(matches!(directory.header, ReadFileHeader::NotFound));
    }

    #[test]
    fn mid_write_reads_clamp_to_valid_data_length() {
        let reader = fixture_reader(&FixtureConfig::default_with_size(6));

        let clamped = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 4,
                len: 99,
                handle: None,
            },
        );
        let _ = expect_ok(clamped, b"ef", true, 6);
    }

    #[test]
    fn identity_mismatch_returns_changed() {
        let reader = fixture_reader(&FixtureConfig::default_with_size(8));

        let first = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 0,
                len: 4,
                handle: None,
            },
        );
        let identity = expect_ok(first, b"abcd", false, 8);
        let changed = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 4,
                len: 4,
                handle: Some(ClipIdentity {
                    first_cluster: identity.first_cluster + 1,
                    total_size: identity.total_size,
                    name_hash: identity.name_hash,
                }),
            },
        );
        assert!(matches!(changed.header, ReadFileHeader::Changed));
    }

    #[test]
    fn checksum_failed_entry_is_not_found() {
        let reader = fixture_reader(&FixtureConfig {
            corrupt_file_checksum: true,
            ..FixtureConfig::default_with_size(8)
        });
        let response = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 0,
                len: 4,
                handle: None,
            },
        );
        assert!(matches!(response.header, ReadFileHeader::NotFound));
    }

    #[test]
    fn torn_intermediate_directory_is_not_found() {
        let reader = fixture_reader(&FixtureConfig {
            corrupt_recent_checksum: true,
            ..FixtureConfig::default_with_size(8)
        });
        let response = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 0,
                len: 4,
                handle: None,
            },
        );
        assert!(matches!(response.header, ReadFileHeader::NotFound));
    }

    #[test]
    fn fat_chain_window_read_ignores_unwritten_tail_clusters() {
        let mut bytes = vec![0_u8; 1024];
        for (i, byte) in bytes.iter_mut().enumerate() {
            *byte = u8::try_from(i % 251).expect("byte range");
        }
        let reader = fixture_reader(&FixtureConfig {
            file_bytes: bytes.clone(),
            valid_data_length: 600,
            data_length: 1536,
            file_no_fat_chain: false,
            fat_entries: vec![(FILE_CLUSTER, FILE_CLUSTER + 1), (FILE_CLUSTER + 1, 0)],
            ..FixtureConfig::default_with_size(600)
        });

        let response = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 500,
                len: 200,
                handle: None,
            },
        );
        let _ = expect_ok(response, &bytes[500..600], true, 600);
    }

    #[test]
    fn identity_changed_between_resolve_and_read_returns_changed() {
        let before_cfg = FixtureConfig::default_with_size(8);
        let before = fixture_image(&before_cfg);
        let after_cfg = FixtureConfig {
            file_first_cluster: FILE_CLUSTER + 1,
            file_bytes: b"ABCDEFGH".to_vec(),
            ..FixtureConfig::default_with_size(8)
        };
        let after = fixture_image(&after_cfg);
        let flip_offset = cluster_offset(START_LBA, FILE_CLUSTER);
        let reader = FlippingReader::new(before, after, flip_offset);

        let response = process_request(
            &reader,
            &ReadFileRequest {
                path: TEST_FILE_PATH.to_owned(),
                offset: 0,
                len: 4,
                handle: None,
            },
        );
        assert!(matches!(response.header, ReadFileHeader::Changed));
    }

    fn expect_ok(reply: ReadReply, expected: &[u8], eof: bool, readable_size: u64) -> ClipIdentity {
        match reply.header {
            ReadFileHeader::Ok {
                identity,
                readable_size: got_size,
                eof: got_eof,
                byte_len,
            } => {
                assert_eq!(got_size, readable_size);
                assert_eq!(got_eof, eof);
                assert_eq!(byte_len as usize, expected.len());
                let body = reply.bytes.expect("ok replies include bytes");
                assert_eq!(body, expected);
                identity
            }
            other => panic!("expected ok header, got {other:?}"),
        }
    }

    #[derive(Clone)]
    struct FixtureConfig {
        file_bytes: Vec<u8>,
        valid_data_length: u64,
        data_length: u64,
        file_first_cluster: u32,
        file_no_fat_chain: bool,
        corrupt_file_checksum: bool,
        corrupt_recent_checksum: bool,
        add_broken_root_dir: bool,
        fat_entries: Vec<(u32, u32)>,
    }

    impl FixtureConfig {
        fn default_with_size(valid_data_length: u64) -> Self {
            Self {
                file_bytes: b"abcdefghij".to_vec(),
                valid_data_length,
                data_length: 10,
                file_first_cluster: FILE_CLUSTER,
                file_no_fat_chain: true,
                corrupt_file_checksum: false,
                corrupt_recent_checksum: false,
                add_broken_root_dir: false,
                fat_entries: Vec::new(),
            }
        }
    }

    fn fixture_reader(config: &FixtureConfig) -> SliceReader {
        SliceReader::new(fixture_image(config))
    }

    fn fixture_image(config: &FixtureConfig) -> Vec<u8> {
        let mut img = vec![0_u8; 8192];

        let mbr = 446;
        img[mbr + 4] = 0x07;
        img[mbr + 8..mbr + 12].copy_from_slice(&START_LBA.to_le_bytes());
        img[mbr + 12..mbr + 16].copy_from_slice(&31_u32.to_le_bytes());
        img[510] = 0x55;
        img[511] = 0xAA;

        let bs = (START_LBA as usize) * CLUSTER_SIZE;
        img[bs..bs + 3].copy_from_slice(&[0xEB, 0x76, 0x90]);
        img[bs + 3..bs + 11].copy_from_slice(b"EXFAT   ");
        img[bs + 64..bs + 72].copy_from_slice(&u64::from(START_LBA).to_le_bytes());
        img[bs + 72..bs + 80].copy_from_slice(&32_u64.to_le_bytes());
        img[bs + 80..bs + 84].copy_from_slice(&1_u32.to_le_bytes());
        img[bs + 84..bs + 88].copy_from_slice(&1_u32.to_le_bytes());
        img[bs + 88..bs + 92].copy_from_slice(&2_u32.to_le_bytes());
        img[bs + 92..bs + 96].copy_from_slice(&16_u32.to_le_bytes());
        img[bs + 96..bs + 100].copy_from_slice(&2_u32.to_le_bytes());
        img[bs + 100..bs + 104].copy_from_slice(&0xCAFE_BABE_u32.to_le_bytes());
        img[bs + 108] = 9;
        img[bs + 109] = 0;
        img[bs + 110] = 1;
        img[bs + 510] = 0x55;
        img[bs + 511] = 0xAA;

        let fat_base = (1 + START_LBA as usize) * CLUSTER_SIZE;
        let root_entry = fat_base + (2 * 4);
        img[root_entry..root_entry + 4].copy_from_slice(&0xFFFF_FFFF_u32.to_le_bytes());
        for &(cluster, value) in &config.fat_entries {
            let entry = fat_base + (cluster as usize * 4);
            img[entry..entry + 4].copy_from_slice(&value.to_le_bytes());
        }

        let upcase = UpcaseTable::ascii_identity();
        let teslacam_dir = encode_entry_set("TeslaCam", true, 3, 512, 512, true, &upcase);
        let mut root_entries = vec![teslacam_dir];
        if config.add_broken_root_dir {
            root_entries.push(encode_entry_set("Broken", true, 5, 512, 512, false, &upcase));
        }
        let mut recent_dir = encode_entry_set("RecentClips", true, 4, 512, 512, true, &upcase);
        if config.corrupt_recent_checksum {
            recent_dir[10] ^= 0xFF;
        }
        let mut file_entry = encode_entry_set(
            TEST_FILE_NAME,
            false,
            config.file_first_cluster,
            config.valid_data_length,
            config.data_length,
            config.file_no_fat_chain,
            &upcase,
        );
        if config.corrupt_file_checksum {
            file_entry[10] ^= 0xFF;
        }

        write_cluster(
            &mut img,
            START_LBA,
            2,
            &directory_cluster(&root_entries, CLUSTER_SIZE),
        );
        write_cluster(
            &mut img,
            START_LBA,
            3,
            &directory_cluster(&[recent_dir], CLUSTER_SIZE),
        );
        write_cluster(
            &mut img,
            START_LBA,
            4,
            &directory_cluster(&[file_entry], CLUSTER_SIZE),
        );
        write_file_payload_clusters(&mut img, START_LBA, config.file_first_cluster, &config.file_bytes);
        img
    }

    fn encode_entry_set(
        name: &str,
        is_directory: bool,
        first_cluster: u32,
        valid_data_length: u64,
        data_length: u64,
        no_fat_chain: bool,
        upcase: &UpcaseTable,
    ) -> Vec<u8> {
        let name_utf16: Vec<u16> = name.encode_utf16().collect();
        let attributes = FileAttributes {
            directory: is_directory,
            archive: !is_directory,
            ..FileAttributes::default()
        };
        encode_file_entry_set(
            &FileEntrySetParams {
                name: &name_utf16,
                attributes,
                timestamps: FileTimestamps::default(),
                first_cluster,
                valid_data_length,
                data_length,
                no_fat_chain,
            },
            upcase,
        )
        .expect("encode entry set")
    }

    fn directory_cluster(entries: &[Vec<u8>], cluster_size: usize) -> Vec<u8> {
        let mut cluster = vec![0_u8; cluster_size];
        let mut offset = 0;
        for entry in entries {
            cluster[offset..offset + entry.len()].copy_from_slice(entry);
            offset += entry.len();
        }
        cluster
    }

    fn write_cluster(img: &mut [u8], start_lba: u32, cluster: u32, payload: &[u8]) {
        let base = usize::try_from(cluster_offset(start_lba, cluster)).expect("cluster offset usize");
        img[base..base + payload.len()].copy_from_slice(payload);
    }

    fn write_file_payload_clusters(img: &mut [u8], start_lba: u32, first_cluster: u32, payload: &[u8]) {
        for (idx, chunk) in payload.chunks(CLUSTER_SIZE).enumerate() {
            let cluster = first_cluster + u32::try_from(idx).expect("cluster index u32");
            write_cluster(img, start_lba, cluster, chunk);
        }
    }

    fn cluster_offset(start_lba: u32, cluster: u32) -> u64 {
        let cluster_index = u64::from(cluster.saturating_sub(2));
        (u64::from(start_lba + 2) * CLUSTER_SIZE as u64) + (cluster_index * CLUSTER_SIZE as u64)
    }

    struct FlippingReader {
        before: Vec<u8>,
        after: Vec<u8>,
        flip_offset: u64,
        flipped: AtomicBool,
    }

    impl FlippingReader {
        fn new(before: Vec<u8>, after: Vec<u8>, flip_offset: u64) -> Self {
            Self {
                before,
                after,
                flip_offset,
                flipped: AtomicBool::new(false),
            }
        }
    }

    impl BlockReader for FlippingReader {
        fn size_bytes(&self) -> u64 {
            u64::try_from(self.before.len()).unwrap_or(u64::MAX)
        }

        fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
            let end = offset
                .checked_add(buf.len() as u64)
                .ok_or(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                })?;
            let backing = if self.flipped.load(Ordering::Acquire) {
                &self.after
            } else {
                &self.before
            };
            let start_usize = usize::try_from(offset).map_err(|_| ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size: self.size_bytes(),
            })?;
            let end_usize = usize::try_from(end).map_err(|_| ReaderError::OutOfRange {
                offset,
                len: buf.len(),
                size: self.size_bytes(),
            })?;
            let Some(src) = backing.get(start_usize..end_usize) else {
                return Err(ReaderError::OutOfRange {
                    offset,
                    len: buf.len(),
                    size: self.size_bytes(),
                });
            };
            buf.copy_from_slice(src);
            if offset == self.flip_offset {
                self.flipped.store(true, Ordering::Release);
            }
            Ok(())
        }
    }
}
