#!/usr/bin/env bash
# setup-lib/05-network.sh — Phase 6.5
#
# Lays down the NetworkManager + AP-mode config that the B-1 web app
# (captive_portal blueprint) expects to exist:
#
#   * /etc/NetworkManager/system-connections/teslausb-ap.nmconnection
#       AP-mode WiFi profile (autoconnect=false, mode=ap, band=bg,
#       channel=6) with a SECRET placeholder PSK that the operator
#       rewrites via the web UI / teslausb-web.toml.
#   * /etc/NetworkManager/dispatcher.d/95-teslausb-ap
#       Safety-net dispatcher: brings the AP up if STA wlan0 drops
#       so we never lose access to the device.
#   * /etc/NetworkManager/dnsmasq-shared.d/teslausb-captive.conf
#       Captive-portal dnsmasq drop-in (DHCP 192.168.4.2-50, 1h lease,
#       wildcard DNS → 192.168.4.1).
#
# HARD SAFETY RAILS (the operator is browsing the Pi over wifi when
# this runs — losing wlan0 is not acceptable):
#   * NEVER `nmcli connection delete` an existing profile.
#   * NEVER edit /etc/wpa_supplicant/wpa_supplicant.conf (legacy dhcpcd).
#   * NEVER bring wlan0 down. NEVER `systemctl restart NetworkManager`.
#   * NEVER `nmcli connection up teslausb-ap` — activation is Phase 6.10.
#   * The AP profile has `autoconnect=false` so dropping it on disk
#     cannot fight the operator's primary STA connection on next boot.
#
# Co-existence with v1's dhcpcd stack: IF both NetworkManager AND
# dhcpcd are active right now, we back up /etc/dhcpcd.conf to a
# .b1-backup-<ISO> sibling and `systemctl disable --now dhcpcd && mask`
# so NM wins on next boot. If only one (or neither) is active, this
# step is a no-op on the netstack front.
#
# Idempotency: every install compares sha256(rendered) vs sha256(on-disk)
# and is a no-op on match. A `b1_backup` sibling is created the FIRST
# time a target is overwritten; subsequent re-runs do NOT pile up
# backups (b1_backup is itself idempotent).
#
# Dry-run: every mutation goes through `b1_run`; probes (systemctl
# is-active, stat, sha256sum) run even under TESLAUSB_DRY_RUN so the
# dry-run report accurately predicts what WOULD change.

# Re-source common in case this file is invoked via --only.
# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# --------------------------------------------------------------------
# Constants — exported so 6.11 (uninstall.sh) can reuse them verbatim.
# --------------------------------------------------------------------

B1_AP_SSID="TeslaUSB-AP"
B1_AP_CONNECTION_NAME="teslausb-ap"
B1_AP_CONNECTION_FILE="/etc/NetworkManager/system-connections/teslausb-ap.nmconnection"
B1_AP_DISPATCHER_FILE="/etc/NetworkManager/dispatcher.d/95-teslausb-ap"
B1_AP_DNSMASQ_FILE="/etc/NetworkManager/dnsmasq-shared.d/teslausb-captive.conf"
B1_WIFI_SCAN_POLKIT_FILE="/etc/polkit-1/rules.d/50-teslausb-wifi-scan.rules"
B1_AP_SUDOERS_FILE="/etc/sudoers.d/teslausb-ap"
B1_AP_TMPFILES_FILE="/etc/tmpfiles.d/teslausb-ap.conf"
B1_DHCPCD_CONF="/etc/dhcpcd.conf"

# Files we may write — tracked so 6.11 (uninstall.sh) knows exactly
# what to remove. Order doesn't matter to the uninstaller.
B1_NETWORK_TARGETS=(
  "${B1_AP_CONNECTION_FILE}"
  "${B1_AP_DISPATCHER_FILE}"
  "${B1_AP_DNSMASQ_FILE}"
  "${B1_WIFI_SCAN_POLKIT_FILE}"
  "${B1_AP_SUDOERS_FILE}"
  "${B1_AP_TMPFILES_FILE}"
)

# Placeholder PSK. The operator/setup edits
# /etc/teslausb/teslausb-web.toml `[wifi].ap_passphrase` and reloads
# via the web UI; this nmconnection file just bootstraps a valid
# parse. The captive-portal "Apply AP config" action rewrites the
# `psk=` line in place (mode 0600, root:root) before any AP-up.
B1_AP_PSK_PLACEHOLDER="__SET_VIA_TESLAUSB_WEB_TOML__"

