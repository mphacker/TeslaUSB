#!/bin/bash
# End-to-end proof of the cloud upload bridge: a REAL indexd process, the REAL
# uploadd serve loop, the REAL rclone binary, and real files on disk.
#
# WHY THIS EXISTS
# Every uploadd unit test runs against hand-written fakes. A fake cannot enforce
# a contract it does not know about, which is exactly how a queue row that could
# never upload (`source_rel` dropped on hydration) passed 114 green tests. The
# first run of this script found that defect, plus a fully-failed drain that
# logged "clean shutdown" and exited 0. It exercises the actual wire.
#
# WHAT IT ASSERTS
#   - discover -> enqueue produces one queue row per camera file
#   - the drain marks them done
#   - the bytes actually land on the remote, byte-identical to the archive
#   - the content-addressed key embeds the true digest (defuses stale evidence)
#   - `durable` stays 0: the unsealed slice must never make footage evictable
#
# HOW TO RUN (Windows host, from the repo root, per copilot-instructions.md):
#
#   wslc run --rm -v "${PWD}:/work" `
#     -v teslausb-cargo-home:/cargo-home -v teslausb-test-target:/test-target `
#     -v teslausb-it-cache:/it-cache `
#     -e CARGO_HOME=/cargo-home -e CARGO_TARGET_DIR=/test-target `
#     -e IT_CACHE=/it-cache `
#     docker.io/library/rust:1.85-bookworm `
#     bash /work/rust/tests/cloud-bridge.test.sh
#
# Needs root and network on first run (apt-get + the rclone download); both are
# cached in IT_CACHE afterwards. It must run in a Linux container: the runtime
# dir holds Unix sockets, which cannot be created on a Windows bind mount
# ("Operation not permitted"), so IT stays on the container's own filesystem.
#
# IT_MUTATE=1 corrupts one landed object on purpose. The integrity and key
# assertions MUST then fail for that file and only that file -- run it that way
# whenever you change the assertions, or you have no evidence they can fail.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUST_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
TARGET_DIR="${CARGO_TARGET_DIR:-$RUST_DIR/target}"

IT=/tmp/itrun                      # runtime dir: must be a real Linux fs
BIN="${IT_CACHE:-/tmp/teslausb-it-cache}"
mkdir -p "$IT" "$BIN"

step() { echo; echo "=== $* ==="; }
fail() { echo "!!! FAIL: $*"; FAILED=1; }
FAILED=0

step "0. deps"
if ! command -v sqlite3 >/dev/null; then
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq sqlite3 unzip curl socat >/dev/null 2>&1
fi
command -v socat >/dev/null || apt-get install -y -qq socat >/dev/null 2>&1
for tool in sqlite3 socat curl; do
  command -v "$tool" >/dev/null || { echo "missing $tool (need root + network on first run)"; exit 1; }
done
echo "sqlite3: $(sqlite3 --version | cut -d' ' -f1)"
echo "socat:   $(socat -V | head -1 | cut -d' ' -f1-3)"

case "$(uname -m)" in
  x86_64)        RC_ARCH=amd64 ;;
  aarch64|arm64) RC_ARCH=arm64 ;;
  *) echo "unsupported arch $(uname -m) for the rclone download"; exit 1 ;;
esac
if [ ! -x "$BIN/rclone" ]; then
  echo "fetching rclone ${RC_ARCH} (cached in $BIN after first run)..."
  curl -fsS -o /tmp/rc.zip "https://downloads.rclone.org/v1.74.4/rclone-v1.74.4-linux-${RC_ARCH}.zip" \
    || { echo "rclone download failed"; exit 1; }
  unzip -o -q /tmp/rc.zip -d /tmp/rc
  install -m0755 "/tmp/rc/rclone-v1.74.4-linux-${RC_ARCH}/rclone" "$BIN/rclone"
fi
echo "rclone: $("$BIN/rclone" version | head -1)  (device runs the same 1.74.4)"

step "1. build indexd + uploadd (host arch, debug)"
cd "$RUST_DIR" || exit 1
cargo build -q -p indexd -p uploadd 2>&1 | tail -5
IDX="$TARGET_DIR/debug/indexd"
UPD="$TARGET_DIR/debug/uploadd"
[ -x "$IDX" ] && [ -x "$UPD" ] || { echo "build failed (looked in $TARGET_DIR/debug)"; exit 1; }
echo "indexd:  $IDX"
echo "uploadd: $UPD"

step "2. clean fixture"
pkill -f "$IDX" 2>/dev/null
rm -rf "$IT"; mkdir -p "$IT/archive/RecentClips/2026-07-27/2026-07-27_10-00-00" "$IT/remote" "$IT/sock"

