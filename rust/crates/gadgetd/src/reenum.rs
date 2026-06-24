use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::config;
use crate::config::GadgetConfig;
use crate::exec;

const PROC_ROOT: &str = "/proc";
const UDC_SYSFS: &str = "/sys/class/udc";

pub(crate) const REENUM_GATE_WINDOW: Duration = Duration::from_millis(700);
pub(crate) const REENUM_GATE_DEADLINE: Duration = Duration::from_secs(10);
const REENUM_GATE_POLL: Duration = Duration::from_millis(200);
const REENUM_FINAL_RECHECK_WINDOW: Duration = Duration::from_millis(200);
const REENUM_VERIFY_POLL: Duration = Duration::from_millis(100);
pub(crate) const REENUM_HOLD: Duration = Duration::from_millis(700);
pub(crate) const REENUM_VERIFY_DEADLINE: Duration = Duration::from_secs(5);
const UDC_REBIND_SETTLE: Duration = Duration::from_secs(2);

pub(crate) trait WriteActivity {
    fn sample(&self) -> io::Result<Option<u64>>;
}

pub(crate) struct ProcWriteActivity;

impl WriteActivity for ProcWriteActivity {
    fn sample(&self) -> io::Result<Option<u64>> {
        let Some(pid) = find_file_storage_pid()? else {
            return Ok(None);
        };
        match read_wchar(pid) {
            Ok(value) => Ok(Some(value)),
            Err(_) => Ok(None),
        }
    }
}

pub(crate) fn find_file_storage_pid() -> io::Result<Option<u32>> {
    for entry in std::fs::read_dir(PROC_ROOT)? {
        let Ok(entry) = entry else {
            continue;
        };
        let name = entry.file_name();
        let Some(raw) = name.to_str() else {
            continue;
        };
        let Ok(pid) = raw.parse::<u32>() else {
            continue;
        };
        let comm = entry.path().join("comm");
        let Ok(content) = std::fs::read_to_string(comm) else {
            continue;
        };
        if content.trim() == "file-storage" {
            return Ok(Some(pid));
        }
    }
    Ok(None)
}

pub(crate) fn read_wchar(pid: u32) -> io::Result<u64> {
    let path = PathBuf::from(PROC_ROOT).join(pid.to_string()).join("io");
    let text = std::fs::read_to_string(path)?;
    let value = text
        .lines()
        .find_map(|line| line.strip_prefix("wchar:"))
        .ok_or_else(|| io::Error::other("missing wchar in /proc/<pid>/io"))?;
    let parsed = value
        .trim()
        .parse::<u64>()
        .map_err(|e| io::Error::other(format!("invalid wchar value: {e}")))?;
    Ok(parsed)
}

pub(crate) fn is_recording_idle<A: WriteActivity>(
    act: &A,
    sleep: &dyn Fn(Duration),
    window: Duration,
) -> bool {
    let Ok(Some(first)) = act.sample() else {
        return false;
    };
    sleep(window);
    let Ok(Some(second)) = act.sample() else {
        return false;
    };
    first == second
}

pub(crate) fn soft_connect_path(udc: &str) -> PathBuf {
    PathBuf::from(UDC_SYSFS).join(udc).join("soft_connect")
}

pub(crate) fn set_soft_connect(udc: &str, connect: bool) -> io::Result<()> {
    // Kernel source-of-truth: usb_udc_softconn_store accepts "connect"/"disconnect".
    let value = if connect { "connect" } else { "disconnect" };
    std::fs::write(soft_connect_path(udc), value)
}

pub(crate) trait SoftConnectWriter {
    fn set(&self, connect: bool) -> io::Result<()>;
}

pub(crate) struct SysfsSoftConnectWriter {
    udc: String,
}

impl SysfsSoftConnectWriter {
    fn new(udc: String) -> Self {
        Self { udc }
    }
}

impl SoftConnectWriter for SysfsSoftConnectWriter {
    fn set(&self, connect: bool) -> io::Result<()> {
        set_soft_connect(&self.udc, connect)
    }
}

pub(crate) trait UdcRebinder {
    fn rebind(&self) -> io::Result<()>;
}

pub(crate) struct SysfsUdcRebinder {
    udc_path: PathBuf,
    udc: String,
}

impl SysfsUdcRebinder {
    fn new(udc_path: PathBuf, udc: String) -> Self {
        Self { udc_path, udc }
    }
}

impl UdcRebinder for SysfsUdcRebinder {
    fn rebind(&self) -> io::Result<()> {
        std::fs::write(&self.udc_path, &self.udc)
    }
}

pub(crate) struct ConnectGuard<'a, W: SoftConnectWriter> {
    writer: &'a W,
    armed: bool,
}

