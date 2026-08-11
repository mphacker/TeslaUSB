use std::collections::HashMap;
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use axum::Json;
use axum::Router;
use axum::extract::State;
use axum::routing::{get, post};
use serde::Serialize;

use crate::AppState;

const RESCAN_INTERVAL: Duration = Duration::from_secs(10);
static LAST_RESCAN: Mutex<Option<Instant>> = Mutex::new(None);

#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
struct WifiStatus {
    connected: bool,
    ssid: Option<String>,
    signal: Option<u8>,
    security: Option<String>,
    ip: Option<String>,
    iface: Option<String>,
}

#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
struct WifiNetwork {
    ssid: String,
    signal: u8,
    security: String,
    saved: bool,
    active: bool,
    protected: bool,
}

#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
struct WifiNetworksResponse {
    networks: Vec<WifiNetwork>,
}

#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
struct SavedWifiNetwork {
    ssid: String,
    /// NetworkManager `connection.autoconnect-priority` (higher = preferred; default 0).
    priority: i32,
    /// `connection.autoconnect` — whether NM will auto-join this profile.
    autoconnect: bool,
    /// True when this saved profile is the active wlan0 connection.
    active: bool,
}

#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
struct SavedWifiResponse {
    networks: Vec<SavedWifiNetwork>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveWifiRow {
    ssid: String,
    signal: Option<u8>,
    security: Option<String>,
}

/// Read-only Wi-Fi routes (`/api/wifi/*`), mounted under `/api` by
/// [`crate::route`]. Every handler degrades to conservative defaults (never 5xx)
/// when `nmcli` is absent/failing.
pub(crate) fn routes() -> Router<AppState> {
    Router::new()
        .route("/wifi/status", get(wifi_status))
        .route("/wifi/networks", get(wifi_networks))
        .route("/wifi/saved", get(wifi_saved))
        .route("/wifi/scan", post(wifi_scan))
}

async fn wifi_status(State(_): State<AppState>) -> Json<WifiStatus> {
    let out = tokio::task::spawn_blocking(read_wifi_status)
        .await
        .unwrap_or_default();
    Json(out)
}

async fn wifi_networks(State(_): State<AppState>) -> Json<WifiNetworksResponse> {
    let out = tokio::task::spawn_blocking(|| WifiNetworksResponse {
        networks: read_wifi_networks(),
    })
    .await
    .unwrap_or_default();
    Json(out)
}

async fn wifi_saved(State(_): State<AppState>) -> Json<SavedWifiResponse> {
    let out = tokio::task::spawn_blocking(|| SavedWifiResponse {
        networks: read_saved_networks(),
    })
    .await
    .unwrap_or_default();
    Json(out)
}

async fn wifi_scan(State(_): State<AppState>) -> Json<WifiNetworksResponse> {
    let out = tokio::task::spawn_blocking(|| {
        if should_rescan(Instant::now()) {
            let _ = capture("nmcli", &["dev", "wifi", "rescan"]);
        }
        WifiNetworksResponse {
            networks: read_wifi_networks(),
        }
    })
    .await
    .unwrap_or_default();
    Json(out)
}

fn read_wifi_status() -> WifiStatus {
    let iface = discover_wifi_iface();
    let device =
        capture("nmcli", &["-t", "-f", "DEVICE,STATE,CONNECTION", "device"]).unwrap_or_default();
    let active_wifi = capture(
        "nmcli",
        &["-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi"],
    )
    .unwrap_or_default();
    let ip_show = capture(
        "nmcli",
        &["-t", "-f", "IP4.ADDRESS", "device", "show", &iface],
    )
    .unwrap_or_default();

    let state = parse_device_state(&device, &iface);
    let active = parse_active_wifi_row(&active_wifi);
    WifiStatus {
        connected: state.as_deref().is_some_and(is_connected_state),
        ssid: active.as_ref().and_then(|row| normalize_value(&row.ssid)),
        signal: active.as_ref().and_then(|row| row.signal),
        security: active
            .as_ref()
            .and_then(|row| row.security.clone())
            .and_then(|s| normalize_value(&s)),
        ip: parse_ip4_address(&ip_show),
        iface: normalize_value(&iface),
    }
}

fn read_wifi_networks() -> Vec<WifiNetwork> {
    let rows = capture(
        "nmcli",
        &[
            "-t",
            "-f",
            "IN-USE,SSID,SIGNAL,SECURITY",
            "dev",
            "wifi",
            "list",
            "--rescan",
            "no",
        ],
    )
    .unwrap_or_default();
    let saved_profiles = saved_profiles_by_ssid();
    parse_network_rows(&rows, &saved_profiles)
}

fn discover_wifi_iface() -> String {
    let out = capture("nmcli", &["-t", "-f", "DEVICE,TYPE", "device"]).unwrap_or_default();
    parse_wifi_iface(&out).unwrap_or_else(|| "wlan0".to_owned())
}

fn parse_wifi_iface(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.len() < 2 {
            return None;
        }
        (fields[1] == "wifi")
            .then(|| normalize_value(&fields[0]))
            .flatten()
    })
}

