//! Live Unix seam implementations for `retentiond`.
//!
//! This module is intentionally bin-internal; the pure policy core remains in
//! the library.
#![allow(unsafe_code)]
#![allow(dead_code)]

use std::ffi::CString;
use std::fs::File;
use std::io::{self, Read};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use retentiond::delete::RandGen;
use retentiond::governor::Statfs;
use retentiond::io::FsStat;
use retentiond::time::{BootId, Clock, MonoMs};

pub(crate) struct LiveClock;

impl Clock for LiveClock {
    fn mono_now(&self) -> MonoMs {
        // SAFETY: `timespec` is plain old data and zero-init is valid before
        // passing a mutable pointer to `clock_gettime`.
        let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
        // SAFETY: `CLOCK_MONOTONIC` is a valid clock id and `ts` points to
        // writable memory for the syscall output.
        let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
        debug_assert_eq!(
            rc, 0,
            "clock_gettime(CLOCK_MONOTONIC) should not fail with valid args"
        );
        if rc != 0 {
            // Unreachable in normal Linux operation with valid arguments; trait
            // cannot return Result, so preserve a deterministic fallback.
            return MonoMs(0);
        }

        let sec_ms = i128::from(ts.tv_sec).saturating_mul(1_000);
        let nsec_ms = i128::from(ts.tv_nsec) / 1_000_000;
        let total_ms = sec_ms.saturating_add(nsec_ms);
        MonoMs(i128_to_i64_clamped(total_ms))
    }

    fn boot_id(&self) -> BootId {
        match std::fs::read_to_string("/proc/sys/kernel/random/boot_id") {
            Ok(s) => BootId(s.trim().to_owned()),
            Err(_) => BootId("unknown-boot".to_owned()),
        }
    }
}

pub(crate) struct LiveRand;

impl RandGen for LiveRand {
    fn next_u128(&self) -> u128 {
        if let Some(value) = getrandom_u128() {
            return value;
        }
        if let Some(value) = urandom_u128() {
            return value;
        }
        fallback_random_u128()
    }
}

static FALLBACK_COUNTER: AtomicU64 = AtomicU64::new(0);
static LAST_RESORT_WARNED: AtomicBool = AtomicBool::new(false);

fn getrandom_u128() -> Option<u128> {
    const MAX_RETRIES: usize = 256;
    let mut bytes = [0_u8; 16];
    let mut filled = 0usize;

    for _ in 0..MAX_RETRIES {
        if filled == bytes.len() {
            return Some(u128::from_le_bytes(bytes));
        }
        // SAFETY: `filled <= bytes.len()` and pointer is advanced only within
        // the destination buffer's bounds.
        let ptr = unsafe { bytes.as_mut_ptr().add(filled) }.cast::<libc::c_void>();
        let remaining = bytes.len() - filled;
        // SAFETY: `ptr` targets `remaining` writable bytes in `bytes`.
        let n = unsafe { libc::getrandom(ptr, remaining, 0) };
        if n > 0 {
            let Ok(read) = usize::try_from(n) else {
                break;
            };
            filled += read;
            continue;
        }
        if n == -1 {
            let err = io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EINTR) {
                continue;
            }
        }
        break;
    }

    if filled == bytes.len() {
        Some(u128::from_le_bytes(bytes))
    } else {
        None
    }
}

fn urandom_u128() -> Option<u128> {
    let mut bytes = [0_u8; 16];
    let mut file = File::open("/dev/urandom").ok()?;
    file.read_exact(&mut bytes).ok()?;
    Some(u128::from_le_bytes(bytes))
}

fn fallback_random_u128() -> u128 {
    if LAST_RESORT_WARNED
        .compare_exchange(false, true, Ordering::Relaxed, Ordering::Relaxed)
        .is_ok()
    {
        eprintln!(
            "retentiond: WARNING: OS CSPRNG unavailable (getrandom and /dev/urandom failed); \
using degraded best-effort random token fallback"
        );
    }
    // RandGen cannot return Result and is currently consumed only by the Phase-2
    // delete path (not yet wired), so this ASLR/PID/counter mix avoids wall-clock
    // derivation across boots; true fail-closed is deferred to a trait change.
    let counter = FALLBACK_COUNTER.fetch_add(1, Ordering::Relaxed);
    let pid = u64::from(std::process::id());
    let probe = 0_u8;
    let addr = std::ptr::addr_of!(probe) as usize as u64;
    let lo = splitmix64(
        counter
            ^ pid.rotate_left(13)
            ^ addr.rotate_left(29)
            ^ 0xa076_1d64_78bd_642f,
    );
    let hi = splitmix64(
        counter
            .wrapping_add(0x9e37_79b9_7f4a_7c15)
            ^ pid.rotate_left(41)
            ^ addr.rotate_left(7)
            ^ 0xe703_7ed1_a0b4_28db,
    );
    (u128::from(hi) << 64) | u128::from(lo)
}