export B1_AP_SSID B1_AP_CONNECTION_NAME B1_AP_CONNECTION_FILE \
       B1_AP_DISPATCHER_FILE B1_AP_DNSMASQ_FILE B1_DHCPCD_CONF \
       B1_WIFI_SCAN_POLKIT_FILE B1_AP_SUDOERS_FILE B1_AP_TMPFILES_FILE \
       B1_NETWORK_TARGETS B1_AP_PSK_PLACEHOLDER

# --------------------------------------------------------------------
# File bodies — constant heredocs at file scope so reviewers + the
# 6.11 uninstaller can read them without executing the script.
# --------------------------------------------------------------------
#
# AP nmconnection format reference: `man 5 nm-settings-keyfile`.
# NetworkManager REFUSES to load any file under system-connections/
# whose mode is not 0600 root:root — we enforce that on install.

read -r -d '' B1_AP_CONNECTION_BODY <<AP_CONN || true
# TeslaUSB B-1 access-point profile.
#
# Managed by setup-lib/05-network.sh (Phase 6.5). DO NOT edit this
# file in-place — re-run \`setup.sh --only 05\` after editing the
# heredoc in setup-lib/05-network.sh. setup.sh backs up any local
# divergence as \`.b1-backup-<timestamp>\` before overwriting.
#
# The \`psk=\` line below is a PLACEHOLDER. The real passphrase is
# kept in /etc/teslausb/teslausb-web.toml under [wifi].ap_passphrase
# and the captive_portal blueprint rewrites this file (preserving
# 0600 root:root) when the operator saves a new passphrase via the
# web UI. NM will refuse to activate the AP while psk= is still the
# placeholder — that's intentional.
#
# Mode locked to 2.4 GHz (band=bg) because the Pi Zero 2 W radio has
# no 5 GHz support. autoconnect=false so this profile never fights
# the operator's primary STA connection on boot — Phase 6.10 owns
# explicit activation decisions.

[connection]
id=${B1_AP_CONNECTION_NAME}
type=wifi
interface-name=wlan0
autoconnect=false

[wifi]
mode=ap
ssid=${B1_AP_SSID}
band=bg
channel=6
hidden=false

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
group=ccmp
pairwise=ccmp
psk=${B1_AP_PSK_PLACEHOLDER}

[ipv4]
method=shared
address1=192.168.4.1/24

[ipv6]
method=ignore

[proxy]
AP_CONN

read -r -d '' B1_AP_DISPATCHER_BODY <<'AP_DISP' || true
#!/bin/sh
# TeslaUSB B-1 AP safety-net dispatcher (Phase 6.5).
#
# NetworkManager invokes dispatcher.d scripts with:
#   $1 = interface name   (e.g. wlan0)
#   $2 = action           (e.g. up, down, pre-up, pre-down, ...)
# plus a pile of CONNECTION_* / DEVICE_* env vars (see
# `man 8 NetworkManager-dispatcher`).
#
# Behaviour:
#   * If wlan0 goes DOWN and the AP profile is not already active,
#     bring it up so the operator can still reach the device.
#   * If a NON-AP profile comes UP on wlan0, tear the AP back down
#     (it would just be wasting RAM and confusing clients).
# Everything else is a no-op.
#
# Failures are intentionally swallowed (|| true): a flapping AP must
# never wedge an unrelated NM dispatch — and the worst case (AP
# fails to come up) leaves the operator no worse off than before.
set -eu

IFACE="${1:-}"
ACTION="${2:-}"
AP_CONNECTION="teslausb-ap"

[ "$IFACE" = "wlan0" ] || exit 0

case "$ACTION" in
  down)
    if nmcli -t -f NAME connection show --active 2>/dev/null \
         | grep -qx "$AP_CONNECTION"; then
      exit 0
    fi
    nmcli connection up "$AP_CONNECTION" ifname wlan0 >/dev/null 2>&1 || true
    ;;
  up)
    # Don't tear ourselves down when WE were the one that came up.
    if [ "${CONNECTION_ID:-}" = "$AP_CONNECTION" ]; then
      exit 0
    fi
    nmcli connection down "$AP_CONNECTION" >/dev/null 2>&1 || true
    ;;
