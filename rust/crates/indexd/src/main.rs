//! `indexd` binary entry point.
//!
//! Wires the scan→derive→persist loop on the Pi. The heavy lifting lives
//! in the library (`indexd::*`); this binary is a thin host for it. A
//! binary may relax the `print_*` lints (like `gadgetd`/`scannerd`) but
//! NOT `unwrap_used`.

#![allow(clippy::print_stdout, clippy::print_stderr)]

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Mutex;
use std::thread::sleep;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use indexd::db::mutations::BootContext;
use indexd::db::{DbError, open};
use indexd::scan::{ScanConfig, run_scan_pass};
use scannerd::reader::{BlockReader, ReaderError};
use scannerd::stability::{StabilityConfig, StabilityTracker};

/// Default on-Pi DB path. ext4, Pi-side — NEVER inside `disk.img` / the
/// Tesla volume (SPEC §6.1 #1 invariant).
const DEFAULT_DB_PATH: &str = "/var/lib/teslausb/index.sqlite3";

/// Seconds between scan passes. Two stable observations spaced by the
/// quiescence window gate a clip in.
const SCAN_INTERVAL_SECS: u64 = 30;

/// A file-backed [`BlockReader`] over the raw USB backing image (the
/// `disk.img` LUN block device). Reads only — `indexd` never writes the
/// car volume. `pread`-equivalent via seek+read under a mutex (the trait
/// takes `&self`).
struct FileReader {
    inner: Mutex<File>,
    size: u64,
}

impl FileReader {
    fn open(path: &str) -> std::io::Result<Self> {
        let file = File::open(path)?;
        let size = file.metadata()?.len();
        Ok(Self {
            inner: Mutex::new(file),
            size,
        })
    }
}

impl BlockReader for FileReader {
    fn size_bytes(&self) -> u64 {
        self.size
    }

    fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> Result<(), ReaderError> {
        let len = buf.len();
        let mut guard = self.inner.lock().map_err(|_| ReaderError::Io {
            offset,
            len,
            source_msg: "backing file mutex poisoned".to_owned(),
        })?;
        guard
            .seek(SeekFrom::Start(offset))
            .map_err(|e| ReaderError::Io {
                offset,
                len,
                source_msg: e.to_string(),
            })?;
        guard.read_exact(buf).map_err(|e| ReaderError::Io {
            offset,
            len,
            source_msg: e.to_string(),
        })?;
        Ok(())
    }
}

/// Resolve config from args/env: `argv[1]` (or `INDEXD_DB`) = DB path;
/// `argv[2]` (or `INDEXD_IMAGE`) = raw backing-image path.
fn resolve_paths() -> Result<(PathBuf, PathBuf), String> {
    let mut args = std::env::args().skip(1);
    let db = args
        .next()
        .or_else(|| std::env::var("INDEXD_DB").ok())
        .unwrap_or_else(|| DEFAULT_DB_PATH.to_owned());
    let image = args
        .next()
        .or_else(|| std::env::var("INDEXD_IMAGE").ok())
        .ok_or_else(|| "missing backing-image path (argv[2] or INDEXD_IMAGE)".to_owned())?;
    Ok((PathBuf::from(db), PathBuf::from(image)))
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

fn run() -> Result<(), String> {
    let (db_path, image_path) = resolve_paths()?;
    let db_display = db_path.display().to_string();
    let mut conn = open(&db_path).map_err(|e: DbError| format!("opening {db_display}: {e}"))?;

    // Single-writer hygiene: reap leases stranded by a previous boot.
    let boot = BootContext::new();
    let reaped = boot
        .reap(&conn)
        .map_err(|e| format!("reaping stale leases: {e}"))?;
    println!(
        "indexd: boot {} ; reaped {reaped} stale lease(s)",
        boot.boot_id()
    );

    let image = image_path.display().to_string();
    let reader = FileReader::open(&image).map_err(|e| format!("opening image {image}: {e}"))?;

    let mut tracker = StabilityTracker::new(StabilityConfig::default());
    let config = ScanConfig::default();

    println!("indexd: watching {image} → {db_display}");
    loop {
        match run_scan_pass(&reader, &mut conn, &mut tracker, now_secs(), config) {
            Ok(report) => println!(
                "indexd: pass — {} files, {} eligible, {} clips, {} trips, {} events, {} pruned, {} errors",
                report.files_seen,
                report.eligible,
                report.clips_upserted,
                report.trips,
                report.events,
                report.pruned,
                report.errors,
            ),
            Err(e) => eprintln!("indexd: scan pass failed: {e}"),
        }
        sleep(Duration::from_secs(SCAN_INTERVAL_SECS));
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("indexd: fatal: {e}");
            ExitCode::FAILURE
        }
    }
}