fn splitmix64(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    x ^ (x >> 31)
}

fn i128_to_i64_clamped(value: i128) -> i64 {
    if value > i128::from(i64::MAX) {
        i64::MAX
    } else if value < i128::from(i64::MIN) {
        i64::MIN
    } else {
        match i64::try_from(value) {
            Ok(v) => v,
            Err(_) => i64::MIN,
        }
    }
}

fn to_u64_saturating<T>(value: T) -> u64
where
    u64: TryFrom<T>,
{
    match u64::try_from(value) {
        Ok(v) => v,
        Err(_) => u64::MAX,
    }
}

pub(crate) struct LiveStatfs;

impl Statfs for LiveStatfs {
    fn statfs(&self, path: &str) -> io::Result<FsStat> {
        let c_path = CString::new(path).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "path contains interior NUL byte")
        })?;

        // SAFETY: `stat` is POD and may be zero-initialized before syscall fill.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: `c_path` is a valid NUL-terminated C string and `st` is a valid
        // output pointer for `stat(2)`.
        if unsafe { libc::stat(c_path.as_ptr(), &mut st) } != 0 {
            return Err(io::Error::last_os_error());
        }

        // SAFETY: `statvfs` is POD and may be zero-initialized before syscall fill.
        let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
        // SAFETY: `c_path` is valid and `s` is writable output storage.
        if unsafe { libc::statvfs(c_path.as_ptr(), &mut s) } != 0 {
            return Err(io::Error::last_os_error());
        }

        let frsize = if s.f_frsize == 0 { s.f_bsize } else { s.f_frsize };
        let frsize = to_u64_saturating(frsize);

        Ok(FsStat {
            dev_id: to_u64_saturating(st.st_dev),
            free_bytes: to_u64_saturating(s.f_bavail).saturating_mul(frsize),
            total_bytes: to_u64_saturating(s.f_blocks).saturating_mul(frsize),
            free_inodes: to_u64_saturating(s.f_favail),
            total_inodes: to_u64_saturating(s.f_files),
        })
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::{LiveClock, LiveRand, LiveStatfs};
    use retentiond::delete::RandGen;
    use retentiond::governor::Statfs;
    use retentiond::time::Clock;

    #[test]
    fn live_clock_is_monotonic() {
        let clock = LiveClock;
        let first = clock.mono_now();
        let second = clock.mono_now();
        assert!(
            second.0 >= first.0,
            "monotonic clock regressed: first={} second={}",
            first.0,
            second.0
        );
    }

    #[test]
    fn live_clock_boot_id_nonempty() {
        let clock = LiveClock;
        let first = clock.boot_id();
        let second = clock.boot_id();
        assert!(!first.0.is_empty(), "boot id should not be empty");
        assert_eq!(first.0, second.0, "boot id should be stable in-process");
    }

    #[test]
    fn live_rand_differs_and_nonzero() {
        let rand = LiveRand;
        let first = rand.next_u128();
        let second = rand.next_u128();
        assert_ne!(first, second, "two random reads should differ");
        assert!(
            !(first == 0 && second == 0),
            "two random reads should not both be zero"
        );
    }

    #[test]
    fn live_statfs_root_ok() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("/");
        assert!(result.is_ok(), "statfs('/') should succeed");
        let Ok(stat) = result else {
            return;
        };
        assert!(stat.total_bytes > 0, "root total bytes should be > 0");
        assert!(stat.dev_id != 0, "root dev id should be non-zero");
    }

    #[test]
    fn live_statfs_bad_path_err() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("/nonexistent/teslausb/zzz");
        assert!(result.is_err(), "missing path should return an error");
    }

    #[test]
    fn live_statfs_interior_nul_err() {
        let statfs = LiveStatfs;
        let result = statfs.statfs("a\0b");
        assert!(result.is_err(), "interior NUL path should return an error");
    }
}
