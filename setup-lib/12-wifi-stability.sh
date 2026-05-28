#!/usr/bin/env bash
# setup-lib/12-wifi-stability.sh — Phase 6.12
#
# Lays down the BCM43436 SDIO WiFi stability stack:
#
#   1) /etc/NetworkManager/conf.d/10-teslausb-no-powersave.conf
#      Disables NetworkManager's wifi.powersave globally so the
#      WiFi chip never enters the low-power state that wedges its
#      firmware under sustained TX load.
#
#   2) /etc/modprobe.d/brcmfmac.conf
#      Disables in-driver roaming + offloaded scan engines that are
#      known to trigger the BCM43436 firmware lockup (HT Avail
#      request errors / err=-110).
#
#   3) /usr/local/sbin/wifi-watchdog.sh
#      The escalation-ladder recovery script (pause uploads → reload
#      brcmfmac → bounce link → reboot). Replaces v1's blunt
#      "3-minutes-no-ping → reboot" with graduated soft recoveries
#      so the device stops rebooting every ~30 min during heavy
#      cloud sync.
#
#   4) /etc/systemd/system/wifi-watchdog.service
#   5) /etc/systemd/system/wifi-watchdog.timer
#      systemd timer (30 s tick) + oneshot service wrapping (3).
#
# Like the rest of Phase 6, this step is purely a file-install step.
# It does NOT restart NetworkManager, reload modprobe state, or
# enable/start the timer — Phase 6.10 (10-activate.sh) owns
# activation. Doing it here would yank wlan0 out from under any
# operator currently SSH'd in.
#
# Source artifacts live under `deploy/wifi-stability/` next to the
# repo root so reviewers (and the 6.11 uninstaller) can read them
# without executing this script.
#
# Idempotency: sha256(source) vs sha256(on-disk) per file. No
# changes → no daemon-reload. First overwrite of any pre-existing
# target spawns one `b1_backup` sibling; subsequent runs do not pile
# up backups.

# shellcheck source=00-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/00-common.sh"

# Repo root + asset directory.
B1_REPO_ROOT_12="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
B1_WIFI_STAB_SRC="${B1_REPO_ROOT_12}/deploy/wifi-stability"

# --------------------------------------------------------------------
# Install targets — exported so 6.11 (uninstall.sh) can reverse this.
# --------------------------------------------------------------------

B1_NM_POWERSAVE_CONF="/etc/NetworkManager/conf.d/10-teslausb-no-powersave.conf"
export B1_NM_POWERSAVE_CONF

B1_BRCMFMAC_CONF="/etc/modprobe.d/brcmfmac.conf"
export B1_BRCMFMAC_CONF

B1_WIFI_WATCHDOG_SCRIPT="/usr/local/sbin/wifi-watchdog.sh"
export B1_WIFI_WATCHDOG_SCRIPT

B1_WIFI_WATCHDOG_UNIT="/etc/systemd/system/wifi-watchdog.service"
export B1_WIFI_WATCHDOG_UNIT

B1_WIFI_WATCHDOG_TIMER="/etc/systemd/system/wifi-watchdog.timer"
export B1_WIFI_WATCHDOG_TIMER

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

_b1_12_sha256() {
  local path="$1"
  [[ -e "${path}" ]] || { printf ''; return 0; }
  sha256sum -- "${path}" 2>/dev/null | awk '{print $1}'
}

# _b1_12_install_file <src> <dst> <mode> [<state-var>]
#   * Compares sha256(src) vs sha256(dst); no-op on match.
#   * Backs up any pre-existing target via b1_backup before overwrite.
#   * Honours TESLAUSB_DRY_RUN via b1_run.
_b1_12_install_file() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  local state_var="${4:-}"

  if [[ ! -f "${src}" ]]; then
    b1_err "missing source artifact: ${src}"
    return 1
  fi

  local src_sum dst_sum
  src_sum="$(_b1_12_sha256 "${src}")"
  dst_sum="$(_b1_12_sha256 "${dst}")"

  if [[ -n "${dst_sum}" && "${src_sum}" == "${dst_sum}" ]]; then
    b1_log "unchanged: ${dst} (sha256=${dst_sum:0:12}…)"
    return 0
  fi

  if [[ -e "${dst}" ]]; then
    b1_log "differs: ${dst} (target=${dst_sum:0:12}…, source=${src_sum:0:12}…) — backing up"
    b1_backup "${dst}"
  else
    b1_log "new: ${dst} (sha256=${src_sum:0:12}…)"
  fi

  b1_run install -m "${mode}" -- "${src}" "${dst}"

  if [[ -n "${state_var}" ]]; then
    printf -v "${state_var}" '%s' 1
  fi
}

# --------------------------------------------------------------------
# Step entry point
# --------------------------------------------------------------------

b1_step_12() {
  if [[ ! -d "${B1_WIFI_STAB_SRC}" ]]; then
    b1_err "asset directory missing: ${B1_WIFI_STAB_SRC}"
    return 1
  fi

  local changed=""

  # 1) NetworkManager powersave-off drop-in.
  b1_run mkdir -p -- "$(dirname "${B1_NM_POWERSAVE_CONF}")"
  _b1_12_install_file \
    "${B1_WIFI_STAB_SRC}/10-teslausb-no-powersave.conf" \
    "${B1_NM_POWERSAVE_CONF}" \
    0644 \
    changed

  # 2) brcmfmac module options.
  b1_run mkdir -p -- "$(dirname "${B1_BRCMFMAC_CONF}")"
  _b1_12_install_file \
    "${B1_WIFI_STAB_SRC}/brcmfmac.conf" \
    "${B1_BRCMFMAC_CONF}" \
    0644 \
    changed

  # 3) wifi-watchdog script (executable).
  b1_run mkdir -p -- "$(dirname "${B1_WIFI_WATCHDOG_SCRIPT}")"
  _b1_12_install_file \
    "${B1_WIFI_STAB_SRC}/wifi-watchdog.sh" \
    "${B1_WIFI_WATCHDOG_SCRIPT}" \
    0755 \
    changed

  # 4) wifi-watchdog.service.
  _b1_12_install_file \
    "${B1_WIFI_STAB_SRC}/wifi-watchdog.service" \
    "${B1_WIFI_WATCHDOG_UNIT}" \
    0644 \
    changed

  # 5) wifi-watchdog.timer.
  _b1_12_install_file \
    "${B1_WIFI_STAB_SRC}/wifi-watchdog.timer" \
    "${B1_WIFI_WATCHDOG_TIMER}" \
    0644 \
    changed

  # daemon-reload only on actual change. We deliberately do NOT
  # start the timer, reload NetworkManager, or run `modprobe -r
  # brcmfmac` here — Phase 6.10 owns activation, and yanking wlan0
  # out from under a running SSH session is exactly what we're
  # trying to PREVENT.
  if [[ -n "${changed}" ]]; then
    b1_log "wifi-stability artifacts changed — running daemon-reload"
    b1_run systemctl daemon-reload
  else
    b1_log "no wifi-stability artifacts changed — skipping daemon-reload"
  fi

  return 0
}