# Two "camera files" with distinct, known content.
PARENT="$IT/archive/RecentClips/2026-07-27/2026-07-27_10-00-00"
head -c 300000 /dev/urandom > "$PARENT/2026-07-27_10-00-00-front.mp4"
head -c 200000 /dev/urandom > "$PARENT/2026-07-27_10-00-00-back.mp4"
FRONT_SHA=$(sha256sum "$PARENT/2026-07-27_10-00-00-front.mp4" | cut -d' ' -f1)
BACK_SHA=$(sha256sum "$PARENT/2026-07-27_10-00-00-back.mp4" | cut -d' ' -f1)
echo "front sha256: $FRONT_SHA"
echo "back  sha256: $BACK_SHA"

cat > "$IT/rclone.conf" <<EOF
[itlocal]
type = local
EOF

step "3. start real indexd against a temp DB"
export INDEXD_DB="$IT/index.sqlite3"
export INDEXD_SOCKET="$IT/sock/indexd.sock"
export INDEXD_SCANNERD_SOCKET="$IT/sock/scannerd.sock"
export INDEXD_HEALTH_FILE="$IT/health"
"$IDX" > "$IT/indexd.log" 2>&1 &
IDX_PID=$!
for _ in $(seq 1 40); do [ -S "$INDEXD_SOCKET" ] && break; sleep 0.25; done
if [ ! -S "$INDEXD_SOCKET" ]; then
  echo "indexd never bound its socket:"; cat "$IT/indexd.log"; exit 1
fi
echo "indexd up (pid $IDX_PID), socket bound"

step "4. seed one archived parent + enable the RecentClips folder class"
sqlite3 "$INDEXD_DB" <<EOF
UPDATE cloud_provider_config SET recent_enabled = 1 WHERE id = 1;
INSERT INTO archive_items
  (id, folder_class, path, size_bytes, file_count, archived_at, created_at, updated_at, delete_state, durable)
VALUES
  (1, 'RecentClips', 'RecentClips/2026-07-27/2026-07-27_10-00-00', 500000, 2,
   strftime('%s','now'), strftime('%s','now'), strftime('%s','now'), 'LIVE', 0);
EOF
SEEDED=$(sqlite3 "$INDEXD_DB" 'SELECT COUNT(*) FROM archive_items;')
[ "$SEEDED" -eq 1 ] || { echo "seed failed - aborting so later assertions cannot pass vacuously"; kill $IDX_PID; exit 1; }
echo "archive_items: $(sqlite3 "$INDEXD_DB" 'SELECT id||" "||folder_class||" durable="||durable||" "||delete_state FROM archive_items;')"
echo "recent_enabled: $(sqlite3 "$INDEXD_DB" 'SELECT recent_enabled FROM cloud_provider_config WHERE id=1;')"

