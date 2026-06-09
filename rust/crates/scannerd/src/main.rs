//! `scannerd` binary: opens the backing image, parses MBR → exFAT →
//! directory tree entirely via raw positioned reads, then probes and
//! SEI-scans every clip. This is also the **spike 2.4 / 2.5 vehicle**:
//! run once over a real Tesla-written exFAT image to prove the raw read
//! path enumerates and decodes real footage without mounting anything.
//!
//! Usage:
//!   scannerd <image-path>            single full scan + report
//!   scannerd <image-path> --watch <interval_secs> <iterations>
//!                                    exercise the cross-scan stability
//!                                    gate, emitting newly-stable clips

#![allow(clippy::print_stdout, clippy::print_stderr, clippy::doc_markdown)]

use std::process::ExitCode;

#[cfg(unix)]
mod io;

#[cfg(unix)]
mod serve;

#[cfg(not(unix))]
fn main() -> ExitCode {
    eprintln!("scannerd: this binary runs on Linux (the Pi) only");
    ExitCode::FAILURE
}

#[cfg(unix)]
fn main() -> ExitCode {
    unix_app::run()
}

#[cfg(unix)]
mod unix_app {
    use std::collections::BTreeMap;
    use std::path::Path;
    use std::process::ExitCode;
    use std::time::{SystemTime, UNIX_EPOCH};

    use scannerd::boot::parse_boot_sector;
    use scannerd::clip::parse_clip_name;
    use scannerd::error::ScannerError;
    use scannerd::mbr::parse_mbr;
    use scannerd::mp4probe::{Codec, probe_mp4};
    use scannerd::seiscan::scan_sei;
    use scannerd::stability::{StabilityConfig, StabilityTracker};
    use scannerd::volume::Volume;
    use scannerd::walk::{FileRecord, walk_volume};

    use crate::io::PreadReader;

    /// Per-clip aggregate for the report.
    #[derive(Default)]
    struct ClipAgg {
        angles: u32,
        total_bytes: u64,
        h264: u32,
        hevc: u32,
        unknown_codec: u32,
        incomplete: u32,
        sei_decoded: u64,
        sei_errors: u64,
        gps_fix: u64,
        lat_min: f64,
        lat_max: f64,
        lon_min: f64,
        lon_max: f64,
    }