fn parse_device_state(output: &str, iface: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.len() < 3 {
            return None;
        }
        (fields[0] == iface)
            .then(|| normalize_value(&fields[1]))
            .flatten()
    })
}

fn parse_active_wifi_row(output: &str) -> Option<ActiveWifiRow> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.len() < 4 || !fields[0].eq_ignore_ascii_case("yes") {
            return None;
        }
        Some(ActiveWifiRow {
            ssid: fields[1].clone(),
            signal: parse_signal(&fields[2]),
            security: normalize_value(&fields[3]),
        })
    })
}

fn parse_ip4_address(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.len() < 2 || !fields[0].starts_with("IP4.ADDRESS") {
            return None;
        }
        let value = normalize_value(&fields[1])?;
        let ip = value.split('/').next().unwrap_or_default().trim();
        normalize_value(ip)
    })
}

fn parse_wifi_profile_names(output: &str) -> Vec<String> {
    output
        .lines()
        .filter_map(|line| {
            let fields = split_terse_line(line);
            if fields.len() < 2 || fields[1] != "802-11-wireless" {
                return None;
            }
            normalize_value(&fields[0])
        })
        .collect()
}

fn parse_ssid_field(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.is_empty() || fields[0] != "802-11-wireless.ssid" {
            return None;
        }
        let value = fields.iter().skip(1).cloned().collect::<Vec<_>>().join(":");
        normalize_value(&value)
    })
}

/// Parse `(autoconnect, priority, ssid)` from a per-profile terse
/// `connection show` dump. `ssid` is required (None → skip the profile);
/// autoconnect defaults false and priority defaults 0 when absent/unparseable.
fn parse_saved_profile_detail(output: &str) -> Option<(bool, i32, String)> {
    let ssid = parse_ssid_field(output)?;
    let mut autoconnect = false;
    let mut priority = 0;

    for line in output.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        match key {
            "connection.autoconnect" => autoconnect = value.trim() == "yes",
            "connection.autoconnect-priority" => {
                priority = value.trim().parse::<i32>().unwrap_or(0);
            }
            _ => {}
        }
    }

    Some((autoconnect, priority, ssid))
}

/// Order saved networks by priority (desc) then ssid (asc), de-duplicating by
/// ssid (keep the highest priority; OR the `active` and `autoconnect` flags).
fn order_saved_networks(rows: Vec<SavedWifiNetwork>) -> Vec<SavedWifiNetwork> {
    let mut by_ssid: HashMap<String, SavedWifiNetwork> = HashMap::new();

    for row in rows {
        if let Some(existing) = by_ssid.get_mut(&row.ssid) {
            existing.priority = existing.priority.max(row.priority);
            existing.active |= row.active;
            existing.autoconnect |= row.autoconnect;
        } else {
            by_ssid.insert(row.ssid.clone(), row);
        }
    }

    let mut out: Vec<SavedWifiNetwork> = by_ssid.into_values().collect();
    out.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then(left.ssid.cmp(&right.ssid))
    });
    out
}