esac

exit 0
AP_DISP

read -r -d '' B1_AP_DNSMASQ_BODY <<'AP_DNSMASQ' || true
# TeslaUSB B-1 captive-portal dnsmasq drop-in (Phase 6.5).
#
# Loaded by NetworkManager's bundled dnsmasq when the AP profile
# (ipv4.method=shared) is active. Effects:
#   * Wildcard DNS A record: every name resolves to the AP gateway
#     192.168.4.1, so iOS/Android/Windows captive-portal probes hit
#     the Flask /portal endpoint and the OS pops the sign-in sheet.
#   * DHCP pool 192.168.4.2-192.168.4.50, /24, 1-hour leases — small
#     on purpose; the AP is for one phone at a time, not a public
#     hotspot.
#
# Managed by setup-lib/05-network.sh. DO NOT edit by hand — re-run
# `setup.sh --only 05` after editing the heredoc instead.

address=/#/192.168.4.1
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,1h
AP_DNSMASQ

read -r -d '' B1_WIFI_SCAN_POLKIT_BODY <<'WIFI_POLKIT' || true
// TeslaUSB B-1 polkit rule — allows the `pi` user (which the
// teslausb-web Flask app runs as) to trigger Wi-Fi scans and toggle
// the radio without an interactive polkit prompt. Without this rule
// `nmcli device wifi rescan` fails with "not authorized" when run
// by `pi`, NetworkManager's cached wifi list collapses to only the
// associated AP within ~1 min of association, and the
// /api/wifi/saved endpoint then reports every non-active saved
// network as "Not in range" even when it's clearly visible.
//
// Limited to scan + radio-toggle actions; the on-board AP profile is
// managed via hostapd + dnsmasq on a virtual wlan0_ap interface
// (see /etc/sudoers.d/teslausb-ap and the wifi_hostapd service
// module) so NetworkManager never touches it.
//
// Managed by setup-lib/05-network.sh — DO NOT edit in place.
polkit.addRule(function(action, subject) {
    if (subject.user == "pi" &&
        (action.id == "org.freedesktop.NetworkManager.wifi.scan" ||
         action.id == "org.freedesktop.NetworkManager.enable-disable-wifi")) {
        return polkit.Result.YES;
    }
});
WIFI_POLKIT

# Sudoers allowlist for the hostapd-based on-board AP. Lets the `pi`
# user (teslausb-web runs as pi) bring the virtual wlan0_ap interface
# up, start hostapd + dnsmasq, and tear them back down — WITHOUT
# granting blanket NOPASSWD. Mode MUST be 0440 (any other mode and
# sudo refuses to load the file). visudo -c -f validates the syntax
# before install; a malformed sudoers file under /etc/sudoers.d/ can
# wedge ALL sudo on the host, including the dead-man reboot path, so
# this is non-optional.
read -r -d '' B1_AP_SUDOERS_BODY <<'AP_SUDO' || true
# TeslaUSB B-1 — privileged commands for hostapd-based on-board AP.
# Managed by setup-lib/05-network.sh. DO NOT edit in place — re-run
# `setup.sh --only 05` after editing the heredoc.
#
# Single-radio Pi Zero 2 W: bringing the AP up via NetworkManager on
# wlan0 deactivates the active WiFi client (and the SSH session
# riding it). We instead run hostapd + dnsmasq on a virtual
# wlan0_ap interface (see web/teslausb_web/services/wifi_hostapd.py).
# Scope is intentionally narrow — no shell wildcards on commands,
# only the precise binaries + arg shapes the service module emits.
Defaults!TESLAUSB_AP !requiretty
Cmnd_Alias TESLAUSB_AP = \
    /sbin/iw dev wlan0 interface add wlan0_ap type __ap, \
    /sbin/iw dev wlan0_ap del, \
    /sbin/ip link set wlan0_ap up, \
    /sbin/ip link set wlan0_ap down, \
    /sbin/ip addr flush dev wlan0_ap, \
    /sbin/ip addr add 192.168.4.1/24 dev wlan0_ap, \
    /usr/bin/nmcli device set wlan0_ap managed no, \
    /usr/bin/nmcli device set wlan0_ap managed yes, \
    /usr/sbin/hostapd -B -P /run/teslausb-ap/hostapd.pid /run/teslausb-ap/hostapd.conf, \
    /usr/sbin/dnsmasq --conf-file=/run/teslausb-ap/dnsmasq.conf --pid-file=/run/teslausb-ap/dnsmasq.pid, \
    /usr/bin/kill [0-9]*, \
    /usr/bin/kill -[0-9]* [0-9]*