step "5. open BOTH throttle planes (uploadd fails closed without them)"
# Link plane: a stub wifid speaking uploadd's 4-byte-LE-length + JSON frame.
WIFI_JSON='{"throttle":{"seq":1,"link_mode":"sta","uploads_allowed":true,"max_tx_bytes_per_s":1048576,"max_chunk_bytes":65536,"action":"run","reason":"none"}}'
L=${#WIFI_JSON}
printf "\\$(printf '%03o' $((L & 255)))\\$(printf '%03o' $(((L >> 8) & 255)))\\000\\000" > "$IT/wifid-reply.bin"
printf '%s' "$WIFI_JSON" >> "$IT/wifid-reply.bin"
socat "UNIX-LISTEN:$IT/sock/wifid.sock,fork,unlink-early" "SYSTEM:cat $IT/wifid-reply.bin" &
SOCAT_PID=$!
for _ in $(seq 1 20); do [ -S "$IT/sock/wifid.sock" ] && break; sleep 0.25; done
[ -S "$IT/sock/wifid.sock" ] || { echo "stub wifid never bound"; kill $IDX_PID $SOCAT_PID 2>/dev/null; exit 1; }
# Storage plane: retentiond's governor file, at the path uploadd hardcodes.
mkdir -p /run/teslausb || { echo "cannot create /run/teslausb (need root)"; kill $IDX_PID $SOCAT_PID 2>/dev/null; exit 1; }
printf '%s' '{"uploads_allowed":true,"seq":1}' > /run/teslausb/retentiond.governor.json
echo "wifid stub bound (pid $SOCAT_PID); governor file written"

step "6. RUN THE REAL BRIDGE: uploadd serve --once"
# `remote_prefix` is trim_matches('/')'d, and an rclone `type = local` remote
# resolves a relative path against the process cwd -- hence cd / and ${IT#/}.
cd / || exit 1
"$UPD" serve --once \
  --indexd-socket "$INDEXD_SOCKET" \
  --wifid-socket "$IT/sock/wifid.sock" \
  --archive-root "$IT/archive" \
  --destination-id it-throwaway \
  --remote-prefix "${IT#/}/remote" \
  --rclone-remote itlocal \
  --rclone-binary "$BIN/rclone" \
  --rclone-config "$IT/rclone.conf" \
  --max-parents-per-pass 4 \
  > "$IT/uploadd.log" 2>&1
echo "uploadd exit: $?"
echo "--- uploadd log ---"; cat "$IT/uploadd.log"

step "7. ASSERTIONS"
echo "-- queue rows --"
sqlite3 -header "$INDEXD_DB" \
  "SELECT archive_item_id, child_key, state, attempts, substr(remote_key,1,50) AS remote_key FROM cloud_upload_queue;"
echo "-- last_error --"
sqlite3 "$INDEXD_DB" "SELECT child_key||' -> '||COALESCE(last_error,'(null)') FROM cloud_upload_queue;"

QUEUED=$(sqlite3 "$INDEXD_DB" "SELECT COUNT(*) FROM cloud_upload_queue;")
DONE=$(sqlite3 "$INDEXD_DB" "SELECT COUNT(*) FROM cloud_upload_queue WHERE state='done';")
DURABLE=$(sqlite3 "$INDEXD_DB" "SELECT durable FROM archive_items WHERE id=1;")
[ -n "$DURABLE" ] || fail "no archive_items row - the fixture did not seed"

echo
echo "-- files actually written to the remote --"
find "$IT/remote" -type f | sed "s|$IT/remote/||"
LANDED=$(find "$IT/remote" -type f | wc -l)

echo
[ "$QUEUED" -eq 2 ] && echo "PASS enqueue: 2 child rows" || fail "enqueue: expected 2 queue rows, got $QUEUED"
[ "$DONE"   -eq 2 ] && echo "PASS drain:   2 rows done"  || fail "drain: expected 2 done, got $DONE"
[ "$LANDED" -eq 2 ] && echo "PASS transfer: 2 objects on the remote" || fail "transfer: expected 2 files, got $LANDED"
[ "${DURABLE:-x}" = "0" ] && echo "PASS safety:  durable still 0 (unsealed slice cannot make footage evictable)" \
                     || fail "SAFETY: durable is '${DURABLE}' (expected 0) - footage could be evicted!"

# Optional mutation hook: proves the integrity/key assertions can actually FAIL.
if [ "${IT_MUTATE:-0}" = "1" ]; then
  victim=$(find "$IT/remote" -type f | head -1)
  printf 'tampered' >> "$victim"
  echo "!! IT_MUTATE=1: appended 8 bytes to $(basename "$victim") - integrity+key MUST fail below"
fi

# Byte-for-byte: the bytes on the remote must equal the bytes in the archive.
CHECKED=0
for f in $(find "$IT/remote" -type f); do
  CHECKED=$((CHECKED + 1))
  got=$(sha256sum "$f" | cut -d' ' -f1)
  if [ "$got" = "$FRONT_SHA" ] || [ "$got" = "$BACK_SHA" ]; then
    echo "PASS integrity: $(basename "$f") matches its archive source"
  else
    fail "integrity: $f digest $got matches neither source file"
  fi
done
# An empty loop is not a pass. Guard against the vacuous-iteration trap.
[ "$CHECKED" -eq 2 ] || fail "integrity: compared $CHECKED files, expected 2 (assertions ran vacuously)"

# The content-addressed key must embed the real digest (defuses stale evidence).
KEYED=0
for f in $(find "$IT/remote" -type f); do
  KEYED=$((KEYED + 1))
  got=$(sha256sum "$f" | cut -d' ' -f1)
  case "$f" in
    *"${got:0:16}"*) echo "PASS key: content-addressed path carries the true digest prefix" ;;
    *) fail "key: ${f} does not embed digest prefix ${got:0:16}" ;;
  esac
done
[ "$KEYED" -eq 2 ] || fail "key: checked $KEYED files, expected 2 (assertions ran vacuously)"

kill $IDX_PID ${SOCAT_PID:-} 2>/dev/null
echo
if [ "$FAILED" -eq 0 ]; then echo "########## BRIDGE PROOF: PASS ##########";
else echo "########## BRIDGE PROOF: FAIL ##########"; fi
exit $FAILED