fn read_saved_networks() -> Vec<SavedWifiNetwork> {
    let show =
        capture("nmcli", &["-t", "-f", "NAME,TYPE", "connection", "show"]).unwrap_or_default();
    let active_uuid = read_active_uuid_on_wlan0();
    let mut rows = Vec::new();

    for name in parse_wifi_profile_names(&show) {
        let detail = capture(
            "nmcli",
            &[
                "-t",
                "-f",
                "connection.autoconnect,connection.autoconnect-priority,802-11-wireless.ssid",
                "connection",
                "show",
                name.as_str(),
            ],
        );
        let uuid = capture(
            "nmcli",
            &[
                "-t",
                "-f",
                "connection.uuid",
                "connection",
                "show",
                name.as_str(),
            ],
        )
        .as_deref()
        .and_then(parse_connection_uuid)
        .unwrap_or_default();
        let Some((autoconnect, priority, ssid)) =
            detail.as_deref().and_then(parse_saved_profile_detail)
        else {
            continue;
        };
        rows.push(SavedWifiNetwork {
            ssid,
            priority,
            autoconnect,
            active: !uuid.is_empty() && active_uuid.as_deref() == Some(uuid.as_str()),
        });
    }

    order_saved_networks(rows)
}

fn read_active_uuid_on_wlan0() -> Option<String> {
    let output = capture(
        "nmcli",
        &[
            "-t",
            "-f",
            "UUID,DEVICE,STATE",
            "connection",
            "show",
            "--active",
        ],
    )?;
    parse_active_uuid_on_wlan0(&output)
}

fn parse_active_uuid_on_wlan0(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.len() < 3 {
            return None;
        }
        if fields[1] == "wlan0" && fields[2].eq_ignore_ascii_case("activated") {
            return normalize_value(&fields[0]);
        }
        None
    })
}

fn parse_connection_uuid(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let fields = split_terse_line(line);
        if fields.is_empty() || fields[0] != "connection.uuid" {
            return None;
        }
        let value = fields.iter().skip(1).cloned().collect::<Vec<_>>().join(":");
        normalize_value(&value)
    })
}

fn saved_profiles_by_ssid() -> HashMap<String, bool> {
    let mut out = HashMap::new();
    let show =
        capture("nmcli", &["-t", "-f", "NAME,TYPE", "connection", "show"]).unwrap_or_default();
    for profile_name in parse_wifi_profile_names(&show) {
        let detail = capture(
            "nmcli",
            &[
                "-t",
                "-f",
                "802-11-wireless.ssid",
                "connection",
                "show",
                profile_name.as_str(),
            ],
        );
        let Some(ssid) = detail.as_deref().and_then(parse_ssid_field) else {
            continue;
        };
        let is_protected = profile_name.starts_with("netplan-");
        out.entry(ssid)
            .and_modify(|protected| *protected |= is_protected)
            .or_insert(is_protected);
    }
    out
}

fn parse_network_rows(output: &str, saved_profiles: &HashMap<String, bool>) -> Vec<WifiNetwork> {
    let mut rows = Vec::new();
    for line in output.lines() {
        let fields = split_terse_line(line);
        if fields.len() < 4 {
            continue;
        }
        let Some(ssid) = normalize_value(&fields[1]) else {
            continue;
        };
        let signal = parse_signal(&fields[2]).unwrap_or(0);
        let security = normalize_value(&fields[3]).unwrap_or_default();
        let protected = saved_profiles.get(&ssid).copied().unwrap_or(false);
        rows.push(WifiNetwork {
            ssid: ssid.clone(),
            signal,
            security,
            saved: saved_profiles.contains_key(&ssid),
            active: fields[0].trim() == "*",
            protected,
        });
    }
    dedupe_sort_networks(rows)
}

fn dedupe_sort_networks(rows: Vec<WifiNetwork>) -> Vec<WifiNetwork> {
    let mut by_ssid: HashMap<String, WifiNetwork> = HashMap::new();
    for row in rows {
        let key = row.ssid.clone();
        if let Some(existing) = by_ssid.get_mut(&key) {
            if row.signal > existing.signal {
                existing.signal = row.signal;
                existing.security = row.security.clone();
            } else if existing.security.is_empty() && !row.security.is_empty() {
                existing.security = row.security.clone();
            }
            existing.active |= row.active;
            existing.saved |= row.saved;
            existing.protected |= row.protected;
            continue;
        }
        by_ssid.insert(key, row);
    }

    let mut out: Vec<WifiNetwork> = by_ssid.into_values().collect();
    out.sort_by(|left, right| {
        right
            .active
            .cmp(&left.active)
            .then(right.signal.cmp(&left.signal))
            .then(left.ssid.cmp(&right.ssid))
    });
    out
}