pi ALL=(root) NOPASSWD: TESLAUSB_AP
AP_SUDO

# Runtime dir for the hostapd + dnsmasq conf + pid files. systemd-
# tmpfiles re-creates it on every boot (/run is tmpfs) so the
# wifi_hostapd service module can write the rendered configs as `pi`
# without needing sudo just to mkdir.
read -r -d '' B1_AP_TMPFILES_BODY <<'AP_TMPF' || true
# TeslaUSB B-1 — runtime dir for hostapd/dnsmasq used by the on-board
# AP. Owned by pi:pi so the teslausb-web service can write rendered
# configs without needing sudo for the mkdir. Managed by
# setup-lib/05-network.sh.
d /run/teslausb-ap 0755 pi pi -
AP_TMPF

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

# _b1_sha256 <path> — sha256 hex of <path>, empty if missing.
_b1_sha256() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    printf ''
    return 0
  fi
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_sha256_string <content> — sha256 hex of a string.
_b1_sha256_string() {
  printf '%s' "$1" | sha256sum | awk '{print $1}'
}

# _b1_install_string <content> <dst> <mode>
#   Idempotent install of an in-memory string to <dst> at <mode>,
#   owner root:root. Backs up the existing target (one-shot) on first
#   divergence. Returns 0 unchanged-or-installed, 1 on hard error.
_b1_install_string() {
  local content="$1"
  local dst="$2"
  local mode="$3"

  local src_sum dst_sum
  src_sum="$(_b1_sha256_string "${content}")"
  dst_sum="$(_b1_sha256 "${dst}")"

  # Even when content matches, fix mode/owner if they drifted — NM
  # silently refuses to load nmconnection files with the wrong mode.
  local need_write=1
  if [[ -n "${dst_sum}" && "${src_sum}" == "${dst_sum}" ]]; then
    local cur_mode cur_owner
    cur_mode="$(stat -c '%a' "${dst}" 2>/dev/null || echo '')"
    cur_owner="$(stat -c '%U:%G' "${dst}" 2>/dev/null || echo '')"
    if [[ "${cur_mode}" == "${mode#0}" && "${cur_owner}" == "root:root" ]]; then
      b1_log "unchanged: ${dst} (sha256=${dst_sum:0:12}…, mode=${cur_mode})"
      return 0
    fi
    b1_log "content ok but mode/owner drift on ${dst} (mode=${cur_mode} owner=${cur_owner}) — re-installing"
    need_write=1
  fi

  if [[ -e "${dst}" ]]; then
    b1_log "differs: ${dst} (target=${dst_sum:0:12}…, source=${src_sum:0:12}…) — backing up"
    b1_backup "${dst}"
  else
    b1_log "new: ${dst} (sha256=${src_sum:0:12}…, mode=${mode})"
  fi

  # Ensure the parent directory exists (dispatcher.d / dnsmasq-shared.d
  # are created by the network-manager package; we don't depend on it).
  local parent
  parent="$(dirname "${dst}")"
  if [[ ! -d "${parent}" ]]; then
    b1_log "creating parent dir: ${parent}"
    b1_run install -d -m 0755 -o root -g root "${parent}"
  fi

  # Stage in a unique repo-local file (NEVER /tmp — runtime policy);
  # cleaned up via trap on return.
  local stage
  stage="$(dirname "${BASH_SOURCE[0]}")/.b1-stage-05-$$-${RANDOM}"
  if [[ "${TESLAUSB_DRY_RUN:-0}" != "1" ]]; then
    printf '%s' "${content}" > "${stage}"
  fi
  # shellcheck disable=SC2064
  trap "rm -f -- '${stage}'" RETURN

  b1_run install -o root -g root -m "${mode}" -- "${stage}" "${dst}"

  # Used by callers' has-changed bookkeeping (currently informational
  # only — 6.5 has no daemon-reload to gate). Returning 0 keeps the
  # pipeline simple.
  : "${need_write}"
  return 0
}

