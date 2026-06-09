#!/usr/bin/env bash
#
# §8 GLOBAL DENYLIST scan (Phase 7.0 contract). Asserts that NO disk/image
# mutator appears in setup.sh, setup-lib/**, uninstall.sh, or any installed unit
# file — EXCEPT the single gadgetd provision/up delegation lines. systemd
# `*.mount` unit dependencies (e.g. sys-kernel-config.mount) are not command
# invocations and are not flagged.
#
# The scanner is self-tested with positive + negative controls so a trivially
# passing (broken) scanner is caught.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup-lib/tests/lib/sandbox.sh
. "${HERE}/lib/sandbox.sh"
cd "$REPO_ROOT" || { echo "cannot cd to repo root"; exit 1; }

# Denylisted command tokens. A token must be preceded by start-of-line or a
# non-word, non-dot delimiter (so `.mount` unit deps are excluded) and followed
# by a non-word char or end-of-line. mkfs*/mkexfat* match their dotted variants.
DENY_PATTERN='(^|[^A-Za-z0-9_.])(dd|truncate|fallocate|mkfs[A-Za-z0-9._-]*|mkexfat[A-Za-z0-9._-]*|parted|sfdisk|sgdisk|losetup|mount|umount|wipefs)([^A-Za-z0-9_]|$)'
# The ONLY allowed exception: gadgetd delegation lines.
DELEGATION='gadgetd[[:space:]]+(provision|up|down|serve)'

scan_hits()      { grep -Eq "$DENY_PATTERN" <<<"$1"; }
is_delegation()  { grep -Eq "$DELEGATION"   <<<"$1"; }

# --- scanner self-test (controls) -------------------------------------------
if scan_hits 'fallocate -l 4096M /data/teslausb/disk.img'; then _ok "scanner catches fallocate"; else _fail "scanner MISSED fallocate"; fi
if scan_hits 'ExecStart=/sbin/mkfs.exfat /dev/loop0';        then _ok "scanner catches mkfs.exfat"; else _fail "scanner MISSED mkfs.exfat"; fi
if scan_hits '  losetup -f disk.img';                        then _ok "scanner catches losetup"; else _fail "scanner MISSED losetup"; fi
if scan_hits 'umount /mnt/x';                                then _ok "scanner catches umount"; else _fail "scanner MISSED umount"; fi
if scan_hits 'After=sys-kernel-config.mount local-fs.target'; then _fail "scanner false-positive on .mount unit dep"; else _ok "scanner ignores .mount unit dep"; fi
if scan_hits 'mkdir -p /data/teslausb';                      then _fail "scanner false-positive on mkdir"; else _ok "scanner ignores mkdir"; fi
if is_delegation 'ExecStart=/usr/local/bin/gadgetd up --image /data/teslausb/disk.img'; then _ok "delegation matcher recognizes 'gadgetd up'"; else _fail "delegation matcher missed 'gadgetd up'"; fi

# --- build the file set in scope --------------------------------------------
files=()
[ -f setup.sh ]     && files+=( setup.sh )
[ -f uninstall.sh ] && files+=( uninstall.sh )
while IFS= read -r f; do files+=( "$f" ); done \
    < <(find setup-lib -type f \( -name '*.sh' -o -name '*.bash' \) ! -name 'denylist.test.sh')
while IFS= read -r f; do files+=( "$f" ); done \
    < <(find deploy/systemd -type f -name '*.service' 2>/dev/null)

# --- scan --------------------------------------------------------------------
violations=0
for f in "${files[@]}"; do
    while IFS= read -r numbered; do
        [ -n "$numbered" ] || continue
        content="${numbered#*:}"
        if is_delegation "$content"; then continue; fi
        printf '  DENYLIST HIT %s -> %s\n' "$f" "$numbered" >&2
        violations=$((violations + 1))
    done < <(grep -nE "$DENY_PATTERN" "$f" 2>/dev/null || true)
done

assert_eq "$violations" 0 "no denylisted disk/image mutator in installer files (${#files[@]} files scanned)"

printf '\n%s passed, %s failed, %s skipped\n' "$TESTS_PASS" "$TESTS_FAIL" "$TESTS_SKIP"
[ "$TESTS_FAIL" -eq 0 ]