fn split_terse_line(line: &str) -> Vec<String> {
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut escaped = false;

    for ch in line.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
            continue;
        }
        match ch {
            '\\' => escaped = true,
            ':' => {
                fields.push(current);
                current = String::new();
            }
            _ => current.push(ch),
        }
    }
    if escaped {
        current.push('\\');
    }
    fields.push(current);
    fields
}

fn parse_signal(raw: &str) -> Option<u8> {
    let value = raw.trim().parse::<u16>().ok()?;
    Some(value.min(100) as u8)
}

fn normalize_value(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed == "--" {
        None
    } else {
        Some(trimmed.to_owned())
    }
}

fn is_connected_state(state: &str) -> bool {
    state
        .split_whitespace()
        .any(|part| part.eq_ignore_ascii_case("connected"))
}

fn can_rescan(last: Option<Instant>, now: Instant) -> bool {
    last.is_none_or(|prev| now.saturating_duration_since(prev) >= RESCAN_INTERVAL)
}

fn should_rescan(now: Instant) -> bool {
    let Ok(mut guard) = LAST_RESCAN.lock() else {
        return false;
    };
    if !can_rescan(*guard, now) {
        return false;
    }
    *guard = Some(now);
    true
}

fn capture(program: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(program).args(args).output().ok()?;
    if out.status.success() {
        String::from_utf8(out.stdout).ok()
    } else {
        None
    }
}

#[cfg(test)]
#[allow(
    clippy::expect_used,
    clippy::indexing_slicing,
    clippy::panic,
    clippy::unwrap_used
)]
mod tests {
    use std::collections::HashMap;
    use std::time::{Duration, Instant};

    use super::{
        SavedWifiNetwork, WifiNetwork, can_rescan, dedupe_sort_networks, order_saved_networks,
        parse_active_wifi_row, parse_network_rows, parse_saved_profile_detail, parse_signal,
        parse_ssid_field, parse_wifi_profile_names, split_terse_line,
    };

    #[test]
    fn split_terse_line_unescapes_colons_and_backslashes() {
        let fields = split_terse_line(r"yes:My\:Net\\Lab:52:WPA2");
        assert_eq!(fields, vec!["yes", "My:Net\\Lab", "52", "WPA2"]);
    }

    #[test]
    fn parse_active_wifi_row_detects_yes_row() {
        let out = "no:Other:91:WPA2\nyes:My\\:Net:52:WPA2\n";
        let active = parse_active_wifi_row(out).expect("active row");
        assert_eq!(active.ssid, "My:Net");
        assert_eq!(active.signal, Some(52));
        assert_eq!(active.security.as_deref(), Some("WPA2"));
    }

    #[test]
    fn parse_signal_clamps_and_rejects_invalid_values() {
        assert_eq!(parse_signal("52"), Some(52));
        assert_eq!(parse_signal("120"), Some(100));
        assert_eq!(parse_signal("bogus"), None);
    }

    #[test]
    fn parse_network_rows_sets_saved_active_and_protected_flags() {
        let mut saved = HashMap::new();
        saved.insert("Trez".to_owned(), true);
        let rows = parse_network_rows("*:Trez:52:WPA2\n:Guest\\:Wifi:35:\n", &saved);
        assert_eq!(rows.len(), 2);

        let first = &rows[0];
        assert_eq!(first.ssid, "Trez");
        assert!(first.active);
        assert!(first.saved);
        assert!(first.protected);
        assert_eq!(first.security, "WPA2");

        let second = &rows[1];
        assert_eq!(second.ssid, "Guest:Wifi");
        assert!(!second.active);
        assert!(!second.saved);
        assert!(!second.protected);
        assert_eq!(second.security, "");
    }

