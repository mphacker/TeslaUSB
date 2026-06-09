#!/usr/bin/env bash
#
# Tests for release/generate-manifest.sh (Task 7.2). Proves the generator's
# output is accepted by the SINGLE canonical verifier (setup-lib/verify-
# release.sh) and fails closed under mutation — the §8 fail-closed property,
# proven end-to-end through generate -> verify rather than re-implementing it.
#
# Reuses release/fixtures/good/{bin,spa,units} as a realistic staged
# tree (contract §7: Lane B reuses the fixtures + verifier, does not fork them).
# Plain bash; needs coreutils + python3 (jsonschema) like the host release env.
# Exits 0 iff every case passes.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"
GEN="${root}/release/generate-manifest.sh"
VR="${root}/setup-lib/verify-release.sh"
SCHEMA="${root}/release/manifest.schema.json"
FIXGOOD="${root}/release/fixtures/good"

# Deterministic, valid metadata for the happy path.
VERSION='1.2.3-test'
COMMIT='0123456789abcdef0123456789abcdef01234567'

# Ensure fixtures are current before reusing their good/ tree.
bash "${root}/release/fixtures/make-fixtures.sh" >/dev/null

pass=0 fail=0
ok()  { printf 'ok   %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf 'FAIL %s -- %s\n' "$1" "${2:-}"; fail=$((fail + 1)); }

# assert_code <expected> <label> -- <cmd...>
assert_code() {
    local expected="$1" label="$2"; shift 3
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" -eq "$expected" ]; then ok "$label (exit $got)"
    else bad "$label" "want $expected, got $got"; fi
}

# stage <destdir> -- copy ONLY the shipped subdirs (no metadata) from good/.
stage() {
    local d="$1"
    mkdir -p "$d"
    cp -a "${FIXGOOD}/bin" "${FIXGOOD}/spa" "${FIXGOOD}/units" "$d/"
}

# ---------------------------------------------------------------------------
# 1) Round-trip: generate a fresh staged tree, verifier must accept (exit 0).
S="$(mktemp -d)"; stage "$S"
assert_code 0 "generate succeeds on staged tree" -- \
    bash "$GEN" --dir "$S" --version "$VERSION" --commit "$COMMIT"
assert_code 0 "verifier accepts generated tree" -- bash "$VR" "$S"

# 2) Metadata outputs exist and exclude themselves from SHA256SUMS.
if [ -f "$S/SHA256SUMS" ] && [ -f "$S/manifest.env" ] && [ -f "$S/manifest.json" ]; then
    ok "all three metadata files written"
else bad "metadata files written"; fi
if grep -Eq '  (SHA256SUMS|manifest\.(env|json))$' "$S/SHA256SUMS"; then
    bad "SHA256SUMS excludes metadata" "metadata listed in SHA256SUMS"
else ok "SHA256SUMS excludes metadata + itself"; fi

# 3) manifest.env keys match §3.2 EXACTLY (names + order).
got_keys="$(sed -n 's/=.*//p' "$S/manifest.env" | paste -sd, -)"
want_keys='RELEASE_VERSION,GIT_COMMIT,TARGET_TRIPLE,UNIT_SET_VERSION,SPA_BUNDLE_SHA256'
if [ "$got_keys" = "$want_keys" ]; then ok "manifest.env keys exact (§3.2)"
else bad "manifest.env keys exact" "got [$got_keys]"; fi

# 4) SPA_BUNDLE_SHA256 equals the independent §3.3 recompute.
env_spa="$(sed -n 's/^SPA_BUNDLE_SHA256=//p' "$S/manifest.env")"
calc_spa="$(grep -E '^[0-9a-f]{64}  spa/' "$S/SHA256SUMS" | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"
if [ -n "$env_spa" ] && [ "$env_spa" = "$calc_spa" ]; then ok "SPA_BUNDLE_SHA256 matches §3.3"
else bad "SPA_BUNDLE_SHA256 matches §3.3" "env=$env_spa calc=$calc_spa"; fi

# 5) manifest.json validates against the schema, and its fields mirror env.
if python3 - "$SCHEMA" "$S/manifest.json" "$S/manifest.env" <<'PY'
import json, os, re, sys
import jsonschema
schema_path, mjson, menv = sys.argv[1], sys.argv[2], sys.argv[3]
schema = json.load(open(schema_path, encoding="utf-8"))
doc = json.load(open(mjson, encoding="utf-8"))
jsonschema.validate(doc, schema)
env = {}
for line in open(menv, encoding="utf-8"):
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    env[k] = v
assert doc["release_version"] == env["RELEASE_VERSION"], "version mismatch"
assert doc["git_commit"] == env["GIT_COMMIT"], "commit mismatch"
assert doc["target_triple"] == env["TARGET_TRIPLE"], "triple mismatch"
assert doc["spa_bundle_sha256"] == env["SPA_BUNDLE_SHA256"], "spa mismatch"
assert len(doc["binaries"]) == 7, "expected 7 binaries, got %d" % len(doc["binaries"])
sums = {}
for raw in open(os.path.join(os.path.dirname(mjson), "SHA256SUMS"), encoding="utf-8"):
    m = re.match(r"^([0-9a-f]{64})  (.+)$", raw.rstrip("\n"))
    if m:
        sums[m.group(2)] = m.group(1)
for b in doc["binaries"]:
    assert sums.get(b["path"]) == b["sha256"], "json/SHA256SUMS hash mismatch for %s" % b["path"]
print("schema-ok")
PY
then ok "manifest.json schema-valid + mirrors env/SHA256SUMS"
else bad "manifest.json schema-valid"; fi

# ---------------------------------------------------------------------------
# 6) Fail-closed: mutate a staged byte WITHOUT regenerating -> verifier rejects.
printf 'TAMPERED after manifest\n' > "$S/bin/webd"
assert_code 4 "mutated binary fails closed (stale SHA256SUMS)" -- bash "$VR" "$S"
# Regenerating over the mutated tree makes it consistent again (generator is honest).
assert_code 0 "regenerate over mutation re-verifies" -- \
    bash "$GEN" --dir "$S" --version "$VERSION" --commit "$COMMIT"
assert_code 0 "verifier accepts regenerated tree" -- bash "$VR" "$S"

# 7) Tampering manifest.env's SPA digest alone -> verifier recompute rejects.
sed -i 's/^SPA_BUNDLE_SHA256=.*/SPA_BUNDLE_SHA256=0000000000000000000000000000000000000000000000000000000000000000/' "$S/manifest.env"
assert_code 4 "forged SPA_BUNDLE_SHA256 fails closed" -- bash "$VR" "$S"
rm -rf "$S"

# ---------------------------------------------------------------------------
# 8) Generator input validation (fail before writing bad metadata).
S2="$(mktemp -d)"; stage "$S2"
assert_code 4 "bad GIT_COMMIT rejected" -- bash "$GEN" --dir "$S2" --version v --commit nothex
assert_code 4 "non-integer UNIT_SET_VERSION rejected" -- \
    bash "$GEN" --dir "$S2" --version v --commit "$COMMIT" --unit-set-version x
# Newline-injection via --triple must be refused (would shadow real keys at verify).
assert_code 4 "newline-injected --triple rejected" -- \
    bash "$GEN" --dir "$S2" --version v --commit "$COMMIT" \
        --triple "$(printf 'aarch64-unknown-linux-gnu\nUNIT_SET_VERSION=999')"
assert_code 4 "newline-injected --version rejected" -- \
    bash "$GEN" --dir "$S2" --commit "$COMMIT" \
        --version "$(printf 'v1\nUNIT_SET_VERSION=999')"
assert_code 2 "missing --version is usage error" -- bash "$GEN" --dir "$S2" --commit "$COMMIT"
assert_code 3 "missing staged dir" -- bash "$GEN" --dir "$S2/nope" --version v --commit "$COMMIT"
rm -rf "$S2"

# 9) Missing required input (no bin/) fails closed (exit 3).
S3="$(mktemp -d)"; mkdir -p "$S3/spa/assets"
printf 'x\n' > "$S3/spa/index.html"
assert_code 3 "missing bin/ fails closed" -- bash "$GEN" --dir "$S3" --version v --commit "$COMMIT"
rm -rf "$S3"

# 10) Optional input (no units/) requires explicit opt-in.
S4="$(mktemp -d)"; stage "$S4"; rm -rf "$S4/units"
assert_code 3 "missing units/ fails closed by default" -- \
    bash "$GEN" --dir "$S4" --version v --commit "$COMMIT"
assert_code 0 "missing units/ allowed with --allow-missing-inputs" -- \
    bash "$GEN" --dir "$S4" --version v --commit "$COMMIT" --allow-missing-inputs
rm -rf "$S4"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