# _b1_validate_ap_connection — post-install sanity check: file exists,
# mode is 0600 root:root, and the three mandatory keyfile sections
# are present. Warn (do not fail) under dry-run since the file may
# not have been written.
_b1_validate_ap_connection() {
  local f="${B1_AP_CONNECTION_FILE}"
  if [[ "${TESLAUSB_DRY_RUN:-0}" == "1" && ! -e "${f}" ]]; then
    b1_log "validate skipped (dry-run, file not written): ${f}"
    return 0
  fi
  if [[ ! -e "${f}" ]]; then
    b1_err "AP connection file missing after install: ${f}"
    return 1
  fi
  local mode owner
  mode="$(stat -c '%a' "${f}" 2>/dev/null || echo '')"
  owner="$(stat -c '%U:%G' "${f}" 2>/dev/null || echo '')"
  if [[ "${mode}" != "600" || "${owner}" != "root:root" ]]; then
    b1_err "AP connection file has wrong perms (got ${owner} ${mode}, want root:root 600): ${f}"
    return 1
  fi
  local key
  for key in '^\[connection\]' '^\[wifi\]' '^\[wifi-security\]'; do
    if ! grep -qE "${key}" "${f}"; then
      b1_err "AP connection file missing section ${key}: ${f}"
      return 1
    fi
  done
  b1_log "AP connection file validated: ${f} (mode=${mode} owner=${owner})"
}

# _b1_install_sudoers <content> <dst>
#   Install a sudoers fragment at <dst> with mode 0440 root:root,
#   AFTER validating syntax via `visudo -c -f` on a staged copy. A
#   malformed sudoers file under /etc/sudoers.d/ wedges ALL sudo on
#   the host (including the dead-man reboot path), so the validation
#   gate is non-optional.
_b1_install_sudoers() {
  local content="$1"
  local dst="$2"

  local stage
  stage="$(dirname "${BASH_SOURCE[0]}")/.b1-stage-sudoers-$$-${RANDOM}"
  printf '%s' "${content}" > "${stage}"
  # shellcheck disable=SC2064
  trap "rm -f -- '${stage}'" RETURN

  if command -v visudo >/dev/null 2>&1; then
    if ! visudo -c -f "${stage}" >/dev/null 2>&1; then
      b1_err "sudoers fragment failed visudo -c validation: ${dst}"
      visudo -c -f "${stage}" || true
      return 1
    fi
    b1_log "sudoers fragment passed visudo -c validation: ${dst}"
  else
    b1_warn "visudo not found — skipping syntax validation for ${dst}"
  fi

  _b1_install_string "${content}" "${dst}" 0440 || return 1
}

# _b1_handle_dhcpcd — if BOTH NetworkManager and dhcpcd are currently
# active, this is the v1 stack still in charge. Back up the dhcpcd
# config (one-shot) and disable+mask the unit so NM owns wlan0 on
# next boot. NEVER touches wlan0 itself.
_b1_handle_dhcpcd() {
  local nm_active=0 dhcpcd_active=0
  if b1_unit_active NetworkManager; then nm_active=1; fi
  if b1_unit_active dhcpcd;        then dhcpcd_active=1; fi

  b1_log "netstack: NetworkManager active=${nm_active}, dhcpcd active=${dhcpcd_active}"

  if (( nm_active == 0 && dhcpcd_active == 0 )); then
    b1_warn "neither NetworkManager nor dhcpcd is active — leaving netstack alone"
    return 0
  fi

  if (( dhcpcd_active == 0 )); then
    # Pure NM (or pure dhcpcd-less) host — nothing to do here.
    b1_log "dhcpcd not active — no netstack handoff needed"
    return 0
  fi

  if (( nm_active == 0 )); then
    # dhcpcd-only host (v1, before 6.1 installs network-manager). NM
    # comes up after 6.1; we'll do the handoff on the NEXT setup run.
    # Refuse to disable dhcpcd here — that would briefly leave wlan0
    # unmanaged and is the exact scenario that drops the operator's
    # SSH session.
    b1_warn "dhcpcd active but NetworkManager NOT active — refusing to disable dhcpcd"
    b1_warn "  (re-run setup.sh after NetworkManager is active to complete the handoff)"
    return 0
  fi

  # Both active — safe handoff: back up the dhcpcd config (idempotent),
  # then disable+mask the unit. NetworkManager continues to own wlan0
  # for the rest of this boot; on next boot dhcpcd stays down.
  if [[ -f "${B1_DHCPCD_CONF}" ]]; then
    b1_log "backing up ${B1_DHCPCD_CONF} before dhcpcd handoff"
    b1_backup "${B1_DHCPCD_CONF}"
  fi

  # `disable --now` stops the unit gracefully; mask prevents any
  # downstream package upgrade from re-enabling it. We do this
  # AFTER the backup so even a failure mid-disable leaves the
  # operator's original config recoverable.
  b1_log "dhcpcd handoff: disable --now + mask"
  b1_run systemctl disable --now dhcpcd
  b1_run systemctl mask dhcpcd
}