    #[test]
    fn dedupe_sort_networks_keeps_strongest_and_sorts_active_first() {
        let rows = vec![
            WifiNetwork {
                ssid: "Cafe".to_owned(),
                signal: 30,
                security: "WPA2".to_owned(),
                saved: false,
                active: false,
                protected: false,
            },
            WifiNetwork {
                ssid: "Trez".to_owned(),
                signal: 52,
                security: "WPA2".to_owned(),
                saved: true,
                active: true,
                protected: true,
            },
            WifiNetwork {
                ssid: "Cafe".to_owned(),
                signal: 78,
                security: "WPA3".to_owned(),
                saved: true,
                active: false,
                protected: false,
            },
        ];
        let out = dedupe_sort_networks(rows);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].ssid, "Trez");
        assert_eq!(out[1].ssid, "Cafe");
        assert_eq!(out[1].signal, 78);
        assert_eq!(out[1].security, "WPA3");
        assert!(out[1].saved);
    }

    #[test]
    fn parsers_handle_empty_output() {
        let saved = HashMap::new();
        assert!(parse_network_rows("", &saved).is_empty());
        assert!(parse_active_wifi_row("").is_none());
        assert!(parse_ssid_field("").is_none());
    }

    #[test]
    fn parse_wifi_profile_and_ssid_fields_handle_terse_escaping() {
        let profiles = parse_wifi_profile_names(
            "netplan-wlan0-Trez:802-11-wireless\nOffice\\:WiFi:802-11-wireless\neth0:802-3-ethernet\n",
        );
        assert_eq!(profiles, vec!["netplan-wlan0-Trez", "Office:WiFi"]);
        assert_eq!(
            parse_ssid_field("802-11-wireless.ssid:Office\\:WiFi\n").as_deref(),
            Some("Office:WiFi")
        );
    }

    #[test]
    fn parse_saved_profile_detail_full() {
        let out = "\
connection.autoconnect:yes
connection.autoconnect-priority:10
802-11-wireless.ssid:Trez
";
        assert_eq!(
            parse_saved_profile_detail(out),
            Some((true, 10, "Trez".to_owned()))
        );
    }

    #[test]
    fn parse_saved_profile_detail_defaults() {
        let out = "802-11-wireless.ssid:Guest\n";
        assert_eq!(
            parse_saved_profile_detail(out),
            Some((false, 0, "Guest".to_owned()))
        );
    }

    #[test]
    fn parse_saved_profile_detail_no_ssid() {
        let out = "\
connection.autoconnect:yes
connection.autoconnect-priority:10
";
        assert_eq!(parse_saved_profile_detail(out), None);
    }

    #[test]
    fn parse_saved_profile_detail_priority_unparseable_is_zero() {
        let out = "\
connection.autoconnect:no
connection.autoconnect-priority:not-a-number
802-11-wireless.ssid:Guest
";
        assert_eq!(
            parse_saved_profile_detail(out),
            Some((false, 0, "Guest".to_owned()))
        );
    }

    #[test]
    fn order_saved_networks_orders_desc_then_ssid() {
        let rows = vec![
            SavedWifiNetwork {
                ssid: "Trez".to_owned(),
                priority: 20,
                autoconnect: true,
                active: false,
            },
            SavedWifiNetwork {
                ssid: "Alpha".to_owned(),
                priority: 20,
                autoconnect: false,
                active: false,
            },
            SavedWifiNetwork {
                ssid: "Guest".to_owned(),
                priority: 0,
                autoconnect: true,
                active: true,
            },
        ];

        let ordered = order_saved_networks(rows);
        assert_eq!(ordered.len(), 3);
        assert_eq!(ordered[0].ssid, "Alpha");
        assert_eq!(ordered[1].ssid, "Trez");
        assert_eq!(ordered[2].ssid, "Guest");
    }

    #[test]
    fn order_saved_networks_dedupes_by_ssid() {
        let rows = vec![
            SavedWifiNetwork {
                ssid: "Trez".to_owned(),
                priority: 10,
                autoconnect: false,
                active: false,
            },
            SavedWifiNetwork {
                ssid: "Trez".to_owned(),
                priority: 30,
                autoconnect: true,
                active: true,
            },
        ];

        let ordered = order_saved_networks(rows);
        assert_eq!(ordered.len(), 1);
        assert_eq!(
            ordered[0],
            SavedWifiNetwork {
                ssid: "Trez".to_owned(),
                priority: 30,
                autoconnect: true,
                active: true,
            }
        );
    }

    #[test]
    fn can_rescan_enforces_ten_second_window() {
        let now = Instant::now();
        assert!(can_rescan(None, now));
        assert!(!can_rescan(Some(now), now + Duration::from_secs(5)));
        assert!(can_rescan(Some(now), now + Duration::from_secs(10)));
    }
}