impl<'a, W: SoftConnectWriter> ConnectGuard<'a, W> {
    pub(crate) fn new(writer: &'a W) -> Self {
        Self {
            writer,
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl<W: SoftConnectWriter> Drop for ConnectGuard<'_, W> {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.writer.set(true);
        }
    }
}

pub(crate) struct UdcRebindGuard<'a, R: UdcRebinder> {
    rebinder: &'a R,
    armed: bool,
}

impl<'a, R: UdcRebinder> UdcRebindGuard<'a, R> {
    pub(crate) fn new(rebinder: &'a R) -> Self {
        Self {
            rebinder,
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl<R: UdcRebinder> Drop for UdcRebindGuard<'_, R> {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.rebinder.rebind();
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) enum ReEnumMethod {
    #[default]
    SoftConnect,
    UdcRebind,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct ReEnumOpts {
    pub(crate) reason: Option<String>,
    pub(crate) force: bool,
    pub(crate) method: ReEnumMethod,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ReEnumOutcome {
    Done { disconnect_ms: u64 },
    Deferred { reason: String },
    Failed { detail: String },
}

fn wait_for_recording_idle<A: WriteActivity>(
    act: &A,
    sleep: &dyn Fn(Duration),
    elapsed: &dyn Fn() -> Duration,
) -> bool {
    loop {
        if is_recording_idle(act, sleep, REENUM_GATE_WINDOW) {
            return true;
        }
        if elapsed() >= REENUM_GATE_DEADLINE {
            return false;
        }
        sleep(REENUM_GATE_POLL);
    }
}

fn execute_flow<A>(
    act: &A,
    opts: &ReEnumOpts,
    sleep: &dyn Fn(Duration),
    elapsed: &dyn Fn() -> Duration,
    fsync_images: &dyn Fn() -> io::Result<()>,
    perform_blip: &dyn Fn() -> io::Result<u64>,
) -> ReEnumOutcome
where
    A: WriteActivity,
{
    if !opts.force && !wait_for_recording_idle(act, sleep, elapsed) {
        return ReEnumOutcome::Deferred {
            reason: "recording_active".to_owned(),
        };
    }

    if let Err(e) = fsync_images() {
        return ReEnumOutcome::Failed {
            detail: format!("fdatasync failed: {e}"),
        };
    }

    if !opts.force && !is_recording_idle(act, sleep, REENUM_FINAL_RECHECK_WINDOW) {
        return ReEnumOutcome::Deferred {
            reason: "recording_active".to_owned(),
        };
    }

    match perform_blip() {
        Ok(disconnect_ms) => ReEnumOutcome::Done { disconnect_ms },
        Err(e) => ReEnumOutcome::Failed {
            detail: e.to_string(),
        },
    }
}

fn fdatasync_path(path: &Path) -> io::Result<()> {
    let file = std::fs::File::open(path)?;
    file.sync_data()
}

fn fsync_reenum_images(cfg: &GadgetConfig) -> io::Result<()> {
    for lun in [config::TESLACAM_LUN, config::MEDIA_LUN] {
        let path = cfg.image_for_lun(lun);
        fdatasync_path(path)
            .map_err(|e| io::Error::other(format!("fdatasync {}: {e}", path.display())))?;
    }
    Ok(())
}

fn reenum_soft_connect(udc: &str, sleep: &dyn Fn(Duration)) -> io::Result<u64> {
    let writer = SysfsSoftConnectWriter::new(udc.to_owned());
    let mut guard = ConnectGuard::new(&writer);
    writer.set(false)?;
    let start = Instant::now();
    sleep(REENUM_HOLD);
    writer.set(true)?;
    guard.disarm();
    let millis = start.elapsed().as_millis();
    let disconnect_ms = u64::try_from(millis).unwrap_or(u64::MAX);
    Ok(disconnect_ms)
}

fn reenum_udc_rebind(cfg: &GadgetConfig, udc: &str, sleep: &dyn Fn(Duration)) -> io::Result<u64> {
    let start = Instant::now();
    let rebinder = SysfsUdcRebinder::new(cfg.udc_path(), udc.to_owned());
    // Startup self-heal in ipc.rs re-binds a configured-but-unbound gadget on the
    // next gadgetd-control restart (Restart=always). But the daemon stays ALIVE if
    // the rebind write below fails in-process, so guard it: best-effort re-bind on
    // drop covers a failed/early-return rebind so the gadget never stays unbound.
    std::fs::write(cfg.udc_path(), "\n")?;
    let mut guard = UdcRebindGuard::new(&rebinder);
    sleep(REENUM_HOLD);
    rebinder.rebind()?;
    guard.disarm();
    sleep(UDC_REBIND_SETTLE);
    let millis = start.elapsed().as_millis();
    Ok(u64::try_from(millis).unwrap_or(u64::MAX))
}

fn verify_recovery(
    cfg: &GadgetConfig,
    sleep: &dyn Fn(Duration),
    elapsed: &dyn Fn() -> Duration,
) -> io::Result<()> {
    loop {
        let status = exec::read_status(cfg);
        let configured = status.udc_state.as_deref() == Some("configured");
        let bound = status
            .bound_udc
            .as_deref()
            .is_some_and(|udc| !udc.is_empty());
        let lun0_ok = status.lun_file.as_deref().is_some_and(|f| !f.is_empty());
        let lun1_ok = status
            .media_lun_file
            .as_deref()
            .is_some_and(|f| !f.is_empty());
        let file_storage_alive = find_file_storage_pid().ok().flatten().is_some();

        if configured && bound && lun0_ok && lun1_ok && file_storage_alive {
            return Ok(());
        }
        if elapsed() >= REENUM_VERIFY_DEADLINE {
            return Err(io::Error::other("recovery verification timed out"));
        }
        sleep(REENUM_VERIFY_POLL);
    }
}

pub(crate) fn reenumerate(cfg: &GadgetConfig, opts: &ReEnumOpts) -> ReEnumOutcome {
    let udc = match exec::detect_udc(None) {
        Ok(name) => name,
        Err(e) => {
            return ReEnumOutcome::Failed {
                detail: format!("resolve udc: {e}"),
            };
        }
    };

    if let Some(reason) = opts.reason.as_deref() {
        eprintln!("gadgetd reenumerate: reason={reason}");
    }

    let activity = ProcWriteActivity;
    let start = Instant::now();
    let sleep = |d: Duration| std::thread::sleep(d);

    let method = opts.method;
    execute_flow(
        &activity,
        opts,
        &sleep,
        &|| start.elapsed(),
        &|| fsync_reenum_images(cfg),
        &|| {
            let disconnect_ms = match method {
                ReEnumMethod::SoftConnect => reenum_soft_connect(&udc, &sleep)?,
                ReEnumMethod::UdcRebind => reenum_udc_rebind(cfg, &udc, &sleep)?,
            };
            let verify_start = Instant::now();
            if let Err(e) = verify_recovery(cfg, &sleep, &|| verify_start.elapsed()) {
                let _ = set_soft_connect(&udc, true);
                return Err(e);
            }
            Ok(disconnect_ms)
        },
    )
}

#[cfg(test)]
#[allow(clippy::panic, clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::{
        ConnectGuard, REENUM_GATE_DEADLINE, ReEnumMethod, ReEnumOpts, ReEnumOutcome,
        SoftConnectWriter, UdcRebindGuard, UdcRebinder, WriteActivity, execute_flow,
        is_recording_idle,
    };
    use std::cell::{Cell, RefCell};
    use std::io;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    struct SeqActivity {
        samples: RefCell<Vec<io::Result<Option<u64>>>>,
    }

    impl SeqActivity {
        fn new(samples: Vec<io::Result<Option<u64>>>) -> Self {
            Self {
                samples: RefCell::new(samples.into_iter().rev().collect()),
            }
        }
    }

    impl WriteActivity for SeqActivity {
        fn sample(&self) -> io::Result<Option<u64>> {
            self.samples.borrow_mut().pop().unwrap_or(Ok(Some(0)))
        }
    }

    struct CountingActivity {
        value: Cell<u64>,
    }

    impl WriteActivity for CountingActivity {
        fn sample(&self) -> io::Result<Option<u64>> {
            let next = self.value.get().saturating_add(1);
            self.value.set(next);
            Ok(Some(next))
        }
    }

    #[test]
    fn is_recording_idle_true_when_wchar_flat() {
        let act = SeqActivity::new(vec![Ok(Some(5)), Ok(Some(5))]);
        assert!(is_recording_idle(&act, &|_| {}, Duration::from_millis(1)));
    }

    #[test]
    fn is_recording_idle_false_when_wchar_grows() {
        let act = SeqActivity::new(vec![Ok(Some(5)), Ok(Some(6))]);
        assert!(!is_recording_idle(&act, &|_| {}, Duration::from_millis(1)));
    }

    #[test]
    fn is_recording_idle_false_on_none_or_err() {
        let none_first = SeqActivity::new(vec![Ok(None), Ok(Some(5))]);
        assert!(!is_recording_idle(
            &none_first,
            &|_| {},
            Duration::from_millis(1)
        ));

        let err_second = SeqActivity::new(vec![Ok(Some(5)), Err(io::Error::other("bad"))]);
        assert!(!is_recording_idle(
            &err_second,
            &|_| {},
            Duration::from_millis(1)
        ));
    }

    #[test]
    fn gate_defers_when_never_idle_by_deadline() {
        let act = CountingActivity {
            value: Cell::new(0),
        };
        let elapsed = Cell::new(Duration::ZERO);
        let out = execute_flow(
            &act,
            &ReEnumOpts {
                reason: None,
                force: false,
                method: ReEnumMethod::SoftConnect,
            },
            &|d| elapsed.set(elapsed.get() + d),
            &|| elapsed.get(),
            &|| Ok(()),
            &|| Ok(1),
        );
        assert_eq!(
            out,
            ReEnumOutcome::Deferred {
                reason: "recording_active".to_owned()
            }
        );
        assert!(elapsed.get() >= REENUM_GATE_DEADLINE);
    }

    #[test]
    fn gate_proceeds_when_idle() {
        let act = SeqActivity::new(vec![Ok(Some(9)), Ok(Some(9)), Ok(Some(12)), Ok(Some(12))]);
        let out = execute_flow(
            &act,
            &ReEnumOpts {
                reason: None,
                force: false,
                method: ReEnumMethod::SoftConnect,
            },
            &|_| {},
            &|| Duration::ZERO,
            &|| Ok(()),
            &|| Ok(42),
        );
        assert_eq!(out, ReEnumOutcome::Done { disconnect_ms: 42 });
    }

    #[test]
    fn force_bypasses_gate_when_activity_is_growing() {
        let act = CountingActivity {
            value: Cell::new(0),
        };
        let out = execute_flow(
            &act,
            &ReEnumOpts {
                reason: None,
                force: true,
                method: ReEnumMethod::SoftConnect,
            },
            &|_| {},
            &|| Duration::ZERO,
            &|| Ok(()),
            &|| Ok(7),
        );
        assert_eq!(out, ReEnumOutcome::Done { disconnect_ms: 7 });
    }

    #[test]
    fn outcome_failed_when_blip_or_verify_fails() {
        let act = SeqActivity::new(vec![]);
        let out = execute_flow(
            &act,
            &ReEnumOpts {
                reason: None,
                force: true,
                method: ReEnumMethod::SoftConnect,
            },
            &|_| {},
            &|| Duration::ZERO,
            &|| Ok(()),
            &|| Err(io::Error::other("verify failed")),
        );
        assert_eq!(
            out,
            ReEnumOutcome::Failed {
                detail: "verify failed".to_owned()
            }
        );
    }

    #[test]
    fn fsync_failure_aborts_before_blip() {
        let act = SeqActivity::new(vec![]);
        let blip_called = Cell::new(false);
        let out = execute_flow(
            &act,
            &ReEnumOpts {
                reason: None,
                force: true,
                method: ReEnumMethod::SoftConnect,
            },
            &|_| {},
            &|| Duration::ZERO,
            &|| Err(io::Error::other("disk full")),
            &|| {
                blip_called.set(true);
                Ok(1)
            },
        );
        assert!(
            matches!(out, ReEnumOutcome::Failed { .. }),
            "expected fsync failure outcome, got {out:?}"
        );
        assert!(!blip_called.get(), "perform_blip must not run");
    }

    struct FakeSoftConnectWriter {
        calls: Arc<Mutex<Vec<bool>>>,
    }

    impl SoftConnectWriter for FakeSoftConnectWriter {
        fn set(&self, connect: bool) -> io::Result<()> {
            self.calls.lock().expect("lock").push(connect);
            Ok(())
        }
    }

    #[test]
    fn connect_guard_drop_restores_connect_on_panic_path() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let writer = FakeSoftConnectWriter {
            calls: Arc::clone(&calls),
        };
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = ConnectGuard::new(&writer);
            panic!("simulated panic");
        }));
        let got = calls.lock().expect("lock");
        assert!(
            got.contains(&true),
            "drop path must best-effort write connect"
        );
    }

    struct FakeUdcRebinder {
        calls: Arc<Mutex<u32>>,
    }

    impl UdcRebinder for FakeUdcRebinder {
        fn rebind(&self) -> io::Result<()> {
            *self.calls.lock().expect("lock") += 1;
            Ok(())
        }
    }

    #[test]
    fn udc_rebind_guard_rebinds_on_armed_drop_only() {
        let calls = Arc::new(Mutex::new(0));
        {
            let rebinder = FakeUdcRebinder {
                calls: Arc::clone(&calls),
            };
            let _guard = UdcRebindGuard::new(&rebinder);
        }
        assert_eq!(*calls.lock().expect("lock"), 1, "armed drop must re-bind");

        let calls2 = Arc::new(Mutex::new(0));
        {
            let rebinder = FakeUdcRebinder {
                calls: Arc::clone(&calls2),
            };
            let mut guard = UdcRebindGuard::new(&rebinder);
            guard.disarm();
        }
        assert_eq!(
            *calls2.lock().expect("lock"),
            0,
            "disarmed drop must not re-bind"
        );
    }
}