# --------------------------------------------------------------------
# Step
# --------------------------------------------------------------------

b1_step_05() {
  # 1) netstack handoff (only if BOTH are active — see helper).
  _b1_handle_dhcpcd || return 1

  # 2) confirm nm-online tool is available; it's the only NM-shipped
  #    binary we'd lean on for any future probing. We do NOT actually
  #    probe — that would block on slow STA scans. Tool absence is a
  #    warning, not a hard fail, so the dry-run still works on a dev
  #    box without network-manager installed.
  if command -v nm-online >/dev/null 2>&1; then
    b1_log "nm-online available (offline check only — no probe issued)"
  else
    b1_warn "nm-online not found — install network-manager (step 01) before activation (6.10)"
  fi

  # 3) install AP connection profile (0600 root:root or NM refuses).
  _b1_install_string "${B1_AP_CONNECTION_BODY}" "${B1_AP_CONNECTION_FILE}" 0600 \
    || return 1
  _b1_validate_ap_connection || return 1

  # 4) install dispatcher script (0755 root:root — NM dispatcher will
  #    silently skip files that aren't executable).
  _b1_install_string "${B1_AP_DISPATCHER_BODY}" "${B1_AP_DISPATCHER_FILE}" 0755 \
    || return 1

  # 5) install dnsmasq-shared drop-in (0644 — read by NM's bundled
  #    dnsmasq which runs as root, no secret content here).
  _b1_install_string "${B1_AP_DNSMASQ_BODY}" "${B1_AP_DNSMASQ_FILE}" 0644 \
    || return 1

  # 6) install polkit rule allowing `pi` to trigger wifi rescans
  #    (0644 — polkitd reads as root; world-readable is fine and
  #    matches the rest of /etc/polkit-1/rules.d/*.rules). Polkit
  #    re-reads rules on file change, no daemon reload needed.
  _b1_install_string "${B1_WIFI_SCAN_POLKIT_BODY}" "${B1_WIFI_SCAN_POLKIT_FILE}" 0644 \
    || return 1

  # 7) install sudoers allowlist for the hostapd-based AP. visudo
  #    validates syntax on the staged copy before install — a
  #    malformed sudoers file under /etc/sudoers.d/ would wedge all
  #    sudo on the host.
  _b1_install_sudoers "${B1_AP_SUDOERS_BODY}" "${B1_AP_SUDOERS_FILE}" \
    || return 1

  # 8) install tmpfiles.d fragment that re-creates /run/teslausb-ap
  #    owned by pi:pi on every boot. Apply it immediately so the AP
  #    can come up later this boot without waiting for reboot.
  _b1_install_string "${B1_AP_TMPFILES_BODY}" "${B1_AP_TMPFILES_FILE}" 0644 \
    || return 1
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    b1_run systemd-tmpfiles --create "${B1_AP_TMPFILES_FILE}"
  else
    b1_warn "systemd-tmpfiles not found — /run/teslausb-ap will be created on next boot"
  fi

  # We deliberately do NOT:
  #   * nmcli connection reload         (Phase 6.10)
  #   * nmcli connection up teslausb-ap (Phase 6.10)
  #   * systemctl restart NetworkManager (would drop wlan0)
  # Dropping the files on disk with autoconnect=false is safe — they
  # take effect at the next explicit `connection reload` or NM restart.
  b1_log "network config staged; activation deferred to Phase 6.10"
  return 0
}