    /// Run the binary.
    pub fn run() -> ExitCode {
        let args: Vec<String> = std::env::args().collect();

        // `serve` is the production daemon mode: bind the IPC socket and
        // stream facts to indexd. The bare `<image>` / `--watch` modes
        // remain the read-only diagnostic spike vehicles.
        if args.get(1).map(String::as_str) == Some("serve") {
            return crate::serve::run_serve(&args);
        }

        let Some(path) = args.get(1) else {
            eprintln!(
                "usage: scannerd <image-path> [--watch <interval_secs> <iterations>]\n       \
                 scannerd serve <image-path> [--socket <path>] [--sample-rate <n>]"
            );
            return ExitCode::FAILURE;
        };

        let reader = match PreadReader::open(Path::new(path)) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("scannerd: cannot open {path}: {e}");
                return ExitCode::FAILURE;
            }
        };

        if args.get(2).map(String::as_str) == Some("--watch") {
            return run_watch(&reader, &args);
        }

        match scan_once(&reader) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("scannerd: {e}");
                ExitCode::FAILURE
            }
        }
    }

    /// Enumerate every exFAT volume once and report.
    fn scan_once(reader: &PreadReader) -> Result<(), ScannerError> {
        let partitions = parse_mbr(reader)?;
        println!("== scannerd raw scan ==");
        println!("image_bytes = {}", reader_size(reader));
        let mut any = false;
        for entry in partitions.iter().filter(|p| p.is_exfat()) {
            any = true;
            scan_partition(reader, entry.slot, entry.start_lba)?;
        }
        if !any {
            println!("(no exFAT partitions found)");
        }
        Ok(())
    }

    /// Parse + walk + probe one exFAT partition.
    fn scan_partition(reader: &PreadReader, slot: u8, start_lba: u32) -> Result<(), ScannerError> {
        let params = parse_boot_sector(reader, start_lba)?;
        println!(
            "\n-- partition slot {slot}: start_lba={start_lba} \
             bytes_per_cluster={} cluster_count={} first_root_cluster={}",
            params.bytes_per_cluster(),
            params.cluster_count,
            params.first_root_cluster
        );
        let volume = Volume::new(reader, params);
        let records = walk_volume(&volume, slot)?;
        let mp4s: Vec<&FileRecord> = records
            .iter()
            .filter(|r| r.name.to_ascii_lowercase().ends_with(".mp4"))
            .collect();
        println!(
            "files_total = {}  mp4_files = {}",
            records.len(),
            mp4s.len()
        );

        let mut clips: BTreeMap<String, ClipAgg> = BTreeMap::new();
        let mut total_decoded = 0u64;
        let mut total_errors = 0u64;
        for record in mp4s {
            probe_one(
                &volume,
                record,
                &mut clips,
                &mut total_decoded,
                &mut total_errors,
            );
        }

        println!("clips = {}", clips.len());
        for (ts, agg) in &clips {
            print_clip_line(ts, agg);
        }
        println!(
            "VOLUME TOTALS: clips={} sei_decoded={total_decoded} sei_errors={total_errors}",
            clips.len()
        );
        Ok(())
    }

    /// Read one clip raw, probe it, and fold into its clip aggregate.
    fn probe_one(
        volume: &Volume<'_, PreadReader>,
        record: &FileRecord,
        clips: &mut BTreeMap<String, ClipAgg>,
        total_decoded: &mut u64,
        total_errors: &mut u64,
    ) {
        let Some(parsed) = parse_clip_name(&record.name) else {
            return;
        };
        let bytes = match read_clip_bytes(volume, record) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("  ! {}: read error: {e}", record.path);
                return;
            }
        };
        let probe = probe_mp4(&bytes);
        let sei = scan_sei(&bytes);
        *total_decoded += sei.decoded;
        *total_errors += sei.decode_errors;

        let agg = clips.entry(parsed.timestamp).or_insert_with(new_agg);
        agg.angles += 1;
        agg.total_bytes += bytes.len() as u64;
        match probe.codec {
            Codec::H264 => agg.h264 += 1,
            Codec::Hevc => agg.hevc += 1,
            Codec::Unknown => agg.unknown_codec += 1,
        }
        if !probe.complete {
            agg.incomplete += 1;
        }
        agg.sei_decoded += sei.decoded;
        agg.sei_errors += sei.decode_errors;
        agg.gps_fix += sei.gps_fix_count;
        for s in sei.samples.iter().filter(|s| s.has_gps_fix) {
            agg.lat_min = agg.lat_min.min(s.latitude_deg);
            agg.lat_max = agg.lat_max.max(s.latitude_deg);
            agg.lon_min = agg.lon_min.min(s.longitude_deg);
            agg.lon_max = agg.lon_max.max(s.longitude_deg);
        }
    }

    /// A clip aggregate seeded with empty (infinite) GPS bounds.
    fn new_agg() -> ClipAgg {
        ClipAgg {
            lat_min: f64::INFINITY,
            lat_max: f64::NEG_INFINITY,
            lon_min: f64::INFINITY,
            lon_max: f64::NEG_INFINITY,
            ..ClipAgg::default()
        }
    }

    /// Print one clip's summary line.
    fn print_clip_line(ts: &str, agg: &ClipAgg) {
        let gps = if agg.gps_fix > 0 {
            format!(
                "gps[{:.5}..{:.5},{:.5}..{:.5}]",
                agg.lat_min, agg.lat_max, agg.lon_min, agg.lon_max
            )
        } else {
            "gps[none]".to_owned()
        };
        println!(
            "  {ts}  angles={} bytes={} h264={} hevc={} unk={} incomplete={} \
             sei_ok={} sei_err={} gps_fix={} {gps}",
            agg.angles,
            agg.total_bytes,
            agg.h264,
            agg.hevc,
            agg.unknown_codec,
            agg.incomplete,
            agg.sei_decoded,
            agg.sei_errors,
            agg.gps_fix,
        );
    }

    /// Read a clip's bytes up to `ValidDataLength` via its cluster chain.
    fn read_clip_bytes(
        volume: &Volume<'_, PreadReader>,
        record: &FileRecord,
    ) -> Result<Vec<u8>, ScannerError> {
        if record.valid_data_length == 0 {
            return Ok(Vec::new());
        }
        let bpc = volume.params().bytes_per_cluster();
        let span = record.valid_data_length.div_ceil(bpc);
        let clusters = volume.follow_chain(record.first_cluster, record.no_fat_chain, span)?;
        let len = usize::try_from(record.valid_data_length).unwrap_or(usize::MAX);
        volume.read_file_range(&clusters, 0, len)
    }

    /// `--watch` mode: scan repeatedly to exercise the stability gate.
    fn run_watch(reader: &PreadReader, args: &[String]) -> ExitCode {
        let interval = args
            .get(3)
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(60);
        let iters = args.get(4).and_then(|s| s.parse::<u32>().ok()).unwrap_or(3);
        let mut tracker = StabilityTracker::new(StabilityConfig::default());
        for i in 0..iters {
            match watch_pass(reader, &mut tracker) {
                Ok(emitted) => println!("scan {i}: newly_stable={emitted}"),
                Err(e) => {
                    eprintln!("scannerd: scan {i} failed: {e}");
                    return ExitCode::FAILURE;
                }
            }
            if i + 1 < iters {
                std::thread::sleep(std::time::Duration::from_secs(interval));
            }
        }
        ExitCode::SUCCESS
    }

    /// One watch pass: walk all exFAT volumes, feed the tracker, count
    /// newly-eligible clips.
    fn watch_pass(
        reader: &PreadReader,
        tracker: &mut StabilityTracker,
    ) -> Result<usize, ScannerError> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let partitions = parse_mbr(reader)?;
        let mut all: Vec<FileRecord> = Vec::new();
        for entry in partitions.iter().filter(|p| p.is_exfat()) {
            let params = parse_boot_sector(reader, entry.start_lba)?;
            let volume = Volume::new(reader, params);
            all.extend(walk_volume(&volume, entry.slot)?);
        }
        Ok(tracker.observe(&all, now).len())
    }

    /// Backing image size for the report.
    fn reader_size(reader: &PreadReader) -> u64 {
        use scannerd::reader::BlockReader;
        reader.size_bytes()
    }
}
