use std::hash::{Hash, Hasher};
use std::io::Write;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

use crate::AppState;

/// Spawn the background chime-enforcement task when the production env is enabled.
pub(crate) fn spawn(state: AppState) {
    tokio::spawn(async move {
        let mut last_enforced: Option<String> = None;

        if let Some(name) = enforce_boot(&state).await {
            last_enforced = Some(name);
        }

        let mut interval = tokio::time::interval(Duration::from_secs(60));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            interval.tick().await;
            if let Some(name) = enforce_tick(&state, last_enforced.as_deref()).await {
                last_enforced = Some(name);
            }
        }
    });
}

async fn enforce_boot(state: &AppState) -> Option<String> {
    let library = library_names(state).await?;
    // An empty/unready media library yields no installable candidate: skip the
    // boot step rather than resolving against schedulerd's stale legacy scan.
    if library.is_empty() {
        return None;
    }
    let tz = local_offset_secs();
    let seed = boot_seed();
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let now = i64::try_from(now).unwrap_or(i64::MAX);
    let request = json!({
        "cmd": "evaluate_boot",
        "unix_secs": now,
        "tz_offset_secs": tz,
        "library": library,
        "boot_seed": seed,
    });
    let pick = call_scheduler(state, request).await?;
    let name = pick_name(&pick)?;
    // Only enforce a chime that actually exists in the authoritative media
    // library; a schedule pointing at a since-deleted file is skipped silently
    // (self-heals if the file returns) instead of looping on a failed install.
    if !library.contains(&name) {
        return None;
    }
    if let Err(err) = install_and_track(state, &name).await {
        let _ = writeln!(std::io::stderr(), "chime enforcer: boot apply failed: {err:?}");
        return None;
    }
    Some(name)
}

async fn enforce_tick(state: &AppState, last_enforced: Option<&str>) -> Option<String> {
    let library = library_names(state).await?;
    if library.is_empty() {
        return None;
    }
    let tz = local_offset_secs();
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let now = i64::try_from(now).unwrap_or(i64::MAX);
    // NOTE: `active_chime` is intentionally NOT set to `last_enforced`. The core
    // resolver EXCLUDES `active_chime` from a random pool, so feeding it back
    // would make a RANDOM/recurring schedule pick a *different* file every tick
    // (a handoff per minute). Idempotency is enforced purely by `next_action`
    // below: a stable seed resolves the same pick, which then dedupes to no-op.
    let request = json!({
        "cmd": "evaluate",
        "unix_secs": now,
        "tz_offset_secs": tz,
        "library": library,
    });
    let pick = call_scheduler(state, request).await?;
    let name = next_action(pick_name(&pick).as_deref(), last_enforced)?;
    if !library.contains(&name) {
        return None;
    }
    if let Err(err) = install_and_track(state, &name).await {
        let _ = writeln!(std::io::stderr(), "chime enforcer: tick apply failed: {err:?}");
        return None;
    }
    Some(name)
}

fn next_action(pick: Option<&str>, last: Option<&str>) -> Option<String> {
    match (pick, last) {
        (Some(name), None) => Some(name.to_owned()),
        (Some(name), Some(last_name)) if name != last_name => Some(name.to_owned()),
        _ => None,
    }
}

async fn install_and_track(state: &AppState, name: &str) -> Result<(), crate::error::ApiError> {
    let _ = crate::chime_library::install_library_chime_as_active(
        state.clone(),
        "chime_scheduler_enforce",
        name,
    )
    .await?;
    Ok(())
}

async fn library_names(state: &AppState) -> Option<Vec<String>> {
    crate::route::read(state.catalog.clone(), crate::query::list_chime_library)
        .await
        .ok()
        .map(|items| items.into_iter().map(|item| item.name).collect())
}

async fn call_scheduler(state: &AppState, request: Value) -> Option<Value> {
    let client = state.scheduler.clone();
    let join = tokio::task::spawn_blocking(move || client.call(request)).await.ok()?;
    match join {
        Ok(value) => Some(value),
        Err(err) => {
            let _ = writeln!(std::io::stderr(), "chime enforcer: scheduler call failed: {err:?}");
            None
        }
    }
}

fn pick_name(value: &Value) -> Option<String> {
    value.get("pick")?.get("chimeFilename").and_then(Value::as_str).map(str::to_owned)
}

fn local_offset_secs() -> i32 {
    if let Ok(v) = std::env::var("WEBD_TZ_OFFSET_SECS") {
        if let Ok(n) = v.parse::<i32>() {
            return n;
        }
    }

    if let Ok(output) = std::process::Command::new("date").arg("+%z").output() {
        if let Ok(s) = std::str::from_utf8(&output.stdout) {
            // `date +%z` yields exactly `±HHMM`. Parse defensively via string
            // methods so a short/odd output can never panic the enforcer task.
            let text = s.trim();
            let (sign, rest) = match text.strip_prefix('-') {
                Some(rest) => (-1, rest),
                None => (1, text.strip_prefix('+').unwrap_or(text)),
            };
            if rest.len() == 4 {
                let hours = rest.get(0..2).and_then(|h| h.parse::<i32>().ok());
                let mins = rest.get(2..4).and_then(|m| m.parse::<i32>().ok());
                if let (Some(hours), Some(mins)) = (hours, mins) {
                    return sign * (hours * 3600 + mins * 60);
                }
            }
        }
    }

    0
}

fn boot_seed() -> u64 {
    if let Ok(bytes) = std::fs::read("/proc/sys/kernel/random/boot_id") {
        let mut hasher = Fnv1a::default();
        bytes.hash(&mut hasher);
        return hasher.finish();
    }

    if let Ok(bytes) = std::fs::read("/proc/stat") {
        let text = String::from_utf8_lossy(&bytes);
        for line in text.lines() {
            if let Some(rest) = line.strip_prefix("btime ") {
                if let Ok(value) = rest.trim().parse::<u64>() {
                    return value;
                }
            }
        }
    }

    0
}

#[derive(Default)]
struct Fnv1a {
    state: u64,
}

impl Hasher for Fnv1a {
    fn finish(&self) -> u64 {
        self.state
    }

    fn write(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.state ^= u64::from(*byte);
            self.state = self.state.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::next_action;

    #[test]
    fn next_action_truth_table() {
        assert_eq!(next_action(Some("A"), None), Some("A".to_owned()));
        assert_eq!(next_action(Some("A"), Some("A")), None);
        assert_eq!(next_action(Some("B"), Some("A")), Some("B".to_owned()));
        assert_eq!(next_action(None, Some("A")), None);
        assert_eq!(next_action(None, None), None);
    }
}
